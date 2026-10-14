"""Cold-start preheat for the process-local lexical / sparse indexes.

The lexical and sparse indexes are built lazily on first use. A request that
arrives before they are built used to trigger an *unbounded* in-request rebuild
(up to ~2万 chunks re-encoded by BGE-M3), which blew past the overall retrieve
budget and produced a guaranteed 504 on the first request after a cold start.

This module moves that build out of the request path: it builds the indexes once
at startup (background thread by default), records readiness, and exposes a
snapshot for the readiness probe. Request-time behaviour (see
`pipeline._run_retrieve_pipeline_inner`) degrades gracefully when the index is
not ready instead of rebuilding inline.

Layer rule: this module lives under `ai/*` and must NOT import `app/*`; the
caller (lifespan) resolves the store/embedder and passes them in.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from core.circuit import get_metrics
from core.config import Settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STATUS: dict[str, dict] = {}
# None = preheat not started yet (readiness unknown); [] = started, nothing to do.
_EXPECTED: list[str] | None = None
_THREAD: threading.Thread | None = None
# R-09: wall-clock start of the preheat attempt, so /ready can bound how long an
# incomplete preheat keeps the instance unready (readiness timeout policy).
_STARTED_AT: float | None = None

_READY_STATES = {"ready", "skipped", "skipped_multi_replica"}


def _set(name: str, **fields: Any) -> None:
    with _LOCK:
        _STATUS.setdefault(name, {}).update(fields)


def get_preheat_snapshot() -> dict:
    """Read-only copy of the per-index preheat status (for /ready and metrics)."""
    with _LOCK:
        return {name: dict(st) for name, st in _STATUS.items()}


def reset_preheat_for_tests() -> None:
    """Clear process-global preheat state so each test starts fresh."""
    global _THREAD, _EXPECTED, _STARTED_AT
    with _LOCK:
        _STATUS.clear()
        _EXPECTED = None
        _THREAD = None
        _STARTED_AT = None


def preheat_started_at() -> float | None:
    """Wall-clock start of the preheat attempt (None until it starts). R-09."""
    return _STARTED_AT


def _enabled_indices(cfg: Settings, embedder: Any) -> list[str]:
    names: list[str] = []
    if getattr(cfg, "retrieve_preheat_lexical", True):
        names.append("lexical")
    if (
        getattr(cfg, "retrieve_preheat_sparse", True)
        and getattr(cfg, "retrieve_sparse_enabled", False)
        and getattr(embedder, "supports_sparse", False)
    ):
        names.append("sparse")
    return names


def preheat_once(cfg: Settings, *, store: Any, embedder: Any) -> dict:
    """Build the lexical/sparse indexes once. Idempotent and safe to re-run."""
    global _EXPECTED, _STARTED_AT
    metrics = get_metrics()

    with _LOCK:
        if _STARTED_AT is None:
            _STARTED_AT = time.time()

    if getattr(cfg, "retrieve_multi_replica_safe", False):
        # Multi-replica deploys are dense-only (process-local indexes are unsafe);
        # never rebuild them per replica.
        with _LOCK:
            _EXPECTED = []
        for name in ("lexical", "sparse"):
            _set(name, state="skipped_multi_replica")
        return get_preheat_snapshot()

    with _LOCK:
        _EXPECTED = _enabled_indices(cfg, embedder)

    from ai.retrieve.lexical import get_lexical_index
    from ai.retrieve.sparse import get_sparse_index

    if getattr(cfg, "retrieve_preheat_lexical", True):
        lex = get_lexical_index()
        if lex.is_ready():
            _set("lexical", state="ready")
        else:
            _set("lexical", state="running", started_at=time.time())
            t0 = time.monotonic()
            try:
                count = lex.sync_from_store(
                    store,
                    limit=int(getattr(cfg, "retrieve_lexical_sync_limit", 0) or 0) or None,
                )
                ms = (time.monotonic() - t0) * 1000
                _set(
                    "lexical",
                    state="ready",
                    docs=count,
                    ms=round(ms, 1),
                    finished_at=time.time(),
                )
                metrics.inc("retrieve.preheat_ready")
                metrics.observe_ms("retrieve.preheat_ms", ms)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("lexical preheat failed: %s", exc)
                _set("lexical", state="failed", error=str(exc))

    if "sparse" in (_EXPECTED or []):
        sp = get_sparse_index()
        if sp.is_ready():
            _set("sparse", state="ready")
        else:
            _set("sparse", state="running", started_at=time.time())
            t0 = time.monotonic()
            try:
                count = sp.sync_from_store(
                    store,
                    embedder,
                    limit=int(getattr(cfg, "retrieve_sparse_max_docs", 0) or 0) or None,
                )
                ms = (time.monotonic() - t0) * 1000
                _set(
                    "sparse",
                    state="ready",
                    docs=count,
                    ms=round(ms, 1),
                    finished_at=time.time(),
                )
                metrics.inc("retrieve.preheat_ready")
                metrics.observe_ms("retrieve.preheat_ms", ms)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("sparse preheat failed: %s", exc)
                _set("sparse", state="failed", error=str(exc))

    return get_preheat_snapshot()


def _readiness_policy(cfg: Settings) -> str:
    """R-09: normalised preheat-failure readiness policy ("fail" | "degrade")."""
    policy = (
        (getattr(cfg, "retrieve_readiness_on_preheat_failure", "fail") or "fail").strip().lower()
    )
    return policy if policy in {"fail", "degrade"} else "fail"


def readiness_verdict(cfg: Settings) -> tuple[bool, str]:
    """(ready, status) for /ready; status in {"ready", "degraded", "not_ready"}.

    R-09: readiness is gated on preheat completion. When preheat does not finish —
    an index ends ``failed`` (e.g. the vector store was unreachable) or is still
    ``running`` past ``retrieve_readiness_timeout_sec`` — the configured policy
    decides whether the instance is routable:

      * ``fail``    -> not ready (503): keep the instance OUT of rotation until a
                       later successful preheat (refuse traffic over serving a
                       half-built instance);
      * ``degrade`` -> ready (200, status="degraded"): dense (and any built
                       lexical) still serve, so the router may use the instance.

    A partially-ready instance is never reported plain ``ready``.
    """
    if not getattr(cfg, "retrieve_preheat_enabled", True):
        return True, "ready"
    if getattr(cfg, "retrieve_multi_replica_safe", False):
        return True, "ready"
    with _LOCK:
        expected = list(_EXPECTED) if _EXPECTED is not None else None
        snap = {name: dict(st) for name, st in _STATUS.items()}
        started_at = _STARTED_AT
    if expected is None:
        # Preheat enabled but not started yet: report not ready (fail-safe).
        return False, "not_ready"
    policy = _readiness_policy(cfg)
    timeout = float(getattr(cfg, "retrieve_readiness_timeout_sec", 0.0) or 0.0)
    timed_out = bool(
        timeout > 0 and started_at is not None and (time.time() - started_at) > timeout
    )
    degraded = False
    for name in expected:
        state = (snap.get(name) or {}).get("state")
        if state in _READY_STATES:
            continue
        if state == "failed" or timed_out:
            if policy == "degrade":
                degraded = True
                continue
            return False, "not_ready"
        # Still running / not yet reported: keep the instance unready.
        return False, "not_ready"
    return True, ("degraded" if degraded else "ready")


def is_preheat_ready(cfg: Settings) -> bool:
    """True when /ready should report this instance as routable (see verdict)."""
    return readiness_verdict(cfg)[0]


def start_preheat_thread(cfg: Settings, *, store: Any, embedder: Any) -> threading.Thread | None:
    """Start the background preheat thread (idempotent per process)."""
    global _THREAD, _EXPECTED, _STARTED_AT
    if not getattr(cfg, "retrieve_preheat_enabled", True):
        return None
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD
        # Publish the expected index set before the thread runs so /ready can
        # distinguish "not started" from "ready".
        _EXPECTED = _enabled_indices(cfg, embedder)
        if _STARTED_AT is None:
            _STARTED_AT = time.time()

    def _safe() -> None:
        try:
            preheat_once(cfg, store=store, embedder=embedder)
        except Exception:  # pragma: no cover - defensive
            logger.exception("preheat failed")

    t = threading.Thread(target=_safe, name="retrieve-preheat", daemon=True)
    t.start()
    with _LOCK:
        _THREAD = t
    return t
