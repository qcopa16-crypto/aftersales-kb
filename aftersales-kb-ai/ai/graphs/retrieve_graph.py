"""LangGraph retrieve graph: hybrid dense+lexical pipeline nodes.

Production HTTP /internal/retrieve uses the flat pipeline
(`run_retrieve_pipeline` via `run_retrieve`) for bulkhead/QPS/stage metrics
and multi-replica controls. This graph is a debug/composition path; nodes
must stay aligned on ACL fail-closed + unit_ids cap + multi_replica dense-only.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from core.config import Settings, get_settings
from ai.graphs.state import RetrieveState
from ai.retrieve.lexical import LexicalIndex, get_lexical_index
from ai.retrieve.pipeline import (
    RetrieveError,
    RetrieveRequestView,
    build_acl_filter,
    fuse_rrf,
    normalize_question,
    pack_response,
    post_filter_acl,
    apply_max_per_unit,
    rule_rerank,
    run_retrieve_pipeline,
)
from ai.retrieve.store import SearchFilter, VectorStore

logger = logging.getLogger(__name__)


def build_retrieve_graph(
    *,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex | None = None,
    cfg: Settings | None = None,
):
    index = lexical or get_lexical_index()
    settings = cfg or get_settings()

    def _resolve_hybrid(state: RetrieveState) -> bool:
        # Align with flat pipeline: multi_replica_safe forces dense-only.
        if settings.retrieve_multi_replica_safe:
            return False
        hybrid = state.get("hybrid")
        if hybrid is None:
            return bool(settings.retrieve_hybrid_enabled)
        return bool(hybrid)

    def node_normalize(state: RetrieveState) -> RetrieveState:
        question = normalize_question(state["question"], settings.retrieve_max_question_chars)
        return {
            "question": question,
            "hybrid": _resolve_hybrid(state),
            "stage": "normalize",
        }  # type: ignore[typeddict-item]

    def node_acl(state: RetrieveState) -> RetrieveState:
        # AclFilter object already built by API; rebuild SearchFilter for store.
        raw = state.get("acl") or {}
        from schemas.ingest import AclFilter

        acl = AclFilter(**raw) if isinstance(raw, dict) else raw
        flt, applied = build_acl_filter(
            acl,
            state.get("unit_ids"),
            bool(settings.retrieve_require_acl),
            max_unit_ids=int(settings.retrieve_max_unit_ids or 0),
        )
        return {
            "search_filter": flt,
            "acl_applied": applied,
            "acl": acl if hasattr(acl, "model_dump") else raw,
            "stage": "acl",
        }  # type: ignore[typeddict-item]

    def node_embed_dense(state: RetrieveState) -> RetrieveState:
        flt: SearchFilter | None = state.get("search_filter")  # type: ignore[assignment]
        top_k = int(state.get("top_k") or settings.retrieve_top_k_default)
        overfetch = state.get("overfetch") or min(
            top_k * settings.retrieve_overfetch_factor,
            settings.retrieve_overfetch_max,
        )
        overfetch = max(top_k, int(overfetch))
        try:
            qvec = embedder.encode([state["question"]])[0]
            hits = store.search(qvec, top_k=overfetch, filter=flt)
        except Exception as exc:
            raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc
        return {"dense_hits": hits, "stage": "dense"}  # type: ignore[typeddict-item]

    def node_lexical(state: RetrieveState) -> RetrieveState:
        hybrid_on = _resolve_hybrid(state)
        if not hybrid_on:
            return {"lexical_hits": [], "hybrid": False, "stage": "lexical_skip"}  # type: ignore[typeddict-item]
        flt: SearchFilter | None = state.get("search_filter")  # type: ignore[assignment]
        top_k = int(state.get("top_k") or settings.retrieve_top_k_default)
        overfetch = state.get("overfetch") or min(
            top_k * settings.retrieve_overfetch_factor,
            settings.retrieve_overfetch_max,
        )
        overfetch = max(top_k, int(overfetch))
        try:
            if index.doc_count() == 0:
                index.sync_from_store(store, limit=int(settings.retrieve_lexical_max_docs or 0) or None)
            hits = index.search(
                state["question"],
                top_k=overfetch,
                filter=flt,
                max_docs=int(settings.retrieve_lexical_max_docs or 0) or None,
            )
        except Exception as exc:
            if settings.retrieve_hybrid_degrade_to_dense:
                logger.warning("lexical degraded: %s", exc)
                return {
                    "lexical_hits": [],
                    "degraded_to_dense": True,
                    "stage": "lexical_degraded",
                }  # type: ignore[typeddict-item]
            raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc
        return {"lexical_hits": hits, "stage": "lexical"}  # type: ignore[typeddict-item]

    def node_fuse(state: RetrieveState) -> RetrieveState:
        from ai.retrieve.pipeline import FuseHit, _hit_key

        # Reuse packing logic via fused helper
        dense_hits = state.get("dense_hits") or []
        lexical_hits = state.get("lexical_hits") or []
        hybrid_on = _resolve_hybrid(state)

        by_key: dict[str, FuseHit] = {}

        def upsert(row: dict, path: str, score: float) -> str:
            cid = str(row.get("id") or row.get("chunk_id") or "")
            unit = str(row.get("unit_id") or "")
            ver = str(row.get("version_id") or "")
            key = _hit_key(unit, cid, ver)
            hit = by_key.get(key)
            if hit is None:
                hit = FuseHit(
                    chunk_id=cid,
                    unit_id=unit,
                    chunk_index=int(row.get("chunk_index") or 0),
                    content=str(row.get("content") or ""),
                    version_id=ver,
                    heading_path=list(row.get("heading_path") or []),
                    acl_department=str(row.get("acl_department") or ""),
                    acl_classification=str(row.get("acl_classification") or ""),
                )
                by_key[key] = hit
            if path == "dense":
                hit.score_dense = float(score)
                if "dense" not in hit.retrieval_paths:
                    hit.retrieval_paths.append("dense")
            else:
                hit.score_lexical = float(score)
                if "lexical" not in hit.retrieval_paths:
                    hit.retrieval_paths.append("lexical")
            return key

        dense_ranking = [upsert(r, "dense", float(r.get("score") or 0.0)) for r in dense_hits]
        lexical_ranking = []
        if hybrid_on:
            lexical_ranking = [upsert(r, "lexical", float(r.get("score") or 0.0)) for r in lexical_hits]
        rankings = [dense_ranking] + ([lexical_ranking] if hybrid_on else [])
        weights = [1.0]
        if len(rankings) > 1:
            weights.append(float(getattr(settings, "retrieve_lexical_rrf_weight", 0.35) or 0.35))
        scores = fuse_rrf(rankings, k=settings.retrieve_hybrid_rrf_k, weights=weights)
        fused_list = []
        for key, hit in by_key.items():
            hit.score_fused = scores.get(key, 0.0)
            fused_list.append(hit)
        fused_list.sort(key=lambda h: h.score_fused, reverse=True)
        return {"fused_hits": fused_list, "hybrid": bool(hybrid_on), "stage": "fuse"}  # type: ignore[typeddict-item]

    def node_post_filter(state: RetrieveState) -> RetrieveState:
        flt: SearchFilter | None = state.get("search_filter")  # type: ignore[assignment]
        fused_list = list(state.get("fused_hits") or [])
        min_score = None
        acl = state.get("acl")
        if acl is not None and hasattr(acl, "min_score"):
            min_score = acl.min_score
        elif isinstance(acl, dict):
            min_score = acl.get("min_score")
        from core.circuit import get_metrics

        kept, dropped = post_filter_acl(
            fused_list,
            flt,
            min_score,
            acl_fields_required=bool(settings.retrieve_acl_fields_required),
            metrics=get_metrics(),
        )
        return {"fused_hits": kept, "filtered_out": dropped, "stage": "post_filter"}  # type: ignore[typeddict-item]

    def node_rerank_pack(state: RetrieveState) -> RetrieveState:
        fused_list = list(state.get("fused_hits") or [])
        top_k = int(state.get("top_k") or settings.retrieve_top_k_default)
        enable_rerank = state.get("enable_rerank")
        if enable_rerank is None:
            enable_rerank = settings.retrieve_enable_rerank
        fused_list = apply_max_per_unit(fused_list, settings.retrieve_max_per_unit)
        if enable_rerank:
            fused_list = rule_rerank(fused_list, state["question"])
            fused_list = apply_max_per_unit(fused_list, settings.retrieve_max_per_unit)
        chunks = pack_response(fused_list, top_k)
        return {"chunks": chunks, "stage": "pack"}  # type: ignore[typeddict-item]

    g = StateGraph(RetrieveState)
    g.add_node("normalize", node_normalize)
    g.add_node("acl", node_acl)
    g.add_node("embed_dense", node_embed_dense)
    g.add_node("lexical", node_lexical)
    g.add_node("fuse", node_fuse)
    g.add_node("post_filter", node_post_filter)
    g.add_node("rerank_pack", node_rerank_pack)
    g.add_edge(START, "normalize")
    g.add_edge("normalize", "acl")
    g.add_edge("acl", "embed_dense")
    g.add_edge("embed_dense", "lexical")
    g.add_edge("lexical", "fuse")
    g.add_edge("fuse", "post_filter")
    g.add_edge("post_filter", "rerank_pack")
    g.add_edge("rerank_pack", END)
    return g.compile()


def run_retrieve(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex | None = None,
    cfg: Settings | None = None,
) -> dict:
    """API-facing entry: uses the flat pipeline for consistent metrics/degrade handling."""
    return run_retrieve_pipeline(
        request=request,
        embedder=embedder,
        store=store,
        lexical=lexical or get_lexical_index(),
        cfg=cfg or get_settings(),
    )
