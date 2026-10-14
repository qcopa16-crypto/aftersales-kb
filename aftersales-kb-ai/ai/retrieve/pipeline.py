"""Enterprise hybrid retrieve pipeline: dense + lexical + RRF + ACL post-filter."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable

from core.config import Settings, embed_model_id
from schemas.ingest import AclFilter
from ai.retrieve.lexical import LexicalIndex
from ai.retrieve.store import SearchFilter, VectorStore, row_passes_filter
from core.circuit import get_circuits, get_metrics
from core.errors import sanitize_text
from core.retry import RetryPolicy, retry_call
from core.trace import get_trace_id

logger = logging.getLogger(__name__)

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# Shared pool for store.search soft timeouts (process-local).
_store_pool: ThreadPoolExecutor | None = None
_store_pool_lock = threading.Lock()
_store_pool_size = 0

# Dedicated cold-start rebuild pool (lexical/sparse sync_from_store), isolated
# from the search pool so a stuck rebuild never occupies live-search threads
# (F-26): a fetch that blocks on one downstream must not starve the other.
_sync_pool: ThreadPoolExecutor | None = None
_sync_pool_lock = threading.Lock()
_sync_pool_size = 0

# Bounded submission queues in front of the pools (F-26/F-37): pool size + queue
# cap. Saturation fails fast instead of piling up behind a stuck downstream.
_store_submit_sem: threading.BoundedSemaphore | None = None
_store_submit_limit = 0
_store_submit_lock = threading.Lock()
_sync_submit_sem: threading.BoundedSemaphore | None = None
_sync_submit_limit = 0
_sync_submit_lock = threading.Lock()
# Bounded queue in front of the optional lexical/sparse branch pool (R-09).
_branch_submit_sem: threading.BoundedSemaphore | None = None
_branch_submit_limit = 0
_branch_submit_lock = threading.Lock()

# Retrieve bulkhead + QPS (process-local).
_bulkhead_sem: threading.BoundedSemaphore | None = None
_bulkhead_limit = 0
_bulkhead_lock = threading.Lock()

_qps_lock = threading.Lock()
_qps_tokens = 0.0
_qps_last = 0.0
_qps_rate = 0.0

_multi_replica_logged = False
_multi_replica_log_lock = threading.Lock()

# Query-embedding LRU (process-local; enabled by retrieve_embed_cache_max > 0).
_embed_cache: OrderedDict[tuple[str, int, str], list[float]] = OrderedDict()
_embed_cache_lock = threading.Lock()


class RetrieveError(Exception):
    def __init__(
        self, message: str, error_code: str = "RETRIEVE_FAILED", status_code: int = 500
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def _question_hash_prefix(question: str) -> str:
    """12-hex SHA-256 prefix for correlation without storing PII text."""
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]


def get_store_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
    """Lazy process-local shared pool. Never rebuilt per request."""
    global _store_pool, _store_pool_size
    with _store_pool_lock:
        size = max(1, int(max_workers or 4))
        if _store_pool is None:
            _store_pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="retrieve-store")
            _store_pool_size = size
        return _store_pool


def reset_store_pool_for_tests() -> None:
    global _store_pool, _store_pool_size
    with _store_pool_lock:
        if _store_pool is not None:
            _store_pool.shutdown(wait=False, cancel_futures=True)
        _store_pool = None
        _store_pool_size = 0


def get_sync_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
    """Lazy process-local pool for cold-start lexical/sparse rebuilds (F-26).

    Deliberately separate from ``_store_pool``: a rebuild that blocks on a slow
    ``list_active_chunks`` must not occupy the threads live searches need.
    """
    global _sync_pool, _sync_pool_size
    with _sync_pool_lock:
        size = max(1, int(max_workers or 2))
        if _sync_pool is None:
            _sync_pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="retrieve-sync")
            _sync_pool_size = size
        return _sync_pool


def reset_sync_pool_for_tests() -> None:
    global _sync_pool, _sync_pool_size
    with _sync_pool_lock:
        if _sync_pool is not None:
            _sync_pool.shutdown(wait=False, cancel_futures=True)
        _sync_pool = None
        _sync_pool_size = 0


def _get_submit_semaphore(
    limit: int,
) -> tuple[threading.BoundedSemaphore | None, threading.Lock]:
    """Bounded submission semaphore in front of a pool (F-26/F-37).

    Returns None when the queue is unbounded (limit <= 0). The semaphore is
    recreated when the limit changes so config edits take effect.
    """
    if limit <= 0:
        return None, threading.Lock()
    global _store_submit_sem, _store_submit_limit, _sync_submit_sem, _sync_submit_limit
    with _store_submit_lock:
        if _store_submit_sem is None or _store_submit_limit != limit:
            _store_submit_sem = threading.BoundedSemaphore(limit)
            _store_submit_limit = limit
        return _store_submit_sem, _store_submit_lock


def _get_sync_submit_semaphore(
    limit: int,
) -> tuple[threading.BoundedSemaphore | None, threading.Lock]:
    if limit <= 0:
        return None, threading.Lock()
    global _sync_submit_sem, _sync_submit_limit
    with _sync_submit_lock:
        if _sync_submit_sem is None or _sync_submit_limit != limit:
            _sync_submit_sem = threading.BoundedSemaphore(limit)
            _sync_submit_limit = limit
        return _sync_submit_sem, _sync_submit_lock


def _get_branch_submit_semaphore(
    limit: int,
) -> threading.BoundedSemaphore | None:
    """Bounded submission queue for the optional lexical/sparse branch pool (R-09).

    Returns None when unbounded (limit <= 0). The semaphore is recreated when
    the limit changes so config edits take effect.
    """
    if limit <= 0:
        return None
    global _branch_submit_sem, _branch_submit_limit
    with _branch_submit_lock:
        if _branch_submit_sem is None or _branch_submit_limit != limit:
            _branch_submit_sem = threading.BoundedSemaphore(limit)
            _branch_submit_limit = limit
        return _branch_submit_sem


def reset_submit_semaphores_for_tests() -> None:
    global _store_submit_sem, _store_submit_limit, _sync_submit_sem, _sync_submit_limit
    global _branch_submit_sem, _branch_submit_limit
    with _store_submit_lock:
        _store_submit_sem = None
        _store_submit_limit = 0
    with _sync_submit_lock:
        _sync_submit_sem = None
        _sync_submit_limit = 0
    with _branch_submit_lock:
        _branch_submit_sem = None
        _branch_submit_limit = 0


# Dedicated pool for the optional lexical/sparse branches (P8 / 新-4). Kept
# separate from `_store_pool` on purpose: the dense branch (which itself submits
# to the store pool) runs on the caller thread, and the two optional branches
# run here — so there is never a nested submit that could deadlock a small pool.
_branch_pool: ThreadPoolExecutor | None = None
_branch_pool_lock = threading.Lock()
_BRANCH_POOL_WORKERS = 2  # lexical + sparse


def get_branch_pool() -> ThreadPoolExecutor:
    """Lazy process-local pool for concurrent lexical/sparse retrieval."""
    global _branch_pool
    with _branch_pool_lock:
        if _branch_pool is None:
            _branch_pool = ThreadPoolExecutor(
                max_workers=_BRANCH_POOL_WORKERS, thread_name_prefix="retrieve-branch"
            )
        return _branch_pool


def reset_branch_pool_for_tests() -> None:
    global _branch_pool, _branch_submit_sem, _branch_submit_limit
    with _branch_pool_lock:
        if _branch_pool is not None:
            _branch_pool.shutdown(wait=False, cancel_futures=True)
        _branch_pool = None
    with _branch_submit_lock:
        _branch_submit_sem = None
        _branch_submit_limit = 0


# R-04: bounded *isolation pool* for non-cancellable external calls (query
# embedding, sparse embedding, cross-encoder rerank). Python cannot force-kill a
# running thread, so these calls are given an explicit concurrency cap and a
# bounded wait derived from the remaining request deadline — the request never
# waits past its budget even though the underlying library call keeps running.
_external_pool: ThreadPoolExecutor | None = None
_external_pool_lock = threading.Lock()
_external_pool_size = 0
_external_submit_sem: threading.BoundedSemaphore | None = None
_external_submit_limit = 0
_external_submit_lock = threading.Lock()


def get_external_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
    """Lazy process-local pool for bounded, non-cancellable external calls (R-04)."""
    global _external_pool, _external_pool_size
    with _external_pool_lock:
        size = max(1, int(max_workers or 8))
        if _external_pool is None:
            _external_pool = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="retrieve-external"
            )
            _external_pool_size = size
        return _external_pool


def _get_external_submit_semaphore(limit: int) -> threading.BoundedSemaphore | None:
    if limit <= 0:
        return None
    global _external_submit_sem, _external_submit_limit
    with _external_submit_lock:
        if _external_submit_sem is None or _external_submit_limit != limit:
            _external_submit_sem = threading.BoundedSemaphore(limit)
            _external_submit_limit = limit
        return _external_submit_sem


def reset_external_pool_for_tests() -> None:
    global _external_pool, _external_pool_size, _external_submit_sem, _external_submit_limit
    with _external_pool_lock:
        if _external_pool is not None:
            _external_pool.shutdown(wait=False, cancel_futures=True)
        _external_pool = None
        _external_pool_size = 0
    with _external_submit_lock:
        _external_submit_sem = None
        _external_submit_limit = 0


def _effective_timeout(configured: float, deadline: float | None) -> float | None:
    """Combine a per-call timeout with the request's remaining budget (R-04).

    Returns None when neither is set (unbounded — the caller runs inline and the
    legacy behaviour is preserved). Otherwise returns the *smaller* of the two so
    a single call can never wait past the request deadline.
    """
    cfg_t = float(configured or 0.0)
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if cfg_t <= 0 and remaining is None:
        return None
    if remaining is None:
        return cfg_t
    if cfg_t <= 0:
        return remaining
    return min(cfg_t, remaining)


def _run_bounded(
    fn: Callable[[], Any],
    *,
    cfg: Settings,
    deadline: float | None,
    timeout_sec: float,
    metrics: Any = None,
    timeout_metric: str = "retrieve.external_timeout",
) -> Any:
    """Run a non-cancellable external call behind a bounded isolation pool (R-04).

    The wait is capped by ``min(timeout_sec, remaining deadline)``; when neither
    is set the call runs inline (legacy, unbounded). A wait expiry raises
    ``RetrieveError`` (RETRIEVE_TIMEOUT/504); pool saturation fails fast
    (RETRIEVE_STORE_OVERLOADED/503) instead of queueing unbounded. The running
    thread is not force-killed (Python cannot), but the *request* returns at the
    deadline — this is a request-wait budget, not a hard end-to-end cap.
    """
    wait = _effective_timeout(timeout_sec, deadline)
    if wait is None:
        return fn()
    if wait <= 0:
        if metrics is not None:
            metrics.inc(timeout_metric)
        raise RetrieveError(
            "retrieve deadline reached before external call", "RETRIEVE_TIMEOUT", 504
        )
    pool_size = max(1, int(getattr(cfg, "retrieve_external_pool_size", 8) or 8))
    queue_max = max(0, int(getattr(cfg, "retrieve_external_queue_max", 0) or 0))
    pool = get_external_pool(pool_size)
    sem = _get_external_submit_semaphore(pool_size + queue_max)
    if sem is not None and not sem.acquire(blocking=False):
        if metrics is not None:
            metrics.inc("retrieve.external_overloaded")
        raise RetrieveError(
            "retrieve external call pool saturated", "RETRIEVE_STORE_OVERLOADED", 503
        )
    try:
        fut = pool.submit(fn)
    except Exception:
        if sem is not None:
            sem.release()
        raise
    if sem is not None:
        fut.add_done_callback(lambda _f: sem.release())
    try:
        return fut.result(timeout=wait)
    except FuturesTimeoutError as exc:
        fut.cancel()
        if metrics is not None:
            metrics.inc(timeout_metric)
        raise RetrieveError(
            f"external call exceeded remaining retrieve budget ({wait:.3f}s)",
            "RETRIEVE_TIMEOUT",
            504,
        ) from exc


def _get_bulkhead(limit: int) -> threading.BoundedSemaphore | None:
    global _bulkhead_sem, _bulkhead_limit
    if limit <= 0:
        return None
    with _bulkhead_lock:
        if _bulkhead_sem is None or _bulkhead_limit != limit:
            _bulkhead_sem = threading.BoundedSemaphore(limit)
            _bulkhead_limit = limit
        return _bulkhead_sem


def reset_bulkhead_for_tests() -> None:
    global _bulkhead_sem, _bulkhead_limit
    with _bulkhead_lock:
        _bulkhead_sem = None
        _bulkhead_limit = 0


def _try_acquire_qps(rate: float) -> bool:
    """Simple process-local token bucket. rate<=0 disables."""
    global _qps_tokens, _qps_last, _qps_rate
    if rate <= 0:
        return True
    now = time.monotonic()
    with _qps_lock:
        if _qps_rate != rate:
            _qps_rate = rate
            _qps_tokens = rate
            _qps_last = now
        elapsed = max(0.0, now - _qps_last)
        _qps_last = now
        _qps_tokens = min(rate, _qps_tokens + elapsed * rate)
        if _qps_tokens >= 1.0:
            _qps_tokens -= 1.0
            return True
        return False


def reset_qps_for_tests() -> None:
    global _qps_tokens, _qps_last, _qps_rate
    with _qps_lock:
        _qps_tokens = 0.0
        _qps_last = 0.0
        _qps_rate = 0.0


def reset_embed_cache_for_tests() -> None:
    with _embed_cache_lock:
        _embed_cache.clear()


def _embed_cache_key(question: str, cfg: Settings) -> tuple[str, int, str]:
    mid = embed_model_id(cfg)
    h = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
    return (mid, int(cfg.embed_dim or 0), h)


def _encode_question_cached(
    *,
    question: str,
    embedder: Any,
    cfg: Settings,
    metrics: Any,
    deadline: float | None = None,
) -> list[float]:
    max_size = int(cfg.retrieve_embed_cache_max or 0)
    if max_size <= 0:
        return _run_bounded(
            lambda: retry_call(
                lambda: embedder.encode([question])[0],
                policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
            ),
            cfg=cfg,
            deadline=deadline,
            timeout_sec=float(getattr(cfg, "retrieve_embed_timeout_sec", 0.0) or 0.0),
            metrics=metrics,
            timeout_metric="retrieve.embed_timeout",
        )
    key = _embed_cache_key(question, cfg)
    with _embed_cache_lock:
        if key in _embed_cache:
            _embed_cache.move_to_end(key)
            vec = _embed_cache[key]
            metrics.inc("retrieve.embed_cache_hit")
            return list(vec)
    metrics.inc("retrieve.embed_cache_miss")
    vec = _run_bounded(
        lambda: retry_call(
            lambda: embedder.encode([question])[0],
            policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
        ),
        cfg=cfg,
        deadline=deadline,
        timeout_sec=float(getattr(cfg, "retrieve_embed_timeout_sec", 0.0) or 0.0),
        metrics=metrics,
        timeout_metric="retrieve.embed_timeout",
    )
    with _embed_cache_lock:
        _embed_cache[key] = list(vec)
        _embed_cache.move_to_end(key)
        while len(_embed_cache) > max_size:
            _embed_cache.popitem(last=False)
    return list(vec)


# Sparse query-vector LRU (P8). Mirrors the dense `_embed_cache` exactly, but for
# BGE-M3 `lexical_weights` (a term→weight dict). Same key discipline: the key is
# `(embed_model_id, embed_dim, question_hash)` so a query encoded under one model
# can never be served from a cache populated under another (no cross-model bleed).
_sparse_cache: OrderedDict[tuple[str, int, str], dict[str, float]] = OrderedDict()
_sparse_cache_lock = threading.Lock()


def reset_sparse_cache_for_tests() -> None:
    with _sparse_cache_lock:
        _sparse_cache.clear()


def _encode_sparse_question_cached(
    *,
    question: str,
    embedder: Any,
    cfg: Settings,
    metrics: Any,
    deadline: float | None = None,
) -> dict[str, float]:
    """Cached sparse query weights. Reuses `retrieve_embed_cache_max` as the cap
    (0 disables, matching the dense cache) so one knob governs both."""
    max_size = int(cfg.retrieve_embed_cache_max or 0)
    if max_size <= 0:
        return _run_bounded(
            lambda: retry_call(
                lambda: embedder.encode_sparse([question])[0],
                policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
            ),
            cfg=cfg,
            deadline=deadline,
            timeout_sec=float(getattr(cfg, "retrieve_embed_timeout_sec", 0.0) or 0.0),
            metrics=metrics,
            timeout_metric="retrieve.sparse_embed_timeout",
        )
    key = _embed_cache_key(question, cfg)
    with _sparse_cache_lock:
        if key in _sparse_cache:
            _sparse_cache.move_to_end(key)
            metrics.inc("retrieve.sparse_cache_hit")
            return dict(_sparse_cache[key])
    metrics.inc("retrieve.sparse_cache_miss")
    vec = _run_bounded(
        lambda: retry_call(
            lambda: embedder.encode_sparse([question])[0],
            policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
        ),
        cfg=cfg,
        deadline=deadline,
        timeout_sec=float(getattr(cfg, "retrieve_embed_timeout_sec", 0.0) or 0.0),
        metrics=metrics,
        timeout_metric="retrieve.sparse_embed_timeout",
    )
    with _sparse_cache_lock:
        _sparse_cache[key] = dict(vec)
        _sparse_cache.move_to_end(key)
        while len(_sparse_cache) > max_size:
            _sparse_cache.popitem(last=False)
    return dict(vec)


# Query-expansion LRU (新-5). `expand_query_texts` / `expand_terms_for_lexical`
# are pure rule-based text transforms (no model), so the key is
# (question, enable_codes, max_variants) — no model dimension needed.
_expand_texts_cache: OrderedDict[tuple[str, bool, int], list[str]] = OrderedDict()
_expand_lex_cache: OrderedDict[tuple[str, bool, int], str] = OrderedDict()
_expand_cache_lock = threading.Lock()


def reset_expand_cache_for_tests() -> None:
    with _expand_cache_lock:
        _expand_texts_cache.clear()
        _expand_lex_cache.clear()


def _expand_query_texts_cached(question: str, *, enable_codes: bool, cfg: Settings) -> list[str]:
    from ai.retrieve.query_expand import expand_query_texts

    max_size = int(getattr(cfg, "retrieve_query_expand_cache_max", 0) or 0)
    key = (question, bool(enable_codes), 2)
    if max_size <= 0:
        return expand_query_texts(question, enable_codes=enable_codes)
    with _expand_cache_lock:
        if key in _expand_texts_cache:
            _expand_texts_cache.move_to_end(key)
            return list(_expand_texts_cache[key])
    texts = expand_query_texts(question, enable_codes=enable_codes)
    with _expand_cache_lock:
        _expand_texts_cache[key] = list(texts)
        _expand_texts_cache.move_to_end(key)
        while len(_expand_texts_cache) > max_size:
            _expand_texts_cache.popitem(last=False)
    return list(texts)


def _expand_terms_for_lexical_cached(question: str, *, enable_codes: bool, cfg: Settings) -> str:
    from ai.retrieve.query_expand import expand_terms_for_lexical

    max_size = int(getattr(cfg, "retrieve_query_expand_cache_max", 0) or 0)
    key = (question, bool(enable_codes), 3)
    if max_size <= 0:
        return expand_terms_for_lexical(question, enable_codes=enable_codes)
    with _expand_cache_lock:
        if key in _expand_lex_cache:
            _expand_lex_cache.move_to_end(key)
            return _expand_lex_cache[key]
    q = expand_terms_for_lexical(question, enable_codes=enable_codes)
    with _expand_cache_lock:
        _expand_lex_cache[key] = q
        _expand_lex_cache.move_to_end(key)
        while len(_expand_lex_cache) > max_size:
            _expand_lex_cache.popitem(last=False)
    return q


def _acl_fields_missing(row: dict, flt: SearchFilter | None) -> bool:
    """True when filter requires ACL metadata that the row does not carry."""
    if flt is None:
        return False
    dept = (flt.department or "").strip()
    if dept and not str(row.get("acl_department") or "").strip():
        return True
    cls = (flt.classification or "").strip()
    if cls and not str(row.get("acl_classification") or "").strip():
        return True
    return False


def _emit_struct_log(payload: dict) -> None:
    try:
        logger.info("retrieve_struct %s", json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover
        logger.debug("retrieve_struct log failed")


def _struct_base(
    *,
    event: str,
    q_hash: str,
    took_ms: float | int | None,
    top_k: int | None = None,
    hybrid: bool | None = None,
    degraded: bool | None = None,
    acl_applied: bool | None = None,
    filtered_out: int | None = None,
    dense_hits: int | None = None,
    lexical_hits: int | None = None,
    returned: int | None = None,
    stage_embed_ms: float | None = None,
    stage_store_ms: float | None = None,
    stage_lexical_ms: float | None = None,
    stage_fuse_ms: float | None = None,
    error_code: str | None = None,
    status_code: int | None = None,
) -> dict:
    return {
        "event": event,
        "q_hash": q_hash,
        "took_ms": took_ms,
        "top_k": top_k,
        "hybrid": hybrid,
        "degraded": degraded,
        "acl_applied": acl_applied,
        "filtered_out": filtered_out,
        "dense_hits": dense_hits,
        "lexical_hits": lexical_hits,
        "returned": returned,
        "stage_embed_ms": stage_embed_ms,
        "stage_store_ms": stage_store_ms,
        "stage_lexical_ms": stage_lexical_ms,
        "stage_fuse_ms": stage_fuse_ms,
        "error_code": error_code,
        "status_code": status_code,
        "trace_id": get_trace_id(),
    }


def _search_store_with_resilience(
    *,
    store: VectorStore,
    qvec: list[float],
    top_k: int,
    flt: SearchFilter | None,
    cfg: Settings,
    circuits: Any,
    metrics: Any,
    deadline: float | None = None,
) -> tuple[list[dict], float]:
    """Dense store.search with circuit + bounded timeout budget. Returns (hits, elapsed_ms).

    R-01: the wait is ``min(retrieve_store_timeout_sec, remaining deadline)`` and the
    *same* value is handed to ``store.search(timeout_sec=...)`` so the backend client
    gets one real RPC deadline (a stuck Milvus gRPC call no longer outlives the
    request). The pool submission is bounded by a semaphore (pool workers + queue
    cap); saturation fails fast with 503 instead of queueing unbounded behind a
    stuck downstream.
    """
    circuit_name = "retrieve_store"
    if circuits.is_open(circuit_name):
        metrics.inc("retrieve.store_circuit_open")
        raise RetrieveError("retrieve store circuit open", "RETRIEVE_FAILED", 503)

    timeout_sec = float(cfg.retrieve_store_timeout_sec or 0)
    # R-01: absolute request deadline caps the per-call store timeout.
    wait = _effective_timeout(timeout_sec, deadline)
    t0 = time.monotonic()
    if wait is None:
        # Legacy unbounded path: neither a store timeout nor an overall deadline is
        # configured, so run inline (no pool, no bounded queue).
        try:
            hits = store.search(qvec, top_k=top_k, filter=flt)
            circuits.record_success(circuit_name)
            return hits, (time.monotonic() - t0) * 1000
        except Exception as exc:
            circuits.record_failure(circuit_name, str(exc))
            metrics.inc(
                "retrieve.store_timeout"
                if "timeout" in type(exc).__name__.lower()
                else "retrieve.fail"
            )
            raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc

    if wait <= 0:
        # The overall deadline is already spent — do not even submit.
        metrics.inc("retrieve.store_timeout")
        raise RetrieveError(
            "retrieve deadline reached before store.search", "RETRIEVE_TIMEOUT", 504
        )

    pool_size = max(1, int(getattr(cfg, "retrieve_store_pool_size", 4) or 4))
    queue_max = max(0, int(getattr(cfg, "retrieve_store_queue_max", 0) or 0))
    pool = get_store_pool(pool_size)
    # R-01: bounded submission queue — saturation is a fast 503, never an unbounded
    # executor backlog. The slot is released by the future's completion callback.
    sem, _ = _get_submit_semaphore(pool_size + queue_max)
    if sem is not None and not sem.acquire(blocking=False):
        metrics.inc("retrieve.store_overloaded")
        raise RetrieveError("retrieve store pool saturated", "RETRIEVE_STORE_OVERLOADED", 503)
    try:
        fut = pool.submit(store.search, qvec, top_k=top_k, filter=flt, timeout_sec=wait)
    except Exception:
        if sem is not None:
            sem.release()
        raise
    if sem is not None:
        fut.add_done_callback(lambda _f: sem.release())
    try:
        hits = fut.result(timeout=wait)
    except FuturesTimeoutError as exc:
        # Cancel only cancels a not-yet-started task; the running RPC is bounded by
        # the same ``timeout_sec`` handed to store.search above.
        fut.cancel()
        circuits.record_failure(circuit_name, f"timeout after {wait}s")
        metrics.inc("retrieve.store_timeout")
        raise RetrieveError(
            f"store.search exceeded retrieve budget ({wait:.3f}s)",
            "RETRIEVE_TIMEOUT",
            504,
        ) from exc
    except Exception as exc:
        circuits.record_failure(circuit_name, str(exc))
        raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc
    circuits.record_success(circuit_name)
    return hits, (time.monotonic() - t0) * 1000


def _validate_unit_ids(values: list[str] | None, max_ids: int = 0) -> list[str] | None:
    if values is None:
        return None
    if max_ids > 0 and len(values) > max_ids:
        raise RetrieveError(
            f"unit_ids length {len(values)} exceeds retrieve_max_unit_ids={max_ids}",
            "RETRIEVE_INVALID_FILTER",
            400,
        )
    out: list[str] = []
    for uid in values:
        s = (uid or "").strip()
        if not s or not _UNIT_ID_RE.match(s):
            raise RetrieveError(
                f"invalid unit_id: {uid!r}",
                "RETRIEVE_INVALID_FILTER",
                400,
            )
        out.append(s)
    return out


@dataclass
class RetrieveRequestView:
    question: str
    top_k: int
    acl_filter: AclFilter | None = None
    unit_ids: list[str] | None = None
    overfetch: int | None = None
    enable_rerank: bool | None = None
    enable_hybrid: bool | None = None


@dataclass
class FuseHit:
    chunk_id: str
    unit_id: str
    chunk_index: int
    content: str
    version_id: str = ""
    heading_path: list[str] = field(default_factory=list)
    score_dense: float | None = None
    score_lexical: float | None = None
    score_sparse: float | None = None
    score_rewrite: float | None = None
    score_fused: float = 0.0
    score_base: float | None = None
    retrieval_paths: list[str] = field(default_factory=list)
    acl_department: str = ""
    acl_classification: str = ""


def normalize_question(raw: str, max_chars: int) -> str:
    q = sanitize_text(raw or "")
    if not q:
        raise RetrieveError(
            "question is empty after sanitization",
            "PAYLOAD_TOO_LARGE",
            400,
        )
    if max_chars > 0 and len(q) > max_chars:
        raise RetrieveError(
            f"question length {len(q)} exceeds retrieve_max_question_chars={max_chars}",
            "PAYLOAD_TOO_LARGE",
            400,
        )
    return q


def build_acl_filter(
    acl: AclFilter | None,
    unit_ids: list[str] | None,
    require_acl: bool,
    max_unit_ids: int = 0,
) -> tuple[SearchFilter | None, bool]:
    """Merge request filter fields into SearchFilter. Fail-closed when required and empty.

    None vs [] semantics (F-07 root fix):
      * ``None`` -> the field is unspecified (it contributes no constraint);
      * ``[]``   -> an explicit empty allow-list = **deny-all** — the
        SearchFilter/Milvus contract ("[] matches nothing"), never "no filter".
    An explicit empty allow-list therefore can never silently degrade into a
    full-corpus search:
      * ``require_acl=false`` -> it compiles to a deny-all filter (zero hits);
      * ``require_acl=true``  -> fail-closed contract rejects the request
        (400 RETRIEVE_ACL_REQUIRED), same as a missing acl_filter.
    The same holds for the allow-list *intersection* (request ``unit_ids`` vs
    ``acl_filter.unit_ids``): an empty intersection is deny-all.
    """
    unit_ids = _validate_unit_ids(unit_ids, max_unit_ids)
    deny = _validate_unit_ids(acl.deny_unit_ids if acl else None, max_unit_ids)
    acl_units = _validate_unit_ids(acl.unit_ids if acl else None, max_unit_ids)

    merged_unit_ids: list[str] | None = None
    if unit_ids is not None:
        # Keep [] as an explicit empty allow-list (deny-all), not "unspecified".
        merged_unit_ids = list(unit_ids)
    if acl_units is not None:
        if merged_unit_ids is None:
            merged_unit_ids = list(acl_units)
        else:
            # Intersection; empty result is intentional deny-all (fail-closed).
            allow = set(merged_unit_ids) & set(acl_units)
            merged_unit_ids = sorted(allow)

    flt = SearchFilter(
        unit_ids=merged_unit_ids,
        deny_unit_ids=deny,
        department=(acl.department if acl else None),
        classification=(acl.classification if acl else None),
    )
    has_other_constraints = (
        bool(deny)
        or bool((acl and (acl.department or "").strip()))
        or bool((acl and (acl.classification or "").strip()))
        or (acl is not None and acl.min_score is not None)
    )
    # unit_ids=[] means deny-all, not "no filter". Only None + no other fields is unconstrained.
    empty = flt.unit_ids is None and not has_other_constraints
    # Explicit empty allow-list with no other constraint: the caller expressed
    # "no unit is allowed". require_acl rejects it (no usable ACL provided);
    # otherwise it becomes a deny-all filter (never a full-corpus search).
    deny_all = flt.unit_ids is not None and len(flt.unit_ids) == 0 and not has_other_constraints
    if require_acl and (empty or deny_all):
        metrics = get_metrics()
        metrics.inc("retrieve.acl_required_reject")
        raise RetrieveError(
            "acl_filter is required but missing",
            "RETRIEVE_ACL_REQUIRED",
            400,
        )
    applied = not empty
    return (flt if applied else None), applied


def is_deny_all_filter(flt: SearchFilter | None) -> bool:
    """Whether ``flt`` allows nothing (explicit empty allow-list).

    Callers may use this to short-circuit: searching with a deny-all filter is
    guaranteed to return zero hits, so there is no point spending embed/store
    budget on it (the request must never fall back to a full-corpus search).
    """
    if flt is None:
        return False
    if flt.unit_ids is not None and len(flt.unit_ids) == 0:
        return True
    return False


def fuse_rrf(
    path_rankings: list[list[str]],
    k: int = 60,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """score_fused = sum w_i/(k+rank); missing path contributes nothing. Rank starts at 1."""
    k = max(1, int(k))
    scores: dict[str, float] = {}
    for i, ranking in enumerate(path_rankings):
        w = 1.0
        if weights is not None and i < len(weights):
            w = float(weights[i])
        if w == 0.0:
            continue
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + w * 1.0 / (k + rank)
    return scores


def _hit_key(unit_id: str, chunk_id: str, version_id: str = "") -> str:
    return f"{unit_id}::{chunk_id}::{version_id}"


def post_filter_acl(
    hits: list[FuseHit],
    flt: SearchFilter | None,
    min_score: float | None,
    *,
    acl_fields_required: bool = False,
    metrics: Any | None = None,
) -> tuple[list[FuseHit], int]:
    kept: list[FuseHit] = []
    dropped = 0
    for h in hits:
        row = {
            "unit_id": h.unit_id,
            "acl_department": h.acl_department,
            "acl_classification": h.acl_classification,
        }
        if acl_fields_required and _acl_fields_missing(row, flt):
            dropped += 1
            if metrics is not None:
                metrics.inc("retrieve.acl_fields_missing_drop")
            continue
        if not row_passes_filter(row, flt):
            dropped += 1
            continue
        if min_score is not None and h.score_fused < min_score:
            dropped += 1
            continue
        kept.append(h)
    return kept, dropped


def apply_max_per_unit(hits: list[FuseHit], max_per_unit: int) -> list[FuseHit]:
    if max_per_unit <= 0:
        return hits
    counts: dict[str, int] = {}
    out: list[FuseHit] = []
    for h in hits:
        n = counts.get(h.unit_id, 0)
        if n >= max_per_unit:
            continue
        counts[h.unit_id] = n + 1
        out.append(h)
    return out


def adaptive_max_per_unit(
    hits: list[FuseHit],
    base: int,
    cap: int,
    top_k: int,
) -> int:
    """Per-unit chunk budget that widens when few distinct units are recalled.

    A single long manual whose answer chunk sits at rank>base would otherwise be
    truncated by a fixed `retrieve_max_per_unit`. When few units are in the
    candidate set we allow up to `cap` chunks per unit so the answer chunk stays
    reachable; with many units we keep the tight base budget.
    """
    if base <= 0:
        return 0
    n_units = max(1, len({h.unit_id for h in hits}))
    widened = max(1, -(-top_k // n_units))  # ceil(top_k / n_units)
    return max(1, min(max(base, widened), max(base, cap)))


def dedup_overlapping_chunks(hits: list[FuseHit], jaccard: float) -> list[FuseHit]:
    """Drop near-duplicate overlapping window chunks from the same unit.

    Keeps the first (highest-scored) chunk and removes later chunks whose token
    Jaccard overlap with a kept chunk exceeds the threshold. This frees the
    per-unit budget for genuinely distinct content instead of overlap copies.
    """
    if not hits or jaccard <= 0.0 or jaccard >= 1.0:
        return hits
    from ai.retrieve.lexical import tokenize

    kept: list[FuseHit] = []
    seen_by_unit: dict[str, list[set[str]]] = {}
    for h in hits:
        toks = set(tokenize(h.content or ""))
        if not toks:
            kept.append(h)
            continue
        unit_seen = seen_by_unit.setdefault(h.unit_id, [])
        is_dup = False
        for prev in unit_seen:
            union = len(toks | prev)
            if union and (len(toks & prev) / union) >= jaccard:
                is_dup = True
                break
        if is_dup:
            continue
        unit_seen.append(toks)
        kept.append(h)
    return kept


def aggregate_unit_score(
    scores: list[float],
    *,
    mode: str = "best",
    topk: int = 3,
    coverage_weight: float = 0.0,
    coverage_k: int = 3,
) -> float:
    """Aggregate one unit's chunk scores into a single unit-level score.

    - ``mode="best"``: the unit's single best chunk score (legacy behaviour).
      Cheap but noisy — a unit with one lucky high chunk can outrank a unit
      that is *consistently* relevant across several chunks.
    - ``mode="topk"``: weighted sum of the unit's top-k chunk scores
      (weights 1, 2/3, 1/2, …) plus an optional coverage bonus
      ``coverage_weight * min(1, n_chunks/coverage_k) * best`` that rewards
      units contributing more than one candidate chunk.

    The coverage bonus is scaled by the unit's own best score so the formula
    stays scale-invariant regardless of the RRF / rule-boost magnitude.
    """
    if not scores:
        return 0.0
    ordered = sorted((float(s) for s in scores), reverse=True)
    if mode != "topk":
        return ordered[0]
    top = ordered[: max(1, int(topk))]
    weights = [1.0 / (1.0 + 0.5 * i) for i in range(len(top))]
    base = sum(w * s for w, s in zip(weights, top))
    if coverage_weight:
        cov = min(1.0, len(ordered) / max(1, int(coverage_k)))
        base += float(coverage_weight) * cov * ordered[0]
    return base


def _unit_agg_kwargs(cfg: Settings) -> dict[str, Any]:
    """Unit-score aggregation knobs (shared by unit order + unit-only rerank)."""
    return {
        "agg_mode": str(getattr(cfg, "retrieve_unit_agg_mode", "best") or "best"),
        "topk": int(getattr(cfg, "retrieve_unit_agg_topk", 3) or 3),
        "coverage_weight": float(getattr(cfg, "retrieve_unit_agg_coverage_weight", 0.0) or 0.0),
        "coverage_k": int(getattr(cfg, "retrieve_unit_agg_coverage_k", 3) or 3),
    }


def interleave_units(hits: list[FuseHit], *, block: int = 1) -> list[FuseHit]:
    """Round-robin chunks across units, emitting `block` chunks per unit per round.

    ``block=1`` maximises unit diversity (A1,B1,C1,A2,…) so every top-k prefix
    spans the most units; ``block=2`` emits pairs (A1,A2,B1,B2,C1,C2,…) — fewer
    units per prefix but keeps more depth per unit (higher precision@k). A
    ``block >= max_per_unit`` degenerates to the original blocked order.

    Unit order (first-seen) and each unit's internal chunk order are preserved;
    only the interleaving changes. Rationale: after unit ordering the pipeline
    emits each unit's chunks as a contiguous block, so with
    `retrieve_max_per_unit=4` a single mis-ranked unit occupies 4 of the 5 slots
    that `chunk_hit@5` looks at — one unit-ordering mistake then wipes out the
    answer chunk. Interleaving keeps every top-k prefix spanning several units,
    which makes chunk-level metrics robust to unit-order noise while the unit's
    representative (its first chunk) still leads. Idempotent for a given block.
    """
    step = max(1, int(block))
    groups: dict[str, list[FuseHit]] = {}
    unit_order: list[str] = []
    for h in hits:
        if h.unit_id not in groups:
            groups[h.unit_id] = []
            unit_order.append(h.unit_id)
        groups[h.unit_id].append(h)
    out: list[FuseHit] = []
    depth = 0
    while True:
        added = False
        for uid in unit_order:
            bucket = groups[uid]
            for offset in range(step):
                if depth + offset < len(bucket):
                    out.append(bucket[depth + offset])
                    added = True
        if not added:
            return out
        depth += step


def unit_representative_order(
    hits: list[FuseHit],
    *,
    agg_mode: str = "best",
    topk: int = 3,
    coverage_weight: float = 0.0,
    coverage_k: int = 3,
) -> list[FuseHit]:
    """Debias multi-chunk manuals: rank units by best chunk, interleave next-best after reps."""
    best_by_unit: dict[str, FuseHit] = {}
    rest_by_unit: dict[str, list[FuseHit]] = {}
    scores_by_unit: dict[str, list[float]] = {}
    for h in hits:
        scores_by_unit.setdefault(h.unit_id, []).append(float(h.score_fused))
        cur = best_by_unit.get(h.unit_id)
        if cur is None or h.score_fused > cur.score_fused:
            if cur is not None:
                rest_by_unit.setdefault(h.unit_id, []).append(cur)
            best_by_unit[h.unit_id] = h
        else:
            rest_by_unit.setdefault(h.unit_id, []).append(h)
    if agg_mode == "best":
        reps = sorted(best_by_unit.values(), key=lambda x: x.score_fused, reverse=True)
    else:

        def _unit_score(uid: str) -> float:
            return aggregate_unit_score(
                scores_by_unit.get(uid, []),
                mode=agg_mode,
                topk=topk,
                coverage_weight=coverage_weight,
                coverage_k=coverage_k,
            )

        reps = sorted(
            best_by_unit.values(),
            key=lambda x: (_unit_score(x.unit_id), x.score_fused),
            reverse=True,
        )
    out: list[FuseHit] = []
    # Pass 1: one representative per unit (unit order).
    out.extend(reps)
    # Pass 2: next-best chunks in same unit order (consumed by max_per_unit later).
    for rep in reps:
        extras = sorted(
            rest_by_unit.get(rep.unit_id, []),
            key=lambda x: x.score_fused,
            reverse=True,
        )
        out.extend(extras)
    return out


_ASCII_TOKEN_RE = re.compile(r"[A-Za-z]+(?:-?\d+)+|\d+(?:\.\d+)?|[A-Za-z]{2,}\d+")


def keyword_aware_unit_order(hits: list[FuseHit], question: str) -> list[FuseHit]:
    """Reorder chunks *within each unit* so query-relevant chunks come first.

    Unit aggregation ranks units well but keeps a unit's internal chunk order by
    `score_fused`, which often puts a generic heading chunk above the chunk that
    actually carries the answer. Here we demote the unit to its best anchor but
    promote, ahead of it, any sibling chunk whose text overlaps the query's
    exact tokens (error codes / model numbers / prices) or Chinese bigrams.

    Unit order itself is preserved (first-seen position), so cross-unit recall
    is unchanged; only intra-unit positions are corrected.
    """
    if not hits:
        return hits
    from ai.retrieve.lexical import tokenize

    terms = set(tokenize(question or ""))
    exact = extract_exact_tokens(question or "")
    if not terms and not exact:
        return hits
    bigrams = {t for t in terms if len(t) == 2 and not t.isascii()}

    # Preserve unit order by first appearance.
    unit_order: list[str] = []
    groups: dict[str, list[FuseHit]] = {}
    for h in hits:
        if h.unit_id not in groups:
            unit_order.append(h.unit_id)
            groups[h.unit_id] = []
        groups[h.unit_id].append(h)

    def kw_score(h: FuseHit) -> float:
        text = h.content[:800]
        up = text.upper()
        s = 0.0
        hit_exact = 0
        for tok in exact:
            if tok and tok in up:
                s += 1.0
                hit_exact += 1
        if bigrams:
            s += 0.5 * len(bigrams & {t for t in tokenize(text) if len(t) == 2 and not t.isascii()})
        s += 0.05 * len(terms & set(tokenize(text)))
        # A chunk carrying an exact query token is always a strong answer signal.
        return s + (0.25 if hit_exact else 0.0)

    out: list[FuseHit] = []
    for uid in unit_order:
        siblings = groups[uid]
        if len(siblings) <= 1:
            out.extend(siblings)
            continue
        # Anchor = best by score_fused; then promote keyword-carrying chunks ahead
        # of it (stable by original score_fused among themselves).
        anchor = max(siblings, key=lambda x: x.score_fused)
        # Score each sibling exactly once. Previously `kw_score` was recomputed
        # up to three times per chunk (sort key + two list comprehensions),
        # re-tokenizing the same content each time — pure duplicated work (P8).
        kw_by_id = {id(h): kw_score(h) for h in siblings}
        scored = sorted(
            siblings,
            key=lambda x: (kw_by_id[id(x)], x.score_fused),
            reverse=True,
        )
        # Keep anchor in the list but ensure it is not blocked by a lower-value
        # chunk: keyword-carrying chunks first, then anchor, then the rest.
        kws = [h for h in scored if kw_by_id[id(h)] > 0]
        rest = [h for h in scored if kw_by_id[id(h)] <= 0]
        ordered = (
            kws + ([anchor] if anchor not in kws else []) + [h for h in rest if h is not anchor]
        )
        out.extend(ordered)
    return out


def extract_exact_tokens(question: str) -> set[str]:
    """Error codes / model numbers / prices: E-05, LA2608, 300, 800."""
    return {m.group(0).upper() for m in _ASCII_TOKEN_RE.finditer(question or "")}


def rule_rerank(
    hits: list[FuseHit],
    question: str,
    *,
    exact_weight: float = 0.15,
    bigram_weight: float = 0.01,
    overlap_weight: float = 1e-3,
) -> list[FuseHit]:
    """Exact-token + bigram boost on fused score. No model dependency.

    The boost is **persisted back onto `score_fused`** so downstream stages
    (unit aggregation / budget truncation) do not silently discard it.
    Weights default to the historical constants so the default pipeline is
    unchanged; they are exposed via `retrieve_rule_rerank_*` for A/B tuning.
    """
    from ai.retrieve.lexical import tokenize

    q_terms = set(tokenize(question))
    exact = extract_exact_tokens(question)
    bigrams = {t for t in q_terms if len(t) == 2 and not t.isascii()}
    if not q_terms and not exact:
        return hits

    def boost(h: FuseHit) -> float:
        text = h.content[:800]
        up = text.upper()
        # Idempotent: always derive from the immutable fused base so repeated
        # rule_rerank passes (flat pipeline + graph) cannot accumulate boosts.
        if h.score_base is None:
            h.score_base = h.score_fused
        score = h.score_base
        # Exact tokens (error codes / model numbers / prices) are high-precision
        # answer signals: weight them well above generic Chinese-bigram overlap.
        n_exact = sum(1 for tok in exact if tok and tok in up)
        score += float(exact_weight) * n_exact
        if bigrams:
            content_bi = {t for t in tokenize(text) if len(t) == 2 and not t.isascii()}
            score += float(bigram_weight) * len(bigrams & content_bi)
        overlap = len(q_terms & set(tokenize(text)))
        score += min(overlap, 10) * float(overlap_weight)
        return score

    for h in hits:
        h.score_fused = boost(h)
    return sorted(hits, key=lambda x: x.score_fused, reverse=True)


def embed_rerank(
    hits: list[FuseHit],
    *,
    question_vec: list[float],
    embedder: Any,
    top_n: int = 20,
    weight: float = 0.35,
) -> list[FuseHit]:
    """Blend cosine(q, chunk) into score_fused for first top_n hits."""
    if not hits or weight <= 0 or not question_vec:
        return hits
    n = max(1, int(top_n))
    head, tail = hits[:n], hits[n:]
    texts = [(h.content or "")[:512] for h in head]
    try:
        vecs = embedder.encode(texts)
    except Exception:
        logger.warning("embed_rerank encode failed; keep fused order", exc_info=True)
        return hits
    q = question_vec

    def cos(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        an = sum(x * x for x in a) ** 0.5 or 1.0
        bn = sum(x * x for x in b) ** 0.5 or 1.0
        return dot / (an * bn)

    scored = []
    for h, v in zip(head, vecs):
        c = cos(q, list(v))
        h.score_fused = h.score_fused + weight * max(0.0, c)
        scored.append(h)
    scored.sort(key=lambda x: x.score_fused, reverse=True)
    return scored + tail


def fuse_hybrid_hits(
    *,
    dense_hits: list[dict],
    lexical_hits: list[dict],
    sparse_hits: list[dict],
    use_lexical: bool,
    use_sparse: bool,
    lexical_weight: float = 0.35,
    sparse_weight: float = 0.3,
    rrf_k: int = 60,
    rewrite_hits: list[dict] | None = None,
    rewrite_weight: float = 0.0,
) -> list[FuseHit]:
    """Fuse dense/lexical/sparse hit lists via RRF into a scored FuseHit list.

    Shared by the flat pipeline and the LangGraph debug graph so both stay
    consistent on three-way fusion and `retrieval_paths`/score fields.

    ``rewrite_hits`` (optional) is an **extra recall path** produced by a second,
    rewritten query. It is fused in as a *separate ranking* with its own weight,
    so the original query's rankings are never weakened — this is the structural
    difference from the legacy "replace the question" rewrite (§15).
    """
    by_key: dict[str, FuseHit] = {}

    def _upsert(row: dict, path: str, score: float) -> str:
        cid = str(row.get("id") or row.get("chunk_id") or "")
        unit = str(row.get("unit_id") or "")
        ver = str(row.get("version_id") or "")
        key = _hit_key(unit, cid, ver)
        hit = by_key.get(key)
        if hit is None:
            hit = FuseHit(
                chunk_id=cid,
                unit_id=unit,
                chunk_index=int(row.get("chunk_index") or 0),
                content=str(row.get("content") or ""),
                version_id=ver,
                heading_path=list(row.get("heading_path") or []),
                acl_department=str(row.get("acl_department") or ""),
                acl_classification=str(row.get("acl_classification") or ""),
            )
            by_key[key] = hit
        if path == "dense":
            hit.score_dense = float(score)
            if "dense" not in hit.retrieval_paths:
                hit.retrieval_paths.append("dense")
        elif path == "sparse":
            hit.score_sparse = float(score)
            if "sparse" not in hit.retrieval_paths:
                hit.retrieval_paths.append("sparse")
        elif path == "rewrite":
            hit.score_rewrite = float(score)
            if "rewrite" not in hit.retrieval_paths:
                hit.retrieval_paths.append("rewrite")
        else:
            hit.score_lexical = float(score)
            if "lexical" not in hit.retrieval_paths:
                hit.retrieval_paths.append("lexical")
        return key

    rankings: list[list[str]] = []
    weights: list[float] = []
    rankings.append([_upsert(r, "dense", float(r.get("score") or 0.0)) for r in dense_hits])
    weights.append(1.0)
    if use_lexical and lexical_hits:
        rankings.append([_upsert(r, "lexical", float(r.get("score") or 0.0)) for r in lexical_hits])
        weights.append(float(lexical_weight))
    if use_sparse and sparse_hits:
        rankings.append([_upsert(r, "sparse", float(r.get("score") or 0.0)) for r in sparse_hits])
        weights.append(float(sparse_weight))
    if rewrite_weight > 0.0 and rewrite_hits:
        rankings.append([_upsert(r, "rewrite", float(r.get("score") or 0.0)) for r in rewrite_hits])
        weights.append(float(rewrite_weight))

    fused_scores = fuse_rrf(rankings, k=max(1, int(rrf_k)), weights=weights)
    for key, hit in by_key.items():
        hit.score_fused = fused_scores.get(key, 0.0)
    return sorted(by_key.values(), key=lambda h: h.score_fused, reverse=True)


def _cfg_num(cfg: Any, name: str, default: float) -> float:
    """Read a numeric cfg field, falling back to ``default`` ONLY when unset.

    The common ``float(getattr(cfg, name, default) or default)`` idiom silently
    rewrites an explicit ``0`` — a meaningful "disable this stage" value — back
    to the default, so a zero weight could never take effect. This helper keeps
    ``0`` while preserving the old behaviour for a missing / ``None`` field.
    """
    value = getattr(cfg, name, None)
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def finalize_hits(
    fused_list: list[FuseHit],
    *,
    question: str,
    cfg: Settings,
    top_k: int,
    enable_rerank: bool,
    embedder: Any = None,
    reranker: Any = None,
    metrics: Any = None,
    trace: dict | None = None,
    deadline: float | None = None,
) -> list[FuseHit]:
    """Post-fusion quality (dedup/adaptive budget/unit order) + optional semantic rerank.

    Shared by the flat pipeline and the LangGraph debug graph to eliminate drift
    in chunk budget, overlap dedup, rule/embed/cross-encoder rerank ordering.

    `trace` (optional) records the distinct unit_ids after each internal step
    under a ``fin.*`` key — offline attribution only, no-op when None.
    """
    metrics = metrics or get_metrics()
    fused_list = dedup_overlapping_chunks(
        fused_list,
        float(getattr(cfg, "retrieve_chunk_dedup_jaccard", 0.85) or 0.85),
    )
    _trace_units(trace, "fin.dedup", fused_list)
    budget = adaptive_max_per_unit(
        fused_list,
        int(cfg.retrieve_max_per_unit),
        int(getattr(cfg, "retrieve_max_per_unit_cap", 8) or 8),
        top_k,
    )
    if getattr(cfg, "retrieve_unit_agg", False):
        fused_list = unit_representative_order(fused_list, **_unit_agg_kwargs(cfg))
        _trace_units(trace, "fin.agg", fused_list)
    # Intra-unit keyword ordering is independent of unit interleaving: it only
    # rotates chunks *within* a unit (unit order preserved), so it is safe to run
    # unconditionally. Without it a generic heading/summary chunk can outrank the
    # chunk that actually carries the answer, and the per-unit budget then
    # truncates the answer out of top-k.
    fused_list = keyword_aware_unit_order(fused_list, question)
    _trace_units(trace, "fin.kw", fused_list)
    fused_list = apply_max_per_unit(fused_list, budget)
    _trace_units(trace, "fin.budget", fused_list)
    if enable_rerank:
        fused_list = rule_rerank(
            fused_list,
            question,
            exact_weight=_cfg_num(cfg, "retrieve_rule_rerank_exact_weight", 0.15),
            bigram_weight=_cfg_num(cfg, "retrieve_rule_rerank_bigram_weight", 0.01),
            overlap_weight=_cfg_num(cfg, "retrieve_rule_rerank_overlap_weight", 1e-3),
        )
        _trace_units(trace, "fin.rule", fused_list)
        if getattr(cfg, "retrieve_unit_agg", False):
            fused_list = unit_representative_order(fused_list, **_unit_agg_kwargs(cfg))
        fused_list = keyword_aware_unit_order(fused_list, question)
        _trace_units(trace, "fin.kw2", fused_list)
        fused_list = apply_max_per_unit(fused_list, budget)
    if getattr(cfg, "retrieve_embed_rerank", False) and embedder is not None:
        embed_rerank_t0 = time.monotonic()
        try:
            qvec = _encode_question_cached(
                question=question, embedder=embedder, cfg=cfg, metrics=metrics, deadline=deadline
            )
            fused_list = embed_rerank(
                fused_list,
                question_vec=qvec,
                embedder=embedder,
                top_n=int(getattr(cfg, "retrieve_embed_rerank_top", 20) or 20),
                weight=_cfg_num(cfg, "retrieve_embed_rerank_weight", 0.35),
            )
            fused_list = apply_max_per_unit(fused_list, budget)
            _trace_units(trace, "fin.embed", fused_list)
            metrics.inc("retrieve.embed_rerank_applied")
        except Exception:
            logger.warning("embed_rerank skipped", exc_info=True)
        finally:
            # Independent stage metric: only emitted when the stage actually runs
            # (embed_rerank enabled + embedder present), so a disabled stage does
            # not pollute latency profiling.
            metrics.observe_ms(
                "retrieve.stage_embed_rerank_ms", (time.monotonic() - embed_rerank_t0) * 1000
            )
    if reranker is not None and getattr(cfg, "retrieve_reranker_enabled", False):
        cross_encoder_t0 = time.monotonic()
        try:
            unit_only = bool(getattr(cfg, "retrieve_reranker_unit_only", True))

            def _run_cross_encoder() -> list[FuseHit]:
                if unit_only:
                    from ai.retrieve.reranker import cross_encoder_unit_rerank

                    # Cross-encoder affects *unit selection only*: it reorders whole
                    # units but leaves each unit's internal chunk order untouched, so
                    # the semantic signal lifts unit_recall without scrambling the
                    # chunk order that drives chunk_hit / ndcg. See §13.
                    return cross_encoder_unit_rerank(
                        fused_list,
                        question=question,
                        reranker=reranker,
                        top_n=int(getattr(cfg, "retrieve_reranker_top_n", 20) or 20),
                        weight=_cfg_num(cfg, "retrieve_reranker_weight", 0.4),
                        regroup=bool(getattr(cfg, "retrieve_reranker_regroup", False)),
                        blend=str(getattr(cfg, "retrieve_reranker_blend", "score") or "score"),
                        rank_k=int(getattr(cfg, "retrieve_reranker_rank_k", 60) or 60),
                        **_unit_agg_kwargs(cfg),
                    )
                from ai.retrieve.reranker import cross_encoder_rerank

                return cross_encoder_rerank(
                    fused_list,
                    question=question,
                    reranker=reranker,
                    top_n=int(getattr(cfg, "retrieve_reranker_top_n", 20) or 20),
                    weight=_cfg_num(cfg, "retrieve_reranker_weight", 0.4),
                )

            # R-04: the cross-encoder is a non-cancellable external call; bound the
            # *wait* by the remaining request budget (degrade to embed_rerank order
            # on timeout instead of overrunning the deadline).
            fused_list = _run_bounded(
                _run_cross_encoder,
                cfg=cfg,
                deadline=deadline,
                timeout_sec=float(getattr(cfg, "retrieve_rerank_timeout_sec", 0.0) or 0.0),
                metrics=metrics,
                timeout_metric="retrieve.rerank_timeout",
            )
            # NOTE: deliberately do NOT re-run `keyword_aware_unit_order` here.
            # The cross-encoder is a stronger *semantic* signal than the lexical
            # keyword ordering, so overriding it degrades chunk_hit@5
            # (measured: 0.9647 -> 0.9412 on the 85-item gold set). The lexical
            # re-order is correct after `rule_rerank` (also lexical) but wrong
            # after a semantic reranker. See docs/retrieval-gate-decision.md §12.
            fused_list = apply_max_per_unit(fused_list, budget)
            _trace_units(trace, "fin.ce", fused_list)
            metrics.inc("retrieve.cross_rerank_applied")
        except Exception:
            logger.warning("cross-encoder rerank failed; keeping embed_rerank order", exc_info=True)
        finally:
            # Independent stage metric: emitted only when the cross-encoder runs
            # (reranker present + enabled), never when it is skipped.
            metrics.observe_ms(
                "retrieve.stage_cross_encoder_ms", (time.monotonic() - cross_encoder_t0) * 1000
            )
    if getattr(cfg, "retrieve_unit_interleave", False):
        # Final structural pass: round-robin across units so any top-k prefix
        # spans several units. Applied last (after all rerank stages) and it
        # preserves unit order + intra-unit order, so it cannot undo a reranker's
        # chunk ordering — it only prevents one unit from monopolising top-k.
        fused_list = interleave_units(
            fused_list, block=int(getattr(cfg, "retrieve_unit_interleave_block", 1) or 1)
        )
    _trace_units(trace, "fin.interleave", fused_list)
    return fused_list


def pack_response(hits: list[FuseHit], top_k: int) -> list[dict]:
    out: list[dict] = []
    for h in hits[: max(1, top_k)]:
        out.append(
            {
                "unit_id": h.unit_id,
                "chunk_id": h.chunk_id,
                "score": float(h.score_fused),
                "content": h.content,
                "version_id": h.version_id or None,
                "heading_path": list(h.heading_path or []),
                "score_dense": h.score_dense,
                "score_lexical": h.score_lexical,
                "score_sparse": h.score_sparse,
                "score_rewrite": h.score_rewrite,
                "score_fused": float(h.score_fused),
                "retrieval_paths": list(h.retrieval_paths),
            }
        )
    return out


def _sync_lexical_from_store(
    *,
    lexical: LexicalIndex,
    store: VectorStore,
    limit: int | None,
    timeout_sec: float,
    cfg: Settings,
    circuits: Any,
    metrics: Any,
    deadline: float | None = None,
) -> int:
    """Cold-start sync with wall-clock budget. Raises RetrieveError on timeout when degrade off.

    R-01: runs on the dedicated sync pool (isolated from live search) behind a
    bounded submission queue; the wait is ``min(timeout_sec, remaining deadline)``
    and saturation fails fast (503) instead of queueing unbounded.
    """
    t0 = time.monotonic()

    def _run() -> int:
        return lexical.sync_from_store(store, limit=limit)

    wait = _effective_timeout(timeout_sec, deadline)
    if wait is None:
        try:
            return _run()
        except Exception as exc:
            metrics.inc("retrieve.fail")
            raise RetrieveError(f"lexical sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc
    if wait <= 0:
        metrics.observe_ms("retrieve.lexical_sync_ms", (time.monotonic() - t0) * 1000)
        raise RetrieveError(
            "lexical sync exceeded overall retrieve deadline", "RETRIEVE_TIMEOUT", 504
        )

    pool_size = max(1, int(getattr(cfg, "retrieve_sync_pool_size", 2) or 2))
    queue_max = max(0, int(getattr(cfg, "retrieve_store_queue_max", 0) or 0))
    pool = get_sync_pool(pool_size)
    sem, _ = _get_sync_submit_semaphore(pool_size + queue_max)
    if sem is not None and not sem.acquire(blocking=False):
        metrics.inc("retrieve.sync_overloaded")
        raise RetrieveError("retrieve sync pool saturated", "RETRIEVE_STORE_OVERLOADED", 503)
    try:
        fut = pool.submit(_run)
    except Exception:
        if sem is not None:
            sem.release()
        raise
    if sem is not None:
        fut.add_done_callback(lambda _f: sem.release())
    try:
        count = fut.result(timeout=wait)
    except FuturesTimeoutError as exc:
        fut.cancel()
        metrics.observe_ms("retrieve.lexical_sync_ms", (time.monotonic() - t0) * 1000)
        raise RetrieveError(
            f"lexical sync exceeded retrieve budget ({wait:.3f}s)",
            "RETRIEVE_TIMEOUT",
            504,
        ) from exc
    except Exception as exc:
        raise RetrieveError(f"lexical sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc
    metrics.observe_ms("retrieve.lexical_sync_ms", (time.monotonic() - t0) * 1000)
    return count


def _sync_sparse_from_store(
    *,
    sparse: Any,
    store: VectorStore,
    embedder: Any,
    limit: int | None,
    timeout_sec: float,
    cfg: Settings,
    circuits: Any,
    metrics: Any,
    deadline: float | None = None,
) -> int:
    """Cold-start sparse sync with wall-clock budget.

    Mirrors `_sync_lexical_from_store`: raises RetrieveError on timeout so the
    caller can catch it and degrade (it must NOT bubble up as an HTTP 504). The
    pool task itself cannot be cancelled, so this path is opt-in via
    `retrieve_sparse_sync_in_request`. R-01: same bounded sync pool + deadline.
    """
    t0 = time.monotonic()

    def _run() -> int:
        return sparse.sync_from_store(store, embedder, limit=limit)

    wait = _effective_timeout(timeout_sec, deadline)
    if wait is None:
        try:
            count = _run()
        except Exception as exc:
            metrics.inc("retrieve.fail")
            raise RetrieveError(f"sparse sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc
        metrics.observe_ms("retrieve.sparse_sync_ms", (time.monotonic() - t0) * 1000)
        return count
    if wait <= 0:
        metrics.observe_ms("retrieve.sparse_sync_ms", (time.monotonic() - t0) * 1000)
        raise RetrieveError(
            "sparse sync exceeded overall retrieve deadline", "RETRIEVE_TIMEOUT", 504
        )

    pool_size = max(1, int(getattr(cfg, "retrieve_sync_pool_size", 2) or 2))
    queue_max = max(0, int(getattr(cfg, "retrieve_store_queue_max", 0) or 0))
    pool = get_sync_pool(pool_size)
    sem, _ = _get_sync_submit_semaphore(pool_size + queue_max)
    if sem is not None and not sem.acquire(blocking=False):
        metrics.inc("retrieve.sync_overloaded")
        raise RetrieveError("retrieve sync pool saturated", "RETRIEVE_STORE_OVERLOADED", 503)
    try:
        fut = pool.submit(_run)
    except Exception:
        if sem is not None:
            sem.release()
        raise
    if sem is not None:
        fut.add_done_callback(lambda _f: sem.release())
    try:
        count = fut.result(timeout=wait)
    except FuturesTimeoutError as exc:
        fut.cancel()
        metrics.observe_ms("retrieve.sparse_sync_ms", (time.monotonic() - t0) * 1000)
        raise RetrieveError(
            f"sparse sync exceeded retrieve budget ({wait:.3f}s)",
            "RETRIEVE_TIMEOUT",
            504,
        ) from exc
    except Exception as exc:
        raise RetrieveError(f"sparse sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc
    metrics.observe_ms("retrieve.sparse_sync_ms", (time.monotonic() - t0) * 1000)
    return count


def index_usable(idx: Any) -> bool:
    """Whether a process-local index can serve this request.

    Usable when it has completed a full sync (`is_ready`) OR already holds
    documents from incremental ingest. Only a genuinely empty, never-synced
    index (the cold-start case) is "not usable" — the retrieve path then skips
    it instead of triggering an unbounded in-request rebuild.
    """
    try:
        if bool(getattr(idx, "is_ready", lambda: False)()):
            return True
        return int(idx.doc_count()) > 0
    except Exception:
        # Unknown index shape: assume usable (never block retrieval on this).
        return True


def _trace_units(trace: dict | None, stage: str, hits: list[Any]) -> None:
    """Record the distinct unit_ids seen at `stage` (offline attribution only).

    No-op unless the caller passes a trace dict, so production behaviour is
    unchanged. Only unit ids (no content) are recorded — safe for offline eval.
    """
    if trace is None:
        return
    seen: list[str] = []
    for h in hits or []:
        uid = str(h.get("unit_id") if isinstance(h, dict) else getattr(h, "unit_id", "") or "")
        if uid and uid not in seen:
            seen.append(uid)
    trace[stage] = seen


def run_retrieve_pipeline(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    cfg: Settings,
    force_lexical_error: Callable[[], None] | None = None,
    sparse: Any = None,
    reranker: Any = None,
    query_rewriter: Any = None,
    session_rewriter: Any = None,
    trace: dict | None = None,
) -> dict:
    """Full hybrid retrieve (dense + lexical + optional sparse + rerank). Returns packed dict."""
    metrics = get_metrics()
    metrics.inc("retrieve.total_requests")
    started_outer = time.monotonic()
    q_hash_outer = _question_hash_prefix(request.question or "")

    # QPS gate (process-local token bucket).
    if not _try_acquire_qps(float(cfg.retrieve_qps_limit or 0)):
        metrics.inc("retrieve.rate_limited")
        err = RetrieveError("retrieve QPS limit exceeded", "RATE_LIMIT", 429)
        if cfg.retrieve_struct_log:
            _emit_struct_log(
                _struct_base(
                    event="retrieve_error",
                    q_hash=q_hash_outer,
                    took_ms=int((time.monotonic() - started_outer) * 1000),
                    error_code=err.error_code,
                    status_code=err.status_code,
                )
            )
        raise err

    bulkhead = _get_bulkhead(int(cfg.retrieve_max_concurrency or 0))
    if bulkhead is not None and not bulkhead.acquire(blocking=False):
        metrics.inc("retrieve.rejected_concurrency")
        err = RetrieveError("retrieve concurrency bulkhead full", "RATE_LIMIT", 429)
        if cfg.retrieve_struct_log:
            _emit_struct_log(
                _struct_base(
                    event="retrieve_error",
                    q_hash=q_hash_outer,
                    took_ms=int((time.monotonic() - started_outer) * 1000),
                    error_code=err.error_code,
                    status_code=err.status_code,
                )
            )
        raise err
    try:
        return _run_retrieve_pipeline_inner(
            request=request,
            embedder=embedder,
            store=store,
            lexical=lexical,
            cfg=cfg,
            force_lexical_error=force_lexical_error,
            metrics=metrics,
            sparse=sparse,
            reranker=reranker,
            query_rewriter=query_rewriter,
            session_rewriter=session_rewriter,
            trace=trace,
        )
    except RetrieveError as exc:
        if cfg.retrieve_struct_log:
            _emit_struct_log(
                _struct_base(
                    event="retrieve_error",
                    q_hash=q_hash_outer,
                    took_ms=int((time.monotonic() - started_outer) * 1000),
                    error_code=exc.error_code,
                    status_code=exc.status_code,
                )
            )
        raise
    finally:
        if bulkhead is not None:
            bulkhead.release()


@dataclass
class _RetrieveContext:
    """Request-scoped context built by the setup stage (stage 1).

    Carries the normalized question, the rewrite/rerank variants, the ACL
    filter and the hybrid/overfetch/rerank switches between stages. Passing it
    explicitly keeps the stage functions free of module-level mutable state, so
    concurrent requests can never bleed into one another.
    """

    question: str
    rewrite_question: str | None
    rerank_question: str
    top_k: int
    flt: SearchFilter | None
    acl_applied: bool
    hybrid_on: bool
    overfetch: int
    enable_rerank: bool
    q_hash: str
    use_lexical: bool
    use_sparse: bool


@dataclass
class _SearchResult:
    """Concurrent search output (stage 2) plus the per-stage wall-clock timings."""

    dense_hits: list[dict]
    lexical_hits: list[dict]
    sparse_hits: list[dict]
    degraded_to_dense: bool
    use_lexical: bool
    stage_embed_ms: float
    stage_store_ms: float
    stage_lexical_ms: float


def _stage_build_request_context(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    cfg: Settings,
    metrics: Any,
    session_rewriter: Any,
    query_rewriter: Any,
    deadline: float | None = None,
) -> _RetrieveContext:
    """Stage 1 — normalize/validate the request and build the request context.

    Question sanitization, session/query rewrite, rerank anchor expansion, ACL
    filter construction and the hybrid/overfetch/rerank switches. Pure setup (no
    branch I/O), so it is safe to run before any search branch is scheduled.

    R-04: the LLM query rewrite is a non-cancellable external call, so it runs
    behind the bounded isolation pool with a wait capped by the remaining
    request budget (falling back to the original question on timeout).
    """
    question = normalize_question(request.question, cfg.retrieve_max_question_chars)
    rewrite_question: str | None = None
    # Session-history rewrite (指代消解). The callable is built by the caller AFTER
    # reading Mongo and applying ACL filtering, so unauthorized turns never reach here.
    # The ORIGINAL question stays the main path (extra-recall path only), and any
    # failure silently falls back to the original query.
    #
    # R-04: this is an LLM call inside the request-context stage — a non-cancellable
    # external call. Route it through the bounded isolation pool so its wait is
    # clamped by the remaining request budget (fallback to the original question on
    # timeout/overload), instead of being bounded only by its own LLM timeout.
    if session_rewriter is not None:
        try:
            session_query = _run_bounded(
                lambda: session_rewriter(question),
                cfg=cfg,
                deadline=deadline,
                timeout_sec=float(getattr(cfg, "retrieve_session_rewrite_timeout_sec", 0.0) or 0.0),
                metrics=metrics,
                timeout_metric="retrieve.session_rewrite_timeout",
            )
        except RetrieveError:
            logger.warning("session rewrite skipped (budget/overload); using original question")
            session_query = None
        except Exception:
            logger.warning("session rewrite failed; using original question", exc_info=True)
            session_query = None
        if session_query and session_query != question:
            rewrite_question = session_query
    if query_rewriter is not None:
        from ai.retrieve.query_rewrite import rewrite_question_cached

        try:
            rewritten = _run_bounded(
                lambda: rewrite_question_cached(
                    question,
                    rewriter=query_rewriter,
                    max_size=int(getattr(cfg, "retrieve_query_rewrite_cache_max", 0) or 0),
                ),
                cfg=cfg,
                deadline=deadline,
                timeout_sec=float(getattr(cfg, "retrieve_query_rewrite_timeout_sec", 0.0) or 0.0),
                metrics=metrics,
                timeout_metric="retrieve.query_rewrite_timeout",
            )
        except RetrieveError:
            # Timeout/overload → fall back to the original question (the rewrite
            # is an optional enhancement; the metric is already recorded).
            logger.warning("query rewrite skipped (budget/overload); using original question")
            rewritten = question
        if getattr(cfg, "retrieve_query_rewrite_extra_path", False):
            # Extra-recall mode: the ORIGINAL question stays the main path; the
            # rewritten query becomes an *additional* RRF ranking (see
            # fuse_hybrid_hits / `retrieve_query_rewrite_rrf_weight`). This is the
            # structural fix for the §15 failure (replace weakened chunk recall).
            rewrite_question = rewritten if rewritten != question else None
        else:
            # Legacy replace mode (§15): the rewritten query overwrites the original.
            question = rewritten

    # Optional: anchor the rerank stage (rule_rerank + intra-unit keyword order) on
    # the phenomenon->error-code expansions. Only the codes (E-01..E-08) are added,
    # and they exist solely in the error-code reference doc, so this cannot promote
    # an unrelated product manual — it is a high-precision exact-token anchor.
    rerank_question = question
    if getattr(cfg, "retrieve_query_expand_codes_rerank", False) and getattr(
        cfg, "retrieve_query_expand", True
    ):
        try:
            from ai.retrieve.query_expand import expand_codes

            codes = expand_codes(question)
            if codes:
                rerank_question = question + " " + " ".join(codes)
                metrics.inc("retrieve.query_expand_codes_rerank")
        except Exception:
            rerank_question = question
    top_k = int(request.top_k or cfg.retrieve_top_k_default)
    max_unit_ids = int(cfg.retrieve_max_unit_ids or 0)
    flt, acl_applied = build_acl_filter(
        request.acl_filter,
        request.unit_ids,
        bool(cfg.retrieve_require_acl),
        max_unit_ids=max_unit_ids,
    )

    hybrid_on = (
        cfg.retrieve_hybrid_enabled
        if request.enable_hybrid is None
        else bool(request.enable_hybrid)
    )
    if cfg.retrieve_multi_replica_safe:
        hybrid_on = False
        with _multi_replica_log_lock:
            global _multi_replica_logged
            if not _multi_replica_logged:
                _multi_replica_logged = True
                logger.warning(
                    "retrieve_multi_replica_safe=true: dense-only path "
                    "(lexical + sparse disabled; process-local indexes are not shared)"
                )
        metrics.inc("retrieve.multi_replica_dense_only")

    overfetch = request.overfetch or min(
        top_k * cfg.retrieve_overfetch_factor, cfg.retrieve_overfetch_max
    )
    overfetch = max(top_k, int(overfetch))
    enable_rerank = (
        cfg.retrieve_enable_rerank if request.enable_rerank is None else bool(request.enable_rerank)
    )
    q_hash = _question_hash_prefix(question)

    use_lexical = hybrid_on
    # R-06: the sparse index is process-local too, so multi-replica safe mode must
    # disable it alongside lexical (previously only `hybrid_on` was forced off,
    # leaving sparse enabled and silently divergent across replicas).
    use_sparse = (
        bool(getattr(cfg, "retrieve_sparse_enabled", False))
        and getattr(embedder, "supports_sparse", False)
        and not bool(getattr(cfg, "retrieve_multi_replica_safe", False))
    )
    return _RetrieveContext(
        question=question,
        rewrite_question=rewrite_question,
        rerank_question=rerank_question,
        top_k=top_k,
        flt=flt,
        acl_applied=acl_applied,
        hybrid_on=hybrid_on,
        overfetch=overfetch,
        enable_rerank=enable_rerank,
        q_hash=q_hash,
        use_lexical=use_lexical,
        use_sparse=use_sparse,
    )


def _stage_parallel_search(
    *,
    ctx: _RetrieveContext,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    sparse: Any,
    cfg: Settings,
    force_lexical_error: Callable[[], None] | None,
    circuits: Any,
    metrics: Any,
    check_budget: Callable[..., bool],
    trace: dict | None,
    deadline: float | None = None,
) -> _SearchResult:
    """Stage 2 — run the three retrieval paths concurrently and collect them.

    The dense path (embed + vector-store I/O, the dominant latency) runs on the
    caller thread so its hard-failure / circuit / budget semantics stay
    byte-for-byte unchanged; the optional lexical (CPU) and sparse (CPU/IO)
    paths run on the branch pool. Each branch keeps its own try/except
    degradation, so a single-path failure behaves exactly as before.
    """
    question = ctx.question
    overfetch = ctx.overfetch
    flt = ctx.flt
    q_hash = ctx.q_hash
    use_lexical = ctx.use_lexical
    use_sparse = ctx.use_sparse
    degraded_to_dense = False

    stage_embed_ms = 0.0
    stage_store_ms = 0.0
    stage_lexical_ms = 0.0

    # ---- dense path (required; runs on the caller thread so its hard-failure,
    # circuit and budget semantics are byte-for-byte unchanged) ----
    def _dense_branch() -> list[dict]:
        nonlocal stage_embed_ms, stage_store_ms
        check_budget("dense_start")
        if circuits.is_open("embed"):
            metrics.inc("retrieve.fail")
            raise RetrieveError("embed circuit open", "EMBED_CIRCUIT_OPEN", 503)
        embed_t0 = time.monotonic()
        try:
            query_texts = [question]
            if getattr(cfg, "retrieve_query_expand", True):
                try:
                    query_texts = _expand_query_texts_cached(
                        question,
                        enable_codes=bool(getattr(cfg, "retrieve_query_expand_codes", False)),
                        cfg=cfg,
                    )
                    if len(query_texts) > 1:
                        metrics.inc("retrieve.query_expanded")
                except Exception:
                    query_texts = [question]
            merged: dict[str, dict] = {}
            store_ms = 0.0
            for qi, qtext in enumerate(query_texts):
                qvec = _encode_question_cached(
                    question=qtext,
                    embedder=embedder,
                    cfg=cfg,
                    metrics=metrics,
                    deadline=deadline,
                )
                check_budget("dense_search")
                hits_i, sm_i = _search_store_with_resilience(
                    store=store,
                    qvec=qvec,
                    top_k=overfetch,
                    flt=flt,
                    cfg=cfg,
                    circuits=circuits,
                    metrics=metrics,
                    deadline=deadline,
                )
                store_ms += sm_i
                for row in hits_i:
                    key = _hit_key(
                        str(row.get("unit_id") or ""),
                        str(row.get("id") or ""),
                        str(row.get("version_id") or ""),
                    )
                    prev = merged.get(key)
                    if prev is None or float(row.get("score") or 0) > float(prev.get("score") or 0):
                        merged[key] = row
                if qi == 0:
                    stage_embed_ms = (time.monotonic() - embed_t0) * 1000
            dense_hits = sorted(
                merged.values(), key=lambda r: float(r.get("score") or 0), reverse=True
            )[: max(1, overfetch)]
            _trace_units(trace, "dense", dense_hits)
            stage_store_ms = store_ms
            stage_embed_ms = (time.monotonic() - embed_t0) * 1000
            metrics.observe_ms("retrieve.stage_embed_ms", stage_embed_ms)
            metrics.observe_ms("retrieve.stage_store_ms", stage_store_ms)
            circuits.record_success("embed")
            return dense_hits
        except RetrieveError:
            metrics.inc("retrieve.fail")
            raise
        except Exception as exc:
            circuits.record_failure("embed", str(exc))
            metrics.inc("retrieve.fail")
            raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc

    # ---- lexical path (optional). Returns (hits, degraded, disabled):
    #   degraded -> set degraded_to_dense; disabled -> drop the lexical path
    #   (skip-when-over-cap), exactly mirroring the former inline logic. ----
    def _lexical_branch() -> tuple[list[dict], bool, bool]:
        nonlocal stage_lexical_ms
        lex_t0 = time.monotonic()
        lex_degraded = False
        lex_disabled = False
        hits: list[dict] = []
        try:
            if force_lexical_error is not None:
                force_lexical_error()
            sync_limit = int(getattr(cfg, "retrieve_lexical_sync_limit", 0) or 0) or None
            if not index_usable(lexical):
                sync_timeout = float(cfg.retrieve_lexical_sync_timeout_sec or 0)
                if sync_timeout <= 0:
                    sync_timeout = float(cfg.retrieve_store_timeout_sec or 0)
                # R-09: never let the cold-start rebuild run past the request's
                # overall deadline (cap by remaining budget).
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RetrieveError(
                            "lexical sync exceeded overall retrieve deadline",
                            "RETRIEVE_TIMEOUT",
                            504,
                        )
                    if sync_timeout <= 0 or remaining < sync_timeout:
                        sync_timeout = remaining
                _sync_lexical_from_store(
                    lexical=lexical,
                    store=store,
                    limit=sync_limit,
                    timeout_sec=sync_timeout,
                    cfg=cfg,
                    circuits=circuits,
                    metrics=metrics,
                    deadline=deadline,
                )
            max_docs = int(cfg.retrieve_lexical_max_docs or 0) or None
            lex_q = question
            if getattr(cfg, "retrieve_query_expand", True):
                try:
                    lex_q = _expand_terms_for_lexical_cached(
                        question,
                        enable_codes=bool(getattr(cfg, "retrieve_query_expand_codes", False)),
                        cfg=cfg,
                    )
                except Exception:
                    lex_q = question
            doc_n = lexical.doc_count()
            if max_docs and doc_n > max_docs:
                metrics.inc("retrieve.lexical_capped")
                if cfg.retrieve_lexical_skip_when_over_cap:
                    lex_disabled = True
                    lex_degraded = True
                    metrics.inc("retrieve.hybrid_degraded")
                    hits = []
                else:
                    hits = lexical.search(
                        lex_q,
                        top_k=overfetch,
                        filter=flt,
                        max_docs=max_docs,
                    )
                    metrics.inc("retrieve.lexical_hits", len(hits))
            else:
                hits = lexical.search(
                    lex_q,
                    top_k=overfetch,
                    filter=flt,
                    max_docs=max_docs,
                )
                metrics.inc("retrieve.lexical_hits", len(hits))
        except Exception as exc:
            logger.warning("lexical search failed q_hash=%s: %s", q_hash, exc)
            if cfg.retrieve_hybrid_degrade_to_dense:
                lex_degraded = True
                metrics.inc("retrieve.hybrid_degraded")
                hits = []
            else:
                metrics.inc("retrieve.fail")
                if isinstance(exc, RetrieveError):
                    raise
                raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc
        finally:
            stage_lexical_ms = (time.monotonic() - lex_t0) * 1000
            metrics.observe_ms("retrieve.stage_lexical_ms", stage_lexical_ms)
        return hits, lex_degraded, lex_disabled

    # ---- sparse path (BGE-M3 lexical_weights); auto-degrades when unsupported
    # or not ready. Never raises: it always degrades to an empty hit list. ----
    def _sparse_branch() -> list[dict]:
        hits: list[dict] = []
        if check_budget("sparse", optional=True):
            metrics.inc("retrieve.sparse_degraded")
            metrics.inc("retrieve.sparse_degraded_budget")
            return []
        sparse_t0 = time.monotonic()
        try:
            sparse_usable = index_usable(sparse)
            skip_when_not_ready = bool(getattr(cfg, "retrieve_sparse_skip_when_not_ready", True))
            sync_in_request = bool(getattr(cfg, "retrieve_sparse_sync_in_request", False))
            if not sparse_usable and (sync_in_request or not skip_when_not_ready):
                # Opt-in bounded in-request rebuild (legacy rollback path). The
                # default path never rebuilds inside a request — it degrades.
                sync_timeout = float(getattr(cfg, "retrieve_sparse_sync_timeout_sec", 0) or 0)
                if sync_timeout <= 0:
                    sync_timeout = float(cfg.retrieve_store_timeout_sec or 0)
                _sync_sparse_from_store(
                    sparse=sparse,
                    store=store,
                    embedder=embedder,
                    limit=int(getattr(cfg, "retrieve_sparse_max_docs", 0) or 0) or None,
                    timeout_sec=sync_timeout,
                    cfg=cfg,
                    circuits=circuits,
                    metrics=metrics,
                    deadline=deadline,
                )
                sparse_usable = index_usable(sparse)
            if not sparse_usable:
                # Cold-start default: the index is built by preheat, not here.
                # Skip sparse (dense+lexical still serve) instead of blocking.
                metrics.inc("retrieve.sparse_degraded")
                metrics.inc("retrieve.sparse_degraded_not_ready")
                hits = []
            else:
                q_sparse = _encode_sparse_question_cached(
                    question=question,
                    embedder=embedder,
                    cfg=cfg,
                    metrics=metrics,
                    deadline=deadline,
                )
                if q_sparse:
                    hits = sparse.search(
                        q_sparse,
                        top_k=overfetch,
                        filter=flt,
                        max_docs=int(getattr(cfg, "retrieve_sparse_max_docs", 0) or 0) or None,
                    )
                metrics.inc("retrieve.sparse_hits", len(hits))
        except RetrieveError:
            # Raised only by the bounded in-request sync on timeout: degrade.
            metrics.inc("retrieve.sparse_degraded")
            metrics.inc("retrieve.sparse_degraded_timeout")
            hits = []
        except Exception as exc:
            logger.warning("sparse search failed q_hash=%s: %s", q_hash, exc)
            metrics.inc("retrieve.sparse_degraded")
            metrics.inc("retrieve.sparse_degraded_error")
            hits = []
        # Independent sparse-recall stage metric (previously unmeasured; sparse
        # latency used to be folded into `retrieve.stage_fuse_ms`).
        metrics.observe_ms("retrieve.stage_sparse_ms", (time.monotonic() - sparse_t0) * 1000)
        return hits

    # ---- schedule the optional branches concurrently with the dense path (新-4) ----
    # Lexical (CPU) + sparse (CPU/IO) run on the branch pool while the dense path
    # (embed + vector-store I/O, the dominant latency) runs on the caller thread;
    # this overlaps the network wait with the in-process work. Each branch keeps
    # its own try/except degradation, so a single-path failure behaves as before.
    pool = get_branch_pool()
    queue_max = int(getattr(cfg, "retrieve_branch_queue_max", 0) or 0)
    branch_sem = _get_branch_submit_semaphore(
        _BRANCH_POOL_WORKERS + queue_max if queue_max > 0 else 0
    )

    def _submit_branch(fn: Callable[[], Any]) -> Any:
        """Submit to the branch pool behind a bounded queue (R-09).

        When the pool + queue are full the branch is *not* scheduled: the caller
        degrades (dense still serves) instead of letting the executor's unbounded
        internal queue grow. The slot is released by the future's completion
        callback, so a still-running task keeps holding its capacity.
        """
        if branch_sem is not None and not branch_sem.acquire(blocking=False):
            metrics.inc("retrieve.branch_overloaded")
            return None
        try:
            fut = pool.submit(fn)
        except Exception:
            if branch_sem is not None:
                branch_sem.release()
            raise
        if branch_sem is not None:
            fut.add_done_callback(lambda _f: branch_sem.release())
        return fut

    lex_fut = _submit_branch(_lexical_branch) if use_lexical else None
    if use_lexical and lex_fut is None:
        # Branch queue full: degrade to dense rather than pile up (R-09).
        degraded_to_dense = True
        use_lexical = False
        metrics.inc("retrieve.hybrid_degraded")
    sparse_fut = _submit_branch(_sparse_branch) if (use_sparse and sparse is not None) else None
    if use_sparse and sparse is not None and sparse_fut is None:
        metrics.inc("retrieve.sparse_degraded")
        metrics.inc("retrieve.sparse_degraded_overload")

    dense_hits = _dense_branch()
    metrics.inc("retrieve.dense_hits", len(dense_hits))

    # Budget gate after dense: unconditional (fires even when hybrid is off),
    # matching the former sequential position exactly.
    check_budget("lexical_start")

    def _remaining() -> float | None:
        """Seconds left before the request's absolute deadline (None = unbounded)."""
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    # Collect the optional branches bounded by the *absolute* deadline (R-09).
    # Sparse first: its branch never raises (it always degrades), whereas the
    # lexical branch may raise RetrieveError when degrade-to-dense is disabled —
    # collect sparse before propagating that.
    sparse_hits: list[dict] = []
    if sparse_fut is not None:
        try:
            sparse_hits = sparse_fut.result(timeout=_remaining())
        except FuturesTimeoutError:
            # The branch thread cannot be killed; drop the wait and degrade.
            sparse_fut.cancel()
            metrics.inc("retrieve.sparse_degraded")
            metrics.inc("retrieve.sparse_degraded_deadline")
            sparse_hits = []

    lexical_hits: list[dict] = []
    if lex_fut is not None:
        try:
            lexical_hits, lex_degraded, lex_disabled = lex_fut.result(timeout=_remaining())
        except FuturesTimeoutError as exc:
            lex_fut.cancel()
            metrics.inc("retrieve.lexical_deadline_exceeded")
            # The overall-budget degrade toggle owns the 504 contract (default
            # off preserves it); lexical's own degrade-to-dense applies only when
            # the *branch* errors, not when the shared deadline is hit.
            if bool(getattr(cfg, "retrieve_overall_timeout_degrade", False)):
                degraded_to_dense = True
                metrics.inc("retrieve.budget_degraded")
                metrics.inc("retrieve.hybrid_degraded")
            else:
                metrics.inc("retrieve.timeout")
                raise RetrieveError(
                    "retrieve exceeded overall deadline waiting for lexical branch",
                    "RETRIEVE_TIMEOUT",
                    504,
                ) from exc
        else:
            if lex_degraded:
                degraded_to_dense = True
            if lex_disabled:
                use_lexical = False

    return _SearchResult(
        dense_hits=dense_hits,
        lexical_hits=lexical_hits,
        sparse_hits=sparse_hits,
        degraded_to_dense=degraded_to_dense,
        use_lexical=use_lexical,
        stage_embed_ms=stage_embed_ms,
        stage_store_ms=stage_store_ms,
        stage_lexical_ms=stage_lexical_ms,
    )


def _stage_fuse_and_rank(
    *,
    ctx: _RetrieveContext,
    search: _SearchResult,
    acl_filter: AclFilter | None,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    sparse: Any,
    cfg: Settings,
    circuits: Any,
    metrics: Any,
    check_budget: Callable[..., bool],
    reranker: Any,
    trace: dict | None,
    deadline: float | None = None,
) -> tuple[list[FuseHit], int]:
    """Stage 3 — RRF fuse (incl. the rewrite extra-recall path), the fail-closed
    ACL post-filter and the final ranking / per-unit crop.

    `fuse_hybrid_hits` merges dense/lexical/sparse (+ the rewritten-query extra
    path) into a scored `FuseHit` list; `post_filter_acl` then drops rows the
    request may not see (fail-closed); `finalize_hits` applies overlap dedup,
    per-unit budget, rule/embed/cross-encoder rerank and interleave. Returns the
    ranked list and the number of ACL-filtered-out rows (for logging only).
    """
    rewrite_question = ctx.rewrite_question
    rerank_question = ctx.rerank_question
    overfetch = ctx.overfetch
    flt = ctx.flt
    q_hash = ctx.q_hash
    top_k = ctx.top_k
    enable_rerank = ctx.enable_rerank
    use_sparse = ctx.use_sparse
    use_lexical = search.use_lexical
    dense_hits = search.dense_hits
    lexical_hits = search.lexical_hits
    sparse_hits = search.sparse_hits

    # Optional: by fuse time we already hold dense (and maybe lexical/sparse)
    # hits, so an over-budget fuse proceeds instead of failing (degrade mode).
    check_budget("fuse", optional=True)
    _trace_units(trace, "lexical", lexical_hits)
    _trace_units(trace, "sparse", sparse_hits)

    # Optional extra recall path from the rewritten query. Fused into RRF as a
    # separate ranking so the original query's recall is never weakened. The
    # path only activates when `retrieve_query_rewrite_rrf_weight > 0`
    # (config default 0.5; set 0.0 to disable).
    rewrite_hits: list[dict] = []
    rewrite_weight = float(getattr(cfg, "retrieve_query_rewrite_rrf_weight", 0.0) or 0.0)
    if (
        rewrite_question
        and rewrite_weight > 0.0
        and not check_budget("rewrite_recall", optional=True)
    ):
        try:
            expand_on = bool(getattr(cfg, "retrieve_query_expand", True))
            codes_on = bool(getattr(cfg, "retrieve_query_expand_codes", False))
            rw_texts = [rewrite_question]
            if expand_on:
                try:
                    rw_texts = _expand_query_texts_cached(
                        rewrite_question, enable_codes=codes_on, cfg=cfg
                    )
                except Exception:
                    rw_texts = [rewrite_question]
            rw_merged: dict[str, dict] = {}
            for qtext in rw_texts:
                qvec = _encode_question_cached(
                    question=qtext,
                    embedder=embedder,
                    cfg=cfg,
                    metrics=metrics,
                    deadline=deadline,
                )
                hits_i, _ = _search_store_with_resilience(
                    store=store,
                    qvec=qvec,
                    top_k=overfetch,
                    flt=flt,
                    cfg=cfg,
                    circuits=circuits,
                    metrics=metrics,
                    deadline=deadline,
                )
                for row in hits_i:
                    key = _hit_key(
                        str(row.get("unit_id") or ""),
                        str(row.get("id") or ""),
                        str(row.get("version_id") or ""),
                    )
                    prev = rw_merged.get(key)
                    if prev is None or float(row.get("score") or 0) > float(prev.get("score") or 0):
                        rw_merged[key] = row
            rw_dense = sorted(
                rw_merged.values(), key=lambda r: float(r.get("score") or 0), reverse=True
            )[: max(1, overfetch)]
            rw_lex: list[dict] = []
            if use_lexical:
                rw_lex_q = rewrite_question
                if expand_on:
                    try:
                        rw_lex_q = _expand_terms_for_lexical_cached(
                            rewrite_question, enable_codes=codes_on, cfg=cfg
                        )
                    except Exception:
                        rw_lex_q = rewrite_question
                rw_lex = lexical.search(
                    rw_lex_q,
                    top_k=overfetch,
                    filter=flt,
                    max_docs=int(cfg.retrieve_lexical_max_docs or 0) or None,
                )
            rw_sparse: list[dict] = []
            if use_sparse and sparse is not None:
                q_sparse = _encode_sparse_question_cached(
                    question=rewrite_question,
                    embedder=embedder,
                    cfg=cfg,
                    metrics=metrics,
                    deadline=deadline,
                )
                if q_sparse:
                    rw_sparse = sparse.search(
                        q_sparse,
                        top_k=overfetch,
                        filter=flt,
                        max_docs=int(getattr(cfg, "retrieve_sparse_max_docs", 0) or 0) or None,
                    )
            rw_fused = fuse_hybrid_hits(
                dense_hits=rw_dense,
                lexical_hits=rw_lex,
                sparse_hits=rw_sparse,
                use_lexical=use_lexical,
                use_sparse=use_sparse,
                lexical_weight=_cfg_num(cfg, "retrieve_lexical_rrf_weight", 0.35),
                sparse_weight=_cfg_num(cfg, "retrieve_sparse_rrf_weight", 0.3),
                rrf_k=int(getattr(cfg, "retrieve_hybrid_rrf_k", 60) or 60),
            )
            rewrite_hits = [
                {
                    "id": h.chunk_id,
                    "unit_id": h.unit_id,
                    "version_id": h.version_id,
                    "chunk_index": h.chunk_index,
                    "content": h.content,
                    "heading_path": list(h.heading_path or []),
                    "acl_department": h.acl_department,
                    "acl_classification": h.acl_classification,
                    "score": h.score_fused,
                }
                for h in rw_fused
            ]
            metrics.inc("retrieve.rewrite_path_hits", len(rewrite_hits))
        except Exception as exc:
            logger.warning("rewrite recall path failed q_hash=%s: %s", q_hash, exc)
            rewrite_hits = []

    fused_list = fuse_hybrid_hits(
        dense_hits=dense_hits,
        lexical_hits=lexical_hits,
        sparse_hits=sparse_hits,
        use_lexical=use_lexical,
        use_sparse=use_sparse,
        lexical_weight=_cfg_num(cfg, "retrieve_lexical_rrf_weight", 0.35),
        sparse_weight=_cfg_num(cfg, "retrieve_sparse_rrf_weight", 0.3),
        rrf_k=int(getattr(cfg, "retrieve_hybrid_rrf_k", 60) or 60),
        rewrite_hits=rewrite_hits,
        rewrite_weight=rewrite_weight,
    )
    _trace_units(trace, "fused", fused_list)

    # post_filter ACL (fail-closed)
    min_score = acl_filter.min_score if acl_filter else None
    fused_list, filtered_out = post_filter_acl(
        fused_list,
        flt,
        min_score,
        acl_fields_required=bool(cfg.retrieve_acl_fields_required),
        metrics=metrics,
    )
    if filtered_out:
        metrics.inc("retrieve.filtered_out", filtered_out)
    _trace_units(trace, "post_acl", fused_list)

    fused_list = finalize_hits(
        fused_list,
        question=rerank_question,
        cfg=cfg,
        top_k=top_k,
        enable_rerank=enable_rerank,
        embedder=embedder,
        reranker=reranker,
        metrics=metrics,
        trace=trace,
        deadline=deadline,
    )
    _trace_units(trace, "final", fused_list)
    return fused_list, filtered_out


def _stage_pack(fused_list: list[FuseHit], top_k: int, *, trace: dict | None) -> list[dict]:
    """Stage 4 — pack the ranked hits into the response chunk list (top_k crop)."""
    chunks = pack_response(fused_list, top_k)
    _trace_units(trace, "returned", chunks)
    return chunks


def _run_retrieve_pipeline_inner(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    cfg: Settings,
    force_lexical_error: Callable[[], None] | None,
    metrics: Any,
    sparse: Any = None,
    reranker: Any = None,
    query_rewriter: Any = None,
    session_rewriter: Any = None,
    trace: dict | None = None,
) -> dict:
    started = time.monotonic()
    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
    budget_sec = float(cfg.retrieve_overall_timeout_sec or 0)
    # R-09: one absolute monotonic deadline shared by every concurrent branch, so
    # a stuck lexical/sparse Future cannot outlive the overall budget.
    #
    # R-04 (scope): this deadline is a **request wait budget**, not a hard
    # end-to-end response cap. Stage-boundary checks are cooperative, and a
    # running thread cannot be force-killed from Python. What R-04 adds is that
    # every *non-cancellable* external call (query rewrite / embedding / sparse
    # embedding / cross-encoder) is routed through a bounded isolation pool whose
    # wait is clamped to the remaining budget, so the request returns near the
    # deadline even if the underlying library call keeps running.
    deadline = (started + budget_sec) if budget_sec > 0 else None

    def _check_budget(stage: str, *, optional: bool = False) -> bool:
        """Enforce the overall wall-clock *wait* budget at a stage boundary.

        Returns False when within budget. When over budget: for an *optional*
        stage with `retrieve_overall_timeout_degrade` on, returns True (caller
        degrades instead of failing); otherwise raises 504 RETRIEVE_TIMEOUT.
        Required stages (dense) never degrade — returning an empty result would
        be worse than a clear timeout.

        This is a cooperative gate (checked at stage entry/exit), not a preemptive
        interrupt; the hard wait bound for in-stage external calls comes from
        ``_run_bounded`` + the shared ``deadline``.
        """
        if budget_sec <= 0:
            return False
        elapsed = time.monotonic() - started
        if elapsed > budget_sec:
            if optional and getattr(cfg, "retrieve_overall_timeout_degrade", False):
                metrics.inc("retrieve.budget_degraded")
                return True
            metrics.inc("retrieve.timeout")
            raise RetrieveError(
                f"retrieve exceeded overall budget {budget_sec}s at stage={stage}",
                "RETRIEVE_TIMEOUT",
                504,
            )
        return False

    # ---- stage 1: request context (normalize / ACL / query variants) ----
    ctx = _stage_build_request_context(
        request=request,
        embedder=embedder,
        cfg=cfg,
        metrics=metrics,
        session_rewriter=session_rewriter,
        query_rewriter=query_rewriter,
        deadline=deadline,
    )

    # F-07: an explicit empty allow-list (or an empty intersection) denies every
    # unit. Short-circuit to an empty result — never fall back to a full-corpus
    # search — and skip embed/store/branch work entirely.
    if is_deny_all_filter(ctx.flt):
        metrics.inc("retrieve.acl_deny_all")
        metrics.inc("retrieve.empty")
        if ctx.acl_applied:
            metrics.inc("retrieve.acl_empty")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        metrics.observe_ms("retrieve.latency", elapsed_ms)
        logger.info(
            "retrieve deny-all q_hash=%s top_k=%s took_ms=%s (explicit empty allow-list)",
            ctx.q_hash,
            ctx.top_k,
            elapsed_ms,
        )
        if cfg.retrieve_struct_log:
            _emit_struct_log(
                _struct_base(
                    event="retrieve_ok",
                    q_hash=ctx.q_hash,
                    took_ms=elapsed_ms,
                    top_k=ctx.top_k,
                    hybrid=ctx.hybrid_on,
                    degraded=False,
                    acl_applied=ctx.acl_applied,
                    filtered_out=0,
                    dense_hits=0,
                    lexical_hits=0,
                    returned=0,
                )
            )
        return {
            "chunks": [],
            "took_ms": elapsed_ms,
            "acl_applied": ctx.acl_applied,
            "hybrid": ctx.hybrid_on,
            "degraded_to_dense": False,
        }

    # ---- stage 2: concurrent dense/lexical/sparse search ----
    # R-04: post-stage deadline gate — the context build runs a non-cancellable
    # query rewrite (bounded by the isolation pool) and other setup; if it ran
    # past the budget, stop before scheduling the search branches.
    _check_budget("context")
    search = _stage_parallel_search(
        ctx=ctx,
        embedder=embedder,
        store=store,
        lexical=lexical,
        sparse=sparse,
        cfg=cfg,
        force_lexical_error=force_lexical_error,
        circuits=circuits,
        metrics=metrics,
        check_budget=_check_budget,
        trace=trace,
        deadline=deadline,
    )

    fuse_t0 = time.monotonic()
    # ---- stage 3: RRF fuse + ACL post-filter + final ranking ----
    fused_list, filtered_out = _stage_fuse_and_rank(
        ctx=ctx,
        search=search,
        acl_filter=request.acl_filter,
        embedder=embedder,
        store=store,
        lexical=lexical,
        sparse=sparse,
        cfg=cfg,
        circuits=circuits,
        metrics=metrics,
        check_budget=_check_budget,
        reranker=reranker,
        trace=trace,
        deadline=deadline,
    )

    # ---- stage 4: pack the response ----
    chunks = _stage_pack(fused_list, ctx.top_k, trace=trace)
    # NOTE: `retrieve.stage_fuse_ms` is a *total* segment, not pure RRF fusion:
    # it spans RRF fuse + ACL post-filter + embed_rerank + cross-encoder. The
    # per-stage metrics (`retrieve.stage_sparse_ms`, `retrieve.stage_embed_rerank_ms`,
    # `retrieve.stage_cross_encoder_ms`) break out the dominant contributors so
    # latency profiling no longer mis-attributes them to fusion.
    stage_fuse_ms = (time.monotonic() - fuse_t0) * 1000
    metrics.observe_ms("retrieve.stage_fuse_ms", stage_fuse_ms)
    if not chunks:
        metrics.inc("retrieve.empty")
        if ctx.acl_applied:
            metrics.inc("retrieve.acl_empty")
    metrics.inc("retrieve.ok")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    metrics.observe_ms("retrieve.latency", elapsed_ms)

    logger.info(
        "retrieve ok q_hash=%s q_len=%s top_k=%s dense=%s lexical=%s returned=%s hybrid=%s "
        "degraded=%s acl=%s filtered_out=%s overfetch=%s took_ms=%s "
        "stage_ms embed=%.1f store=%.1f lexical=%.1f fuse=%.1f",
        ctx.q_hash,
        len(ctx.question),
        ctx.top_k,
        len(search.dense_hits),
        len(search.lexical_hits),
        len(chunks),
        ctx.hybrid_on,
        search.degraded_to_dense,
        ctx.acl_applied,
        filtered_out,
        ctx.overfetch,
        elapsed_ms,
        search.stage_embed_ms,
        search.stage_store_ms,
        search.stage_lexical_ms,
        stage_fuse_ms,
    )
    if cfg.retrieve_struct_log:
        _emit_struct_log(
            _struct_base(
                event="retrieve_ok",
                q_hash=ctx.q_hash,
                took_ms=elapsed_ms,
                top_k=ctx.top_k,
                hybrid=ctx.hybrid_on,
                degraded=search.degraded_to_dense,
                acl_applied=ctx.acl_applied,
                filtered_out=filtered_out,
                dense_hits=len(search.dense_hits),
                lexical_hits=len(search.lexical_hits),
                returned=len(chunks),
                stage_embed_ms=round(search.stage_embed_ms, 3),
                stage_store_ms=round(search.stage_store_ms, 3),
                stage_lexical_ms=round(search.stage_lexical_ms, 3),
                stage_fuse_ms=round(stage_fuse_ms, 3),
            )
        )

    return {
        "chunks": chunks,
        "took_ms": elapsed_ms,
        "acl_applied": ctx.acl_applied,
        "hybrid": ctx.hybrid_on,
        "degraded_to_dense": search.degraded_to_dense,
    }
