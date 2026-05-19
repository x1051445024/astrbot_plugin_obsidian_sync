"""
KB Importer — 批量文本嵌入脚本（含 Token 优化预处理）
用法: python embed.py <summaries.json路径> [--output embeddings.json] [--no-optimize]
"""
import json, re, sys, asyncio, argparse, os, random
from pathlib import Path

# Windows 中文环境兼容：强制 stdout/stderr 使用 UTF-8
if sys.platform == "win32":
    os.system("chcp 65001 >nul 2>&1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EMBED_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBED_API_KEY = "sk-e03714846bba477ca44fa9fcf993380a"
EMBED_MODEL = "text-embedding-v4"
DIM = 1024
BATCH_SIZE = 10
MAX_RETRIES = 3
BACKOFF_BASE = 2.5

VALUE_CHAIN = re.compile(r'(?<![a-zA-Z])(\d+)\s*((?:→\s*\d+\s*){4,})')


def optimize_text(text: str) -> str:
    def compress(m):
        first_str = m.group(1)
        chain = m.group(2)
        parts = chain.split("→")
        values = [int(first_str)] + [int(p.strip()) for p in parts if p.strip().isdigit()]

        if len(values) < 5:
            return m.group()

        first, last = values[0], values[-1]
        if len(set(values)) == 1:
            return f"{first}(固定)"

        diffs = [values[i+1] - values[i] for i in range(len(values)-1)]
        if len(set(diffs)) == 1:
            return f"{first}→{last}（每级{diffs[0]:+d}）"
        return f"{first}→{last}（非均匀）"

    text = VALUE_CHAIN.sub(compress, text)
    text = text.replace("【FGO从者】", "")
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def embed_batch(client, texts, sem, batch_index: int):
    async with sem:
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await client.embeddings.create(input=texts, model=EMBED_MODEL)
                return [d.embedding for d in r.data]
            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_BASE ** attempt + random.uniform(0, 1.5)
                    print(f"  Batch {batch_index} failed (attempt {attempt}/{MAX_RETRIES}): {e}; retry in {delay:.1f}s")
                    await asyncio.sleep(delay)
        print(f"  Batch {batch_index} permanently failed: {last_err}")
        return [[0.0] * DIM] * len(texts)


async def main(input_path, output_path, no_optimize=False):
    from openai import AsyncOpenAI

    with open(input_path, "r", encoding="utf-8") as f:
        summaries = json.load(f)

    total_before = sum(len(s["text"]) for s in summaries)

    if not no_optimize:
        uniform_cnt = fixed_cnt = irregular_cnt = 0
        for s in summaries:
            s["text"] = optimize_text(s["text"])
            for m in re.finditer(r'（每级[+-]?\d+）', s["text"]): uniform_cnt += 1
            for m in re.finditer(r'\(固定\)', s["text"]): fixed_cnt += 1
            for m in re.finditer(r'（非均匀）', s["text"]): irregular_cnt += 1

        total_after = sum(len(s["text"]) for s in summaries)
        saved = total_before - total_after
        print(f"Token 优化: {total_before:,} → {total_after:,} 字 (节省 {saved:,}, {saved/total_before*100:.0f}%)")
        print(f"  压缩统计: {uniform_cnt}均匀 + {fixed_cnt}固定 + {irregular_cnt}非均匀 = {uniform_cnt+fixed_cnt+irregular_cnt}处")

    texts = [s["text"] for s in summaries]
    client = AsyncOpenAI(api_key=EMBED_API_KEY, base_url=EMBED_API_URL, timeout=120.0)
    sem = asyncio.Semaphore(5)

    tasks = []
    batch_no = 0
    for i in range(0, len(texts), BATCH_SIZE):
        batch_no += 1
        tasks.append(embed_batch(client, texts[i : i + BATCH_SIZE], sem, batch_no))

    results = await asyncio.gather(*tasks)
    embeddings = [e for batch in results for e in batch]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"embeddings": embeddings, "summaries": summaries}, f, ensure_ascii=False)
    print(f"✅ {len(embeddings)} embeddings → {output_path}")
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KB Importer — 批量文本嵌入")
    parser.add_argument("input", help="summaries JSON 文件路径")
    parser.add_argument("--output", "-o", default="embeddings.json", help="输出路径")
    parser.add_argument("--no-optimize", action="store_true", help="跳过 token 优化")
    args = parser.parse_args()
    asyncio.run(main(args.input, args.output, args.no_optimize))
