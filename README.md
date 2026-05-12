## Obsidian 知识库同步

将本地 Obsidian Markdown 笔记增量同步到 AstrBot 知识库。

### 功能特性
- 支持每日定时或定时间隔同步
- 仅对变更文件调用嵌入 API
- 使用本地缓存合并全量向量后重建知识库
- 支持手动同步与同步状态查看
- 支持文档名修复脚本

## ⚠️ 兼容性说明

本插件生成的知识库使用 **SQLite doc.db** 存储格式（AstrBot 新版格式）。

如果同步后在 AstrBot WebUI 中出现 **“文档数量正常但正文内容为空”** 的情况，请检查知识库插件的向量存储后端配置：

文件路径：`astrbot_plugin_knowledge_base/vector_store/__init__.py`

确保导入的是新版 Store：
```python
from .astrbot_faiss_store import FaissStore
```

如果仍然是旧版导入：
```python
from .faiss_store import FaissStore
```

则需要修改为新版导入，并重启 AstrBot。旧版 `faiss_store.py` 仅支持 pickle 格式的 `.db` 文件，无法读取本插件生成的 SQLite 格式。

### 修复方法

1. 打开 `plugins/astrbot_plugin_knowledge_base/vector_store/__init__.py`
2. 将 `from .faiss_store import FaissStore` 改为 `from .astrbot_faiss_store import FaissStore`
3. 重启 AstrBot
4. 在 Obsidian 目录中修改任意 `.md` 文件触发一次增量同步
5. 检查 WebUI 知识库内容是否正常显示

### 辅助工具

`fix_kb_doc_names.py` 可独立运行，用于修复知识库中文档名显示为 UUID 的问题：

```bash
python fix_kb_doc_names.py --kb-name "Obsidian-Vault"
python fix_kb_doc_names.py --kb-name "Obsidian-Vault" --data-dir "D:/AstrBotData"
```

## 发布说明

### v0.8.1
- 修复与 AstrBot 新版知识库存储格式的兼容问题
- 补充正文内容为空时的排障说明
- 补充文档名修复脚本说明
