#!/usr/bin/env python3
"""预计算 embedding 相关推荐（OpenAI 兼容在线模型）。

扫描两个站点的 source/_posts，调用 OpenAI 兼容的 /v1/embeddings 接口
（默认 qwen/qwen3-embedding-8b，整篇不切块），计算余弦相似度，按 MMR 去重后为每篇
文章选 TOP_N 篇相关文章，写入各站点的 source/_data/recommend.json。

构建时由 scripts/embedding-recommend.js 读取并注入文章页；CF Pages 上
build.py 会自动调用本脚本，因此不需要提交生成的 JSON。

环境变量：
    OPENAI_BASE_URL     默认 https://api.openai.com/v1
    OPENAI_API_KEY      API key
    OPENAI_EMBED_MODEL  默认 qwen/qwen3-embedding-8b
    OPENAI_EMBED_DIM    默认按模型自动（Qwen 4096 / 3-small 1536）
    OPENAI_EMBED_FULL_TEXT 默认 1：整篇正文不切块、不限长；设为 0 回退切块
    OPENAI_EMBED_BATCH  切块模式每批输入条数，默认 64
    OPENAI_EMBED_WORKERS 切块模式并发请求数，默认 4
    OPENAI_EMBED_FULL_WORKERS 全篇模式并发请求数，默认 6

用法：
    python precompute_recommend.py
    python precompute_recommend.py --site blogxyz
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SITES = {
    "blogxyz": {"label": "相关推荐"},
    "blogxyz-en": {"label": "Related Posts"},
}

TOP_N = 6
MMR_LAMBDA = 1.0
META_WEIGHT = 0.45
BODY_WEIGHT = 0.55
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
MAX_CHUNKS = 10
DESC_LIMIT = 90

def env_any(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


API_BASE = env_any("OPENAI_BASE_URL", default="https://api.openai.com/v1").rstrip("/")
API_KEY = env_any("OPENAI_API_KEY")
MODEL = env_any("OPENAI_EMBED_MODEL", default="qwen/qwen3-embedding-8b")
FULL_TEXT = env_any("OPENAI_EMBED_FULL_TEXT", default="1").lower() not in ("0", "false", "no", "off")


def default_dim_for_model(model):
    name = model.lower()
    if "qwen3-embedding" in name:
        return 4096
    if "text-embedding-3-large" in name:
        return 3072
    if "text-embedding-3-small" in name:
        return 1536
    return 0


_dim_env = env_any("OPENAI_EMBED_DIM")
EMBED_DIM = int(_dim_env) if _dim_env else default_dim_for_model(MODEL)
BATCH_SIZE = max(1, int(env_any("OPENAI_EMBED_BATCH", default="64")))
WORKERS = max(1, int(env_any("OPENAI_EMBED_WORKERS", default="4")))
FULL_WORKERS = max(1, int(env_any("OPENAI_EMBED_FULL_WORKERS", default="6")))


def log(msg):
    print(f"[recommend] {msg}", flush=True)


def load_api_key():
    global API_KEY
    if API_KEY:
        return API_KEY
    raise SystemExit("OPENAI_API_KEY is not set")


def parse_post(filepath):
    """解析 Hexo 文章，返回元数据与正文；加密/草稿文章返回 None。"""
    with open(filepath, encoding="utf-8") as fp:
        text = fp.read()

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        log(f"skip {filepath}: bad front matter ({exc})")
        return None
    body = text[match.end():]

    # 加密文章也参与推荐：仅用明文源文生成向量；
    # 推荐卡片只展示公开的 title/cover/date/description，不输出正文。
    if meta.get("published") is False or meta.get("draft") is True:
        return None

    abbrlink = str(meta.get("abbrlink") or "").strip()
    if not abbrlink:
        log(f"skip {filepath}: no abbrlink")
        return None

    title = str(meta.get("title") or "").strip()
    tags = meta.get("tags") or []
    categories = meta.get("categories") or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(categories, str):
        categories = [categories]
    tags = [str(t).strip() for t in tags if str(t).strip()]
    categories = [str(c).strip() for c in categories if str(c).strip()]

    date = meta.get("date")
    if isinstance(date, dt.datetime):
        date_str = date.strftime("%Y-%m-%d")
    else:
        date_str = str(date or "")[:10]

    cover = meta.get("cover")
    if isinstance(cover, str):
        cover = cover.strip()
        if not cover or cover.lower() in ("false", "none"):
            cover = ""
    else:
        cover = ""

    description = str(meta.get("description") or "").strip()
    description = re.sub(r"\s+", " ", description)

    return {
        "path": f"posts/{abbrlink}/",
        "title": title,
        "tags": tags,
        "categories": categories,
        "date": date_str,
        "cover": cover,
        "description": description,
        "body": body,
    }


def clean_text(text):
    """去掉 Markdown 标记、代码块、链接等，保留可读正文。"""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", " ", text, flags=re.M)
    text = re.sub(r"[*_~`>]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP, limit=MAX_CHUNKS):
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text) and len(chunks) < limit:
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


def make_document_text(post):
    meta = "；".join(
        part for part in [
            post["title"],
            " ".join(post["tags"]),
            " ".join(post["categories"]),
            post["description"],
        ] if part
    )
    # 标题重复一次，提高主题权重
    meta = f"{post['title']}。{meta}"
    body = clean_text(post["body"])
    return meta, body


def normalize(vec):
    norm = math.sqrt(float(sum(v * v for v in vec)))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def dot(a, b):
    return float(sum(x * y for x, y in zip(a, b)))


def post_json(url, payload, headers, timeout=180, retries=4):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code != 429 and exc.code < 500:
                raise RuntimeError(last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            delay = min(20, 2 ** attempt) + random.random()
            log(f"embedding request failed ({last_error}); retry in {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(f"embedding request failed after {retries + 1} attempts: {last_error}")


def embed_batch(batch):
    key = load_api_key()
    url = f"{API_BASE}/embeddings"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # 部分网关会拦 Python 默认 UA（Cloudflare 1010）
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    }
    payload = {"model": MODEL, "input": batch}
    if EMBED_DIM > 0:
        payload["dimensions"] = EMBED_DIM
    data = post_json(url, payload, headers)
    items = data.get("data") or []
    if len(items) != len(batch):
        raise RuntimeError(
            f"embedding count mismatch: expected {len(batch)}, got {len(items)}"
        )
    items = sorted(items, key=lambda item: item.get("index", 0))
    vectors = []
    for item in items:
        embedding = item.get("embedding")
        if not embedding:
            raise RuntimeError("embedding response is missing vector data")
        vectors.append([float(v) for v in embedding])
    return vectors


def embed_texts(texts, batch_size=None, workers=None):
    """并发调用 embedding 接口批量生成向量，返回与输入顺序一致的 float 列表。"""
    bs = max(1, batch_size or BATCH_SIZE)
    wk = max(1, workers or WORKERS)
    total = len(texts)
    batches = [texts[start:start + bs] for start in range(0, total, bs)]
    results = [None] * len(batches)
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=min(wk, len(batches) or 1)) as executor:
        futures = {
            executor.submit(embed_batch, batch): index
            for index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            with lock:
                done += len(batches[index])
            log(f"embedded {done}/{total}")

    vectors = []
    for batch_vectors in results:
        vectors.extend(batch_vectors)
    return vectors


def embed_full_bodies(bodies):
    """全篇模式：每篇正文单独一个请求，失败的单篇回退 meta-only。"""
    body_vecs = [None] * len(bodies)
    errors = {}

    def work(index, body):
        if not body:
            return index, None, None
        try:
            vec = embed_texts([body], batch_size=1, workers=1)[0]
            return index, vec, None
        except Exception as exc:
            return index, None, f"{type(exc).__name__}: {str(exc)[:240]}"

    with ThreadPoolExecutor(max_workers=min(FULL_WORKERS, len(bodies) or 1)) as executor:
        futures = [executor.submit(work, i, body) for i, body in enumerate(bodies)]
        done = 0
        for future in as_completed(futures):
            index, vec, err = future.result()
            body_vecs[index] = vec
            if err:
                errors[index] = err
            done += 1
            log(f"full-text embedded {done}/{len(bodies)}")
    return body_vecs, errors


def embed_site(posts):
    """收集站点文本；默认整篇不切块，FULL_TEXT=0 时回退切块。"""
    metas = []
    bodies = []
    for post in posts:
        meta_text, body_text = make_document_text(post)
        metas.append(meta_text)
        bodies.append(body_text)

    if FULL_TEXT:
        log(f"requesting {len(metas)} meta inputs + {len(bodies)} full-text bodies")
        meta_vecs = embed_texts(metas)
        body_vecs, errors = embed_full_bodies(bodies)
        if errors:
            log(f"full-text fallback to meta-only for {len(errors)} posts")
            for index, err in list(errors.items())[:8]:
                log(f"  fallback [{posts[index]['title']}]: {err}")
        result = []
        for meta_vec, body_vec in zip(meta_vecs, body_vecs):
            if body_vec is None:
                result.append(normalize(meta_vec))
            else:
                combined = [
                    META_WEIGHT * m + BODY_WEIGHT * b
                    for m, b in zip(meta_vec, body_vec)
                ]
                result.append(normalize(combined))
        dim = len(meta_vecs[0]) if meta_vecs else 0
        return result, dim

    all_texts = []
    spans = []
    for meta_text, body_text in zip(metas, bodies):
        chunks = chunk_text(body_text)
        texts = [meta_text] + chunks
        spans.append((len(all_texts), len(texts)))
        all_texts.extend(texts)

    log(f"requesting {len(all_texts)} embedding inputs in batches of {BATCH_SIZE}")
    vectors = embed_texts(all_texts)

    result = []
    for post, (start, count) in zip(posts, spans):
        meta_vec = vectors[start]
        body_vecs = vectors[start + 1:start + count]
        if body_vecs:
            mean = [0.0] * len(body_vecs[0])
            for vec in body_vecs:
                for i, value in enumerate(vec):
                    mean[i] += value
            mean = [value / len(body_vecs) for value in mean]
            combined = [
                META_WEIGHT * m + BODY_WEIGHT * b
                for m, b in zip(meta_vec, mean)
            ]
        else:
            combined = meta_vec
        result.append(normalize(combined))
    return result, len(vectors[0]) if vectors else 0


def mmr_select(index, vectors, similarity, top_n=TOP_N, lam=MMR_LAMBDA):
    count = len(vectors)
    candidates = set(range(count))
    candidates.discard(index)
    selected = []
    while candidates and len(selected) < top_n:
        best = None
        best_score = -1e9
        for cand in candidates:
            redundancy = 0.0
            if selected:
                redundancy = max(dot(vectors[cand], vectors[s]) for s in selected)
            score = lam * similarity[index][cand] - (1 - lam) * redundancy
            if score > best_score:
                best_score = score
                best = cand
        if best is None:
            break
        selected.append(best)
        candidates.discard(best)
    return selected, similarity[index]


def build_site(site_name, config):
    site_dir = os.path.join(BASE_DIR, site_name)
    posts_dir = os.path.join(site_dir, "source", "_posts")
    out_file = os.path.join(site_dir, "source", "_data", "recommend.json")

    posts = []
    for filepath in sorted(glob.glob(os.path.join(posts_dir, "*.md"))):
        post = parse_post(filepath)
        if post:
            posts.append(post)

    if not posts:
        log(f"{site_name}: no posts, skip")
        return 0

    log(f"{site_name}: embedding {len(posts)} posts with {MODEL} "
        f"({'full-text' if FULL_TEXT else 'chunked'}, dim={EMBED_DIM or 'auto'})")
    vectors, dim = embed_site(posts)

    similarity = [[dot(a, b) for b in vectors] for a in vectors]

    result = {}
    for i, post in enumerate(posts):
        selected, scores = mmr_select(i, vectors, similarity)
        selected = sorted(selected, key=lambda j: scores[j], reverse=True)
        result[post["path"]] = [
            {
                "path": posts[j]["path"],
                "title": posts[j]["title"],
                "cover": posts[j]["cover"],
                "date": posts[j]["date"],
                "description": posts[j]["description"][:DESC_LIMIT],
                "score": round(scores[j], 4),
            }
            for j in selected
        ]

    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    payload = {
        "provider": "openai-compatible",
        "model": MODEL,
        "dim": dim,
        "generated": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "count": len(posts),
        "top_n": TOP_N,
        "label": config["label"],
        "posts": result,
    }
    with open(out_file, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=1)

    log(f"{site_name}: wrote {out_file} ({len(posts)} posts, {sum(len(v) for v in result.values())} links)")
    return len(posts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=sorted(SITES), action="append", help="只处理指定站点（可重复）")
    args = parser.parse_args()

    sites = args.site or sorted(SITES)
    total = 0
    for site_name in sites:
        total += build_site(site_name, SITES[site_name])
    log(f"done: {total} posts")


if __name__ == "__main__":
    main()
