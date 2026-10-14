"""Ingest review gate: POST /internal/review (阶段1, JSON body).

AI 只返回 verdict/confidence/reasons；Java 侧决策与落库。沿用 ``X-Internal-Token``
鉴权中间件。

**HTTP 契约（阶段2 Java 依赖）**：档位级失败走 **HTTP 200 + ``tiers``**，不占用
HTTP 错误码（避免 VLM 故障丢掉 LLM 结论）；仅请求级问题用 400 ``REVIEW_BAD_REQUEST``
/ 503 ``REVIEW_DISABLED`` / 504 ``REVIEW_TIMEOUT``；**502 ``REVIEW_LLM_FAILED`` /
``REVIEW_VLM_FAILED`` 未实现**。本端点 504 是事后预算报告，硬墙钟超时由 Java 侧
``ai.review-timeout-ms`` 负责。

**并发护栏（F-21）**：``build_review`` 是同步 LLM/VLM 链路，绝不在事件循环里
直接调用（否则一次质检即可阻塞健康检查与其他内部路由）。它运行在一个专用受限
线程池上：并发上限 ``review_max_concurrency`` + 队列上限 ``review_queue_max``，
饱和返回 503 ``REVIEW_BUSY``；总截止 = ``review_llm_timeout_sec`` +
``review_vlm_timeout_sec``，超时返回 504 ``REVIEW_TIMEOUT``。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from ai.generate.llm import LlmClient
from ai.generate.vlm import VlmClient
from ai.ingest.review import build_review
from app.deps import get_llm, get_vlm
from core.circuit import get_metrics
from core.config import Settings, get_settings
from core.errors import detail_body
from schemas.ingest import ReviewRequest, ReviewResponse

from app.api.internal._common import _max_msg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-review"])

# Dedicated bounded executor for the blocking review chain (F-21). Separate from
# the default anyio threadpool so a saturated review chain cannot starve other
# blocking routes; the semaphore (workers + queue) turns saturation into an
# explicit 503 instead of unbounded queueing.
#
# R-08: the semaphore slot is bound to the *task's actual completion* (a
# concurrent-future done callback), not to the HTTP await. A request that times
# out only ends the client's wait — the still-running thread keeps holding its
# slot, so the queue cannot be refilled by retries and the workers+queue bound
# actually holds. `_review_orphans` counts timed-out tasks still running.
_review_pool: ThreadPoolExecutor | None = None
_review_pool_sem: threading.BoundedSemaphore | None = None
_review_pool_key: tuple[int, int] | None = None
_review_pool_lock = threading.Lock()
_review_orphans = 0
_review_orphans_lock = threading.Lock()


def review_orphaned_running() -> int:
    """Number of timed-out review tasks still executing (R-08 observability)."""
    with _review_orphans_lock:
        return _review_orphans


def _orphan_inc() -> int:
    global _review_orphans
    with _review_orphans_lock:
        _review_orphans += 1
        return _review_orphans


def _orphan_dec() -> int:
    global _review_orphans
    with _review_orphans_lock:
        _review_orphans = max(0, _review_orphans - 1)
        return _review_orphans


def _get_review_pool(cfg: Settings) -> tuple[ThreadPoolExecutor, threading.BoundedSemaphore]:
    """Lazy process-local review pool; rebuilt when the sizing config changes."""
    global _review_pool, _review_pool_sem, _review_pool_key
    workers = max(1, int(getattr(cfg, "review_max_concurrency", 2) or 2))
    queue = max(0, int(getattr(cfg, "review_queue_max", 8) or 0))
    with _review_pool_lock:
        if _review_pool is None or _review_pool_key != (workers, queue):
            if _review_pool is not None:
                _review_pool.shutdown(wait=False)
            _review_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="review")
            _review_pool_sem = threading.BoundedSemaphore(workers + queue)
            _review_pool_key = (workers, queue)
        return _review_pool, _review_pool_sem


def reset_review_pool_for_tests() -> None:
    """Drop the process-local review pool (test hook)."""
    global _review_pool, _review_pool_sem, _review_pool_key, _review_orphans
    with _review_pool_lock:
        if _review_pool is not None:
            _review_pool.shutdown(wait=False, cancel_futures=True)
        _review_pool = None
        _review_pool_sem = None
        _review_pool_key = None
    with _review_orphans_lock:
        _review_orphans = 0


def _first_validation_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid request body"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc") or [])
    return f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))


@router.post("/review", response_model=ReviewResponse)
async def review(
    request: Request,
    cfg: Settings = Depends(get_settings),
    llm: LlmClient = Depends(get_llm),
    vlm: VlmClient = Depends(get_vlm),
) -> ReviewResponse:
    """Three-tier ingest review verdict (rules → LLM → VLM)."""
    if not cfg.review_llm_enabled and not cfg.review_vlm_enabled:
        raise HTTPException(
            status_code=503,
            detail=detail_body(
                "REVIEW_DISABLED", "review disabled by config", max_chars=_max_msg(cfg)
            ),
        )

    # Manual parse so a bad body yields 400 REVIEW_BAD_REQUEST (not FastAPI's 422),
    # keeping HttpAiClient.extractErrorCode compatible.
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed JSON is a client error
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "REVIEW_BAD_REQUEST", f"invalid JSON body: {exc}", max_chars=_max_msg(cfg)
            ),
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "REVIEW_BAD_REQUEST", "body must be a JSON object", max_chars=_max_msg(cfg)
            ),
        )
    try:
        req = ReviewRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "REVIEW_BAD_REQUEST", _first_validation_message(exc), max_chars=_max_msg(cfg)
            ),
        ) from exc

    t0 = time.monotonic()
    # F-21: run the blocking review chain on a dedicated bounded executor with a
    # total deadline, so the event loop stays free for health/other routes.
    budget_sec = float(cfg.review_llm_timeout_sec or 0) + float(cfg.review_vlm_timeout_sec or 0)
    pool, sem = _get_review_pool(cfg)
    if not sem.acquire(blocking=False):
        get_metrics().inc("review.busy")
        logger.warning("review pool saturated (workers+queue cap reached)")
        raise HTTPException(
            status_code=503,
            detail=detail_body(
                "REVIEW_BUSY",
                "review concurrency/queue limit reached; retry later",
                max_chars=_max_msg(cfg),
            ),
        )
    loop = asyncio.get_running_loop()
    # R-08: submit to the pool directly and release the semaphore from the
    # *concurrent future's* done callback. `loop.run_in_executor` would hand back
    # an asyncio future whose cancellation (on timeout) fires the callback while
    # the thread is still running — releasing capacity early. Submitting here and
    # wrapping afterwards ties the slot to the real task completion instead.
    try:
        concurrent_fut = pool.submit(
            functools.partial(build_review, req, llm=llm, vlm=vlm, cfg=cfg)
        )
    except Exception:
        sem.release()
        raise
    # R-08: `counted` marks that this task was counted as a *running* orphan.
    # The timeout handler and the done callback both mutate it under `state_lock`
    # so the increment can never race the decrement into a leak (a task that
    # completes exactly between the deadline check and `cancel()` must be counted
    # at most once — and decremented exactly once).
    state = {"counted": False}
    state_lock = threading.Lock()

    def _on_done(_f) -> None:
        try:
            sem.release()
        except ValueError:  # pragma: no cover - defensive over-release guard
            logger.warning("review semaphore over-released")
        with state_lock:
            was_counted = state["counted"]
            state["counted"] = False
        if was_counted:
            _orphan_dec()
            get_metrics().inc("review.orphaned_finished")

    concurrent_fut.add_done_callback(_on_done)
    wrapped = asyncio.wrap_future(concurrent_fut, loop=loop)
    # Swallow a late exception/result so a still-running orphan does not log
    # "Future exception was never retrieved".
    wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    try:
        # `shield` keeps the underlying concurrent future alive on timeout so the
        # slot is tied to the *task*, not the client wait (R-08).
        resp = await asyncio.wait_for(
            asyncio.shield(wrapped),
            timeout=budget_sec if budget_sec > 0 else None,
        )
    except asyncio.TimeoutError as exc:
        # Only the *client wait* ends here. Decide "orphan" from the future's
        # terminal state inside the same critical section as the increment: if it
        # already finished, the done callback owns the (zero) count.
        with state_lock:
            if concurrent_fut.done():
                counted_now = False
            else:
                state["counted"] = True
                _orphan_inc()
                counted_now = True
        # Cancel a not-yet-started task (frees its slot, no work lost); if it was
        # queued, the done callback decrements the count we just took.
        if concurrent_fut.cancel():
            get_metrics().inc("review.timeout_queued_cancelled")
        if counted_now:
            get_metrics().inc("review.orphaned_started")
        get_metrics().inc("review.timeout")
        logger.warning(
            "review exceeded budget %.0fs; still-running orphans=%s",
            budget_sec,
            review_orphaned_running(),
        )
        raise HTTPException(
            status_code=504,
            detail=detail_body(
                "REVIEW_TIMEOUT",
                f"review exceeded budget {budget_sec:.0f}s",
                max_chars=_max_msg(cfg),
            ),
        ) from exc
    total_ms = (time.monotonic() - t0) * 1000.0
    budget_ms = budget_sec * 1000.0
    if budget_ms > 0 and total_ms > budget_ms:
        # Backstop: the finished call slipped past the deadline (e.g. it started
        # before the budget was consumed elsewhere). Report as before.
        raise HTTPException(
            status_code=504,
            detail=detail_body(
                "REVIEW_TIMEOUT",
                f"review exceeded budget {budget_ms:.0f}ms (took {total_ms:.0f}ms)",
                max_chars=_max_msg(cfg),
            ),
        )
    return resp
