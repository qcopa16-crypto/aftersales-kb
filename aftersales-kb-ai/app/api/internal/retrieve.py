"""Retrieve /internal routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.config import Settings, get_settings
from app.deps import get_embedder, get_vector_store
from schemas.retrieve import RetrieveRequest, RetrieveResponse, RetrievedChunk
from ai.embed.embedder import Embedder
from ai.retrieve.lexical import get_lexical_index
from ai.retrieve.pipeline import RetrieveError, RetrieveRequestView, run_retrieve_pipeline
from ai.retrieve.store import VectorStore
from core.errors import detail_body

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-retrieve"])

@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    cfg: Settings = Depends(get_settings),
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

