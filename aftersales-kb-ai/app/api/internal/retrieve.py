"""Retrieve /internal routes."""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from core.config import Settings, get_settings
from app.deps import get_embedder, get_llm, get_vector_store
from schemas.retrieve import RetrieveRequest, RetrieveResponse, RetrievedChunk
from ai.embed.embedder import Embedder
from ai.generate.llm import LlmClient
from ai.retrieve.lexical import get_lexical_index
from ai.retrieve.sparse import get_sparse_index
from ai.retrieve.reranker import get_reranker
from ai.retrieve.query_rewrite import get_query_rewriter
from ai.retrieve.pipeline import RetrieveError, RetrieveRequestView, run_retrieve_pipeline
from ai.retrieve.store import VectorStore
from ai.session.history import filter_turns
from ai.session.rewrite import rewrite_with_history
from ai.session.store import get_session_store
from core.errors import detail_body

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-retrieve"])


def _build_session_rewriter(
    body: RetrieveRequest, cfg: Settings, llm: LlmClient
) -> Callable[[str], str] | None:
    """Build the session-history rewrite callable (指代消解), or None when disabled.

    Order matters (plan §2.3): read Mongo -> ACL filter -> only then may history be
    used for the rewrite. A read failure yields an EMPTY history — never "skip the
    filter". ``acl_filter.unit_ids`` (issued by Java) is the only allow-set source.
    """
    if not (cfg.retrieve_session_rewrite_enabled and body.enable_session_rewrite):
        return None
    if not body.session_id or not body.owner_id:
        return None
    acl_unit_ids = (body.acl_filter.unit_ids if body.acl_filter else None) or []
    try:
        turns = get_session_store().get_turns(
            owner_id=body.owner_id, session_id=body.session_id, limit=20
        )
    except Exception:  # pragma: no cover - store already degrades internally
        logger.warning("session history read failed; skipping rewrite", exc_info=True)
        turns = []
    safe_turns = filter_turns(turns, acl_unit_ids)
    if not safe_turns:
        return None
    timeout = float(getattr(cfg, "retrieve_session_rewrite_timeout_sec", 3.0) or 3.0)
    return lambda q: rewrite_with_history(q, safe_turns, llm, timeout=timeout)


@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    cfg: Settings = Depends(get_settings),
    llm: LlmClient = Depends(get_llm),
) -> RetrieveResponse:
    """Hybrid retrieve (dense + lexical + RRF). Python executes acl_filter only."""
    request = RetrieveRequestView(
        question=body.question,
        top_k=body.top_k or cfg.retrieve_top_k_default,
        acl_filter=body.acl_filter,
        unit_ids=body.unit_ids,
        overfetch=body.overfetch,
        enable_rerank=body.enable_rerank,
        enable_hybrid=body.enable_hybrid,
    )
    try:
        result = run_retrieve_pipeline(
            request=request,
            embedder=embedder,
            store=store,
            lexical=get_lexical_index(),
            cfg=cfg,
            sparse=get_sparse_index(),
            reranker=get_reranker(cfg),
            query_rewriter=get_query_rewriter(cfg, llm),
            session_rewriter=_build_session_rewriter(body, cfg, llm),
        )
    except RetrieveError as exc:
        logger.error("retrieve failed code=%s status=%s", exc.error_code, exc.status_code)
        raise HTTPException(
            status_code=exc.status_code,
            detail=detail_body(
                exc.error_code,
                str(exc),
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    except ValueError as exc:
        # Pydantic-style filter validation already handled; keep fail-closed mapping.
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "RETRIEVE_INVALID_FILTER",
                str(exc),
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    except Exception as exc:
        logger.exception("retrieve failed")
        raise HTTPException(
            status_code=500,
            detail=detail_body(
                "RETRIEVE_FAILED",
                str(exc),
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    chunks = [
        RetrievedChunk(
            unit_id=c["unit_id"],
            chunk_id=c["chunk_id"],
            score=c["score"],
            content=c["content"],
            version_id=c.get("version_id"),
            heading_path=c.get("heading_path") or [],
            score_dense=c.get("score_dense"),
            score_lexical=c.get("score_lexical"),
            score_sparse=c.get("score_sparse"),
            score_fused=c.get("score_fused"),
            retrieval_paths=c.get("retrieval_paths") or [],
        )
        for c in result["chunks"]
    ]
    return RetrieveResponse(
        chunks=chunks,
        took_ms=result.get("took_ms"),
        acl_applied=bool(result.get("acl_applied")),
        hybrid=bool(result.get("hybrid", True)),
        degraded_to_dense=bool(result.get("degraded_to_dense")),
    )
