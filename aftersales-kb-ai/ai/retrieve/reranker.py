"""Optional cross-encoder reranker for the retrieve pipeline.

Keeps the heavy dependency (sentence-transformers) lazy so the default path
never imports it. Falls back to the existing embed_rerank cosine rerank on any
failure. Enabled only when `retrieve_reranker_enabled=true` and a model is set.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class Reranker(ABC):
    @abstractmethod
    def rerank(self, question: str, texts: list[str]) -> list[float]: ...


class CrossEncoderReranker(Reranker):
    """sentence-transformers CrossEncoder; relevance scores for (q, d) pairs."""

    def __init__(self, model: str, device: str | None = None) -> None:
        if not model:
            raise RuntimeError("retrieve_reranker_model is empty")
        from sentence_transformers import CrossEncoder

        kwargs: dict[str, Any] = {}
        if device:
            kwargs["device"] = device
        self._model = CrossEncoder(model, **kwargs)

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        pairs = [(question, t) for t in texts]
        scores = self._model.predict(pairs)
        return [float(s) for s in scores]


def create_reranker(cfg: Any) -> Reranker | None:
    if not getattr(cfg, "retrieve_reranker_enabled", False):
        return None
    model = (getattr(cfg, "retrieve_reranker_model", "") or "").strip()
    if not model:
        return None
    try:
        return CrossEncoderReranker(model, device=getattr(cfg, "retrieve_reranker_device", None))
    except Exception:
        logger.warning("cross-encoder reranker failed to load; disabled", exc_info=True)
        return None


_reranker: Reranker | None = None
# Negative cache: distinguishes "never attempted" from "attempted and got None
# (disabled / failed to load)". Without it, a None result is indistinguishable
# from the uninitialised state, so every request retries the (possibly
# expensive, always-failing) model load. False until the first attempt.
_reranker_loaded = False


def get_reranker(cfg: Any | None = None) -> Reranker | None:
    global _reranker, _reranker_loaded
    if not _reranker_loaded:
        _reranker = create_reranker(cfg)
        _reranker_loaded = True
    return _reranker


def reset_reranker_for_tests() -> None:
    global _reranker, _reranker_loaded
    _reranker = None
    _reranker_loaded = False


def cross_encoder_rerank(
    hits: list[Any],
    *,
    question: str,
    reranker: Reranker,
    top_n: int = 20,
    weight: float = 0.4,
) -> list[Any]:
    """Blend cross-encoder relevance into score_fused for the first top_n hits.

    DEPRECATED for the default path: this mixes the CE score into `score_fused`
    and re-sorts *all* chunks, so it also scrambles the intra-unit chunk order
    (measured net-negative on chunk-level metrics, see
    docs/retrieval-gate-decision.md §12). Kept for the `retrieve_reranker_unit_only
    = false` escape hatch and for unit tests.
    """
    if not hits or weight <= 0 or reranker is None:
        return hits
    n = max(1, int(top_n))
    head, tail = hits[:n], hits[n:]
    texts = [(h.content or "")[:512] for h in head]
    scores = reranker.rerank(question, texts)
    for h, s in zip(head, scores):
        h.score_fused = h.score_fused + float(weight) * max(0.0, float(s))
    head.sort(key=lambda h: h.score_fused, reverse=True)
    return head + tail


def cross_encoder_unit_rerank(
    hits: list[Any],
    *,
    question: str,
    reranker: Reranker,
    top_n: int = 0,
    weight: float = 0.4,
    agg_mode: str = "best",
    topk: int = 3,
    coverage_weight: float = 0.0,
    coverage_k: int = 3,
    regroup: bool = False,
    blend: str = "score",
    rank_k: int = 60,
) -> list[Any]:
    """Use cross-encoder scores for **unit selection only**.

    Motivation (docs/retrieval-gate-decision.md §12): the cross-encoder is a
    strong *semantic* signal for "which unit answers this question" but a poor
    one for "which chunk inside that unit comes first" — mixing it into
    `score_fused` and re-sorting globally lifted unit_recall@1 (+3.5pt) at the
    cost of chunk_hit@5 (-1.2pt) / ndcg@5 (-1.8pt).

    Here the CE score only affects the *unit* ordering:
      - each unit's CE representative = max CE over its chunks (capped to
        `top_n` chunks, 0 = score every candidate);
      - unit score = aggregate_unit_score(fused) + weight * max(0, CE_rep);
      - units are sorted by that score, but **each unit's internal chunk order
        is left exactly as it arrived** (the `keyword_aware_unit_order` /
        embed-rerank order produced upstream). No `keyword_aware_unit_order`
        is re-applied after the cross-encoder (that was measured harmful, §12.2).

    ``blend="rank"`` fuses the two unit *rankings* RRF-style instead of raw
    scores — a unit that the fused signal already ranks #1 cannot be pushed far
    down by a single weak CE judgement (the raw-score blend let CE demote the
    fused #1 unit by ~7 places on v3-e15/v3-v03).
    """
    if not hits or weight <= 0 or reranker is None:
        return hits
    if regroup:
        from ai.retrieve.pipeline import keyword_aware_unit_order

        hits = keyword_aware_unit_order(hits, question)
    # Group by unit, preserving the incoming intra-unit chunk order.
    unit_order: list[str] = []
    groups: dict[str, list[Any]] = {}
    for h in hits:
        uid = str(getattr(h, "unit_id", "") or "")
        if uid not in groups:
            unit_order.append(uid)
            groups[uid] = []
        groups[uid].append(h)

    cap = len(hits) if int(top_n) <= 0 else min(len(hits), max(int(top_n), 1))
    head = hits[:cap]
    texts = [(h.content or "")[:512] for h in head]
    scores = reranker.rerank(question, texts)
    ce_by_id: dict[int, float] = {}
    for h, s in zip(head, scores):
        ce_by_id[id(h)] = float(s)

    from ai.retrieve.pipeline import aggregate_unit_score

    base_scores: dict[str, float] = {}
    ce_reps: dict[str, float] = {}
    for uid, hs in groups.items():
        base_scores[uid] = aggregate_unit_score(
            [float(h.score_fused) for h in hs],
            mode=agg_mode,
            topk=topk,
            coverage_weight=coverage_weight,
            coverage_k=coverage_k,
        )
        ce_reps[uid] = max((ce_by_id.get(id(h), 0.0) for h in hs), default=0.0)

    if blend == "rank":
        kk = max(1, int(rank_k))
        fused_rank = {
            u: i
            for i, u in enumerate(
                sorted(unit_order, key=lambda u: base_scores[u], reverse=True), start=1
            )
        }
        ce_rank = {
            u: i
            for i, u in enumerate(
                sorted(unit_order, key=lambda u: max(0.0, ce_reps[u]), reverse=True), start=1
            )
        }
        unit_scores = {
            u: 1.0 / (kk + fused_rank[u]) + float(weight) * (1.0 / (kk + ce_rank[u]))
            for u in unit_order
        }
    else:
        unit_scores = {u: base_scores[u] + float(weight) * max(0.0, ce_reps[u]) for u in unit_order}

    ordered = sorted(
        unit_order,
        key=lambda u: (unit_scores.get(u, 0.0), base_scores.get(u, 0.0)),
        reverse=True,
    )
    out: list[Any] = []
    for u in ordered:
        out.extend(groups[u])
    return out
