import hmac
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from core.config import get_settings
from app.api import health, internal
from core.trace import get_trace_id, install_trace_logging, new_trace_id, set_trace_id

logger = logging.getLogger(__name__)

INTERNAL_PREFIX = "/internal"
_DEFAULT_TOKENS = frozenset({"", "change-me-long-random"})
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _warn_if_weak_token(cfg) -> None:
    token = (cfg.internal_token or "").strip()
    if token in _DEFAULT_TOKENS:
        logger.warning(
            "INTERNAL_TOKEN is empty or the documented default; "
            "set a unique long random token before production"
        )
        if cfg.refuse_on_default_internal_token:
            raise RuntimeError(
                "refuse_on_default_internal_token=true and INTERNAL_TOKEN is empty/default"
            )


def _enforce_strict_prod(cfg) -> None:
    """Fail-closed production gate: retrieve_strict_prod requires RETRIEVE_REQUIRE_ACL=true."""
    if not cfg.retrieve_strict_prod:
        return
    if not cfg.retrieve_require_acl:
        raise RuntimeError(
            "retrieve_strict_prod=true requires retrieve_require_acl=true "
            "(set RETRIEVE_REQUIRE_ACL=true in production)"
        )


def _warn_multi_replica(cfg) -> None:
    """Process-local lexical/active map are single-writer; multi-replica needs explicit mode."""
    if (cfg.vector_store or "").lower() != "milvus":
        return
    if cfg.retrieve_multi_replica_safe:
        logger.info(
            "RETRIEVE_MULTI_REPLICA_SAFE=true: lexical path disabled; dense-only + ACL post-filter"
        )
        return
    logger.warning(
        "lexical index + milvus active map are process-local single-writer; "
        "deploy as single-writer or accept cross-replica drift. "
        "Set RETRIEVE_MULTI_REPLICA_SAFE=true for multi-replica dense-only mode."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_trace_logging()
    cfg = get_settings()
    _warn_if_weak_token(cfg)
    _enforce_strict_prod(cfg)
    _warn_multi_replica(cfg)
    # warm vector store collection
    from app.deps import get_vector_store

    try:
        get_vector_store().ensure_collection()
        logger.info("vector store ready: %s", cfg.vector_store)
    except Exception:
        logger.exception("vector store init failed (will retry on first use)")
    yield


app = FastAPI(title="aftersales-kb-ai", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def internal_token_middleware(request: Request, call_next):
    if request.url.path.startswith(INTERNAL_PREFIX):
        token = request.headers.get("X-Internal-Token") or ""
        expected = get_settings().internal_token
        # constant-time compare to reduce token timing side-channel
        ok = bool(expected) and hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        )
        if not ok:
            return Response(
                content='{"detail":"invalid internal token"}',
                status_code=401,
                media_type="application/json",
                headers={"X-Trace-Id": get_trace_id()},
            )
    return await call_next(request)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """Outermost: inject trace_id for the whole request; echo on response."""
    incoming = (request.headers.get("X-Trace-Id") or "").strip()
    # Reject log-injection / oversized values; only echo a safe id.
    if incoming and _TRACE_ID_RE.match(incoming):
        tid = incoming
    else:
        tid = new_trace_id()
    set_trace_id(tid)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error path=%s", request.url.path)
        raise
    response.headers["X-Trace-Id"] = tid
    return response


app.include_router(health.router)
app.include_router(internal.router)
