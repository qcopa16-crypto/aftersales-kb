"""Small real-chain retrieve recall eval (gold set × param grid).

Spec: docs/compose/spec/retrieve-recall-eval-small.md
Uses shopkeeper-ai + real BGE + real Milvus. Refuses production collection kb_chunks.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.retrieve.recall_metrics import aggregate, classify_miss, score_item  # noqa: E402

GOLD_PATH = ROOT / "scripts" / "data" / "retrieve_gold_small.json"
REPORT_DIR = ROOT / "samples" / "eval_retrieve_small"
DEFAULT_COLLECTION = "kb_ai_chunks_eval_full"
KS = (1, 5, 8)


def load_gold(path: Path = GOLD_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not items:
        raise SystemExit(f"empty gold set: {path}")
    return data


def _apply_env(collection: str) -> Any:
    # Reuse ingest eval env bootstrap for real BGE/Milvus.
    from scripts.eval_ingest_real import _apply_env as _ingest_env

    _ingest_env(collection, None)
    from core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


def run_one(
    client,
    headers: dict[str, str],
    question: str,
    *,
    top_k: int,
    hybrid: bool | None,
    overfetch: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload: dict[str, Any] = {"question": question, "top_k": top_k}
    if hybrid is not None:
        payload["enable_hybrid"] = hybrid
    if overfetch is not None:
        payload["overfetch"] = overfetch
    resp = client.post("/internal/retrieve", headers=headers, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"retrieve HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    return [
        {
            "unit_id": c.get("unit_id"),
            "chunk_id": c.get("chunk_id"),
            "score": c.get("score"),
            "content": (c.get("content") or "")[:500],
            "retrieval_paths": c.get("retrieval_paths") or [],
        }
        for c in body.get("chunks") or []
    ], body


def grid(overfetch_alt: int = 16) -> list[dict[str, Any]]:
    combos = []
    for top_k, hybrid, overfetch in itertools.product((5, 8), (True, False), (None, overfetch_alt)):
        combos.append({"top_k": top_k, "enable_hybrid": hybrid, "overfetch": overfetch})
    return combos


def evaluate_config(
    client,
    headers: dict[str, str],
    items: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    rows = []
    errors = []
    for gold in items:
        t0 = time.monotonic()
        try:
            hits, meta = run_one(
                client,
                headers,
                gold["question"],
                top_k=params["top_k"],
                hybrid=params.get("enable_hybrid"),
                overfetch=params.get("overfetch"),
            )
            err = None
        except Exception as exc:
            hits, meta, err = [], {}, str(exc)
        row = score_item(hits, gold, top_k=params["top_k"])
        row["error"] = err
        row["took_ms"] = meta.get("took_ms") if meta else None
        row["degraded_to_dense"] = meta.get("degraded_to_dense") if meta else None
        rows.append(row)
        if err or not (row.get("unit_hit") and row.get("content_hit") and (row.get("unit_rank") or 99) <= 3):
            errors.append(
                {
                    "id": gold.get("id"),
                    "question": gold.get("question"),
                    "category": gold.get("category"),
                    "bucket": "request_error" if err else classify_miss(row),
                    "unit_rank": row.get("unit_rank"),
                    "top_units": row.get("top_units"),
                    "gold_unit_ids": gold.get("gold_unit_ids"),
                    "error": err,
                }
            )
    agg = aggregate(rows, ks=KS)
    # pairwise hybrid contrast filled later in report
    return {"params": params, "agg": agg, "rows": rows, "errors": errors}


def write_report(results: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # contrast hybrid on/off at same top_k/overfetch for lexical_gap / dense_gap
    by_key = {
        (r["params"]["top_k"], r["params"]["overfetch"], r["params"]["enable_hybrid"]): r
        for r in results
    }
    for r in results:
        p = r["params"]
        other = by_key.get((p["top_k"], p["overfetch"], not p["enable_hybrid"]))
        for e in r["errors"]:
            if e.get("bucket") not in {"miss_all", "partial_keyword", "hit_low_rank"}:
                continue
            oid = e["id"]
            other_row = next((x for x in (other["rows"] if other else []) if x.get("id") == oid), None)
            this_row = next((x for x in r["rows"] if x.get("id") == oid), None)
            this_hit = bool(this_row and this_row.get("unit_hit"))
            other_hit = bool(other_row and other_row.get("unit_hit"))
            if other is not None:
                if p["enable_hybrid"] and this_hit and not other_hit:
                    e["bucket"] = "lexical_gap"
                elif p["enable_hybrid"] and not this_hit and other_hit:
                    e["bucket"] = "dense_gap"

    meta_out = {k: v for k, v in meta.items() if not k.startswith("_")}
    payload = {"meta": meta_out, "results": results}
    (REPORT_DIR / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (REPORT_DIR / "errors.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            for e in r["errors"]:
                f.write(json.dumps({"params": r["params"], **e}, ensure_ascii=False) + "\n")

    lines = [
        "# 检索 Recall 测评",
        "",
        f"- 时间：{meta.get('finished_at')}",
        f"- 集合：`{meta.get('collection')}`",
        f"- 金标：{meta.get('n_gold')} 条 · 网格 {meta.get('n_configs')} 组",
        f"- Embedding：`{meta.get('embed_provider')}` dim=`{meta.get('embed_dim')}`",
        "",
        "## 参数网格汇总",
        "",
        "| top_k | hybrid | overfetch | R@1 | R@5 | R@8 | C@5 | MRR | miss | low_rank | partial |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        p, a = r["params"], r["agg"]
        buckets = {}
        for e in r["errors"]:
            buckets[e.get("bucket") or "?"] = buckets.get(e.get("bucket") or "?", 0) + 1
        lines.append(
            f"| {p['top_k']} | {p['enable_hybrid']} | {p['overfetch']} | "
            f"{a.get('unit_recall@1')} | {a.get('unit_recall@5')} | {a.get('unit_recall@8')} | "
            f"{a.get('content_hit@5')} | {a.get('mrr')} | "
            f"{buckets.get('miss_all', 0)} | {buckets.get('hit_low_rank', 0)} | {buckets.get('partial_keyword', 0)} |"
        )

    # Per category / difficulty at the best config (highest mrr then R@5)
    if results:
        best = max(results, key=lambda r: (r["agg"].get("mrr") or 0, r["agg"].get("unit_recall@5") or 0))
        gold_by_id = {g["id"]: g for g in (meta.get("_items") or [])}
        core_ids = {
            str((gold_by_id.get(row.get("id")) or {}).get("id") or row.get("id"))
            for row in best["rows"]
            if (gold_by_id.get(row.get("id")) or {}).get("core")
        }
        core_rows = [r for r in best["rows"] if r.get("id") in core_ids]
        if core_rows:
            n = len(core_rows)
            hits5 = sum(1 for r in core_rows if r.get("unit_rank") and r["unit_rank"] <= (best["params"]["top_k"] or 5))
            mrr = sum(float(r.get("reciprocal_rank") or 0) for r in core_rows) / n
            lines.extend(
                [
                    "",
                    f"## core 子集（n={n}）",
                    "",
                    f"- R@{best['params']['top_k']}: {hits5}/{n} = {round(hits5 / n, 4)}",
                    f"- MRR: {round(mrr, 4)}",
                ]
            )
        lines.append("| bucket | n | unit_hit@<=k | mrr |")
        lines.append("|---|---:|---:|---:|")
        k = best["params"]["top_k"]
        for key_name in ("category", "difficulty"):
            groups: dict[str, list] = {}
            for row in best["rows"]:
                g = gold_by_id.get(row.get("id")) or {}
                tag = str(g.get(key_name) or "unknown")
                groups.setdefault(tag, []).append(row)
            for tag, rs in sorted(groups.items()):
                hits = sum(1 for r in rs if r.get("unit_rank") and r["unit_rank"] <= k)
                mrr = sum(float(r.get("reciprocal_rank") or 0) for r in rs) / (len(rs) or 1)
                lines.append(f"| {key_name}:{tag} | {len(rs)} | {hits}/{len(rs)} | {round(mrr, 4)} |")
    lines.extend(["", "## 错例（按配置）", ""])
    for r in results:
        p = r["params"]
        lines.append(f"### top_k={p['top_k']} hybrid={p['enable_hybrid']} overfetch={p['overfetch']}")
        lines.append("")
        if not r["errors"]:
            lines.append("- （无）")
        for e in r["errors"]:
            lines.append(
                f"- `{e.get('id')}` [{e.get('bucket')}] rank={e.get('unit_rank')} "
                f"Q: {e.get('question')} → gold={e.get('gold_unit_ids')} top={e.get('top_units')}"
            )
        lines.append("")
    md = REPORT_DIR / "report.md"
    lines.extend(
        [
            "",
            "## 结论（重要）",
            "",
            "- 若网格普遍 R@1≈1.0：当前金标落在**小闭集**上，区分度不足（天花板效应），**不能**据此认为检索调优完成。",
            "- 本脚本验证真实链路 / 指标 / 错例管道可用；参数对比需更难金标或更大语料。",
            "- 下一步：扩难度（改写/跨文档/表格细节）或挂 `kb_ai_chunks_eval_full` 后再比 hybrid/overfetch。",
            "",
        ]
    )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md


def main() -> int:
    parser = argparse.ArgumentParser(description="Small retrieve recall eval")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--gold", default=str(GOLD_PATH))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--top-k", type=int, default=None, help="single-run override (skip grid)")
    args = parser.parse_args()

    gold = load_gold(Path(args.gold))
    items = gold["items"]
    if args.list:
        print(f"gold items: {len(items)}")
        for it in items:
            print(f"  {it['id']}\t{it['category']}\t{it['question'][:40]}")
        print(f"grid size: {len(grid())}")
        cats = {}
        diffs = {}
        for it in items:
            cats[it.get("category")] = cats.get(it.get("category"), 0) + 1
            diffs[it.get("difficulty")] = diffs.get(it.get("difficulty"), 0) + 1
        print(f"categories: {cats}")
        print(f"difficulties: {diffs}")
        return 0

    cfg = _apply_env(args.collection)
    if cfg.milvus_collection == "kb_chunks":
        print("[ENV] refuse production collection kb_chunks")
        return 2

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    headers = {"X-Internal-Token": cfg.internal_token}
    rh = client.get("/health")
    print(f"[health] {rh.status_code}")

    configs = (
        [{"top_k": args.top_k, "enable_hybrid": True, "overfetch": None}]
        if args.top_k
        else grid(overfetch_alt=24)
    )
    results = []
    for i, params in enumerate(configs, 1):
        print(f"\n=== config {i}/{len(configs)} {params} ===")
        result = evaluate_config(client, headers, items, params)
        print(f"agg: {result['agg']} errors={len(result['errors'])}")
        results.append(result)

    meta = {
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "collection": cfg.milvus_collection,
        "embed_provider": cfg.embed_provider,
        "embed_dim": cfg.embed_dim,
        "n_gold": len(items),
        "n_configs": len(configs),
        "python": sys.executable,
        "gold_path": str(Path(args.gold)),
        "_items": items,
    }
    md = write_report(results, meta)
    best = max(results, key=lambda r: (r["agg"].get("mrr") or 0, r["agg"].get("unit_recall@5") or 0))
    print("\n=== BEST ===")
    print(best["params"], best["agg"])
    print(f"report: {md}")
    print(f"json:   {REPORT_DIR / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
