import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.config import get_settings
from app.api import health, internal

logger = logging.getLogger(__name__)

INTERNAL_PREFIX = "/internal"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = get_settings()
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
            )
    return await call_next(request)


app.include_router(health.router)
app.include_router(internal.router)
