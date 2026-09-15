"""Classified retries: exponential backoff for transient errors only."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorClass(str, Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


# Error codes / substrings treated as transient.
# Circuit-open codes are intentionally NOT retryable: fail fast so we do not
# hammer a cooling dependency; Java may re-dispatch after cooldown.
_RETRYABLE_CODES = {
    "MINERU_TIMEOUT",
    "MINERU_RATE_LIMIT",
    "SOURCE_FETCH_FAILED",
    "SOURCE_TIMEOUT",
    "EMBED_FAILED",
    "RETRIEVE_FAILED",
    "PARSE_FAILED",
    "PARSE_QUEUE_FULL",
    "PARSE_TIMEOUT",
    "TIMEOUT",
}

_NON_RETRYABLE_CODES = {
    "BAD_SOURCE",
    "BAD_TYPE",
    "EMPTY_PDF",
    "PARSE_EMPTY_RESULT",
    "CHUNKS_HASH_MISMATCH",
    "RESUME_CHECKPOINT_MISMATCH",
    "EMBED_EMPTY_DOCUMENT",
    "EMBED_QUALITY_GATE",
    "EMBED_CONTENT_TOO_LONG",
    "EMBED_CLIENT_ERROR",
    "EMBED_DIM_MISMATCH",
    "EMBED_TOO_MANY_CHUNKS",
    "MARKDOWN_TOO_LONG",
    "PAYLOAD_TOO_LARGE",
    "EMBED_CIRCUIT_OPEN",
    "MINERU_CIRCUIT_OPEN",
    "SOURCE_CIRCUIT_OPEN",
    "UPLOAD_TOO_LARGE",
    "TASK_CANCELLED",
    "RETRIEVE_ACL_REQUIRED",
    "RETRIEVE_INVALID_FILTER",
    "RETRIEVE_TIMEOUT",
    "RATE_LIMIT",
    "EMBED_ACL_FIELDS_REQUIRED",
    "SOURCE_SSRF_BLOCKED",
    "SOURCE_BAD_URL",
    "SOURCE_EMPTY",
    "SOURCE_CLIENT_ERROR",
    "SOURCE_TOO_LARGE",
    "MINERU_DISABLED",
    "MINERU_NO_TOKEN",
    "MINERU_BAD_FILE",
    "MINERU_EMPTY_MD",
    "MINERU_DAILY_QUOTA",
    "MINERU_EXTRACT_FAILED",
}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_sec: float = 0.2
    max_delay_sec: float = 5.0
    multiplier: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        """attempt is 1-based failure count before sleep."""
        raw = self.base_delay_sec * (self.multiplier ** max(0, attempt - 1))
        return min(raw, self.max_delay_sec)


def _extract_error_code(exc: BaseException) -> str:
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code:
        return code.upper()
    return type(exc).__name__.upper()


def classify_exception(exc: BaseException) -> ErrorClass:
    code = _extract_error_code(exc)
    if code in _NON_RETRYABLE_CODES:
        return ErrorClass.NON_RETRYABLE
    if code in _RETRYABLE_CODES:
        return ErrorClass.RETRYABLE
    name = type(exc).__name__.lower()
    if any(x in name for x in ("timeout", "connection", "temporarily")):
        return ErrorClass.RETRYABLE
    # httpx-style status if present
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        if status >= 500 or status == 429:
            return ErrorClass.RETRYABLE
        if 400 <= status < 500:
            return ErrorClass.NON_RETRYABLE
    # Unknown: do not amplify load.
    return ErrorClass.NON_RETRYABLE


def is_retryable(exc: BaseException) -> bool:
    return classify_exception(exc) is ErrorClass.RETRYABLE


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> T:
    """Call fn; retry only classified-retryable failures with exponential backoff."""
    pol = policy or RetryPolicy()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt >= pol.max_attempts or not is_retryable(exc):
                raise
            delay = pol.delay_for_attempt(attempt)
            logger.warning(
                "retryable failure attempt=%s/%s delay=%.2fs: %s",
                attempt,
                pol.max_attempts,
                delay,
                exc,
            )
            sleep(delay)


def retryable(policy: RetryPolicy | None = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return retry_call(fn, *args, policy=policy, **kwargs)

        return wrapper

    return deco
