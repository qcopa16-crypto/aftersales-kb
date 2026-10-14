"""Durable health tracking + repair for the process-local lexical/sparse indexes (R-10).

The lexical and sparse indexes are auxiliary recall paths that are updated
*incrementally* right after a Milvus publish. When that incremental update fails,
the dense vector version is already live but the auxiliary index silently lags:
previously the ingest endpoint logged a warning and still returned success, so the
divergence was invisible and never repaired (the replica could miss new documents
or keep serving old ones until restart).

This module records a **durable** "repair needed" marker per ``(index, unit_id)``
and exposes a pending count for health/metrics, so a failed sync is observable.
A full rebuild from the Milvus active chunks (``rebuild_pending_indexes``) clears
the markers; ``schedule_rebuild`` runs it single-flight in the background.

R-03 (multi-process / fail-visible):
  * Every marker is tagged with a **worker identity** (env
    ``RETRIEVE_INDEX_REPAIR_WORKER_ID`` / ``AI_REPLICA_ID``, else ``host:pid``).
    A rebuild only clears the markers **this worker owns** — a peer that rebuilds
    its own process-local index can no longer erase another worker's still-stale
    marker (the old "pending=0 but the local index is still stale" bug).
  * Persistence uses a cross-process lock + read-merge-write so concurrent
    workers never clobber each other's markers (last-writer-wins on the whole
    file is gone).
  * A persistence failure is **not** swallowed: ``mark_repair_needed`` returns
    ``False``, the ledger is flagged degraded (surfaced on health), and a metric
    is emitted — the caller must not claim the repair task is durably queued.

R-08 (process/instance-bound identity + orphan recovery):
  * The default identity is **``host:pid``**, not the bare hostname. Multiple
    Python workers on one host (``uvicorn --workers N``) previously shared the
    hostname identity and could clear each other's still-stale markers; binding
    the default to the PID makes concurrent same-host workers distinct. An
    explicit ``RETRIEVE_INDEX_REPAIR_WORKER_ID`` / ``AI_REPLICA_ID`` (a stable,
    deployment-level id) still wins.
  * Because the default now changes on every restart, startup recovery **adopts
    orphaned same-host markers** — a ``host:pid`` marker whose process is no
    longer alive (or a legacy bare-hostname marker from a pre-R-08 process) — and
    re-tags it to this worker so a restarted process still repairs the drift it
    inherited.
  * Markers written by an **explicit** identity (``RETRIEVE_INDEX_REPAIR_WORKER_ID``
    / ``AI_REPLICA_ID``) carry ``explicit: true`` and are **never** adopted: an
    explicit id is a stable, deployment-level owner, so a restarted process with
    the same id re-claims it by exact match and never by shape. A live peer's
    marker is likewise never adopted, so cross-instance clearing stays impossible.

KNOWN LIMITATIONS (documented, not bugs):
  * PID liveness is best-effort and fail-safe: on an *inconclusive* probe
    (e.g. ``ERROR_ACCESS_DENIED`` on Windows) the owner is treated as **alive**
    (never adopted), so a stale marker may linger rather than a live peer be
    stolen from.
  * Legacy markers written **before** this change carry no ``explicit`` flag, so
    they still fall back to shape-based detection: an old marker whose owner
    happens to look like ``host:pid`` on this host and whose pid is dead will be
    adopted. The window is bounded by the upgrade; new markers are always tagged.
  * Adopting a *legacy bare-hostname* marker during a rolling upgrade could race
    a still-running old-version worker; the window is bounded by the upgrade.
  * For strict multi-replica consistency configure a distinct
    ``RETRIEVE_INDEX_REPAIR_WORKER_ID`` per replica (enforced at startup when
    ``RETRIEVE_MULTI_REPLICA_SAFE=true``), or move the ledger to a store with
    atomic CAS (MySQL/Redis).

Layer rule: lives under ``ai/*`` and must NOT import ``app/*``. The store/embedder
are passed in by the caller (the ingest endpoint), never resolved here.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

# Reuse the package's portable cross-process lock (O_CREAT|O_EXCL, fail-closed).
# Same ``ai/*`` layer, no app/* import; store.py does not import this module, so
# there is no import cycle.
from ai.retrieve.store import _cross_process_lock

logger = logging.getLogger(__name__)

_DEFAULT_PATH = ".data/index_repair.json"

_LOCK = threading.Lock()
_PATH: str = _DEFAULT_PATH
_PENDING: dict[str, dict] = {}
_LOADED = False

# R-03: identity of the process-local lexical/sparse indexes that own the markers
# written here. Stable across restarts on the same host so startup recovery can
# re-claim its own markers; overridable per replica via env.
_LOCK_TIMEOUT = 5.0


def _hostname() -> str:
    try:
        return (socket.gethostname() or "unknown").strip() or "unknown"
    except Exception:  # pragma: no cover - hostname always available in practice
        return "unknown"


def _default_worker_id() -> str:
    """R-08: process-bound default identity ``host:pid``.

    The bare hostname is identical for every Python worker on one host
    (``uvicorn --workers N``), so those workers shared marker ownership and one
    could clear another's still-stale repair marker. Binding the default to the
    PID makes concurrent same-host workers distinct. An explicit
    ``RETRIEVE_INDEX_REPAIR_WORKER_ID`` / ``AI_REPLICA_ID`` still wins.
    """
    return f"{_hostname()}:{os.getpid()}"


def _explicit_worker_env() -> str:
    """Operator-provided (explicit) worker identity from the environment, or ""."""
    return (
        os.environ.get("RETRIEVE_INDEX_REPAIR_WORKER_ID") or os.environ.get("AI_REPLICA_ID") or ""
    ).strip()


def _resolve_worker_id() -> str:
    raw = _explicit_worker_env()
    if raw:
        return raw[:64]
    return _default_worker_id()[:64]


_WORKER_HOST: str = _hostname()
# R-08: True when the identity is operator-provided (stable, deployment-level).
# Explicit-id markers are never shape-adopted on startup — only exact-matched.
_WORKER_EXPLICIT: bool = bool(_explicit_worker_env())
_WORKER_ID: str = _resolve_worker_id()
# R-03: set when the last persist failed; surfaced on health so an operator can
# tell "no markers" apart from "markers could not be written".
_PERSIST_DEGRADED = False


# R-10: the pending *gauge* is emitted as a delta from the ledger size, not by
# blindly incrementing on every mark. Re-marking an existing (index, unit) must
# not drift the metric, and a fresh process that loads a non-empty ledger must
# report the loaded size (metrics are process-local, so they restart at 0).
_METRIC_LOCK = threading.Lock()
_METRIC_REPORTED = 0

# R-07: singleton bounded rebuild worker with exponential-backoff retry. A
# one-shot daemon thread (the previous design) cannot recover a marker written
# just before a restart, and a single failed attempt left the marker forever.
_REBUILD_LOCK = threading.Lock()
_REBUILD_THREAD: threading.Thread | None = None
_REBUILD_WAKE = threading.Event()
_REBUILD_STATE_LOCK = threading.Lock()
# Latest scheduled task (store/embedder/cfg). Re-scheduling replaces it; the
# worker always drains the newest one. None = nothing to do.
_REBUILD_TASK: dict[str, Any] | None = None
_REBUILD_ATTEMPTS = 0
_LAST_ATTEMPT_TS = 0.0
_LAST_ERROR = ""
_REBUILD_STOP = False


def _emit_pending_gauge(target: int) -> None:
    """Emit ``retrieve.index_repair_pending`` as the delta to ``target`` (ledger size).

    Keeps the process-local metric aligned with the durable ledger regardless of
    how many times a key is re-marked or cleared.
    """
    global _METRIC_REPORTED
    target = int(target)
    with _METRIC_LOCK:
        delta = target - _METRIC_REPORTED
        if delta == 0:
            return
        _METRIC_REPORTED = target
    try:
        from core.circuit import get_metrics

        get_metrics().inc("retrieve.index_repair_pending", delta)
    except Exception:  # pragma: no cover - metrics must never break ingest
        pass


def configure(path: str | None, worker_id: str | None = None) -> None:
    """Point the durable repair ledger at ``path`` (None restores the default).

    ``worker_id`` (R-03) overrides the marker-owner identity; used by tests to
    simulate distinct replicas. ``None`` keeps the current (env/host) identity.
    An overridden id is treated as **explicit** (R-08): its markers are never
    shape-adopted on startup.
    """
    global _PATH, _LOADED, _PENDING, _WORKER_ID, _WORKER_EXPLICIT, _PERSIST_DEGRADED
    with _LOCK:
        _PATH = str(path or _DEFAULT_PATH)
        if worker_id is not None:
            _WORKER_ID = str(worker_id)[:64] or _WORKER_ID
            _WORKER_EXPLICIT = True
        _LOADED = False
        _PENDING = {}
        _PERSIST_DEGRADED = False


def reset_for_tests() -> None:
    """Drop in-memory + rebuild state so each test starts clean."""
    global _PENDING, _LOADED, _REBUILD_THREAD, _PATH, _METRIC_REPORTED, _WORKER_ID
    global _WORKER_EXPLICIT, _PERSIST_DEGRADED
    global _REBUILD_TASK, _REBUILD_ATTEMPTS, _LAST_ATTEMPT_TS, _LAST_ERROR, _REBUILD_STOP
    with _LOCK:
        _PENDING = {}
        _LOADED = False
        _PATH = _DEFAULT_PATH
        _WORKER_ID = _resolve_worker_id()
        _WORKER_EXPLICIT = bool(_explicit_worker_env())
        _PERSIST_DEGRADED = False
    _stop_rebuild_worker()
    with _REBUILD_STATE_LOCK:
        _REBUILD_TASK = None
        _REBUILD_ATTEMPTS = 0
        _LAST_ATTEMPT_TS = 0.0
        _LAST_ERROR = ""
        _REBUILD_STOP = False
    with _METRIC_LOCK:
        _METRIC_REPORTED = 0


def _key(index: str, unit_id: str) -> str:
    return f"{index}::{unit_id}"


def worker_id() -> str:
    """The marker-owner identity of this process (R-03/R-08 observability)."""
    return _WORKER_ID


def worker_host() -> str:
    """The host component of the default identity (R-08 orphan recovery)."""
    return _WORKER_HOST


# Win32 error codes used by ``_pid_alive`` (Windows branch only).
_ERROR_INVALID_PARAMETER = 87  # "no such process" for OpenProcess
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win_open_process(pid: int) -> tuple[bool, int]:
    """Windows: ``(opened, last_error)`` for OpenProcess (R-08).

    Isolated so the policy in ``_pid_alive`` is unit-testable cross-platform
    (a test can inject a fake result). ``use_last_error=True`` makes
    ``ctypes.get_last_error()`` reliably reflect the OpenProcess call — plain
    ``ctypes.windll`` does not preserve it.
    """
    import ctypes  # pragma: no cover - Windows only

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True, 0
    return False, int(ctypes.get_last_error())


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for a same-host default identity's PID (R-08).

    **Fail-safe by design**: only a *definitive* "no such process" reports
    ``False``; every inconclusive outcome reports ``True`` (alive) so a marker
    that might still belong to a live peer is never adopted. On Windows we must
    NOT use ``os.kill(pid, 0)`` (it terminates the target), so we open the
    process with the query-limited-information access right and inspect
    ``GetLastError``: ``ERROR_ACCESS_DENIED`` (e.g. a System/other-user process)
    is inconclusive → alive; only ``ERROR_INVALID_PARAMETER`` means gone.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        try:
            opened, err = _win_open_process(pid)
            if opened:
                return True
            # Only "no such process" is definitive; anything else (ACCESS_DENIED,
            # ERROR_PARTIAL_COPY, unknown) is inconclusive -> treat as alive.
            return err != _ERROR_INVALID_PARAMETER
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return True  # inconclusive -> treat as alive (safe)
    return True


def _is_orphaned(entry: dict) -> bool:
    """True when ``entry``'s owner is a same-host identity whose process is gone (R-08).

    Eligible: a default ``host:pid`` marker from *this* host whose PID is no
    longer alive, or a legacy bare-hostname marker from a pre-R-08 process. Both
    refer to a process-local index that died with its process.

    **Explicit ids are never adopted**: a marker written by an operator-provided
    identity carries ``explicit: true`` and is re-claimed only by an exact owner
    match. Legacy markers (no ``explicit`` flag, written before this change) fall
    back to shape-based detection — the bounded upgrade window documented above.
    Other hosts are never adopted.
    """
    owner = (entry.get("worker") or "").strip()
    if not owner or owner == _WORKER_ID:
        return False
    if entry.get("explicit") is True:
        return False
    # Legacy bare-hostname identity (pre-R-08 default): adopt it on this host.
    if owner == _WORKER_HOST:
        return True
    host, sep, pid_s = owner.rpartition(":")
    if not sep or host != _WORKER_HOST or not pid_s.isdigit():
        return False
    pid = int(pid_s)
    if pid == os.getpid():
        return False
    return not _pid_alive(pid)


def ledger_path() -> str:
    """Resolved durable-ledger path (public accessor for health/ops reporting)."""
    return _resolved_path()


def _resolved_path() -> str:
    """Explicit path if configured, else the settings default (lazy import)."""
    if _PATH != _DEFAULT_PATH:
        return _PATH
    try:
        from core.config import get_settings

        return str(
            getattr(get_settings(), "retrieve_index_repair_path", _DEFAULT_PATH) or _DEFAULT_PATH
        )
    except Exception:  # pragma: no cover - config always importable in practice
        return _DEFAULT_PATH


def _read_ledger_file(path: Path) -> dict[str, dict]:
    """Parse the ledger file; return {} for missing/corrupt content."""
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        logger.warning("index repair ledger read failed path=%s", path, exc_info=True)
    return {}


def _load_locked() -> None:
    global _LOADED, _PENDING
    if _LOADED:
        return
    _LOADED = True
    _PENDING = _read_ledger_file(Path(_resolved_path()))


def _persist_locked() -> bool:
    """Durably write the ledger; return False (and flag degraded) on failure.

    R-03: cross-process read-merge-write under the shared fail-closed lock —
    only THIS worker's markers are replaced, so a concurrent peer's markers are
    never clobbered. A write/replace failure is reported (not swallowed): the
    caller must not treat the marker as durably recorded.
    """
    global _PERSIST_DEGRADED
    path_value = _resolved_path()
    if not path_value:
        return True
    path = Path(path_value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _cross_process_lock(path, timeout=_LOCK_TIMEOUT, fail_closed=True):
            fresh = _read_ledger_file(path)
            # Drop our previous markers, then re-apply the live ones. Peers' markers
            # (different worker id) are left untouched.
            for key in [k for k, v in fresh.items() if v.get("worker") == _WORKER_ID]:
                fresh.pop(key, None)
            for key, val in _PENDING.items():
                if val.get("worker") == _WORKER_ID:
                    fresh[key] = val
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        # Reconcile the in-memory view with what actually landed (own + peers).
        _PENDING.clear()
        _PENDING.update(fresh)
        _PERSIST_DEGRADED = False
        return True
    except Exception as exc:
        _PERSIST_DEGRADED = True
        logger.error("index repair ledger persist failed path=%s: %s", path, exc)
        _emit_repair_metric("retrieve.index_repair_persist_failed")
        return False


def persist_degraded() -> bool:
    """True when the last ledger persist failed (durability not guaranteed)."""
    return bool(_PERSIST_DEGRADED)


def _local_pending_locked() -> int:
    """Count of markers owned by THIS worker (the ones it can actually repair)."""
    return sum(1 for v in _PENDING.values() if v.get("worker") == _WORKER_ID)


def local_pending_count() -> int:
    """Pending markers owned by this worker (drives its own rebuild loop)."""
    with _LOCK:
        _load_locked()
        n = _local_pending_locked()
    return n


def mark_repair_needed(index: str, unit_id: str, reason: str = "") -> bool:
    """Record (durably) that ``index`` is stale for ``unit_id`` and needs a rebuild.

    Returns True when the marker was persisted durably; False when persistence
    failed (R-03) — the marker is still held in memory (so this process can
    repair it) but the caller must NOT claim it is durably queued. No-op inputs
    return True (nothing to record is not a failure).
    """
    if not index or not unit_id:
        return True
    with _LOCK:
        _load_locked()
        _PENDING[_key(index, unit_id)] = {
            "index": str(index),
            "unit_id": str(unit_id),
            "reason": str(reason)[:300],
            "worker": _WORKER_ID,
            # R-08: operator-provided identity -> never shape-adopted on startup.
            "explicit": _WORKER_EXPLICIT,
            "ts": time.time(),
        }
        persisted = _persist_locked()
        n = len(_PENDING)
    try:
        from core.circuit import get_metrics

        # Event counter (one per call) + gauge reconciled to the ledger size.
        get_metrics().inc("retrieve.index_repair_marked")
    except Exception:  # pragma: no cover - metrics must never break ingest
        pass
    _emit_pending_gauge(n)
    logger.error(
        "index repair needed index=%s unit_id=%s reason=%s pending=%s durable=%s worker=%s",
        index,
        unit_id,
        reason,
        n,
        persisted,
        _WORKER_ID,
    )
    return persisted


def clear_repair(index: str, unit_id: str) -> None:
    """Clear a single unit's repair marker (incremental update succeeded).

    R-03: only clears a marker THIS worker owns — a peer's marker is left intact.
    """
    if not index or not unit_id:
        return
    with _LOCK:
        _load_locked()
        key = _key(index, unit_id)
        existing = _PENDING.get(key)
        if existing is not None and existing.get("worker") == _WORKER_ID:
            _PENDING.pop(key, None)
            _persist_locked()
            cleared = True
        else:
            cleared = False
        n = len(_PENDING)
    if not cleared:
        return
    try:
        from core.circuit import get_metrics

        get_metrics().inc("retrieve.index_repair_cleared")
    except Exception:  # pragma: no cover
        pass
    _emit_pending_gauge(n)


def clear_index(index: str) -> int:
    """Clear every marker for ``index`` that THIS worker owns (after a rebuild).

    R-03: a full rebuild only repairs this process's local index, so it must not
    clear another worker's markers for the same index.
    """
    with _LOCK:
        _load_locked()
        keys = [
            k
            for k, v in _PENDING.items()
            if v.get("index") == index and v.get("worker") == _WORKER_ID
        ]
        for k in keys:
            _PENDING.pop(k, None)
        if keys:
            _persist_locked()
        n = len(_PENDING)
    if keys:
        try:
            from core.circuit import get_metrics

            get_metrics().inc("retrieve.index_repair_cleared", len(keys))
        except Exception:  # pragma: no cover
            pass
    _emit_pending_gauge(n)
    return len(keys)


def pending_snapshot(*, local_only: bool = False) -> list[dict]:
    """Snapshot of pending markers (all workers, or just this worker's)."""
    with _LOCK:
        _load_locked()
        snapshot = [
            dict(v) for v in _PENDING.values() if not local_only or v.get("worker") == _WORKER_ID
        ]
        n = len(_PENDING)
    _emit_pending_gauge(n)
    return snapshot


def pending_count() -> int:
    """Total pending markers across all workers (health/metrics view)."""
    with _LOCK:
        _load_locked()
        n = len(_PENDING)
    _emit_pending_gauge(n)
    return n


def rebuild_pending_indexes(*, store: Any, embedder: Any, cfg: Any = None) -> int:
    """Full rebuild of any index with pending repairs; clears markers on success.

    Returns the number of indexes rebuilt. A rebuild re-derives the whole index
    from the active Milvus chunks, so a successful pass repairs *all* pending
    units of that index (incremental drift cannot survive it).

    R-03: only this worker's markers drive (and are cleared by) the rebuild —
    another worker's drift can only be repaired by that worker.
    """
    pending = pending_snapshot(local_only=True)
    if not pending:
        return 0
    indexes = {p.get("index") for p in pending}
    limit = int(getattr(cfg, "retrieve_lexical_sync_limit", 0) or 0) or None
    repaired = 0

    if "lexical" in indexes:
        try:
            from ai.retrieve.lexical import get_lexical_index

            get_lexical_index().sync_from_store(store, limit=limit)
            clear_index("lexical")
            repaired += 1
            logger.info("lexical index rebuilt from store (repair)")
        except Exception:
            logger.exception("lexical index rebuild failed")

    if "sparse" in indexes and getattr(embedder, "supports_sparse", False):
        try:
            from ai.retrieve.sparse import get_sparse_index

            get_sparse_index().sync_from_store(
                store,
                embedder,
                limit=int(getattr(cfg, "retrieve_sparse_max_docs", 0) or 0) or None,
            )
            clear_index("sparse")
            repaired += 1
            logger.info("sparse index rebuilt from store (repair)")
        except Exception:
            logger.exception("sparse index rebuild failed")

    return repaired


def _emit_repair_metric(name: str, n: int = 1) -> None:
    try:
        from core.circuit import get_metrics

        get_metrics().inc(name, n)
    except Exception:  # pragma: no cover - metrics must never break repair
        pass


def _backoff_seconds(cfg: Any, attempt: int) -> float:
    base = float(getattr(cfg, "retrieve_index_repair_retry_base_sec", 5.0) or 0.0)
    cap = float(getattr(cfg, "retrieve_index_repair_retry_max_sec", 300.0) or 0.0)
    if base <= 0:
        return 0.0
    delay = base * (2 ** max(0, attempt - 1))
    if cap > 0:
        delay = min(delay, cap)
    return delay


def _rebuild_once(task: dict[str, Any]) -> str:
    """Run one full rebuild pass; return "" on success, else the last error."""
    try:
        rebuild_pending_indexes(
            store=task.get("store"), embedder=task.get("embedder"), cfg=task.get("cfg")
        )
        return ""
    except Exception as exc:  # pragma: no cover - rebuild_pending_indexes is defensive
        logger.exception("index repair rebuild raised")
        return str(exc)


def _rebuild_loop() -> None:
    """Single-instance worker: drain the latest repair task with backoff retry.

    The worker is a long-lived daemon that blocks on ``_REBUILD_WAKE`` when idle
    (it does NOT exit between tasks — a cheap, always-available scheduler). The
    inner loop drains the latest scheduled task: on success (ledger empty) it
    clears the task and returns to the idle wait; on failure it retries with
    exponential backoff (bounded by config), recording ``last_attempt`` /
    ``last_error`` for the health probe.
    """
    global _REBUILD_ATTEMPTS, _LAST_ATTEMPT_TS, _LAST_ERROR, _REBUILD_TASK
    while True:
        _REBUILD_WAKE.wait()
        _REBUILD_WAKE.clear()
        if _REBUILD_STOP:
            return
        while True:
            with _REBUILD_STATE_LOCK:
                task = dict(_REBUILD_TASK) if _REBUILD_TASK else None
                stop = _REBUILD_STOP
            if stop:
                return
            if task is None:
                break
            # R-03: only this worker's markers drive the loop — a peer's pending
            # marker must not make this worker retry forever.
            if local_pending_count() == 0:
                with _REBUILD_STATE_LOCK:
                    _REBUILD_TASK = None
                    _REBUILD_ATTEMPTS = 0
                break
            with _REBUILD_STATE_LOCK:
                _LAST_ATTEMPT_TS = time.time()
            err = _rebuild_once(task)
            if local_pending_count() == 0:
                with _REBUILD_STATE_LOCK:
                    _REBUILD_TASK = None
                    _REBUILD_ATTEMPTS = 0
                    _LAST_ERROR = ""
                _emit_repair_metric("retrieve.index_repair_recovered")
                logger.info("index repair rebuild succeeded; markers cleared")
                break
            cfg = task.get("cfg")
            with _REBUILD_STATE_LOCK:
                _REBUILD_ATTEMPTS += 1
                attempts = _REBUILD_ATTEMPTS
                _LAST_ERROR = (err or "rebuild left pending markers")[:300]
            _emit_repair_metric("retrieve.index_repair_retry")
            logger.error(
                "index repair rebuild attempt %s left %s pending marker(s): %s",
                attempts,
                local_pending_count(),
                _LAST_ERROR,
            )
            max_attempts = int(getattr(cfg, "retrieve_index_repair_retry_max_attempts", 0) or 0)
            if max_attempts and attempts >= max_attempts:
                # Stop until a new schedule / restart; the marker stays visible.
                with _REBUILD_STATE_LOCK:
                    _REBUILD_TASK = None
                break
            delay = _backoff_seconds(cfg, attempts)
            if delay > 0:
                # Interruptible backoff: a stop signal (or a fresh schedule) wakes
                # the worker early. A spurious extra pass is harmless — the task is
                # re-read and an empty task just breaks the loop.
                _REBUILD_WAKE.wait(timeout=delay)


def _ensure_rebuild_worker() -> None:
    global _REBUILD_THREAD
    with _REBUILD_LOCK:
        if _REBUILD_THREAD is not None and _REBUILD_THREAD.is_alive():
            return
        _REBUILD_THREAD = threading.Thread(target=_rebuild_loop, name="index-repair", daemon=True)
        _REBUILD_THREAD.start()


def _stop_rebuild_worker() -> None:
    """Signal + join the worker (used by reset/tests). Never raises."""
    global _REBUILD_THREAD, _REBUILD_STOP
    with _REBUILD_STATE_LOCK:
        _REBUILD_STOP = True
    _REBUILD_WAKE.set()
    t = _REBUILD_THREAD
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=2.0)
    with _REBUILD_LOCK:
        _REBUILD_THREAD = None
    with _REBUILD_STATE_LOCK:
        _REBUILD_STOP = False
    _REBUILD_WAKE.clear()


def schedule_rebuild(*, store: Any, embedder: Any, cfg: Any = None) -> None:
    """Schedule a single-flight, retrying background full rebuild (R-07).

    Replaces the previous one-shot daemon thread: the work is queued on a
    process-wide singleton worker that retries failures with exponential
    backoff and clears the markers on success. Never raises.
    """
    global _REBUILD_TASK
    with _REBUILD_STATE_LOCK:
        _REBUILD_TASK = {"store": store, "embedder": embedder, "cfg": cfg}
    _REBUILD_WAKE.set()
    _ensure_rebuild_worker()


def _adopt_orphans_locked() -> int:
    """Re-tag orphaned same-host markers to this worker (R-08). Returns the count.

    Must be called with ``_LOCK`` held and the ledger loaded. Because the default
    identity is ``host:pid`` (and therefore changes on every restart), a marker
    written by a previous incarnation of this process on this host would
    otherwise be orphaned forever. A marker owned by a *live* peer or by an
    explicit id is left untouched, so cross-instance clearing stays impossible.
    """
    adopted = 0
    for key, val in list(_PENDING.items()):
        if val.get("worker") == _WORKER_ID:
            continue
        if _is_orphaned(val):
            val = dict(val)
            val["worker"] = _WORKER_ID
            # Adopted markers now belong to this process; tag the ownership mode.
            val["explicit"] = _WORKER_EXPLICIT
            _PENDING[key] = val
            adopted += 1
    if adopted:
        _persist_locked()
    return adopted


def recover_on_startup(*, store: Any, embedder: Any, cfg: Any = None) -> bool:
    """Read the pending ledger at startup and schedule a rebuild if non-empty (R-07).

    Returns True when a recovery rebuild was scheduled. This closes the gap where
    a marker written just before a process restart was never consumed.

    R-03: only *this worker's* markers trigger recovery — this process can only
    repair its own process-local indexes, so a peer's marker must not start an
    endless rebuild loop here.

    R-08: before counting, adopt any orphaned same-host markers (a dead
    ``host:pid`` owner, or a legacy bare-hostname owner) so a restarted process
    still repairs the drift it inherited under the process-bound default identity.
    """
    if not bool(getattr(cfg, "retrieve_index_repair_retry_enabled", True)):
        return False
    try:
        with _LOCK:
            _load_locked()
            adopted = _adopt_orphans_locked()
        pending = local_pending_count()
    except Exception:  # pragma: no cover - ledger read is defensive
        return False
    if adopted:
        logger.warning(
            "index repair ledger: adopted %s orphaned same-host marker(s) as worker=%s",
            adopted,
            _WORKER_ID,
        )
    if pending <= 0:
        return False
    logger.warning(
        "index repair ledger has %s pending marker(s) for worker=%s at startup; "
        "scheduling recovery",
        pending,
        _WORKER_ID,
    )
    _emit_repair_metric("retrieve.index_repair_startup_recovered", pending)
    schedule_rebuild(store=store, embedder=embedder, cfg=cfg)
    return True


def rebuild_status() -> dict:
    """Observability snapshot for the repair worker (R-07/R-03 health/metrics)."""
    with _REBUILD_STATE_LOCK:
        task_pending = _REBUILD_TASK is not None
        last_attempt = _LAST_ATTEMPT_TS
        last_error = _LAST_ERROR
        attempts = _REBUILD_ATTEMPTS
    with _REBUILD_LOCK:
        running = _REBUILD_THREAD is not None and _REBUILD_THREAD.is_alive()
    return {
        "scheduled": task_pending,
        "running": bool(running),
        "attempts": int(attempts),
        "last_attempt": last_attempt,
        "last_error": last_error,
        # R-03/R-08: durability/ownership observability (per-instance).
        "degraded": bool(_PERSIST_DEGRADED),
        "worker": _WORKER_ID,
        "host": _WORKER_HOST,
        "local_pending": local_pending_count(),
    }
