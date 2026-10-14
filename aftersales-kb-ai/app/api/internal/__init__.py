"""Internal API routers composed under prefix /internal."""

from fastapi import APIRouter

from app.api.internal.ingest import router as ingest_router
from app.api.internal.retrieve import router as retrieve_router
from app.api.internal.generate import router as generate_router
from app.api.internal.vlm import router as vlm_router
from app.api.internal.session import router as session_router
from app.api.internal.review import router as review_router

router = APIRouter(prefix="/internal", tags=["internal"])
router.include_router(ingest_router)
router.include_router(retrieve_router)
router.include_router(generate_router)
router.include_router(vlm_router)
router.include_router(session_router)
router.include_router(review_router)

# Compat re-exports for tests that patch app.api.internal.*
from app.api.internal import ingest as _ingest  # noqa: E402
from app.api.internal._common import (  # noqa: E402
    _get_parse_sem,
    _parse_spool_dir,
)

run_parse = _ingest.run_parse  # used by timeout monkeypatch tests

__all__ = ["router", "run_parse", "_get_parse_sem", "_parse_spool_dir"]
