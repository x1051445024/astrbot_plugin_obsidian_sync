"""Obsidian 知识库同步插件 for AstrBot
==================================
v0.8.1 — 兼容性修复版

- 支持每日定时或定时间隔同步
- 通过 WebUI 配置面板设置参数
- 检测 Obsidian 目录变更后自动增量更新知识库
- 嵌入缓存：仅对变更文件调 API，全量向量合并后重建索引
- 支持命令权限控制与同步状态记录
- 支持配置面板手动同步与状态回显

v0.8.0 变更:
- [FIX] 线程安全：_do_sync 加锁，防止并发写临时文件
- [FIX] async 兼容：手动同步用 run_in_executor 避免阻塞事件循环
- [FIX] 缓存 key 统一为 POSIX 相对路径，修复已删除文件缓存残留
- [OPT] 配置文件读取统一抽取，消除重复代码
- [OPT] 定时器基于上次同步完成时间重算，避免手动触发后过早自动同步
- [OPT] 大文件保护：超过阈值的 md 文件跳过嵌入
- [OPT] 缓存条目上限警告
- [OPT] 临时文件名加 PID 防多实例冲突
- [OPT] 合并 obsync/obsync_now 为单一 obsync 指令
- [OPT] 异常捕获粒度细化，不再吞掉所有 Exception

v0.9.0 变更:
- [FIX] 启动时依赖检测：检查 embed.py 和 build_kb.py 是否存在
- [FIX] 将 embed.py、build_kb.py 打包进插件 scripts/ 目录，不再依赖外部 skill
- [DOC] README 补充依赖说明
"""

import sys
import json
import pathlib
import threading
import subprocess
import datetime
import os
import asyncio
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


def _check_required_scripts(embed_script: pathlib.Path, build_script: pathlib.Path) -> list[str]:
    """检测必需脚本是否存在，返回缺失列表。"""
    missing = []
    if not embed_script.exists():
        missing.append(str(embed_script))
    if not build_script.exists():
        missing.append(str(build_script))
    return missing

# ── 常量 ─────────────────────────────────────────────────
MAX_CACHE_ENTRIES = 2000       # 缓存条目上限，超出时发出性能警告
MAX_FILE_SIZE_BYTES = 512 * 1024  # 512KB — 超过此大小的 md 不做嵌入
CACHE_KEY_VERSION = 1         # 缓存 key 格式版本，用于格式迁移


def _detect_data_dir() -> pathlib.Path:
    """自动检测 AstrBot 数据目录，兼容不同部署方式。"""
    # 1. 环境变量
    env = os.environ.get("ASTRBOT_DATA")
    if env:
        p = pathlib.Path(env)
        if p.exists():
            return p
    # 2. 向上查找 plugins/ 和 knowledge_base/ 共存的目录
    this_file = pathlib.Path(__file__).resolve()
    for parent in this_file.parents:
        if (parent / "plugins").is_dir() and (parent / "knowledge_base").is_dir():
            return parent
    # 3. 兜底：插件目录上三级
    return this_file.parent.parent.parent


ASTRBOT_DATA = _detect_data_dir()
# 插件自带的脚本目录（免外部依赖）
_PLUGIN_SCRIPTS = pathlib.Path(__file__).resolve().parent / "scripts"
EMBED_SCRIPT = _PLUGIN_SCRIPTS / "embed.py"
BUILD_SCRIPT = _PLUGIN_SCRIPTS / "build_kb.py"
TMP_DIR = ASTRBOT_DATA / "plugin_data"
STATUS_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.json"
REPORT_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.md"
FILE_STATE_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_file_states.json"
EMBED_CACHE_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_embed_cache.json"
CONFIG_FILE = ASTRBOT_DATA / "config" / "obsidian_sync_config.json"


def _posix_relative(md: pathlib.Path, obsidian_dir: pathlib.Path) -> str:
    """统一缓存 key 格式：POSIX 正斜杠相对路径。"""
    return md.relative_to(obsidian_dir).as_posix()


@register("obsidian_sync", "牧濑红莉栖", "监听本地 Obsidian 目录，定时同步到 AstrBot 知识库", "0.9.0")
class ObsidianSync(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self._stop_event = threading.Event()
        self._manual_trigger = threading.Event()
        self._sync_lock = threading.Lock()
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._obsidian_dir = pathlib.Path(self._config.get("obsidian_dir", "D:/AstrBotData/Obsidian"))
        self._sync_mode = self._config.get("sync_mode", "daily")
        self._sync_daily_time = self._config.get("sync_daily_time", "03:00")
        self._sync_interval_hours = max(1, int(self._config.get("sync_interval_hours", 24)))
        self._kb_name = self._config.get("kb_name", "Obsidian-Vault")
        self._kb_file_id = self._config.get("kb_file_id", "obsidian_vault")
        self._restrict_commands = bool(self._config.get("restrict_commands", True))
        self._admin_user_ids = set(str(x) for x in self._config.get("admin_user_ids", []))
        self._allowed_user_ids = set(str(x) for x in self._config.get("allowed_user_ids", []))
        self._sync_on_startup = bool(self._config.get("sync_on_startup", False))
        self._last_sync_end_time: float = 0.0  # 上次同步结束的 Unix 时间戳

        # ── 依赖检测 ────────────────────────────────────────
        missing = _check_required_scripts(EMBED_SCRIPT, BUILD_SCRIPT)
        if missing:
            logger.error(
                f"[ObsidianSync] ❌ 缺失必需脚本:
"
                f"  - {EMBED_SCRIPT}
"
                f"  - {BUILD_SCRIPT}
"
                f"请确保 scripts/ 目录完整，或重新安装本插件。"
            )
            raise RuntimeError(f"缺失必需脚本: {', '.join(missing)}")
        else:
            logger.info("[ObsidianSync] ✓ 依赖脚本检测通过")

        self._thread.start()
        logger.info(
            f"[ObsidianSync] 已启动 | 目录: {self._obsidian_dir} | 模式: {self._sync_mode} | "
            f"{'定时: ' + self._sync_daily_time if self._sync_mode == 'daily' else '间隔: ' + str(self._sync_interval_hours) + 'h'} | 知识库: {self._kb_name}"
        )

    # ── 配置文件统一读写 ──────────────────────────────────
    def _read_config_file(self) -> dict:
        """读取 WebUI 配置文件，失败返回空字典。"""
        if not CONFIG_FILE.exists():
            return {}
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[ObsidianSync] 读取配置文件失败: {e}")
            return {}

    def _write_config_file(self, cfg: dict):
        """原子写入 WebUI 配置文件。"""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

    # ── 权限检查 ──────────────────────────────────────────
    def _is_admin_or_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._restrict_commands:
            return True
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = ""
        return uid in self._admin_user_ids or uid in self._allowed_user_ids

    # ── 状态文件写入 ──────────────────────────────────────
    def _write_status(self, **kwargs):
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        status = {
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **kwargs,
        }
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_FILE)

        md_lines = [
            "# Obsidian Sync Status",
            f"- Updated: {status['updated_at']}",
            f"- OK: {status.get('ok')}",
            f"- Stage: {status.get('stage', '')}",
            f"- Message: {status.get('message', '')}",
            f"- Knowledge Base: {status.get('kb_name', self._kb_name)}",
            f"- Changed: {status.get('changed', 0)}",
            f"- Deleted: {status.get('deleted', 0)}",
        ]
        try:
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_FILE.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _persist_readonly_status(self, ok: bool, message: str, stage: str, changed: int = 0, deleted: int = 0):
        """将同步结果写回 WebUI 配置面板的只读字段。"""
        try:
            cfg = self._read_config_file()
            if not cfg:
                return
            cfg["last_sync_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cfg["last_sync_status"] = "成功" if ok else "失败"
            cfg["last_sync_message"] = message[:300]
            cfg["sync_now_request"] = False
            cfg["last_sync_stage"] = stage
            cfg["last_sync_changed"] = changed
            cfg["last_sync_deleted"] = deleted
            self._write_config_file(cfg)
            logger.debug("[ObsidianSync] 配置面板状态已写回")
        except Exception as e:
            logger.warning(f"[ObsidianSync] 写回状态到配置失败: {e}")

    # ── 配置热更新 ────────────────────────────────────────
    def _reload_config(self):
        cfg = self._read_config_file()
        if not cfg:
            return
        try:
            self._obsidian_dir = pathlib.Path(cfg.get("obsidian_dir", str(self._obsidian_dir)))
            self._sync_mode = cfg.get("sync_mode", self._sync_mode)
            self._sync_daily_time = cfg.get("sync_daily_time", self._sync_daily_time)
            self._sync_interval_hours = max(1, int(cfg.get("sync_interval_hours", self._sync_interval_hours)))
            self._kb_name = cfg.get("kb_name", self._kb_name)
            self._kb_file_id = cfg.get("kb_file_id", self._kb_file_id)
            self._restrict_commands = bool(cfg.get("restrict_commands", self._restrict_commands))
            self._admin_user_ids = set(str(x) for x in cfg.get("admin_user_ids", list(self._admin_user_ids)))
            self._allowed_user_ids = set(str(x) for x in cfg.get("allowed_user_ids", list(self._allowed_user_ids)))
            self._sync_on_startup = bool(cfg.get("sync_on_startup", self._sync_on_startup))
        except Exception as e:
            logger.warning(f"[ObsidianSync] 配置热更新失败，使用旧配置: {e}")

    def _check_manual_sync(self) -> bool:
        cfg = self._read_config_file()
        return bool(cfg.get("sync_now_request", False))

    # ── 定时计算（基于上次同步结束时间） ─────────────────
    def _get_wait_seconds(self) -> float:
        now = datetime.datetime.now()
        if self._sync_mode == "daily":
            try:
                hour, minute = map(int, self._sync_daily_time.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 3, 0
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info(f"[ObsidianSync] 下次定时同步: {target.strftime('%Y-%m-%d %H:%M')}（{wait / 3600:.1f}h 后）")
            return wait

        # 间隔模式：从上次同步结束时算起
        elapsed = now.timestamp() - self._last_sync_end_time
        interval = self._sync_interval_hours * 3600
        remaining = max(0.0, interval - elapsed)
        logger.info(f"[ObsidianSync] 下次间隔同步: {remaining / 3600:.1f}h 后")
        return remaining

    # ── 文件状态持久化 ────────────────────────────────────
    def _load_state(self) -> dict[str, Any]:
        if FILE_STATE_FILE.exists():
            try:
                return json.loads(FILE_STATE_FILE.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_state(self, state: dict[str, Any]):
        FILE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE_STATE_FILE)

    # ── 嵌入缓存 ──────────────────────────────────────────
    def _load_embed_cache(self) -> dict:
        if EMBED_CACHE_FILE.exists():
            try:
                data = json.loads(EMBED_CACHE_FILE.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and "entries" in data:
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"entries": {}, "updated_at": ""}

    def _save_embed_cache(self, cache: dict):
        EMBED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache["key_version"] = CACHE_KEY_VERSION
        tmp = EMBED_CACHE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, EMBED_CACHE_FILE)
        logger.info(f"[ObsidianSync] 嵌入缓存已保存，共 {len(cache.get('entries', {}))} 条")

    # ── 文件扫描 ──────────────────────────────────────────
    def _scan_files(self) -> list[pathlib.Path]:
        if not self._obsidian_dir.exists():
            logger.warning(f"[ObsidianSync] 目录不存在: {self._obsidian_dir}")
            return []
        return [f for f in self._obsidian_dir.rglob("*.md") if ".obsidian" not in f.parts]

    # ── 子进程工具 ────────────────────────────────────────
    def _run_subprocess(self, cmd: list, timeout: int):
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    # ── 文档名规范化 ──────────────────────────────────────
    @staticmethod
    def _normalize_doc_name(path: pathlib.Path, text: str) -> str:
        """从文件前 8 行提取 # 标题作为文档名，找不到就用文件名。"""
        lines = text.lstrip("\ufeff").splitlines()
        for line in lines[:8]:
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                if title:
                    return title
        return path.stem

    # ── 临时文件清理 ──────────────────────────────────────
    @staticmethod
    def _cleanup_temps(*paths: pathlib.Path):
        for p in paths:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    # ── 核心同步逻辑 ─────────────────────────────────────
    def _do_sync(self) -> tuple[bool, str]:
        """
        核心同步入口。通过 _sync_lock 保证同一时刻只有一个同步任务在执行。
        返回 (ok, message)。
        """
        if not self._sync_lock.acquire(blocking=False):
            logger.info("[ObsidianSync] 另一个同步任务正在执行，跳过本次")
            return False, "sync already in progress"
        try:
            return self._do_sync_inner()
        finally:
            self._last_sync_end_time = datetime.datetime.now().timestamp()
            self._sync_lock.release()

    def _do_sync_inner(self) -> tuple[bool, str]:
        """
        实际同步逻辑：
        1. 扫描 md 文件，检测变更（mtime + size）
        2. 统一缓存 key 格式（版本迁移）
        3. 只对变更文件调用嵌入 API
        4. 合并新嵌入到本地缓存，移除已删除条目
        5. 全量缓存写入 embeddings.json → build_kb.py 重建索引
        """
        all_files = self._scan_files()
        if not all_files:
            msg = "obsidian dir empty or missing"
            self._write_status(ok=False, stage="scan", message=msg, changed=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message=msg, stage="scan", changed=0)
            return False, msg

        # 大文件保护
        oversized = [f for f in all_files if f.stat().st_size > MAX_FILE_SIZE_BYTES]
        if oversized:
            logger.warning(
                f"[ObsidianSync] {len(oversized)} 个文件超过 {MAX_FILE_SIZE_BYTES // 1024}KB，已跳过: "
                + ", ".join(f.name for f in oversized[:5])
                + ("..." if len(oversized) > 5 else "")
            )
        valid_files = [f for f in all_files if f.stat().st_size <= MAX_FILE_SIZE_BYTES]

        # 检测变更
        old_state = self._load_state().get("files", {})
        changed_files = []
        current_paths = set()
        new_state = {}
        for md in valid_files:
            path_str = str(md)
            current_paths.add(path_str)
            st = md.stat()
            new_state[path_str] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
            prev = old_state.get(path_str)
            if not prev or prev.get("mtime_ns") != st.st_mtime_ns or prev.get("size") != st.st_size:
                changed_files.append(md)

        deleted_paths = [p for p in old_state.keys() if p not in current_paths]
        has_changes = bool(changed_files) or bool(deleted_paths)

        self._save_state({"files": new_state, "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

        if not has_changes:
            logger.info("[ObsidianSync] 无变更，跳过同步")
            self._write_status(ok=True, stage="idle", message="no changes", changed=0, deleted=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=True, message="无变更，跳过", stage="idle", changed=0, deleted=0)
            return True, "no changes"

        logger.info(f"[ObsidianSync] 检测到变更: {len(changed_files)} 新增/修改, {len(deleted_paths)} 删除")
        self._write_status(ok=True, stage="scan", message="changes detected", changed=len(changed_files), deleted=len(deleted_paths), kb_name=self._kb_name)

        # 加载嵌入缓存
        cache = self._load_embed_cache()
        entries = cache.get("entries", {})

        # 步骤1: 缓存 key 格式版本迁移
        if cache.get("key_version") != CACHE_KEY_VERSION:
            migrated = {}
            for key, value in entries.items():
                try:
                    abs_path = self._obsidian_dir / key
                    new_key = abs_path.relative_to(self._obsidian_dir).as_posix()
                except (ValueError, OSError):
                    new_key = key
                migrated[new_key] = value
            entries = migrated
            logger.info(f"[ObsidianSync] 缓存 key 格式已迁移，共 {len(entries)} 条")

        # 步骤2: 移除已删除文件的缓存（统一用 POSIX key）
        for dp in deleted_paths:
            dp_path = pathlib.Path(dp)
            posix_key = _posix_relative(dp_path, self._obsidian_dir)
            entries.pop(posix_key, None)
            entries.pop(dp, None)  # 兼容清理旧格式 key
        if deleted_paths:
            logger.info(f"[ObsidianSync] 从缓存中移除 {len(deleted_paths)} 个已删除文件")

        # 步骤3: 嵌入变更文件
        api_call_count = 0
        if changed_files:
            changed_summaries = []
            for md in changed_files:
                try:
                    text = md.read_text(encoding="utf-8")
                    if not text.strip():
                        continue
                    rel = _posix_relative(md, self._obsidian_dir)
                    title = self._normalize_doc_name(md, text)
                    changed_summaries.append({
                        "text": text,
                        "source": rel,
                        "title": title,
                        "name": title,
                        "doc_name": title,
                    })
                except (OSError, UnicodeDecodeError) as e:
                    logger.warning(f"[ObsidianSync] 读取 {md.name} 失败: {e}")

            if not changed_summaries:
                logger.info("[ObsidianSync] 变更文件均为空，跳过嵌入")
                self._write_status(ok=True, stage="build", message="empty files only", changed=len(changed_files), kb_name=self._kb_name)
                self._persist_readonly_status(ok=True, message="变更文件均为空", stage="build", changed=len(changed_files))
                return True, "empty files only"

            # 临时文件名加 PID 防多实例冲突
            pid = os.getpid()
            tmp_sum = TMP_DIR / f"obsidian_sync_tmp_sum_{pid}.json"
            tmp_emb = TMP_DIR / f"obsidian_sync_tmp_emb_{pid}.json"
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            tmp_sum.write_text(json.dumps(changed_summaries, ensure_ascii=False, indent=2), encoding="utf-8")

            logger.info(f"[ObsidianSync] 嵌入 {len(changed_summaries)} 个变更文件...")
            try:
                r1 = self._run_subprocess(
                    [sys.executable, str(EMBED_SCRIPT), str(tmp_sum), "-o", str(tmp_emb)],
                    600,
                )
            except subprocess.TimeoutExpired:
                logger.error("[ObsidianSync] embed.py 执行超时（600s）")
                self._write_status(ok=False, stage="embed", message="embed.py timeout", changed=len(changed_files), kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message="embed.py 超时", stage="embed", changed=len(changed_files))
                self._cleanup_temps(tmp_sum, tmp_emb)
                return False, "embed timeout"

            if r1.returncode != 0 or not tmp_emb.exists():
                error_msg = (r1.stderr or r1.stdout or "unknown error")[-500:]
                logger.error(f"[ObsidianSync] embed.py 失败: {error_msg}")
                self._write_status(ok=False, stage="embed", message="embed.py failed", changed=len(changed_files), kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=f"embed failed: {error_msg[:200]}", stage="embed", changed=len(changed_files))
                self._cleanup_temps(tmp_sum, tmp_emb)
                return False, "embed failed"

            # 读取新嵌入，更新缓存
            try:
                emb_data = json.loads(tmp_emb.read_text(encoding="utf-8-sig"))
                new_embeddings = emb_data.get("embeddings", [])
                new_summaries = emb_data.get("summaries", changed_summaries)
                for i, summary in enumerate(new_summaries):
                    source = summary.get("source", "")
                    if source and i < len(new_embeddings):
                        entries[source] = {
                            "embedding": new_embeddings[i],
                            "summary": summary,
                        }
                api_call_count = len(new_summaries)
                logger.info(f"[ObsidianSync] 缓存更新: +{api_call_count} 条新嵌入")
            except (json.JSONDecodeError, OSError, IndexError) as e:
                logger.error(f"[ObsidianSync] 读取嵌入结果失败: {e}")
                self._write_status(ok=False, stage="embed", message=f"read embeddings failed: {e}", changed=len(changed_files), kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message="读取嵌入结果失败", stage="embed", changed=len(changed_files))
                self._cleanup_temps(tmp_sum, tmp_emb)
                return False, "read embeddings failed"

            self._cleanup_temps(tmp_sum, tmp_emb)

        # 步骤4: 缓存大小检查
        if len(entries) > MAX_CACHE_ENTRIES:
            logger.warning(
                f"[ObsidianSync] 缓存条目 ({len(entries)}) 超过建议上限 ({MAX_CACHE_ENTRIES})，"
                f"可能影响性能。建议清理不再需要的文件或调整上限。"
            )

        # 步骤5: 保存更新后的缓存
        cache["entries"] = entries
        self._save_embed_cache(cache)

        # 步骤6: 从缓存构建全量 embeddings.json 传给 build_kb.py
        if not entries:
            logger.warning("[ObsidianSync] 缓存为空，无法构建知识库")
            self._write_status(ok=False, stage="build", message="cache empty", changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message="缓存为空", stage="build", changed=len(changed_files))
            return False, "cache empty"

        all_summaries = []
        all_embeddings = []
        for source, entry in entries.items():
            all_summaries.append(entry.get("summary", {"source": source}))
            all_embeddings.append(entry.get("embedding", []))

        pid = os.getpid()
        full_emb = TMP_DIR / f"obsidian_sync_full_emb_{pid}.json"
        try:
            full_emb.write_text(
                json.dumps({"embeddings": all_embeddings, "summaries": all_summaries}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"[ObsidianSync] 写入全量嵌入文件失败: {e}")
            return False, f"write full embeddings failed: {e}"

        # 步骤7: 全量构建知识库
        logger.info(f"[ObsidianSync] 构建知识库（全量 {len(all_summaries)} 条向量）...")
        try:
            r2 = self._run_subprocess(
                [
                    sys.executable, str(BUILD_SCRIPT),
                    str(full_emb),
                    "--name", self._kb_name,
                    "--file-id", self._kb_file_id,
                    "--data-dir", str(ASTRBOT_DATA),
                ],
                300,
            )
        except subprocess.TimeoutExpired:
            logger.error("[ObsidianSync] build_kb.py 执行超时（300s）")
            self._cleanup_temps(full_emb)
            return False, "build_kb timeout"

        self._cleanup_temps(full_emb)

        if r2.returncode != 0:
            error_msg = (r2.stderr or r2.stdout or "unknown error")[-500:]
            logger.error(f"[ObsidianSync] build_kb.py 失败: {error_msg}")
            self._write_status(ok=False, stage="build", message="build_kb.py failed", changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message=f"build_kb failed: {error_msg[:200]}", stage="build", changed=len(changed_files))
            return False, "build_kb failed"

        total = len(all_summaries)
        result_msg = f"sync ok (总{total}条, API调用{api_call_count}条)"
        logger.info(f"[ObsidianSync] 同步完成！知识库共 {total} 条，本次 API 调用 {api_call_count} 条")
        self._write_status(ok=True, stage="build", message="sync ok", changed=len(changed_files), deleted=len(deleted_paths), kb_name=self._kb_name)
        self._persist_readonly_status(ok=True, message=result_msg, stage="build", changed=len(changed_files), deleted=len(deleted_paths))
        return True, result_msg

    # ── 同步循环（后台线程） ─────────────────────────────
    def _sync_loop(self):
        if self._sync_on_startup:
            try:
                logger.info("[ObsidianSync] 启动时执行首次同步...")
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 初始同步出错: {e}")
                self._write_status(ok=False, stage="startup", message=str(e)[:300], changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="startup", changed=0)
        else:
            logger.info("[ObsidianSync] 已跳过启动时同步，等待下一个计划时间点")

        while not self._stop_event.is_set():
            self._reload_config()
            wait = self._get_wait_seconds()
            remaining = wait
            while remaining > 0 and not self._stop_event.is_set():
                chunk = min(15, remaining)
                self._stop_event.wait(chunk)
                remaining -= chunk

            # 检测两种手动触发方式
            if self._check_manual_sync() or self._manual_trigger.is_set():
                logger.info("[ObsidianSync] 检测到手动同步请求，立即执行...")
                self._manual_trigger.clear()
                break

            if self._stop_event.is_set():
                break

            try:
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 同步出错: {e}")
                self._write_status(ok=False, stage="sync", message=str(e)[:300], changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="sync", changed=0)

    # ── 聊天指令 ──────────────────────────────────────────
    @filter.command("obsync")
    async def manual_sync(self, event: AstrMessageEvent):
        '''手动触发 Obsidian 知识库同步'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限使用这个命令。")
            return
        try:
            # 在线程池中执行同步，不阻塞事件循环
            loop = asyncio.get_running_loop()
            ok, msg = await loop.run_in_executor(None, self._do_sync)
            yield event.plain_result(f"Obsidian 同步{'完成' if ok else '失败'}！{msg}")
        except Exception as e:
            yield event.plain_result(f"同步出错: {e}")

    @filter.command("obsync_status")
    async def sync_status(self, event: AstrMessageEvent):
        '''查看 Obsidian 同步状态'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限查看这个状态。")
            return
        try:
            if STATUS_FILE.exists():
                data = json.loads(STATUS_FILE.read_text(encoding="utf-8-sig"))
                msg = (
                    f"最后同步: {data.get('updated_at', 'unknown')}\n"
                    f"状态: {'成功' if data.get('ok') else '失败'}\n"
                    f"阶段: {data.get('stage', 'unknown')}\n"
                    f"说明: {data.get('message', '')}\n"
                    f"知识库: {data.get('kb_name', self._kb_name)}\n"
                    f"变更数: {data.get('changed', 0)}"
                )
            else:
                msg = "还没有同步状态记录。"
            yield event.plain_result(msg)
        except (json.JSONDecodeError, OSError) as e:
            yield event.plain_result(f"读取状态失败: {e}")

    # ── 生命周期 ──────────────────────────────────────────
    async def terminate(self):
        self._stop_event.set()
        self._manual_trigger.set()  # 唤醒可能在等待的循环
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[ObsidianSync] 已停止")
