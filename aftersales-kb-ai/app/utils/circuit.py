"""Simple circuit breaker + lightweight service metrics (process-local)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CircuitState:
    failures: int = 0
    open_until: float = 0.0
    last_error: str = ""


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_sec: float = 60.0) -> None:
        self._threshold = max(1, failure_threshold)
        self._cooldown = max(0.05, cooldown_sec)
        self._lock = threading.Lock()
        self._states: dict[str, CircuitState] = {}

    def is_open(self, name: str) -> bool:
        with self._lock:
            st = self._states.get(name)
            if not st:
                return False
            if st.open_until and time.monotonic() < st.open_until:
                return True
            return False

    def record_success(self, name: str) -> None:
        with self._lock:
            st = self._states.get(name)
            if st:
                st.failures = 0
                st.open_until = 0.0
                st.last_error = ""

    def record_failure(self, name: str, error: str = "") -> None:
        with self._lock:
            st = self._states.setdefault(name, CircuitState())
            st.failures += 1
            st.last_error = error[:200]
            if st.failures >= self._threshold:
                st.open_until = time.monotonic() + self._cooldown

    def snapshot(self) -> dict[str, dict]:
        now = time.monotonic()
        out: dict[str, dict] = {}
        with self._lock:
            for name, st in self._states.items():
                out[name] = {
                    "failures": st.failures,
                    "open": bool(st.open_until and now < st.open_until),
                    "open_for_sec": max(0.0, round(st.open_until - now, 1)) if st.open_until > now else 0.0,
                    "last_error": st.last_error,
                }
        return out


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list[float]] = {}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe_ms(self, name: str, ms: float) -> None:
        with self._lock:
            arr = self._timings.setdefault(name, [])
            arr.append(ms)
            if len(arr) > 200:
                del arr[:-200]

    def snapshot(self) -> dict:
        with self._lock:
            timings = {}
            for k, arr in self._timings.items():
                if not arr:
                    continue
                s = sorted(arr)
                timings[k] = {
                    "count": len(s),
                    "p50_ms": round(s[len(s) // 2], 1),
                    "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 1),
                }
            return {"counters": dict(self._counters), "timings": timings}


_circuits: CircuitBreaker | None = None
_metrics: Metrics | None = None
_lock = threading.Lock()


def get_circuits(threshold: int = 3, cooldown_sec: float = 60.0) -> CircuitBreaker:
    global _circuits
    with _lock:
        if _circuits is None:
            _circuits = CircuitBreaker(threshold, cooldown_sec)
        return _circuits


def get_metrics() -> Metrics:
    global _metrics
    with _lock:
        if _metrics is None:
            _metrics = Metrics()
        return _metrics
