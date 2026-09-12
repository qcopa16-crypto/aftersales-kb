import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.utils.circuit import get_circuits, get_metrics
from app.utils.limits import get_limits

router = APIRouter(tags=["health"])


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
    }


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    """Prometheus text exposition. Token-gated unless metrics_public=true."""
    cfg = get_settings()
    if not cfg.metrics_public:
        token = request.headers.get("X-Internal-Token") or ""
        expected = cfg.internal_token or ""
        ok = bool(expected) and hmac.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        )
        if not ok:
            raise HTTPException(status_code=401, detail="invalid internal token")
    return get_metrics().to_prometheus_text()
