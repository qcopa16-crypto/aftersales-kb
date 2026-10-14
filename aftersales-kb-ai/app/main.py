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
    """Fail-closed INTERNAL_TOKEN gate (F-05).

    ``APP_PROFILE=prod`` (or the explicit ``refuse_on_default_internal_token``
    flag) must never serve traffic with an empty / documented-sample / too-short
    token. The Settings validator already rejects this combination at
    construction time; this startup gate keeps the guarantee explicit for any
    code path that builds or mutates the config later.
    """
    token = (cfg.internal_token or "").strip()
    profile = (getattr(cfg, "app_profile", "") or "").strip().lower()
    if token in _DEFAULT_TOKENS:
        if cfg.refuse_on_default_internal_token:
            raise RuntimeError(
                "refuse_on_default_internal_token=true and INTERNAL_TOKEN is empty/default"
            )
        if profile == "prod":
            raise RuntimeError(
                "APP_PROFILE=prod and INTERNAL_TOKEN is empty/default; "
                "set a unique long random token"
            )
        logger.warning(
            "INTERNAL_TOKEN is empty or the documented default; "
            "set a unique long random token before production"
        )
        return
    if profile == "prod":
        min_len = int(getattr(cfg, "internal_token_min_length", 0) or 0)
        if min_len > 0 and len(token) < min_len:
            raise RuntimeError(
                f"APP_PROFILE=prod requires INTERNAL_TOKEN length >= {min_len}, got {len(token)}"
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


def _enforce_explicit_worker_id(cfg) -> None:
    """R-08 startup gate: require a deployment-level stable index-repair worker id.

    The default identity is now ``host:pid`` (distinct per process, so concurrent
    same-host workers no longer share marker ownership), but it is *not* stable
    across restarts. A deployment that wants to prove a deliberate, stable owner
    (any multi-worker / multi-replica topology) sets
    ``RETRIEVE_REQUIRE_EXPLICIT_WORKER_ID=true`` and must provide one.
    """
    if not getattr(cfg, "retrieve_require_explicit_worker_id", False):
        return
    from core.config import explicit_worker_id

    if not explicit_worker_id():
        raise RuntimeError(
            "retrieve_require_explicit_worker_id=true requires an explicit "
            "RETRIEVE_INDEX_REPAIR_WORKER_ID (or AI_REPLICA_ID); the default host:pid "
            "identity is not a deployment-level stable owner (see audit R-08)"
        )


def _log_profile(cfg) -> None:
    """Surface the resolved deployment posture (M2b) for operators.

    The combination legality is enforced at `Settings` construction by
    `core.config.Settings._validate_config_combos`; this only makes the
    resulting posture visible in the startup log.
    """
    profile = (getattr(cfg, "app_profile", "") or "").strip().lower() or "legacy"
    logger.info(
        "app profile=%s strict_prod=%s require_acl=%s preheat_blocking=%s "
        "preheat_require_ready=%s readiness_on_preheat_failure=%s readiness_timeout_sec=%s",
        profile,
        cfg.retrieve_strict_prod,
        cfg.retrieve_require_acl,
        cfg.retrieve_preheat_blocking,
        cfg.retrieve_preheat_require_ready,
        cfg.retrieve_readiness_on_preheat_failure,
        cfg.retrieve_readiness_timeout_sec,
    )


def _warn_multi_replica(cfg) -> None:
    """Process-local lexical/active map are single-writer; multi-replica needs explicit mode."""
    from ai.retrieve import index_health
    from core.config import explicit_worker_id

    explicit = explicit_worker_id()
    if (cfg.vector_store or "").lower() != "milvus":
        return
    if cfg.retrieve_multi_replica_safe:
        logger.info(
            "RETRIEVE_MULTI_REPLICA_SAFE=true: lexical path disabled; dense-only + ACL "
            "post-filter; index-repair worker id=%s",
            index_health.worker_id(),
        )
        return
    logger.warning(
        "lexical index + milvus active map are process-local single-writer; "
        "deploy as single-writer or accept cross-replica drift. "
        "Set RETRIEVE_MULTI_REPLICA_SAFE=true for multi-replica dense-only mode."
    )
    if not explicit:
        # R-08: the default identity is host:pid (unique per process). It is safe
        # for concurrent same-host workers, but it is not a *stable* owner across
        # restarts — set an explicit id for any multi-worker topology.
        logger.warning(
            "index-repair worker id defaults to %s (host:pid); set "
            "RETRIEVE_INDEX_REPAIR_WORKER_ID (or AI_REPLICA_ID) to a stable per-instance id "
            "for multi-worker/multi-replica deployments",
            index_health.worker_id(),
        )


def _warn_acl_posture(cfg) -> None:
    """Surface an ACL fail-open posture when running against a real vector store.

    `retrieve_require_acl=false` (the default) lets a retrieve call run without an
    acl_filter, i.e. full-corpus search. That is fine for local/demo (memory) but
    is a production hazard, so warn loudly rather than fail silently.
    """
    if (cfg.vector_store or "").lower() == "memory":
        return
    if cfg.retrieve_require_acl:
        return
    logger.warning(
        "RETRIEVE_REQUIRE_ACL=false with vector_store=%s: retrieve is ACL fail-open "
        "(a request without acl_filter searches the full corpus). Set "
        "RETRIEVE_REQUIRE_ACL=true (+ RETRIEVE_STRICT_PROD=true) in production.",
        cfg.vector_store,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_trace_logging()
    cfg = get_settings()
    _warn_if_weak_token(cfg)
    _enforce_strict_prod(cfg)
    _enforce_explicit_worker_id(cfg)
    _log_profile(cfg)
    _warn_multi_replica(cfg)
    _warn_acl_posture(cfg)
    # warm vector store collection
    from app.deps import get_embedder, get_vector_store

    try:
        get_vector_store().ensure_collection()
        logger.info("vector store ready: %s", cfg.vector_store)
    except Exception:
        logger.exception("vector store init failed (will retry on first use)")

    # Cold-start preheat of the process-local lexical/sparse indexes so the first
    # request never triggers an unbounded in-request rebuild (the 504 root cause).
    # Default: background (non-blocking) — /ready gates orchestration instead.
    if cfg.retrieve_preheat_enabled:
        try:
            from ai.retrieve.preheat import get_preheat_snapshot, start_preheat_thread

            thread = start_preheat_thread(cfg, store=get_vector_store(), embedder=get_embedder())
            if cfg.retrieve_preheat_blocking and thread is not None:
                thread.join(timeout=cfg.retrieve_preheat_timeout_sec)
                if thread.is_alive():
                    logger.warning("preheat not ready within %ss", cfg.retrieve_preheat_timeout_sec)
                    if cfg.retrieve_preheat_require_ready:
                        raise RuntimeError(
                            "retrieve_preheat_require_ready=true but preheat is not ready"
                        )
                else:
                    logger.info("preheat ready: %s", get_preheat_snapshot())
        except Exception:
            logger.exception("preheat init failed (will degrade to dense/lexical)")

    # R-07: consume any pending index-repair marker left by a previous process
    # (a marker written just before restart was never recovered before). The
    # singleton retry worker rebuilds and clears it, with backoff on failure.
    try:
        from ai.retrieve import index_health

        if index_health.recover_on_startup(
            store=get_vector_store(), embedder=get_embedder(), cfg=cfg
        ):
            logger.warning("index repair recovery scheduled from pending ledger")
    except Exception:
        logger.exception("index repair startup recovery failed")
    yield


app = FastAPI(title="aftersales-kb-ai", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def internal_token_middleware(request: Request, call_next):
    if request.url.path.startswith(INTERNAL_PREFIX):
        token = request.headers.get("X-Internal-Token") or ""
        expected = get_settings().internal_token
        # constant-time compare to reduce token timing side-channel
        ok = bool(expected) and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
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
