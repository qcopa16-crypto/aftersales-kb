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
    # Process-local latency histogram buckets (ms) for SLO-friendly /metrics.
    LATENCY_BUCKETS_MS = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._timings: dict[str, list[float]] = {}
        self._histograms: dict[str, list[int]] = {}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe_ms(self, name: str, ms: float) -> None:
        with self._lock:
            arr = self._timings.setdefault(name, [])
            arr.append(ms)
            if len(arr) > 200:
                del arr[:-200]
            buckets = self._histograms.setdefault(
                name, [0] * (len(self.LATENCY_BUCKETS_MS) + 1)
            )
            for i, bound in enumerate(self.LATENCY_BUCKETS_MS):
                if ms <= bound:
                    buckets[i] += 1
                    return
            buckets[-1] += 1

    def histogram_snapshot(self) -> dict[str, dict]:
        with self._lock:
            return self._histogram_snapshot_unlocked()

    def _histogram_snapshot_unlocked(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, buckets in self._histograms.items():
            cumulative = 0
            by_bucket: dict[str, int] = {}
            for i, bound in enumerate(self.LATENCY_BUCKETS_MS):
                cumulative += buckets[i]
                by_bucket[f"le_{int(bound)}"] = cumulative
            cumulative += buckets[-1]
            by_bucket["le_inf"] = cumulative
            out[name] = by_bucket
        return out

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
            counters = dict(self._counters)
            ok = counters.get("retrieve.ok", 0)
            ratios: dict[str, float] = {}
            if ok > 0:
                ratios["retrieve.empty_ratio"] = round(counters.get("retrieve.empty", 0) / ok, 4)
                # Rows dropped / successful requests (may exceed 1.0; ops diagnostic, not a rate).
                ratios["retrieve.filtered_per_ok"] = round(
                    counters.get("retrieve.filtered_out", 0) / ok, 4
                )
            return {
                "counters": counters,
                "timings": timings,
                "histograms": self._histogram_snapshot_unlocked(),
                "ratios": ratios,
            }

    def to_prometheus_text(self) -> str:
        """Minimal Prometheus text exposition; no third-party client."""
        snap = self.snapshot()
        lines: list[str] = []
        for name, value in sorted(snap["counters"].items()):
            metric = _prom_name(name)
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {value}")
        for name, stats in sorted(snap["timings"].items()):
            metric = _prom_name(name)
            lines.append(f"# TYPE {metric} summary")
            lines.append(f'{metric}{{quantile="0.5"}} {stats["p50_ms"]}')
            lines.append(f'{metric}{{quantile="0.95"}} {stats["p95_ms"]}')
            lines.append(f"{metric}_count {stats['count']}")
        for name, buckets in sorted(snap.get("histograms", {}).items()):
            metric = _prom_name(name) + "_bucket"
            lines.append(f"# TYPE {metric} histogram")
            for le, count in buckets.items():
                bound = "+Inf" if le == "le_inf" else le.removeprefix("le_")
                lines.append(f'{metric}{{le="{bound}"}} {count}')
        return "\n".join(lines) + ("\n" if lines else "")


def _prom_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not safe or safe[0].isdigit():
        safe = f"m_{safe}"
    return f"kb_ai_{safe}"


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
