import time

from core.config import Settings
from core.limits import Limits, RateLimiter


def test_rate_limiter_allows_within_window():
    rl = RateLimiter(3, window_sec=1.0)
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire() is True
    assert rl.acquire(timeout=0.05) is False


def test_rate_limiter_window_slides():
    rl = RateLimiter(1, window_sec=0.15)
    assert rl.acquire() is True
    assert rl.acquire(timeout=0.02) is False
    time.sleep(0.2)
    assert rl.acquire() is True


def test_limits_defaults_match_product_caps():
    cfg = Settings(
        vlm_max_concurrency=10,
        llm_max_concurrency=5,
        mineru_max_concurrency=1,
        mineru_submit_per_minute=50,
        mineru_poll_per_minute=1000,
        mineru_daily_file_limit=5000,
    )
    lim = Limits(cfg)
    snap = lim.snapshot()
    assert snap["vlm_max_concurrency"] == 10
    assert snap["llm_max_concurrency"] == 5
    assert snap["mineru_max_concurrency"] == 1
    assert snap["mineru_submit_per_minute"] == 50
    assert snap["mineru_poll_per_minute"] == 1000
    assert snap["mineru_daily_limit"] == 5000


def test_mineru_daily_quota():
    cfg = Settings(mineru_daily_file_limit=2)
    lim = Limits(cfg)
    assert lim.try_consume_mineru_file(1) is True
    assert lim.try_consume_mineru_file(1) is True
    assert lim.try_consume_mineru_file(1) is False


def test_vlm_slot_blocks_over_cap():
    import threading

    cfg = Settings(vlm_max_concurrency=2)
    lim = Limits(cfg)
    entered = []
    done = threading.Event()

    def worker():
        with lim.vlm_slot():
            entered.append(1)
            time.sleep(0.05)
        if len(entered) >= 4:
            done.set()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)
    assert len(entered) == 4
