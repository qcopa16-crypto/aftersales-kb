from fastapi import APIRouter

from app.config import get_settings
from app.utils.circuit import get_circuits, get_metrics
from app.utils.limits import get_limits

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness + capability + light ops snapshot."""
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
