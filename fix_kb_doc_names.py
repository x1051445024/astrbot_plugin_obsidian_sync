"""
修复 AstrBot 知识库中旧文档名，使其与 Obsidian 文件名/标题一致。

用法：
  python fix_kb_doc_names.py --kb-name "Obsidian-Vault"
  python fix_kb_doc_names.py --kb-name "Obsidian-Vault" --data-dir "D:/AstrBotData"

逻辑：
1. 在 kb.db 中找到指定 knowledge base 的 kb_id
2. 打开对应 collection 的 doc.db
3. 用 kb_documents.doc_id 对应 documents.metadata.kb_doc_id
4. 优先按 Markdown 第一行标题(# 标题) 生成文档名；否则使用 file_path 的文件名（不含后缀）
5. 仅更新 kb_documents.doc_name，不重建向量库，不改知识库名称
"""
import argparse
import json
import sqlite3
from pathlib import Path

# 默认数据目录：优先使用环境变量，其次使用常见路径
DEFAULT_DATA_DIRS = [
    Path.home() / ".astrbot" / "data",
    Path("C:/Users") / Path.home().name / ".astrbot" / "data",
    Path("/root/.astrbot/data"),
]


def _find_data_dir() -> Path:
    for d in DEFAULT_DATA_DIRS:
        if (d / "knowledge_base" / "kb.db").exists():
            return d
    return DEFAULT_DATA_DIRS[0]


def extract_title_from_text(text: str) -> str | None:
    if not text:
        return None
    lines = text.lstrip("\ufeff").splitlines()
    for line in lines[:8]:
        s = line.strip()
        if s.startswith("# "):
            t = s[2:].strip()
            if t:
                return t
    return None


def infer_name(file_path: str, text: str, fallback: str) -> str:
    title = extract_title_from_text(text)
    if title:
        return title
    if file_path:
        stem = Path(file_path).stem
        if stem:
            return stem
    return fallback


def main(kb_name: str, data_dir: Path):
    kb_db = data_dir / "knowledge_base" / "kb.db"
    if not kb_db.exists():
        raise FileNotFoundError(
            f"kb.db 不存在: {kb_db}\n"
            f"请使用 --data-dir 指定正确的 AstrBot 数据目录"
        )

    kb_conn = sqlite3.connect(str(kb_db))
    kb_conn.row_factory = sqlite3.Row
    try:
        kb_row = kb_conn.execute(
            "SELECT kb_id, kb_name FROM knowledge_bases WHERE kb_name=?",
            (kb_name,),
        ).fetchone()
        if not kb_row:
            raise ValueError(f"未找到知识库: {kb_name}")

        kb_id = kb_row["kb_id"]
        doc_db_path = data_dir / "knowledge_base" / kb_id / "doc.db"
        if not doc_db_path.exists():
            raise FileNotFoundError(f"doc.db 不存在: {doc_db_path}")

        doc_conn = sqlite3.connect(str(doc_db_path))
        doc_conn.row_factory = sqlite3.Row
        try:
            docs = kb_conn.execute(
                "SELECT doc_id, doc_name, file_path FROM kb_documents WHERE kb_id=?",
                (kb_id,),
            ).fetchall()

            updated = 0
            for row in docs:
                kb_doc_id = row["doc_id"]
                old_name = row["doc_name"] or ""
                file_path = row["file_path"] or ""

                doc_row = doc_conn.execute(
                    "SELECT text, metadata FROM documents WHERE kb_doc_id=? LIMIT 1",
                    (kb_doc_id,),
                ).fetchone()

                text = ""
                if doc_row:
                    text = doc_row["text"] or ""
                    if not file_path:
                        try:
                            meta = json.loads(doc_row["metadata"] or "{}")
                            file_path = meta.get("source", "") or file_path
                        except Exception:
                            pass

                new_name = infer_name(file_path, text, old_name or kb_doc_id)
                if new_name and new_name != old_name:
                    kb_conn.execute(
                        "UPDATE kb_documents SET doc_name=? WHERE kb_id=? AND doc_id=?",
                        (new_name, kb_id, kb_doc_id),
                    )
                    updated += 1

            kb_conn.commit()
            print(f"✅ 修复完成：知识库 {kb_name} 共更新 {updated} 条文档名")
        finally:
            doc_conn.close()
    finally:
        kb_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="修复知识库文档名")
    parser.add_argument("--kb-name", required=True, help="知识库名称，例如 Obsidian-Vault")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="AstrBot 数据目录路径，例如 D:/AstrBotData。不指定则自动检测",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir) if args.data_dir else _find_data_dir()
    main(args.kb_name, data_dir)
