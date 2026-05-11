"""
Obsidian 知识库同步插件 for AstrBot
==================================
- 支持每日定时或定时间隔同步
- 通过 WebUI 配置面板设置参数
- 检测 Obsidian 目录变更后自动增量更新知识库
- 支持命令权限控制与同步状态记录
- 支持配置面板手动同步与状态回显
"""
import sys
import json
import pathlib
import threading
import subprocess
import datetime
import os
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


def _detect_data_dir() -> pathlib.Path:
    """自动检测 AstrBot 数据目录，兼容不同部署方式。"""
    # 1. 环境变量
    env = os.environ.get("ASTRBOT_DATA")
    if env:
        p = pathlib.Path(env)
        if p.exists():
            return p
    # 2. 相对于插件目录向上查找（plugins/obsidian_sync → data）
    this_file = pathlib.Path(__file__).resolve()
    for parent in this_file.parents:
        if (parent / "plugins").is_dir() and (parent / "knowledge_base").is_dir():
            return parent
    # 3. 常见路径
    candidates = [
        pathlib.Path.home() / ".astrbot" / "data",
        pathlib.Path("/root/.astrbot/data"),
    ]
    for c in candidates:
        if c.exists():
            return c
    # 4. 兜底：向上两级
    return this_file.parent.parent.parent


ASTRBOT_DATA = _detect_data_dir()
EMBED_SCRIPT = ASTRBOT_DATA / "skills" / "kb-importer-1.0.0" / "scripts" / "embed.py"
BUILD_SCRIPT = ASTRBOT_DATA / "skills" / "kb-importer-1.0.0" / "scripts" / "build_kb.py"
TMP_DIR = ASTRBOT_DATA / "plugin_data"
STATUS_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.json"
REPORT_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.md"
FILE_STATE_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_file_states.json"


@register("obsidian_sync", "牧濑红莉栖", "监听本地 Obsidian 目录，定时同步到 AstrBot 知识库", "0.5.0")
class ObsidianSync(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self._stop_event = threading.Event()
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

        self._thread.start()
        logger.info(
            f"[ObsidianSync] 已启动 | 目录: {self._obsidian_dir} | 模式: {self._sync_mode} | "
            f"{'定时: ' + self._sync_daily_time if self._sync_mode == 'daily' else '间隔: ' + str(self._sync_interval_hours) + 'h'} | 知识库: {self._kb_name}"
        )

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
        except Exception:
            pass

    # ── 配置面板只读状态写回 ──────────────────────────────

    def _persist_readonly_status(self, ok: bool, message: str, stage: str, changed: int = 0, deleted: int = 0):
        """
        将同步结果写回到配置文件，使 WebUI 配置面板能看到：
        - 上次同步时间 / 状态 / 说明
        - 手动同步开关自动复位
        """
        try:
            cfg_path = ASTRBOT_DATA / "config" / "obsidian_sync_config.json"
            if not cfg_path.exists():
                return
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            cfg["last_sync_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cfg["last_sync_status"] = "成功" if ok else "失败"
            cfg["last_sync_message"] = message[:300]
            cfg["sync_now_request"] = False
            cfg["last_sync_stage"] = stage
            cfg["last_sync_changed"] = changed
            cfg["last_sync_deleted"] = deleted
            tmp = cfg_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cfg_path)
            logger.debug("[ObsidianSync] 配置面板状态已写回")
        except Exception as e:
            logger.warning(f"[ObsidianSync] 写回状态到配置失败: {e}")

    # ── 配置热更新 ────────────────────────────────────────

    def _reload_config(self):
        try:
            cfg_path = ASTRBOT_DATA / "config" / "obsidian_sync_config.json"
            if not cfg_path.exists():
                return
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
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

    # ── 手动同步请求检测 ──────────────────────────────────

    def _check_manual_sync(self) -> bool:
        try:
            cfg_path = ASTRBOT_DATA / "config" / "obsidian_sync_config.json"
            if not cfg_path.exists():
                return False
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            return bool(cfg.get("sync_now_request", False))
        except Exception:
            return False

    # ── 定时计算 ──────────────────────────────────────────

    def _get_wait_seconds(self):
        if self._sync_mode == "daily":
            now = datetime.datetime.now()
            try:
                hour, minute = map(int, self._sync_daily_time.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 3, 0
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info(f"[ObsidianSync] 下次定时同步: {target.strftime('%Y-%m-%d %H:%M')}（{wait/3600:.1f}h 后）")
            return wait
        wait = self._sync_interval_hours * 3600
        logger.info(f"[ObsidianSync] 下次间隔同步: {wait/3600:.1f}h 后")
        return wait

    # ── 文件状态持久化 ────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if FILE_STATE_FILE.exists():
            try:
                return json.loads(FILE_STATE_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                return {}
        return {}

    def _save_state(self, state: dict[str, Any]):
        FILE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE_STATE_FILE)

    # ── 文件扫描 ──────────────────────────────────────────

    def _scan_files(self):
        if not self._obsidian_dir.exists():
            logger.warning(f"[ObsidianSync] 目录不存在: {self._obsidian_dir}")
            return []
        return [f for f in self._obsidian_dir.rglob("*.md") if ".obsidian" not in f.parts]

    # ── 子进程工具 ────────────────────────────────────────

    def _run_subprocess(self, cmd, timeout):
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    # ── 文档名规范化 ──────────────────────────────────────

    @staticmethod
    def _normalize_doc_name(path: pathlib.Path, text: str) -> str:
        lines = text.lstrip("\ufeff").splitlines()
        for line in lines[:8]:
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                if title:
                    return title
        return path.stem

    # ── 增量构建 ──────────────────────────────────────────

    def _do_incremental_build(self, changed_files: list):
        summaries = []
        for md in changed_files:
            try:
                text = md.read_text(encoding="utf-8")
                if not text.strip():
                    continue
                rel = str(md.relative_to(self._obsidian_dir)).replace("\\", "/")
                title = self._normalize_doc_name(md, text)
                summaries.append({
                    "text": text,
                    "source": rel,
                    "title": title,
                    "name": title,
                    "doc_name": title,
                })
            except Exception as e:
                logger.warning(f"[ObsidianSync] 读取 {md.name} 失败: {e}")

        if not summaries:
            return

        tmp_sum = TMP_DIR / "obsidian_sync_tmp_summaries.json"
        tmp_emb = TMP_DIR / "obsidian_sync_tmp_embeddings.json"
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        tmp_sum.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(f"[ObsidianSync] 嵌入 {len(summaries)} 个文件...")
        r1 = self._run_subprocess([sys.executable, str(EMBED_SCRIPT), str(tmp_sum), "-o", str(tmp_emb)], 600)
        if r1.returncode != 0 or not tmp_emb.exists():
            logger.error(f"[ObsidianSync] embed.py 失败: {(r1.stderr or r1.stdout)[-500:]}")
            self._write_status(ok=False, stage="embed", message="embed.py failed", changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message="embed.py failed", stage="embed", changed=len(changed_files))
            return

        logger.info("[ObsidianSync] 构建知识库增量...")
        r2 = self._run_subprocess([
            sys.executable, str(BUILD_SCRIPT), str(tmp_emb),
            "--name", self._kb_name, "--file-id", self._kb_file_id,
            "--data-dir", str(ASTRBOT_DATA)
        ], 300)
        if r2.returncode != 0:
            logger.error(f"[ObsidianSync] build_kb.py 失败: {(r2.stderr or r2.stdout)[-500:]}")
            self._write_status(ok=False, stage="build", message="build_kb.py failed", changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message="build_kb.py failed", stage="build", changed=len(changed_files))
        else:
            logger.info(f"[ObsidianSync] 增量同步完成 ({len(summaries)} 文件)")
            self._write_status(ok=True, stage="build", message="sync ok", changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=True, message="sync ok", stage="build", changed=len(changed_files))

        for tmp in [tmp_sum, tmp_emb]:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ── 主同步逻辑 ────────────────────────────────────────

    def _do_sync(self):
        all_files = self._scan_files()
        if not all_files:
            self._write_status(ok=False, stage="scan", message="obsidian dir empty or missing", changed=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message="obsidian dir empty or missing", stage="scan", changed=0)
            return

        old_state = self._load_state().get("files", {})
        new_state = {}
        changed = []
        current_paths = set()

        for md in all_files:
            path_str = str(md)
            current_paths.add(path_str)
            st = md.stat()
            new_state[path_str] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
            prev = old_state.get(path_str)
            if not prev or prev.get("mtime_ns") != st.st_mtime_ns or prev.get("size") != st.st_size:
                changed.append(md)

        deleted = [p for p in old_state.keys() if p not in current_paths]

        if not changed and not deleted:
            logger.info("[ObsidianSync] 无变更，跳过同步")
            self._write_status(ok=True, stage="idle", message="no changes", changed=0, deleted=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=True, message="无变更，跳过", stage="idle", changed=0, deleted=0)
            return

        self._save_state({"files": new_state, "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        logger.info(f"[ObsidianSync] 检测到变更: {len(changed)} 新增/修改, {len(deleted)} 删除")
        self._write_status(ok=True, stage="scan", message="changes detected", changed=len(changed), deleted=len(deleted), kb_name=self._kb_name)

        if changed:
            self._do_incremental_build(changed)
        else:
            # 只有删除没有新增时也需要写回状态
            self._persist_readonly_status(ok=True, message="deleted only", stage="scan", changed=0, deleted=len(deleted))

    # ── 同步循环 ──────────────────────────────────────────

    def _sync_loop(self):
        if self._sync_on_startup:
            try:
                logger.info("[ObsidianSync] 启动时执行首次同步...")
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 初始同步出错: {e}")
                self._write_status(ok=False, stage="startup", message=str(e), changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="startup", changed=0)
        else:
            logger.info("[ObsidianSync] 已跳过启动时同步，等待下一个计划时间点")

        while not self._stop_event.is_set():
            self._reload_config()
            wait = self._get_wait_seconds()
            # 把等待拆成 15 秒小段，每段检查一次手动同步请求
            remaining = wait
            while remaining > 0 and not self._stop_event.is_set():
                chunk = min(15, remaining)
                self._stop_event.wait(chunk)
                remaining -= chunk
                if self._check_manual_sync():
                    logger.info("[ObsidianSync] 检测到手动同步请求（配置面板勾选），立即执行...")
                    break
            if self._stop_event.is_set():
                break
            try:
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 同步出错: {e}")
                self._write_status(ok=False, stage="sync", message=str(e), changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="sync", changed=0)

    # ── 聊天指令 ──────────────────────────────────────────

    @filter.command("obsync")
    async def manual_sync(self, event: AstrMessageEvent):
        '''手动触发 Obsidian 知识库同步'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限使用这个命令。")
            return
        try:
            self._do_sync()
            yield event.plain_result("Obsidian 同步完成!")
        except Exception as e:
            yield event.plain_result(f"同步出错: {e}")

    @filter.command("obsync_now")
    async def obsync_now(self, event: AstrMessageEvent):
        '''快捷指令：立刻执行一次同步'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限使用这个指令。")
            return
        try:
            self._do_sync()
            yield event.plain_result("Obsidian 已即时同步完成！")
        except Exception as e:
            yield event.plain_result(f"即时同步出错: {e}")

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
        except Exception as e:
            yield event.plain_result(f"读取状态失败: {e}")

    # ── 生命周期 ──────────────────────────────────────────

    async def terminate(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[ObsidianSync] 已停止")
