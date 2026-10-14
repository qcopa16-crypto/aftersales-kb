"""Pure metrics for retrieve recall eval (no I/O).

Keeps the original unit-level API (unit_hit_rank / keyword_hit / score_item /
aggregate / classify_miss) for backward compatibility, and adds chunk-level
metrics (nDCG@k / precision@k / chunk_hit@k) for the harder v3 gold set.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

KS_DEFAULT = (1, 5, 8)


def unit_hit_rank(hits: list[dict[str, Any]], gold_unit_ids: list[str]) -> int | None:
    """1-based rank of first hit whose unit_id is in gold; None if miss."""
    gold = set(gold_unit_ids or [])
    for i, h in enumerate(hits, start=1):
        if str(h.get("unit_id") or "") in gold:
            return i
    return None


def keyword_hit(
    hits: list[dict[str, Any]], gold_keywords: list[str] | None, *, top_k: int | None = None
) -> bool:
    if not gold_keywords:
        return False
    kws = [k.lower() for k in gold_keywords if k]
    scoped = hits[:top_k] if top_k else hits
    for h in scoped:
        text = str(h.get("content") or "").lower()
        if any(k in text for k in kws):
            return True
    return False


def score_item(
    hits: list[dict[str, Any]], gold: dict[str, Any], *, top_k: int | None = None
) -> dict[str, Any]:
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
    out["mrr"] = round(
        sum(float(r.get("reciprocal_rank") or r.get("rrf") or 0.0) for r in rows) / n, 4
    )
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


# ---------------------------------------------------------------------------
# Chunk-level metrics (v3). Relevance is keyword-based at the chunk level when
# gold_keywords are present, falling back to unit-level when they are absent.
# ---------------------------------------------------------------------------


def _norm_kws(gold_keywords: list[str] | None) -> list[str]:
    return [k.lower() for k in (gold_keywords or []) if k]


def chunk_relevance(hit: dict[str, Any], gold_keywords: list[str] | None) -> int | None:
    """1 if the chunk text carries a gold keyword; None when no keywords given."""
    kws = _norm_kws(gold_keywords)
    if not kws:
        return None
    text = str(hit.get("content") or "").lower()
    return 1 if any(k in text for k in kws) else 0


def unit_relevance(hit: dict[str, Any], gold_unit_ids: list[str] | None) -> int | None:
    gold = set(gold_unit_ids or [])
    if not gold:
        return None
    return 1 if str(hit.get("unit_id") or "") in gold else 0


def _dcg(relevances: list[int]) -> float:
    return sum((r) / math.log2(i + 2) for i, r in enumerate(relevances))


def _ndcg(relevances: Iterable[int]) -> float:
    rel = [int(r or 0) for r in relevances]
    ideal = sorted(rel, reverse=True)
    idcg = _dcg(ideal)
    if idcg <= 0:
        return 0.0
    return _dcg(rel) / idcg


def ndcg_at_k(hits: list[dict[str, Any]], gold: dict[str, Any], k: int) -> float | None:
    """nDCG@k over chunk-level binary relevance (gold keywords)."""
    kws = _norm_kws(gold.get("gold_keywords"))
    if not kws:
        return None
    rel = [chunk_relevance(h, kws) for h in hits[:k]]
    if None in rel:
        return None
    return round(_ndcg([r for r in rel if r is not None]), 4)


def precision_at_k(hits: list[dict[str, Any]], gold: dict[str, Any], k: int) -> float | None:
    """Precision@k over unit-level relevance."""
    gold_units = set(gold.get("gold_unit_ids") or [])
    if not gold_units:
        return None
    top = hits[:k]
    if not top:
        return 0.0
    return round(sum(1 for h in top if str(h.get("unit_id") or "") in gold_units) / len(top), 4)


def chunk_hit_at_k(hits: list[dict[str, Any]], gold: dict[str, Any], k: int) -> bool | None:
    """True if any of top-k chunks carries a gold keyword."""
    kws = _norm_kws(gold.get("gold_keywords"))
    if not kws:
        return None
    return any(chunk_relevance(h, kws) == 1 for h in hits[:k])


# ---------------------------------------------------------------------------
# Evidence-level metrics (v4 calibration). Distinct from both unit_recall
# (unit id only) and the *legacy* chunk_hit (keyword anywhere, unit-agnostic).
#
#   evidence_hit@k : some chunk in top-k is BOTH in a gold unit AND carries a
#                    required answer point -> the answer is actually reachable
#                    *with* its supporting unit, not just keyword-matched
#                    somewhere in the corpus.
#   all_evidence_hit@k : every *required* gold unit has such a chunk. Only
#                    defined for multi-unit items (denominator = #multi-unit
#                    items); flagged "样本不足" when that count is small.
# ---------------------------------------------------------------------------


def required_answer_points(gold: dict[str, Any]) -> list[tuple[str, set[str]]]:
    """(point_text, allowed_unit_ids) pairs a chunk must carry to count.

    Answer gold supplies per-item ``required_answer_points`` (each a dict with
    ``text`` and the ``gold_unit_ids`` that support it); the retrieve gold has
    no such field, so its ``gold_keywords`` act as the required points with no
    per-point unit restriction (the item-level gold units still apply).
    """
    pts = gold.get("required_answer_points")
    out: list[tuple[str, set[str]]] = []
    if pts:
        for p in pts:
            if isinstance(p, dict):
                text = str(p.get("text") or "").strip()
                units = {str(u) for u in (p.get("gold_unit_ids") or []) if u}
            else:
                text, units = str(p).strip(), set()
            if text:
                out.append((text, units))
        return out
    return [(str(k).strip(), set()) for k in (gold.get("gold_keywords") or []) if str(k).strip()]


def required_gold_units(gold: dict[str, Any]) -> list[str]:
    """The gold units that *must* be retrieved for this item.

    Explicit ``required_gold_unit_ids`` wins; otherwise the point-declared
    units when present, else the item-level ``gold_unit_ids``.
    """
    explicit = gold.get("required_gold_unit_ids")
    if explicit:
        return sorted({str(u) for u in explicit if u})
    pts = gold.get("required_answer_points")
    if pts:
        units = {
            str(u) for p in pts if isinstance(p, dict) for u in (p.get("gold_unit_ids") or []) if u
        }
        if units:
            return sorted(units)
    return sorted({str(u) for u in (gold.get("gold_unit_ids") or []) if u})


def _norm_text(s: str) -> str:
    return " ".join((s or "").lower().split())


def _chunk_carries_point(hit: dict[str, Any], uid: str, points: list[tuple[str, set[str]]]) -> bool:
    text = _norm_text(str(hit.get("content") or ""))
    if not text:
        return False
    for ptext, punits in points:
        if punits and uid not in punits:
            continue
        if _norm_text(ptext) in text:
            return True
    return False


def evidence_hit_at_k(hits: list[dict[str, Any]], gold: dict[str, Any], k: int) -> bool | None:
    """True if a top-k chunk is in a gold unit AND carries a required point."""
    points = required_answer_points(gold)
    gold_units = {str(u) for u in (gold.get("gold_unit_ids") or []) if u}
    if not points or not gold_units:
        return None
    for h in hits[:k]:
        uid = str(h.get("unit_id") or "")
        if uid in gold_units and _chunk_carries_point(h, uid, points):
            return True
    return False


def all_evidence_hit_at_k(hits: list[dict[str, Any]], gold: dict[str, Any], k: int) -> bool | None:
    """True only when *every* required gold unit has a qualifying top-k chunk.

    None for single-unit items (unscorable for this metric).
    """
    units = required_gold_units(gold)
    if len(units) < 2:
        return None
    points = required_answer_points(gold)
    if not points:
        return None
    top = hits[:k]
    for u in units:
        if not any(
            str(h.get("unit_id") or "") == u and _chunk_carries_point(h, u, points) for h in top
        ):
            return False
    return True


def score_item_v3(
    hits: list[dict[str, Any]],
    gold: dict[str, Any],
    *,
    top_k: int | None = None,
    ks: tuple[int, ...] = KS_DEFAULT,
) -> dict[str, Any]:
    """Extended row: base unit-level row + chunk-level relevance vectors.

    ``chunk_hit`` stays as the *legacy* metric (keyword anywhere in top-k, unit
    agnostic); ``evidence_hit`` / ``all_evidence_hit`` are the calibrated
    additions and never replace it.
    """
    row = score_item(hits, gold, top_k=top_k)
    row["relevances"] = [chunk_relevance(h, gold.get("gold_keywords")) for h in hits]
    row["unit_relevances"] = [unit_relevance(h, gold.get("gold_unit_ids")) for h in hits]
    row["chunk_hit"] = {k: chunk_hit_at_k(hits, gold, k) for k in ks}
    row["evidence_hit"] = {k: evidence_hit_at_k(hits, gold, k) for k in ks}
    row["all_evidence_hit"] = {k: all_evidence_hit_at_k(hits, gold, k) for k in ks}
    row["ndcg"] = {k: ndcg_at_k(hits, gold, k) for k in ks}
    row["precision"] = {k: precision_at_k(hits, gold, k) for k in ks}
    return row


def _mean_or_none(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100). Empty -> None.

    Mirrors ``ai.ingest.eval_metrics.percentile`` so retrieve latency numbers
    stay directly comparable with the ingest-eval ones.
    """
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    if p <= 0:
        return xs[0]
    if p >= 100:
        return xs[-1]
    k = max(1, int((p / 100.0) * len(xs) + 0.999999999))  # ceil
    return xs[min(k, len(xs)) - 1]


def latency_stats(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """End-to-end retrieve latency percentiles (ms) from ``rows[].took_ms``.

    Rows whose ``took_ms`` is absent/None (e.g. request errors) are ignored. An
    empty sample (or all-None) yields None for every field, so a latency SLO
    gate can *skip* rather than score a phantom 0 ms as "fast".
    """
    vals = [float(r["took_ms"]) for r in rows if r.get("took_ms") is not None]
    if not vals:
        return {"latency_p50_ms": None, "latency_p95_ms": None, "latency_max_ms": None}
    p50 = _percentile(vals, 50)
    p95 = _percentile(vals, 95)
    return {
        "latency_p50_ms": round(p50, 1) if p50 is not None else None,
        "latency_p95_ms": round(p95, 1) if p95 is not None else None,
        "latency_max_ms": round(max(vals), 1),
    }


def aggregate_v3(
    rows: list[dict[str, Any]],
    ks: tuple[int, ...] = KS_DEFAULT,
) -> dict[str, Any]:
    """Aggregate extended rows with both unit-level and chunk-level metrics."""
    out = aggregate(rows, ks=ks)
    n = len(rows)
    for k in ks:
        out[f"chunk_hit@{k}"] = _mean_or_none(
            [
                float(r["chunk_hit"][k])
                for r in rows
                if r.get("chunk_hit") and r["chunk_hit"].get(k) is not None
            ]
        )
        out[f"evidence_hit@{k}"] = _mean_or_none(
            [
                float(r["evidence_hit"][k])
                for r in rows
                if r.get("evidence_hit") and r["evidence_hit"].get(k) is not None
            ]
        )
        out[f"all_evidence_hit@{k}"] = _mean_or_none(
            [
                float(r["all_evidence_hit"][k])
                for r in rows
                if r.get("all_evidence_hit") and r["all_evidence_hit"].get(k) is not None
            ]
        )
        out[f"ndcg@{k}"] = _mean_or_none(
            [float(r["ndcg"][k]) for r in rows if r.get("ndcg") and r["ndcg"].get(k) is not None]
        )
        out[f"precision@{k}"] = _mean_or_none(
            [
                float(r["precision"][k])
                for r in rows
                if r.get("precision") and r["precision"].get(k) is not None
            ]
        )
    # coverage notes
    with_kw = sum(
        1 for r in rows if r.get("relevances") and None not in (r.get("relevances") or [])
    )
    n_evidence = sum(
        1
        for r in rows
        if r.get("evidence_hit") and any(v is not None for v in (r["evidence_hit"] or {}).values())
    )
    n_multi_unit = sum(
        1
        for r in rows
        if r.get("all_evidence_hit")
        and any(v is not None for v in (r["all_evidence_hit"] or {}).values())
    )
    out["n"] = n
    out["n_chunk_scored"] = with_kw
    out["n_evidence_scored"] = n_evidence
    out["n_multi_unit"] = n_multi_unit
    out["all_evidence_hit_note"] = "样本不足(<6)" if n_multi_unit < 6 else None
    # Latency SLO inputs (P0-4): derived from rows[].took_ms, None-safe.
    out.update(latency_stats(rows))
    return out
