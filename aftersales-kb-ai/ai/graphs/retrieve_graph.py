"""LangGraph retrieve graph: hybrid dense+sparse+lexical pipeline nodes.

Production HTTP /internal/retrieve uses the flat pipeline
(`run_retrieve_pipeline` via `run_retrieve`) for bulkhead/QPS/stage metrics
and multi-replica controls. This graph is a debug/composition path; nodes
reuse the same `fuse_hybrid_hits` / `finalize_hits` helpers as the flat
pipeline so three-way fusion, adaptive budget and dedup stay aligned.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from core.config import Settings, get_settings
from ai.graphs.state import RetrieveState
from ai.retrieve.lexical import LexicalIndex, get_lexical_index
from ai.retrieve.pipeline import (
    RetrieveError,
    RetrieveRequestView,
    _search_store_with_resilience,
    build_acl_filter,
    finalize_hits,
    fuse_hybrid_hits,
    index_usable,
    normalize_question,
    pack_response,
    post_filter_acl,
    run_retrieve_pipeline,
)
from ai.retrieve.sparse import SparseIndex, get_sparse_index
from ai.retrieve.store import SearchFilter, VectorStore

logger = logging.getLogger(__name__)


def build_retrieve_graph(
    *,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex | None = None,
    cfg: Settings | None = None,
    sparse: SparseIndex | None = None,
    reranker: Any = None,
    query_rewriter: Any = None,
):
    index = lexical or get_lexical_index()
    sparse_index = sparse or get_sparse_index()
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
        # Extra-recall mode keeps the ORIGINAL question as the main path (the
        # rewritten query becomes an extra RRF ranking in the flat pipeline). This
        # debug graph does not implement that extra path, so it must not replace
        # the question either — otherwise it would diverge from the flat pipeline.
        if query_rewriter is not None and not getattr(
            settings, "retrieve_query_rewrite_extra_path", False
        ):
            from ai.retrieve.query_rewrite import rewrite_question_cached

            question = rewrite_question_cached(
                question,
                rewriter=query_rewriter,
                max_size=int(getattr(settings, "retrieve_query_rewrite_cache_max", 0) or 0),
            )
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
            # R-01: go through the same resilient wrapper as the flat pipeline so
            # this debug/composition node also gets the real RPC deadline
            # (timeout_sec = min(store timeout, remaining budget)) and the bounded
            # store pool — never a raw, unbounded store.search.
            from core.circuit import get_circuits, get_metrics

            budget = float(settings.retrieve_overall_timeout_sec or 0)
            deadline = (time.monotonic() + budget) if budget > 0 else None
            hits, _ = _search_store_with_resilience(
                store=store,
                qvec=qvec,
                top_k=overfetch,
                flt=flt,
                cfg=settings,
                circuits=get_circuits(
                    settings.circuit_failure_threshold, settings.circuit_cooldown_sec
                ),
                metrics=get_metrics(),
                deadline=deadline,
            )
        except RetrieveError:
            # Preserve the specific error code/status (503 circuit-open / 504
            # timeout / 503 overloaded) instead of flattening to a generic 500.
            raise
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
            if not index_usable(index):
                index.sync_from_store(
                    store, limit=int(settings.retrieve_lexical_max_docs or 0) or None
                )
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

    def node_sparse(state: RetrieveState) -> RetrieveState:
        hybrid_on = _resolve_hybrid(state)
        use_sparse = bool(getattr(settings, "retrieve_sparse_enabled", False)) and getattr(
            embedder, "supports_sparse", False
        )
        if not hybrid_on or not use_sparse:
            return {"sparse_hits": [], "stage": "sparse_skip"}  # type: ignore[typeddict-item]
        flt: SearchFilter | None = state.get("search_filter")  # type: ignore[assignment]
        top_k = int(state.get("top_k") or settings.retrieve_top_k_default)
        overfetch = state.get("overfetch") or min(
            top_k * settings.retrieve_overfetch_factor,
            settings.retrieve_overfetch_max,
        )
        overfetch = max(top_k, int(overfetch))
        try:
            sparse_usable = index_usable(sparse_index)
            skip_when_not_ready = bool(
                getattr(settings, "retrieve_sparse_skip_when_not_ready", True)
            )
            sync_in_request = bool(getattr(settings, "retrieve_sparse_sync_in_request", False))
            if not sparse_usable and (sync_in_request or not skip_when_not_ready):
                # Mirror the flat pipeline: opt-in bounded in-request sync only.
                from ai.retrieve.pipeline import _sync_sparse_from_store
                from core.circuit import get_circuits, get_metrics

                sync_timeout = float(getattr(settings, "retrieve_sparse_sync_timeout_sec", 0) or 0)
                if sync_timeout <= 0:
                    sync_timeout = float(settings.retrieve_store_timeout_sec or 0)
                _sync_sparse_from_store(
                    sparse=sparse_index,
                    store=store,
                    embedder=embedder,
                    limit=int(getattr(settings, "retrieve_sparse_max_docs", 0) or 0) or None,
                    timeout_sec=sync_timeout,
                    cfg=settings,
                    circuits=get_circuits(
                        settings.circuit_failure_threshold, settings.circuit_cooldown_sec
                    ),
                    metrics=get_metrics(),
                )
                sparse_usable = index_usable(sparse_index)
            if not sparse_usable:
                return {"sparse_hits": [], "stage": "sparse_not_ready"}  # type: ignore[typeddict-item]
            q_sparse = embedder.encode_sparse([state["question"]])[0]
            hits = (
                sparse_index.search(
                    q_sparse,
                    top_k=overfetch,
                    filter=flt,
                    max_docs=int(getattr(settings, "retrieve_sparse_max_docs", 0) or 0) or None,
                )
                if q_sparse
                else []
            )
        except Exception as exc:
            logger.warning("sparse degraded: %s", exc)
            return {"sparse_hits": [], "stage": "sparse_degraded"}  # type: ignore[typeddict-item]
        return {"sparse_hits": hits, "stage": "sparse"}  # type: ignore[typeddict-item]

    def node_fuse(state: RetrieveState) -> RetrieveState:
        dense_hits = state.get("dense_hits") or []
        lexical_hits = state.get("lexical_hits") or []
        sparse_hits = state.get("sparse_hits") or []
        hybrid_on = _resolve_hybrid(state)
        fused_list = fuse_hybrid_hits(
            dense_hits=dense_hits,
            lexical_hits=lexical_hits,
            sparse_hits=sparse_hits,
            use_lexical=hybrid_on,
            use_sparse=bool(sparse_hits),
            lexical_weight=float(getattr(settings, "retrieve_lexical_rrf_weight", 0.35) or 0.35),
            sparse_weight=float(getattr(settings, "retrieve_sparse_rrf_weight", 0.3) or 0.3),
            rrf_k=int(getattr(settings, "retrieve_hybrid_rrf_k", 60) or 60),
        )
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
        fused_list = finalize_hits(
            fused_list,
            question=state["question"],
            cfg=settings,
            top_k=top_k,
            enable_rerank=bool(enable_rerank),
            embedder=embedder,
            reranker=reranker,
        )
        chunks = pack_response(fused_list, top_k)
        return {"chunks": chunks, "stage": "pack"}  # type: ignore[typeddict-item]

    g = StateGraph(RetrieveState)
    g.add_node("normalize", node_normalize)
    g.add_node("acl", node_acl)
    g.add_node("embed_dense", node_embed_dense)
    g.add_node("lexical", node_lexical)
    g.add_node("sparse", node_sparse)
    g.add_node("fuse", node_fuse)
    g.add_node("post_filter", node_post_filter)
    g.add_node("rerank_pack", node_rerank_pack)
    g.add_edge(START, "normalize")
    g.add_edge("normalize", "acl")
    g.add_edge("acl", "embed_dense")
    g.add_edge("embed_dense", "lexical")
    g.add_edge("lexical", "sparse")
    g.add_edge("sparse", "fuse")
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
    sparse: SparseIndex | None = None,
    reranker: Any = None,
    query_rewriter: Any = None,
) -> dict:
    """API-facing entry: uses the flat pipeline for consistent metrics/degrade handling."""
    settings = cfg or get_settings()
    return run_retrieve_pipeline(
        request=request,
        embedder=embedder,
        store=store,
        lexical=lexical or get_lexical_index(),
        cfg=settings,
        sparse=sparse or get_sparse_index(),
        reranker=reranker,
        query_rewriter=query_rewriter,
    )
