"""Enterprise audit-gap fix tests (P0/P1).

Covers: multi-replica dense-only, bulkhead, QPS, stage metrics, unit_ids cap,
lexical cold-sync timeout degrade.

约束:
- 业务夹具只使用 docs/demo-knowledge/sop/工单处理SOP.md
- 默认 memory + fake embed
- 不 setdefault ENABLE_PARSE_IDEMPOTENCY
- pytest -k 时必须 --ignore=tests/test_langgraph_real.py
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

from schemas.ingest import AclFilter  # noqa: E402
from ai.retrieve.lexical import LexicalIndex  # noqa: E402
from ai.retrieve.pipeline import (  # noqa: E402
    RetrieveError,
    RetrieveRequestView,
    reset_bulkhead_for_tests,
    reset_qps_for_tests,
    reset_store_pool_for_tests,
    run_retrieve_pipeline,
)
from ai.retrieve.store import MemoryVectorStore, VectorItem  # noqa: E402
from core.circuit import get_metrics  # noqa: E402
from core.retry import _NON_RETRYABLE_CODES, _RETRYABLE_CODES, classify_exception  # noqa: E402

SOP = Path(__file__).resolve().parents[2] / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
assert SOP.is_file(), f"missing fixture: {SOP}"
SOP_TEXT = SOP.read_text(encoding="utf-8")


def _fake_embedder(dim: int = 32):
    class _E:
        def encode(self, texts):
            import hashlib

            out = []
            for t in texts:
                h = hashlib.sha256(t.encode("utf-8")).digest()
                vals = [((h[i % len(h)] + i) % 255) / 255.0 - 0.5 for i in range(dim)]
                out.append(vals)
            return out

    return _E()


def _seed_store(dim: int = 32) -> MemoryVectorStore:
    store = MemoryVectorStore(dim=dim)
    emb = _fake_embedder(dim)
    vecs = emb.encode([SOP_TEXT[:800], "工单处理 SOP 退换货", "屏幕碎了保修政策"])
    store.publish_unit(
        "U-FIX",
        [
            VectorItem(
                chunk_index=0,
                content=SOP_TEXT[:800],
                embedding=vecs[0],
                version_id="v1",
                acl_department="售后部",
                acl_classification="internal",
            ),
            VectorItem(
                chunk_index=1,
                content="工单处理 SOP 退换货流程",
                embedding=vecs[1],
                version_id="v1",
                acl_department="售后部",
                acl_classification="internal",
            ),
        ],
    )
    store.publish_unit(
        "U-OTHER",
        [
            VectorItem(
                chunk_index=0,
                content="屏幕碎了保修政策说明",
                embedding=vecs[2],
                version_id="v1",
                acl_department="售后部",
                acl_classification="internal",
            )
        ],
    )
    return store


def _base_cfg(**overrides) -> Settings:
    data = dict(
        vector_store="memory",
        embed_provider="fake",
        embed_dim=32,
        internal_token="test-token",
        mineru_enabled=False,
        retrieve_require_acl=False,
        retrieve_hybrid_enabled=True,
        retrieve_multi_replica_safe=False,
        retrieve_max_concurrency=32,
        retrieve_qps_limit=0.0,
        retrieve_store_timeout_sec=5.0,
        retrieve_overall_timeout_sec=8.0,
        retrieve_lexical_max_docs=5000,
        retrieve_max_unit_ids=500,
        retrieve_lexical_sync_timeout_sec=0.0,
    )
    data.update(overrides)
    return Settings(**data)


def test_t_mr_01_multi_replica_safe_dense_only_acl_still_fail_closed():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg(retrieve_multi_replica_safe=True)
    store = _seed_store()
    lexical = LexicalIndex()
    lexical.sync_from_store(store)

    result = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=3,
            acl_filter=AclFilter(unit_ids=["U-FIX"], department="售后部", classification="internal"),
        ),
        embedder=_fake_embedder(),
        store=store,
        lexical=lexical,
        cfg=cfg,
    )
    assert result["hybrid"] is False
    assert result["acl_applied"] is True
    assert result["chunks"], "expected dense hits under multi-replica mode"
    metrics = get_metrics().snapshot()["counters"]
    assert metrics.get("retrieve.multi_replica_dense_only", 0) >= 1
    empty = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=3,
            acl_filter=AclFilter(unit_ids=["U-NONE"]),
        ),
        embedder=_fake_embedder(),
        store=store,
        lexical=lexical,
        cfg=cfg,
    )
    assert empty["chunks"] == []
    assert empty["acl_applied"] is True


def test_t_bh_01_bulkhead_second_request_429():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg(retrieve_max_concurrency=1)
    store = _seed_store()
    lexical = LexicalIndex()
    started = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    results: list[dict] = []

    class _BlockStore:
        def search(self, query_vector, top_k=8, *, filter=None):
            started.set()
            assert release.wait(timeout=5), "blocker not released"
            return store.search(query_vector, top_k=top_k, filter=filter)

        def ensure_collection(self):
            return None

        def list_active_chunks(self, limit=10000):
            return store.list_active_chunks(limit=limit)

    def _run_blocked():
        try:
            results.append(
                run_retrieve_pipeline(
                    request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
                    embedder=_fake_embedder(),
                    store=_BlockStore(),  # type: ignore[arg-type]
                    lexical=lexical,
                    cfg=cfg,
                )
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=_run_blocked, daemon=True)
    t.start()
    try:
        assert started.wait(timeout=5)
        try:
            run_retrieve_pipeline(
                request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
                embedder=_fake_embedder(),
                store=store,
                lexical=lexical,
                cfg=cfg,
            )
            raise AssertionError("expected bulkhead 429")
        except RetrieveError as exc:
            assert exc.status_code == 429
            assert exc.error_code == "RATE_LIMIT"
    finally:
        release.set()
        t.join(timeout=5)
        reset_bulkhead_for_tests()
    assert not errors, errors
    assert results and results[0]["chunks"] is not None
    metrics = get_metrics().snapshot()["counters"]
    assert metrics.get("retrieve.rejected_concurrency", 0) >= 1


def test_t_rl_01_qps_limit_429():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg(retrieve_qps_limit=1.0)
    store = _seed_store()
    lexical = LexicalIndex()
    embedder = _fake_embedder()
    try:
        first = run_retrieve_pipeline(
            request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
            embedder=embedder,
            store=store,
            lexical=lexical,
            cfg=cfg,
        )
        assert first["took_ms"] >= 0
        try:
            run_retrieve_pipeline(
                request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
                embedder=embedder,
                store=store,
                lexical=lexical,
                cfg=cfg,
            )
            raise AssertionError("expected QPS 429")
        except RetrieveError as exc:
            assert exc.status_code == 429
            assert exc.error_code == "RATE_LIMIT"
        metrics = get_metrics().snapshot()["counters"]
        assert metrics.get("retrieve.rate_limited", 0) >= 1
    finally:
        reset_qps_for_tests()


def test_rate_limit_code_is_non_retryable():
    assert "RATE_LIMIT" in _NON_RETRYABLE_CODES
    assert "RATE_LIMIT" not in _RETRYABLE_CODES

    class _E(Exception):
        error_code = "RATE_LIMIT"

    from core.retry import ErrorClass, is_retryable

    assert classify_exception(_E()) is ErrorClass.NON_RETRYABLE
    assert is_retryable(_E()) is False


def test_t_stg_01_stage_metrics_present_after_success():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg()
    store = _seed_store()
    lexical = LexicalIndex()
    result = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=2,
            acl_filter=AclFilter(unit_ids=["U-FIX"]),
        ),
        embedder=_fake_embedder(),
        store=store,
        lexical=lexical,
        cfg=cfg,
    )
    assert result["took_ms"] >= 0
    snap = get_metrics().snapshot()
    for key in (
        "retrieve.stage_embed_ms",
        "retrieve.stage_store_ms",
        "retrieve.stage_lexical_ms",
        "retrieve.stage_fuse_ms",
        "retrieve.latency",
    ):
        assert key in snap["timings"], f"missing timing {key}: {list(snap['timings'])}"
    assert "retrieve.latency" in snap.get("histograms", {})


def test_t_uid_01_unit_ids_over_cap_400():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg(retrieve_max_unit_ids=3)
    store = _seed_store()
    lexical = LexicalIndex()
    long_ids = [f"U{i}" for i in range(5)]
    try:
        run_retrieve_pipeline(
            request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=long_ids),
            embedder=_fake_embedder(),
            store=store,
            lexical=lexical,
            cfg=cfg,
        )
        raise AssertionError("expected unit_ids cap 400")
    except RetrieveError as exc:
        assert exc.status_code == 400
        assert exc.error_code == "RETRIEVE_INVALID_FILTER"
        assert "retrieve_max_unit_ids" in str(exc)


class _SlowListStore:
    def __init__(self, delay_sec: float = 0.5) -> None:
        self.delay_sec = delay_sec
        self.search_calls = 0

    def list_active_chunks(self, limit=10000):
        time.sleep(self.delay_sec)
        return []

    def search(self, query_vector, top_k=8, *, filter=None):
        self.search_calls += 1
        return [
            {
                "id": "c0",
                "unit_id": "U-FIX",
                "chunk_index": 0,
                "content": "工单处理 SOP",
                "version_id": "v1",
                "score": 0.9,
                "acl_department": "售后部",
                "acl_classification": "internal",
                "heading_path": [],
            }
        ]

    def ensure_collection(self):
        return None


def test_t_sync_01_cold_sync_timeout_degrades_not_500():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    reset_store_pool_for_tests()
    cfg = _base_cfg(
        retrieve_hybrid_enabled=True,
        retrieve_hybrid_degrade_to_dense=True,
        retrieve_lexical_sync_timeout_sec=0.05,
        retrieve_store_pool_size=2,
        retrieve_overall_timeout_sec=5.0,
    )
    store = _SlowListStore(delay_sec=0.3)
    lexical = LexicalIndex()
    assert lexical.doc_count() == 0
    try:
        result = run_retrieve_pipeline(
            request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
            embedder=_fake_embedder(),
            store=store,  # type: ignore[arg-type]
            lexical=lexical,
            cfg=cfg,
        )
        assert result["degraded_to_dense"] is True
        assert result["chunks"], "dense path should still return hits"
        metrics = get_metrics().snapshot()["counters"]
        assert metrics.get("retrieve.hybrid_degraded", 0) >= 1
    finally:
        reset_store_pool_for_tests()


def test_lexical_capped_metric_when_over_cap():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    cfg = _base_cfg(retrieve_lexical_max_docs=1, retrieve_hybrid_enabled=True)
    store = _seed_store()
    lexical = LexicalIndex()
    lexical.sync_from_store(store)
    assert lexical.doc_count() > 1
    before = get_metrics().snapshot()["counters"].get("retrieve.lexical_capped", 0)
    run_retrieve_pipeline(
        request=RetrieveRequestView(question="工单处理 SOP", top_k=2),
        embedder=_fake_embedder(),
        store=store,
        lexical=lexical,
        cfg=cfg,
    )
    after = get_metrics().snapshot()["counters"].get("retrieve.lexical_capped", 0)
    assert after > before


def test_shared_store_pool_reused():
    from ai.retrieve.pipeline import get_store_pool

    reset_store_pool_for_tests()
    p1 = get_store_pool(4)
    p2 = get_store_pool(4)
    assert p1 is p2
    reset_store_pool_for_tests()
