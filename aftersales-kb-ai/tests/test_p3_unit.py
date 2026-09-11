"""P3 unit tests: circuit breaker + embed partial store."""

import time

from app.utils.circuit import CircuitBreaker, Metrics
from app.services.embed_partial import EmbedPartial, get_embed_partial_store


def test_circuit_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=2, cooldown_sec=0.2)
    assert cb.is_open("vlm") is False
    cb.record_failure("vlm", "e1")
    assert cb.is_open("vlm") is False
    cb.record_failure("vlm", "e2")
    assert cb.is_open("vlm") is True
    time.sleep(0.25)
    assert cb.is_open("vlm") is False


def test_circuit_success_resets():
    cb = CircuitBreaker(failure_threshold=2, cooldown_sec=30)
    cb.record_failure("mineru")
    cb.record_success("mineru")
    cb.record_failure("mineru")
    assert cb.is_open("mineru") is False


def test_metrics_snapshot():
    m = Metrics()
    m.inc("embed.ok", 2)
    m.observe_ms("embed", 10)
    m.observe_ms("embed", 20)
    snap = m.snapshot()
    assert snap["counters"]["embed.ok"] == 2
    assert snap["timings"]["embed"]["count"] == 2


def test_embed_partial_store():
    store = get_embed_partial_store(max_units=8)
    store.put(EmbedPartial(unit_id="U-p", dim=4, vectors=[[0.1] * 4], contents=["a"]))
    got = store.get("U-p")
    assert got is not None and len(got.vectors) == 1
    store.clear("U-p")
    assert store.get("U-p") is None
