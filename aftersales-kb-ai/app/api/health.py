import hmac
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from core.config import get_settings
from core.circuit import get_circuits, get_metrics
from core.limits import get_limits

router = APIRouter(tags=["health"])


def _index_repair_status() -> dict:
    """R-10: surface the lexical/sparse index repair ledger on the health probe.

    A failed incremental index sync after a Milvus publish is recorded durably
    (see ``ai.retrieve.index_health``); a non-zero ``pending`` means the auxiliary
    recall indexes lag the active vector version for those units. Reported as a
    plain ops signal — it never flips the liveness/readiness verdict.
    """
    from ai.retrieve import index_health

    pending = index_health.pending_count()
    path = Path(index_health.ledger_path())
    parent_writable = False
    try:
        parent_writable = path.parent.is_dir() and os.access(str(path.parent), os.W_OK)
    except OSError:
        parent_writable = False
    return {
        "pending": pending,
        # R-03: markers owned by this worker (the ones it can actually repair),
        # the owner identity, and whether the last persist failed (durability).
        "local_pending": index_health.local_pending_count(),
        "worker": index_health.worker_id(),
        "degraded": index_health.persist_degraded(),
        "path": str(path),
        "exists": path.is_file(),
        "parent_writable": bool(parent_writable),
        # R-07: the singleton retry worker's last attempt/error (observability).
        "rebuild": index_health.rebuild_status(),
    }


@router.get("/health")
def health() -> dict:
    """Liveness + capability + light ops snapshot. Always public for probes."""
    cfg = get_settings()
    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
    return {
        "status": "ok",
        "embed_provider": cfg.embed_provider,
        "embed_dim": cfg.embed_dim,
        "vector_store": cfg.vector_store,
        "mineru_enabled": cfg.mineru_enabled,
        "vlm_enabled": cfg.vlm_enabled,
        "limits": get_limits().snapshot(),
        "circuits": circuits.snapshot(),
        "metrics": get_metrics().snapshot(),
        # R-10: index-version lag (auxiliary lexical/sparse indexes behind the
        # active vector version) — observability only, never affects `status`.
        "index_repair": _index_repair_status(),
    }


@router.get("/ready")
def ready() -> JSONResponse:
    """Readiness probe (public). 503 until cold-start preheat has built indexes.

    Liveness stays on /health (always 200). Orchestrators/load balancers MUST gate
    traffic with this probe so a cold replica does not serve requests before its
    process-local lexical/sparse indexes are ready.

    R-09: the readiness verdict is driven *only* by preheat. When preheat fails or
    exceeds ``RETRIEVE_READINESS_TIMEOUT_SEC``, ``RETRIEVE_READINESS_ON_PREHEAT_FAILURE``
    decides whether the instance is kept out of rotation (``fail``, 503) or served
    degraded (``degrade``, 200 + status="degraded").

    The ``index_repair`` / ``index_repair_pending`` fields are **observational
    only** — a lagging auxiliary lexical/sparse index must never take a replica out
    of rotation (dense still serves), so they do not affect the status code.
    """
    from ai.retrieve import index_health
    from ai.retrieve.preheat import get_preheat_snapshot, readiness_verdict

    cfg = get_settings()
    ok, status = readiness_verdict(cfg)
    pending = index_health.pending_count()
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": status,
            "preheat": get_preheat_snapshot(),
            "preheat_policy": {
                "on_failure": cfg.retrieve_readiness_on_preheat_failure,
                "timeout_sec": cfg.retrieve_readiness_timeout_sec,
            },
            "sparse_enabled": cfg.retrieve_sparse_enabled,
            "multi_replica_safe": cfg.retrieve_multi_replica_safe,
            # R-08: per-instance identity + this instance's own repair backlog.
            "worker": index_health.worker_id(),
            "index_repair": {
                "pending": pending,
                "local_pending": index_health.local_pending_count(),
                "degraded": index_health.persist_degraded(),
            },
            # Kept for backward compatibility with existing probes/dashboards.
            "index_repair_pending": pending,
        },
    )


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    """Prometheus text exposition. Token-gated unless metrics_public=true."""
    cfg = get_settings()
    if not cfg.metrics_public:
        token = request.headers.get("X-Internal-Token") or ""
        expected = cfg.internal_token or ""
        ok = bool(expected) and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        if not ok:
            raise HTTPException(status_code=401, detail="invalid internal token")
    return get_metrics().to_prometheus_text()
