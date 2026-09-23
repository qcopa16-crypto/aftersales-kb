"""Attribute residual retrieve misses: retrieval-side vs ingest-side.

For each error row, check whether gold unit chunks contain gold keywords
(in-store content). If content is present → retrieval/rank issue; if absent
→ ingest/fragment/missing content.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

GOLD = ROOT / "scripts" / "data" / "retrieve_gold_hard.json"
ERRORS = ROOT / "samples" / "eval_retrieve_small" / "errors.jsonl"
REPORT_DIR = ROOT / "samples" / "eval_retrieve_small"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="kb_ai_chunks_eval_full")
    parser.add_argument("--params-top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    from scripts.eval_ingest_real import _apply_env

    _apply_env(args.collection, None)
    from core.config import get_settings

    get_settings.cache_clear()
    get_settings()
    from app.deps import get_vector_store

    store = get_vector_store()
    gold_by_id = {g["id"]: g for g in json.loads(GOLD.read_text(encoding="utf-8"))["items"]}

    rows = []
    if ERRORS.is_file():
        for line in ERRORS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            rows.append(r)
    # unique by id
    seen = set()
    uniq = []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    uniq = uniq[: args.limit]

    def unit_contents(unit_id: str) -> list[str]:
        client = getattr(store, "_client", None)
        coll = getattr(store, "_collection", None)
        if client is None or coll is None:
            return []
        safe = unit_id.replace("\\", "\\\\").replace('"', '\\"')
        try:
            rows = client.query(
                collection_name=coll,
                filter=f'unit_id == "{safe}"',
                output_fields=["content", "chunk_index"],
                limit=200,
            )
        except Exception:
            return []
        return [str(r.get("content") or "") for r in rows or []]

    out = []
    for r in uniq:
        g = gold_by_id.get(r["id"]) or {}
        kws = [k.lower() for k in (g.get("gold_keywords") or []) if k]
        units = g.get("gold_unit_ids") or []
        hits_content = []
        chunk_n = 0
        kw_hit = False
        for u in units:
            for text in unit_contents(u):
                chunk_n += 1
                hits_content.append(text[:120])
                low = text.lower()
                if any(k in low for k in kws):
                    kw_hit = True
        if chunk_n == 0:
            bucket = "ingest_missing_unit"
            action = "re-ingest or fix unit_id mapping"
        elif kws and not kw_hit:
            bucket = "ingest_content_gap"
            action = "chunker/keyword not in any gold unit chunk — inspect ingest"
        elif r.get("bucket") == "partial_keyword":
            bucket = "retrieval_rank_or_keyword"
            action = "content exists; improve rank/expand/rerank"
        elif r.get("bucket") == "miss_all":
            bucket = "retrieval_miss"
            action = "content exists in gold unit but not retrieved — query/dense gap"
        else:
            bucket = str(r.get("bucket") or "other")
            action = "review"
        out.append(
            {
                "id": r["id"],
                "question": r.get("question"),
                "prev_bucket": r.get("bucket"),
                "unit_rank": r.get("unit_rank"),
                "gold_unit_ids": units,
                "gold_unit_chunk_n": chunk_n,
                "keyword_in_gold_unit": kw_hit,
                "attribution": bucket,
                "action": action,
                "sample_content": hits_content[:2],
            }
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "miss_attribution.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# 剩余 miss 归因表", "", "| id | 问题 | 原桶 | 归因 | 金标chunk数 | 关键词在金标 | 建议 |", "|---|---|---|---|---:|---|---|"]
    for x in out:
        md.append(
            f"| {x['id']} | {x['question']} | {x['prev_bucket']} | **{x['attribution']}** | "
            f"{x['gold_unit_chunk_n']} | {x['keyword_in_gold_unit']} | {x['action']} |"
        )
    (REPORT_DIR / "miss_attribution.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {path} n={len(out)}")
    for x in out:
        print(x["id"], x["attribution"], "kw_in_gold", x["keyword_in_gold_unit"], "chunks", x["gold_unit_chunk_n"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
