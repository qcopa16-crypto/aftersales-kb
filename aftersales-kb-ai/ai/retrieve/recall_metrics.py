"""Pure metrics for retrieve recall eval (no I/O)."""

from __future__ import annotations

from typing import Any


def unit_hit_rank(hits: list[dict[str, Any]], gold_unit_ids: list[str]) -> int | None:
    """1-based rank of first hit whose unit_id is in gold; None if miss."""
    gold = set(gold_unit_ids or [])
    for i, h in enumerate(hits, start=1):
        if str(h.get("unit_id") or "") in gold:
            return i
    return None


def keyword_hit(hits: list[dict[str, Any]], gold_keywords: list[str] | None, *, top_k: int | None = None) -> bool:
    if not gold_keywords:
        return False
    kws = [k.lower() for k in gold_keywords if k]
    scoped = hits[:top_k] if top_k else hits
    for h in scoped:
        text = str(h.get("content") or "").lower()
        if any(k in text for k in kws):
            return True
    return False


def score_item(hits: list[dict[str, Any]], gold: dict[str, Any], *, top_k: int | None = None) -> dict[str, Any]:
    gold_units = list(gold.get("gold_unit_ids") or [])
    kws = list(gold.get("gold_keywords") or [])
    rank = unit_hit_rank(hits, gold_units)
    kw = keyword_hit(hits, kws, top_k=top_k)
    unit_ok = rank is not None
    content_ok = kw if kws else unit_ok
    return {
        "id": gold.get("id"),
        "unit_hit": unit_ok,
        "unit_rank": rank,
        "keyword_hit": kw if kws else None,
        "content_hit": content_ok,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
        "top_units": [str(h.get("unit_id") or "") for h in hits[:5]],
    }


def aggregate(rows: list[dict[str, Any]], ks: tuple[int, ...] = (1, 5, 8)) -> dict[str, Any]:
    n = len(rows) or 1
    out: dict[str, Any] = {"n": len(rows)}
    for k in ks:
        out[f"unit_recall@{k}"] = round(
            sum(1 for r in rows if r.get("unit_rank") and r["unit_rank"] <= k) / n, 4
        )
        out[f"content_hit@{k}"] = round(sum(1 for r in rows if _hit_at_k(r, k)) / n, 4)
    out["mrr"] = round(sum(float(r.get("reciprocal_rank") or r.get("rrf") or 0.0) for r in rows) / n, 4)
    return out


def _hit_at_k(row: dict[str, Any], k: int) -> bool:
    rank = row.get("unit_rank")
    if rank and rank <= k:
        return True
    return bool(row.get("keyword_hit")) and k >= 1 and not rank


def classify_miss(
    row: dict[str, Any],
    *,
    hybrid_hit: bool | None = None,
    dense_hit: bool | None = None,
) -> str | None:
    """Error bucket for non-perfect rows (or contrast between modes)."""
    if row.get("unit_hit") and row.get("content_hit") and (row.get("unit_rank") or 99) <= 3:
        return None
    if not row.get("unit_hit") and not row.get("content_hit"):
        if hybrid_hit is False and dense_hit is True:
            return "dense_gap"
        if hybrid_hit is True and dense_hit is False:
            return "lexical_gap"
        return "miss_all"
    if row.get("unit_hit") and not row.get("content_hit"):
        return "partial_keyword"
    if row.get("content_hit") and not row.get("unit_hit"):
        return "partial_keyword"
    if row.get("unit_rank") and row["unit_rank"] > 3:
        return "hit_low_rank"
    return "miss_all"
