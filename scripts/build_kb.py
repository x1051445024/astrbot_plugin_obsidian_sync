"""
KB Importer — 知识库构建脚本
用法: python build_kb.py <embeddings.json> --name "集合名称" --file-id "ascii_id"
       python build_kb.py <embeddings.json> <summaries.json> --name "集合名称" --file-id "ascii_id"
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
import re
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np

# Windows 中文环境兼容
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def find_astrbot_data():
    candidates = [
        Path.home() / ".astrbot" / "data",
        Path(r"C:\Users\Lenovo\.astrbot\data"),
    ]
    for c in candidates:
        if (c / "knowledge_base" / "kb.db").exists():
            return c
    raise FileNotFoundError("Cannot find AstrBot data directory")


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release_lock(fd, lock_path: Path):
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def atomic_copy(src: Path, dst: Path):
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(str(src), str(tmp))
    os.replace(tmp, dst)


def atomic_write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def extract_doc_name(summary: dict, index: int) -> str:
    for key in ("title", "name", "doc_name", "servant"):
        val = summary.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    source = summary.get("source", "")
    if isinstance(source, str) and source.strip():
        stem = Path(source).stem
        if stem:
            return stem
    return f"doc_{index}"


def build_collection(embeddings_path, summaries_path, collection_name, file_id, data_dir):
    lock_path = data_dir / "knowledge_base" / ".build.lock"
    lock_fd = None
    try:
        lock_fd = acquire_lock(lock_path)
    except FileExistsError:
        raise RuntimeError("Another knowledge base build is already running")

    try:
        with open(embeddings_path, "r", encoding="utf-8") as f:
            emb_data = json.load(f)
        embeddings = emb_data["embeddings"]

        if "summaries" in emb_data:
            summaries = emb_data["summaries"]
        elif summaries_path:
            with open(summaries_path, "r", encoding="utf-8") as f:
                summaries = json.load(f)
        else:
            raise ValueError("No summaries found in embeddings.json and no summaries path provided")

        n = min(len(summaries), len(embeddings))
        total_chars = sum(len(s["text"]) for s in summaries[:n])
        print(f"Building '{collection_name}': {n} items, ~{total_chars:,}字 total, file_id='{file_id}'")

        # 复用已有 UUID，避免每次同步都重建目录导致 AstrBot 内存引用失效
        kb_db_path = data_dir / "knowledge_base" / "kb.db"
        existing_uuid = None
        existing_id = None
        if kb_db_path.exists():
            try:
                _tmp_db = sqlite3.connect(str(kb_db_path))
                try:
                    _row = _tmp_db.execute("SELECT id, kb_id FROM knowledge_bases WHERE kb_name=?", (collection_name,)).fetchone()
                    if _row:
                        existing_id, existing_uuid = _row[0], _row[1]
                        print(f"  Reusing existing UUID={existing_uuid[:8]}... for '{collection_name}'")
                finally:
                    _tmp_db.close()
            except Exception as e:
                print(f"  Warning: could not query existing kb: {e}")

        coll_uuid = existing_uuid if existing_uuid else str(uuid.uuid4())
        coll_dir = data_dir / "knowledge_base" / coll_uuid
        coll_dir.mkdir(parents=True, exist_ok=True)

        # 1) FAISS
        emb_array = np.array(embeddings[:n], dtype=np.float32)
        faiss.normalize_L2(emb_array)
        index = faiss.IndexFlatIP(emb_array.shape[1])
        index.add(emb_array)
        faiss.write_index(index, str(coll_dir / "index.faiss"))
        print(f"  FAISS: {index.ntotal}v, IndexFlatIP, {emb_array.shape[1]}dim")

        # 2) doc.db
        doc_db = sqlite3.connect(str(coll_dir / "doc.db"))
        metas = []
        try:
            for tbl in ["documents", "documents_fts", "documents_fts_data",
                        "documents_fts_idx", "documents_fts_docsize", "documents_fts_config"]:
                try:
                    doc_db.execute(f"DROP TABLE IF EXISTS [{tbl}]")
                except Exception:
                    pass

            doc_db.execute("""CREATE TABLE documents (
                id INTEGER NOT NULL, doc_id VARCHAR NOT NULL, text VARCHAR NOT NULL,
                metadata TEXT, created_at DATETIME, updated_at DATETIME,
                kb_doc_id TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.kb_doc_id')) STORED,
                user_id TEXT GENERATED ALWAYS AS (json_extract(metadata, '$.user_id')) STORED,
                PRIMARY KEY (id))""")
            doc_db.execute("""CREATE VIRTUAL TABLE documents_fts USING fts5(
                search_text, content='', contentless_delete=1, tokenize='unicode61')""")

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            for i, s in enumerate(summaries[:n]):
                doc_uuid = str(uuid.uuid4())
                kb_doc_uuid = str(uuid.uuid4())
                extra = {k: v for k, v in s.items() if k not in ("text",)}
                meta = {"kb_id": coll_uuid, "kb_doc_id": kb_doc_uuid, "chunk_index": 0, **extra}
                metas.append(meta)
                doc_db.execute(
                    "INSERT INTO documents(id, doc_id, text, metadata, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (i + 1, doc_uuid, s["text"], json.dumps(meta, ensure_ascii=False), now, now),
                )
                doc_db.execute("INSERT INTO documents_fts(rowid, search_text) VALUES (?,?)", (i + 1, s["text"]))
            doc_db.commit()
            doc_cnt = doc_db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            doc_db.close()

        # 3) kb.db — 原地更新，保留已有 id 和 kb_id
        kb_db = sqlite3.connect(str(data_dir / "knowledge_base" / "kb.db"))
        try:
            now2 = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            if existing_id is not None and existing_uuid is not None:
                # 已有记录：清除旧文档，更新元数据
                print(f"  Updating existing '{collection_name}' (id={existing_id}, uuid={existing_uuid[:8]}...)")
                kb_db.execute("DELETE FROM kb_documents WHERE kb_id=?", (existing_uuid,))
                kb_db.execute(
                    """UPDATE knowledge_bases
                       SET updated_at=?, chunk_count=?, doc_count=?
                       WHERE id=?""",
                    (now2, n, doc_cnt, existing_id),
                )
            else:
                # 新建记录
                existing_id = kb_db.execute("SELECT COALESCE(MAX(id),0)+1 FROM knowledge_bases").fetchone()[0]
                kb_db.execute(
                    "INSERT INTO knowledge_bases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (existing_id, coll_uuid, collection_name, "", "📚", "openai_embedding", None,
                     512, 50, 50, 50, 5, now2, now2, n, doc_cnt),
                )
                print(f"  Created new '{collection_name}' (id={existing_id}, uuid={coll_uuid[:8]}...)")

            for i, (s, meta) in enumerate(zip(summaries[:n], metas)):
                kb_db.execute(
                    "INSERT INTO kb_documents(doc_id, kb_id, doc_name, file_type, file_size, file_path, chunk_count, media_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (meta["kb_doc_id"], coll_uuid, extract_doc_name(s, i),
                     "text", len(s["text"]), s.get("source", ""), 1, 0, now2, now2),
                )
            kb_db.commit()
        finally:
            kb_db.close()

        print(f"  kb.db: id={existing_id}, docs={n}, chunks={doc_cnt}")

        # 4) user_collection_prefs
        prefs_path = data_dir / "plugin_data" / "astrbot_plugin_knowledge_base" / "user_collection_prefs.json"
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        prefs.setdefault("collection_metadata", {})[collection_name] = {
            "version": 1,
            "emoji": "📚",
            "description": "",
            "created_at": int(datetime.now().timestamp()),
            "file_id": file_id,
            "origin": "kb-importer",
            "embedding_provider_id": "openai_embedding",
            "rerank_provider_id": None,
        }
        atomic_write_json(prefs_path, prefs)
        print(f"  user_prefs: '{collection_name}' → '{file_id}'")

        # 5) Sync plugin_data
        plugin_dir = data_dir / "plugin_data" / "astrbot_plugin_knowledge_base" / "faiss_data"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        atomic_copy(coll_dir / "index.faiss", plugin_dir / f"{file_id}.index")
        atomic_copy(coll_dir / "doc.db", plugin_dir / f"{file_id}.db")
        print("  plugin_data: synced")

        print(f"\n✅ '{collection_name}' ready: {n} items, UUID={coll_uuid[:8]}...")
        print(f"   ℹ️ UUID preserved; AstrBot will pick up changes on next knowledge base access.")
    finally:
        if lock_fd is not None:
            release_lock(lock_fd, lock_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build AstrBot knowledge base collection")
    parser.add_argument("embeddings", help="Path to embeddings JSON (from embed.py)")
    parser.add_argument("summaries", nargs="?", help="Optional: separate summaries JSON")
    parser.add_argument("--name", "-n", required=True, help="Collection display name")
    parser.add_argument("--file-id", "-f", required=True, help="ASCII file ID for disk")
    parser.add_argument("--data-dir", "-d", help="AstrBot data directory")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else find_astrbot_data()
    build_collection(args.embeddings, args.summaries, args.name, args.file_id, data_dir)
