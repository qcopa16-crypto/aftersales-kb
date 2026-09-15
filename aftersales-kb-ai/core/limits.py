"""Process-wide concurrency + rate limits for external AI / parse APIs.

Caps (product + vendor):
- VLM describe: max concurrent 10
- LLM chat: max concurrent 5
- MinerU submit: 50 files/min (vendor), process serial by default
- MinerU poll: 1000/min (vendor)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from functools import lru_cache

from core.config import Settings, get_settings


class RateLimiter:
    """Sliding-window limiter: at most `limit` acquisitions per `window_sec`."""

    def __init__(self, limit: int, window_sec: float = 60.0) -> None:
        self._limit = max(1, limit)
        self._window = max(0.001, window_sec)
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a slot is free. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= self._window:
                    self._hits.popleft()
                if len(self._hits) < self._limit:
                    self._hits.append(now)
                    return True
                wait = self._window - (now - self._hits[0])
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.05) if wait > 0 else 0.01)

    @contextmanager
    def slot(self, timeout: float | None = 30.0):
        if not self.acquire(timeout=timeout):
            raise TimeoutError("rate limit wait timeout")
        try:
            yield
        finally:
            pass  # window uses timestamps; nothing to release


class Limits:
    def __init__(self, cfg: Settings) -> None:
        self.vlm_max = max(1, cfg.vlm_max_concurrency)
        self.llm_max = max(1, cfg.llm_max_concurrency)
        self.mineru_max = max(1, cfg.mineru_max_concurrency)
        self.vlm_conc = threading.Semaphore(self.vlm_max)
        self.llm_conc = threading.Semaphore(self.llm_max)
        self.mineru_conc = threading.Semaphore(self.mineru_max)
        # vendor: 50 submit/min, 1000 poll/min
        self.mineru_submit = RateLimiter(cfg.mineru_submit_per_minute, 60.0)
        self.mineru_poll = RateLimiter(cfg.mineru_poll_per_minute, 60.0)
        self._daily_lock = threading.Lock()
        self._daily_count = 0
        self._daily_day = time.strftime("%Y-%m-%d")
        self.mineru_daily_file_limit = cfg.mineru_daily_file_limit

    @contextmanager
    def vlm_slot(self):
        with self.vlm_conc:
            yield

    @contextmanager
    def llm_slot(self):
        with self.llm_conc:
            yield

    @contextmanager
    def mineru_submit_slot(self, timeout: float = 60.0):
        with self.mineru_conc:
            with self.mineru_submit.slot(timeout=timeout):
                yield

    @contextmanager
    def mineru_poll_slot(self, timeout: float = 30.0):
        with self.mineru_poll.slot(timeout=timeout):
            yield

    def try_consume_mineru_file(self, n: int = 1) -> bool:
        """Daily file quota (vendor: 5000 files/day/user)."""
        with self._daily_lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._daily_day:
                self._daily_day = today
                self._daily_count = 0
            if self._daily_count + n > self.mineru_daily_file_limit:
                return False
            self._daily_count += n
            return True

    def snapshot(self) -> dict:
        return {
            "vlm_max_concurrency": self.vlm_max,
            "llm_max_concurrency": self.llm_max,
            "mineru_max_concurrency": self.mineru_max,
            "mineru_submit_per_minute": self.mineru_submit._limit,
            "mineru_poll_per_minute": self.mineru_poll._limit,
            "mineru_daily_used": self._daily_count,
            "mineru_daily_limit": self.mineru_daily_file_limit,
        }


@lru_cache
def get_limits() -> Limits:
    return Limits(get_settings())
