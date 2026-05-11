# obsidian_sync

AstrBot 插件：将本地 Obsidian 笔记目录定时同步到知识库（FAISS），支持增量更新、配置面板手动同步、命令权限控制。

## 功能

- **自动增量同步** — 检测文件变更（mtime + size），仅处理新增/修改的 Markdown 文件
- **定时同步** — 支持 daily（每天定时）和 interval（固定间隔）两种模式
- **手动同步** — 配置面板勾选即触发，或使用 `/obsync` / `/obsync_now` 聊天指令
- **状态回显** — 配置面板自动显示上次同步时间、状态、说明
- **命令权限** — 管理员/白名单 QQ 号控制
- **文档名映射** — 优先使用 Markdown 标题，其次使用文件名
- **旧库修复** — 自带 `fix_kb_doc_names.py` 脚本，无需重建知识库

## 快速开始

1. 在 AstrBot 插件管理中安装本插件
2. 在 WebUI 配置面板设置 Obsidian 目录路径和知识库名称
3. 确保已安装 `kb-importer-1.0.0` 技能（提供 embed.py / build_kb.py）
4. 启用插件，等待首次定时同步，或手动触发

## 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| Obsidian 笔记目录路径 | Obsidian vault 的完整路径 | `D:/AstrBotData/Obsidian` |
| 同步模式 | `daily`=每天定时，`interval`=每隔固定时间 | `daily` |
| 每日定时同步时间 | 24小时制 HH:MM | `03:00` |
| 定时间隔 | 每隔多少小时同步一次 | `24` |
| 启动后是否立即同步 | 重载时是否立刻扫描全库 | `false` |
| 手动同步请求 | 勾选后 15 秒内触发同步 | `false` |
| 上次同步时间 | 自动写回，只读 | `-` |
| 上次同步状态 | 自动写回：成功/失败/未同步 | `未同步` |
| 上次同步说明 | 自动写回，只读 | `暂无` |
| 知识库名称 | AstrBot 知识库页面中创建的名称 | `Obsidian-Vault` |
| 知识库文件标识 | FAISS 索引的 ASCII 标识 | `obsidian_vault` |
| 是否限制命令权限 | 开启后仅白名单用户可用 | `true` |
| 管理员 QQ 号白名单 | 允许执行手动命令的管理员 QQ | `[]` |
| 额外允许 QQ 号白名单 | 除管理员外额外允许执行命令的 QQ | `[]` |

## 聊天指令

| 指令 | 说明 |
|------|------|
| `/obsync` | 手动触发一次同步 |
| `/obsync_now` | 立即执行一次同步（推荐） |
| `/obsync_status` | 查看上次同步状态 |

## 修复旧文档名

```bash
cd <AstrBot数据目录>/plugins/obsidian_sync
python fix_kb_doc_names.py --kb-name "Obsidian-Vault"
# 数据目录不在默认位置时：
python fix_kb_doc_names.py --kb-name "Obsidian-Vault" --data-dir "/你的/AstrBot/数据目录"
```

## 依赖

- `openai`（DashScope Embedding API 兼容）
- `faiss-cpu`
- `numpy`
- `kb-importer-1.0.0` 技能（提供 embed.py / build_kb.py）

## License

MIT
