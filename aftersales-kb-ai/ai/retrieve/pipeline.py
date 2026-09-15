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

# Shared pool for store.search / lexical sync soft timeouts (process-local).
_store_pool: ThreadPoolExecutor | None = None
_store_pool_lock = threading.Lock()
_store_pool_size = 0

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
    def __init__(self, message: str, error_code: str = "RETRIEVE_FAILED", status_code: int = 500) -> None:
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
            _store_pool = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="retrieve-store"
            )
            _store_pool_size = size
        return _store_pool


def reset_store_pool_for_tests() -> None:
    global _store_pool, _store_pool_size
    with _store_pool_lock:
        if _store_pool is not None:
            _store_pool.shutdown(wait=False, cancel_futures=True)
        _store_pool = None
        _store_pool_size = 0


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
) -> list[float]:
    max_size = int(cfg.retrieve_embed_cache_max or 0)
    if max_size <= 0:
        return retry_call(
            lambda: embedder.encode([question])[0],
            policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
        )
    key = _embed_cache_key(question, cfg)
    with _embed_cache_lock:
        if key in _embed_cache:
            _embed_cache.move_to_end(key)
            vec = _embed_cache[key]
            metrics.inc("retrieve.embed_cache_hit")
            return list(vec)
    metrics.inc("retrieve.embed_cache_miss")
    vec = retry_call(
        lambda: embedder.encode([question])[0],
        policy=RetryPolicy(max_attempts=3, base_delay_sec=0.2, max_delay_sec=5.0),
    )
    with _embed_cache_lock:
        _embed_cache[key] = list(vec)
        _embed_cache.move_to_end(key)
        while len(_embed_cache) > max_size:
            _embed_cache.popitem(last=False)
    return list(vec)


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
) -> tuple[list[dict], float]:
    """Dense store.search with circuit + soft timeout budget. Returns (hits, elapsed_ms)."""
    circuit_name = "retrieve_store"
    if circuits.is_open(circuit_name):
        metrics.inc("retrieve.store_circuit_open")
        raise RetrieveError("retrieve store circuit open", "RETRIEVE_FAILED", 503)

    timeout_sec = float(cfg.retrieve_store_timeout_sec or 0)
    t0 = time.monotonic()
    if timeout_sec <= 0:
        try:
            hits = store.search(qvec, top_k=top_k, filter=flt)
            circuits.record_success(circuit_name)
            return hits, (time.monotonic() - t0) * 1000
        except Exception as exc:
            circuits.record_failure(circuit_name, str(exc))
            metrics.inc("retrieve.store_timeout" if "timeout" in type(exc).__name__.lower() else "retrieve.fail")
            raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc

    pool = get_store_pool(cfg.retrieve_store_pool_size)
    fut = pool.submit(store.search, qvec, top_k=top_k, filter=flt)
    try:
        hits = fut.result(timeout=timeout_sec)
    except FuturesTimeoutError as exc:
        circuits.record_failure(circuit_name, f"timeout after {timeout_sec}s")
        metrics.inc("retrieve.store_timeout")
        raise RetrieveError(
            f"store.search exceeded retrieve_store_timeout_sec={timeout_sec}",
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
    score_fused: float = 0.0
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
    """Merge request filter fields into SearchFilter. Fail-closed when required and empty."""
    unit_ids = _validate_unit_ids(unit_ids, max_unit_ids)
    deny = _validate_unit_ids(acl.deny_unit_ids if acl else None, max_unit_ids)
    acl_units = _validate_unit_ids(acl.unit_ids if acl else None, max_unit_ids)

    merged_unit_ids: list[str] | None = None
    if unit_ids:
        merged_unit_ids = list(unit_ids)
    if acl_units:
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
    # unit_ids=[] means deny-all, not "no filter". Only None + no other fields is unconstrained.
    empty = (
        flt.unit_ids is None
        and not deny
        and not (acl and (acl.department or "").strip())
        and not (acl and (acl.classification or "").strip())
        and not (acl and acl.min_score is not None)
    )
    if require_acl and empty:
        metrics = get_metrics()
        metrics.inc("retrieve.acl_required_reject")
        raise RetrieveError(
            "acl_filter is required but missing",
            "RETRIEVE_ACL_REQUIRED",
            400,
        )
    applied = not empty
    return (flt if applied else None), applied


def fuse_rrf(
    path_rankings: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """score_fused = sum 1/(k+rank); missing path contributes nothing. Rank starts at 1."""
    k = max(1, int(k))
    scores: dict[str, float] = {}
    for ranking in path_rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
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


def rule_rerank(hits: list[FuseHit], question: str) -> list[FuseHit]:
    """Lightweight lexical-overlap boost on fused score. No model dependency."""
    q_terms = set()
    from ai.retrieve.lexical import tokenize

    q_terms.update(tokenize(question))
    if not q_terms:
        return hits

    def boost(h: FuseHit) -> float:
        overlap = len(q_terms & set(tokenize(h.content[:500])))
        return h.score_fused + min(overlap, 10) * 1e-4

    return sorted(hits, key=boost, reverse=True)


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
) -> int:
    """Cold-start sync with wall-clock budget. Raises RetrieveError on timeout when degrade off."""
    t0 = time.monotonic()

    def _run() -> int:
        return lexical.sync_from_store(store, limit=limit)

    if timeout_sec <= 0:
        try:
            return _run()
        except Exception as exc:
            metrics.inc("retrieve.fail")
            raise RetrieveError(f"lexical sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc

    pool = get_store_pool(cfg.retrieve_store_pool_size)
    fut = pool.submit(_run)
    try:
        count = fut.result(timeout=timeout_sec)
    except FuturesTimeoutError as exc:
        metrics.observe_ms("retrieve.lexical_sync_ms", (time.monotonic() - t0) * 1000)
        raise RetrieveError(
            f"lexical sync exceeded retrieve_lexical_sync_timeout_sec={timeout_sec}",
            "RETRIEVE_TIMEOUT",
            504,
        ) from exc
    except Exception as exc:
        raise RetrieveError(f"lexical sync failed: {exc}", "RETRIEVE_FAILED", 500) from exc
    metrics.observe_ms("retrieve.lexical_sync_ms", (time.monotonic() - t0) * 1000)
    return count


def run_retrieve_pipeline(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    cfg: Settings,
    force_lexical_error: Callable[[], None] | None = None,
) -> dict:
    """Full hybrid retrieve. Returns packed response dict."""
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


def _run_retrieve_pipeline_inner(
    *,
    request: RetrieveRequestView,
    embedder: Any,
    store: VectorStore,
    lexical: LexicalIndex,
    cfg: Settings,
    force_lexical_error: Callable[[], None] | None,
    metrics: Any,
) -> dict:
    started = time.monotonic()
    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
    budget_sec = float(cfg.retrieve_overall_timeout_sec or 0)

    def _check_budget(stage: str) -> None:
        if budget_sec <= 0:
            return
        elapsed = time.monotonic() - started
        if elapsed > budget_sec:
            metrics.inc("retrieve.timeout")
            raise RetrieveError(
                f"retrieve exceeded overall budget {budget_sec}s at stage={stage}",
                "RETRIEVE_TIMEOUT",
                504,
            )

    question = normalize_question(request.question, cfg.retrieve_max_question_chars)
    top_k = int(request.top_k or cfg.retrieve_top_k_default)
    max_unit_ids = int(cfg.retrieve_max_unit_ids or 0)
    flt, acl_applied = build_acl_filter(
        request.acl_filter,
        request.unit_ids,
        bool(cfg.retrieve_require_acl),
        max_unit_ids=max_unit_ids,
    )

    hybrid_on = cfg.retrieve_hybrid_enabled if request.enable_hybrid is None else bool(request.enable_hybrid)
    if cfg.retrieve_multi_replica_safe:
        hybrid_on = False
        with _multi_replica_log_lock:
            global _multi_replica_logged
            if not _multi_replica_logged:
                _multi_replica_logged = True
                logger.warning(
                    "retrieve_multi_replica_safe=true: dense-only path (lexical disabled)"
                )
        metrics.inc("retrieve.multi_replica_dense_only")

    overfetch = request.overfetch or min(top_k * cfg.retrieve_overfetch_factor, cfg.retrieve_overfetch_max)
    overfetch = max(top_k, int(overfetch))
    enable_rerank = cfg.retrieve_enable_rerank if request.enable_rerank is None else bool(request.enable_rerank)
    degraded_to_dense = False
    q_hash = _question_hash_prefix(question)

    stage_embed_ms = 0.0
    stage_store_ms = 0.0
    stage_lexical_ms = 0.0
    stage_fuse_ms = 0.0

    # dense path
    _check_budget("dense_start")
    if circuits.is_open("embed"):
        metrics.inc("retrieve.fail")
        raise RetrieveError("embed circuit open", "EMBED_CIRCUIT_OPEN", 503)
    embed_t0 = time.monotonic()
    try:
        qvec = _encode_question_cached(
            question=question,
            embedder=embedder,
            cfg=cfg,
            metrics=metrics,
        )
        stage_embed_ms = (time.monotonic() - embed_t0) * 1000
        metrics.observe_ms("retrieve.stage_embed_ms", stage_embed_ms)
        _check_budget("dense_search")
        dense_hits, stage_store_ms = _search_store_with_resilience(
            store=store,
            qvec=qvec,
            top_k=overfetch,
            flt=flt,
            cfg=cfg,
            circuits=circuits,
            metrics=metrics,
        )
        metrics.observe_ms("retrieve.stage_store_ms", stage_store_ms)
        circuits.record_success("embed")
    except RetrieveError:
        metrics.inc("retrieve.fail")
        raise
    except Exception as exc:
        circuits.record_failure("embed", str(exc))
        metrics.inc("retrieve.fail")
        raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc

    metrics.inc("retrieve.dense_hits", len(dense_hits))

    # lexical path
    _check_budget("lexical_start")
    lexical_hits: list[dict] = []
    use_lexical = hybrid_on
    if use_lexical:
        lex_t0 = time.monotonic()
        try:
            if force_lexical_error is not None:
                force_lexical_error()
            sync_limit = int(cfg.retrieve_lexical_max_docs or 0) or None
            if lexical.doc_count() == 0:
                sync_timeout = float(cfg.retrieve_lexical_sync_timeout_sec or 0)
                if sync_timeout <= 0:
                    sync_timeout = float(cfg.retrieve_store_timeout_sec or 0)
                _sync_lexical_from_store(
                    lexical=lexical,
                    store=store,
                    limit=sync_limit,
                    timeout_sec=sync_timeout,
                    cfg=cfg,
                    circuits=circuits,
                    metrics=metrics,
                )
            max_docs = int(cfg.retrieve_lexical_max_docs or 0) or None
            doc_n = lexical.doc_count()
            if max_docs and doc_n > max_docs:
                metrics.inc("retrieve.lexical_capped")
                if cfg.retrieve_lexical_skip_when_over_cap:
                    use_lexical = False
                    degraded_to_dense = True
                    metrics.inc("retrieve.hybrid_degraded")
                    lexical_hits = []
                else:
                    lexical_hits = lexical.search(
                        question,
                        top_k=overfetch,
                        filter=flt,
                        max_docs=max_docs,
                    )
                    metrics.inc("retrieve.lexical_hits", len(lexical_hits))
            else:
                lexical_hits = lexical.search(
                    question,
                    top_k=overfetch,
                    filter=flt,
                    max_docs=max_docs,
                )
                metrics.inc("retrieve.lexical_hits", len(lexical_hits))
        except Exception as exc:
            logger.warning("lexical search failed q_hash=%s: %s", q_hash, exc)
            if cfg.retrieve_hybrid_degrade_to_dense:
                degraded_to_dense = True
                metrics.inc("retrieve.hybrid_degraded")
                lexical_hits = []
            else:
                metrics.inc("retrieve.fail")
                if isinstance(exc, RetrieveError):
                    raise
                raise RetrieveError(f"retrieve failed: {exc}", "RETRIEVE_FAILED", 500) from exc
        finally:
            stage_lexical_ms = (time.monotonic() - lex_t0) * 1000
            metrics.observe_ms("retrieve.stage_lexical_ms", stage_lexical_ms)

    fuse_t0 = time.monotonic()
    _check_budget("fuse")

    # fuse
    by_key: dict[str, FuseHit] = {}

    def _upsert_from_row(row: dict, path: str, score: float, rank: int) -> None:
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
        else:
            hit.score_lexical = float(score)
            if "lexical" not in hit.retrieval_paths:
                hit.retrieval_paths.append("lexical")

    dense_ranking: list[str] = []
    for i, row in enumerate(dense_hits, start=1):
        cid = str(row.get("id") or "")
        unit = str(row.get("unit_id") or "")
        ver = str(row.get("version_id") or "")
        key = _hit_key(unit, cid, ver)
        _upsert_from_row(row, "dense", float(row.get("score") or 0.0), i)
        dense_ranking.append(key)

    lexical_ranking: list[str] = []
    if use_lexical and lexical_hits:
        for i, row in enumerate(lexical_hits, start=1):
            cid = str(row.get("id") or "")
            unit = str(row.get("unit_id") or "")
            ver = str(row.get("version_id") or "")
            key = _hit_key(unit, cid, ver)
            _upsert_from_row(row, "lexical", float(row.get("score") or 0.0), i)
            lexical_ranking.append(key)

    rankings = [dense_ranking]
    if use_lexical:
        rankings.append(lexical_ranking)
    fused_scores = fuse_rrf(rankings, k=cfg.retrieve_hybrid_rrf_k)
    for key, hit in by_key.items():
        hit.score_fused = fused_scores.get(key, 0.0)

    fused_list = sorted(by_key.values(), key=lambda h: h.score_fused, reverse=True)

    # post_filter ACL (fail-closed)
    min_score = request.acl_filter.min_score if request.acl_filter else None
    fused_list, filtered_out = post_filter_acl(
        fused_list,
        flt,
        min_score,
        acl_fields_required=bool(cfg.retrieve_acl_fields_required),
        metrics=metrics,
    )
    if filtered_out:
        metrics.inc("retrieve.filtered_out", filtered_out)

    # quality
    fused_list = apply_max_per_unit(fused_list, cfg.retrieve_max_per_unit)
    if enable_rerank:
        fused_list = rule_rerank(fused_list, question)
        fused_list = apply_max_per_unit(fused_list, cfg.retrieve_max_per_unit)

    chunks = pack_response(fused_list, top_k)
    stage_fuse_ms = (time.monotonic() - fuse_t0) * 1000
    metrics.observe_ms("retrieve.stage_fuse_ms", stage_fuse_ms)
    if not chunks:
        metrics.inc("retrieve.empty")
        if acl_applied:
            metrics.inc("retrieve.acl_empty")
    metrics.inc("retrieve.ok")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    metrics.observe_ms("retrieve.latency", elapsed_ms)

    logger.info(
        "retrieve ok q_hash=%s q_len=%s top_k=%s dense=%s lexical=%s returned=%s hybrid=%s "
        "degraded=%s acl=%s filtered_out=%s overfetch=%s took_ms=%s "
        "stage_ms embed=%.1f store=%.1f lexical=%.1f fuse=%.1f",
        q_hash,
        len(question),
        top_k,
        len(dense_hits),
        len(lexical_hits),
        len(chunks),
        hybrid_on,
        degraded_to_dense,
        acl_applied,
        filtered_out,
        overfetch,
        elapsed_ms,
        stage_embed_ms,
        stage_store_ms,
        stage_lexical_ms,
        stage_fuse_ms,
    )
    if cfg.retrieve_struct_log:
        _emit_struct_log(
            _struct_base(
                event="retrieve_ok",
                q_hash=q_hash,
                took_ms=elapsed_ms,
                top_k=top_k,
                hybrid=hybrid_on,
                degraded=degraded_to_dense,
                acl_applied=acl_applied,
                filtered_out=filtered_out,
                dense_hits=len(dense_hits),
                lexical_hits=len(lexical_hits),
                returned=len(chunks),
                stage_embed_ms=round(stage_embed_ms, 3),
                stage_store_ms=round(stage_store_ms, 3),
                stage_lexical_ms=round(stage_lexical_ms, 3),
                stage_fuse_ms=round(stage_fuse_ms, 3),
            )
        )

    return {
        "chunks": chunks,
        "took_ms": elapsed_ms,
        "acl_applied": acl_applied,
        "hybrid": hybrid_on,
        "degraded_to_dense": degraded_to_dense,
    }
