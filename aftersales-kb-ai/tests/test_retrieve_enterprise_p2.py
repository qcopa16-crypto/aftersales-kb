"""Enterprise P2/ops closure tests (A4/C3/C4/D4/E3).

约束: 业务夹具只使用 工单处理SOP.md；pytest -k 时必须 --ignore=tests/test_langgraph_real.py
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

from schemas.ingest import AclFilter  # noqa: E402
from ai.retrieve.lexical import LexicalIndex  # noqa: E402
from ai.retrieve.pipeline import (  # noqa: E402
    RetrieveRequestView,
    reset_bulkhead_for_tests,
    reset_embed_cache_for_tests,
    reset_qps_for_tests,
    run_retrieve_pipeline,
)
from ai.retrieve.store import MemoryVectorStore, VectorItem  # noqa: E402
from core.circuit import get_metrics  # noqa: E402

SOP = Path(__file__).resolve().parents[2] / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
assert SOP.is_file(), SOP
SOP_TEXT = SOP.read_text(encoding="utf-8")


class CountingEmbedder:
    def __init__(self, dim: int = 32) -> None:
        self.dim = dim
        self.encode_calls = 0

    def encode(self, texts):
        import hashlib

        self.encode_calls += 1
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([((h[i % len(h)] + i) % 255) / 255.0 - 0.5 for i in range(self.dim)])
        return out


def _cfg(**over) -> Settings:
    base = dict(
        vector_store="memory",
        embed_provider="fake",
        embed_dim=32,
        retrieve_hybrid_enabled=True,
        retrieve_multi_replica_safe=False,
        retrieve_max_concurrency=32,
        retrieve_qps_limit=0.0,
        retrieve_acl_fields_required=False,
        retrieve_struct_log=True,
        retrieve_embed_cache_max=0,
        retrieve_max_unit_ids=500,
    )
    base.update(over)
    return Settings(**base)


def _seed(dim: int = 32) -> MemoryVectorStore:
    store = MemoryVectorStore(dim=dim)
    emb = CountingEmbedder(dim)
    vec = emb.encode([SOP_TEXT[:400]])[0]
    store.publish_unit(
        "U-p2",
        [
            VectorItem(
                chunk_index=0,
                content=SOP_TEXT[:400],
                embedding=vec,
                version_id="v1",
                acl_department="售后部",
                acl_classification="internal",
            )
        ],
    )
    return store


def test_t_a4_01_missing_acl_fields_dropped_no_corpus_leak():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    emb = CountingEmbedder(32)

    class _StubStore:
        def search(self, query_vector, top_k=8, *, filter=None):
            return [
                {
                    "id": "legacy0",
                    "unit_id": "U-legacy",
                    "chunk_index": 0,
                    "content": SOP_TEXT[:200],
                    "version_id": "v1",
                    "score": 0.99,
                    "acl_department": "",
                    "acl_classification": "",
                    "heading_path": [],
                },
                {
                    "id": "ok0",
                    "unit_id": "U-ok",
                    "chunk_index": 0,
                    "content": "工单处理 SOP",
                    "version_id": "v1",
                    "score": 0.5,
                    "acl_department": "售后部",
                    "acl_classification": "internal",
                    "heading_path": [],
                },
            ]

        def ensure_collection(self):
            return None

        def list_active_chunks(self, limit=10000):
            return []

    cfg = _cfg(retrieve_acl_fields_required=True, retrieve_hybrid_enabled=False)
    before = get_metrics().snapshot()["counters"].get("retrieve.acl_fields_missing_drop", 0)
    result = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=5,
            acl_filter=AclFilter(department="售后部", classification="internal"),
        ),
        embedder=emb,
        store=_StubStore(),  # type: ignore[arg-type]
        lexical=LexicalIndex(),
        cfg=cfg,
    )
    after = get_metrics().snapshot()["counters"].get("retrieve.acl_fields_missing_drop", 0)
    assert after > before, "A4 drop metric must increment for missing ACL fields"
    assert all(ch["unit_id"] != "U-legacy" for ch in result["chunks"])
    assert any(ch["unit_id"] == "U-ok" for ch in result["chunks"])


def test_t_c3_01_struct_log_has_q_hash_and_stage_ms(caplog):
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    store = _seed()
    emb = CountingEmbedder()
    with caplog.at_level(logging.INFO, logger="ai.retrieve.pipeline"):
        result = run_retrieve_pipeline(
            request=RetrieveRequestView(
                question="工单处理 SOP",
                top_k=2,
                acl_filter=AclFilter(unit_ids=["U-p2"]),
            ),
            embedder=emb,
            store=store,
            lexical=LexicalIndex(),
            cfg=_cfg(),
        )
    assert result["took_ms"] >= 0
    struct_lines = [r.getMessage() for r in caplog.records if "retrieve_struct" in r.getMessage()]
    assert struct_lines, caplog.text
    payload = struct_lines[-1]
    assert "q_hash" in payload
    assert "stage_embed_ms" in payload
    assert "工单处理" not in payload


def test_t_c4_01_acl_empty_and_total_requests():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    store = _seed()
    emb = CountingEmbedder()
    before = get_metrics().snapshot()["counters"]
    total0 = before.get("retrieve.total_requests", 0)
    acl_empty0 = before.get("retrieve.acl_empty", 0)
    result = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=2,
            acl_filter=AclFilter(unit_ids=["U-NONE"]),
        ),
        embedder=emb,
        store=store,
        lexical=LexicalIndex(),
        cfg=_cfg(retrieve_hybrid_enabled=False),
    )
    assert result["chunks"] == []
    assert result["acl_applied"] is True
    after = get_metrics().snapshot()["counters"]
    assert after.get("retrieve.total_requests", 0) > total0
    assert after.get("retrieve.acl_empty", 0) > acl_empty0
    assert "ratios" in get_metrics().snapshot()


def test_t_d4_01_embed_cache_hit_skips_encode():
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    reset_embed_cache_for_tests()
    store = _seed()
    emb = CountingEmbedder()
    cfg = _cfg(retrieve_embed_cache_max=8, retrieve_hybrid_enabled=False)
    run_retrieve_pipeline(
        request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=["U-p2"]),
        embedder=emb,
        store=store,
        lexical=LexicalIndex(),
        cfg=cfg,
    )
    calls_after_first = emb.encode_calls
    assert calls_after_first >= 1
    run_retrieve_pipeline(
        request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=["U-p2"]),
        embedder=emb,
        store=store,
        lexical=LexicalIndex(),
        cfg=cfg,
    )
    assert emb.encode_calls == calls_after_first
    counters = get_metrics().snapshot()["counters"]
    assert counters.get("retrieve.embed_cache_hit", 0) >= 1
    reset_embed_cache_for_tests()


def test_t_e3_01_graph_import_and_multi_replica_alignment():
    from ai.graphs.retrieve_graph import build_retrieve_graph, run_retrieve

    assert callable(build_retrieve_graph)
    assert callable(run_retrieve)
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    store = _seed()
    emb = CountingEmbedder()
    cfg = _cfg(retrieve_multi_replica_safe=True, retrieve_hybrid_enabled=True)
    result = run_retrieve(
        request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=["U-p2"]),
        embedder=emb,
        store=store,
        lexical=LexicalIndex(),
        cfg=cfg,
    )
    assert result["hybrid"] is False
    assert result["chunks"]


def test_graph_node_normalize_forces_hybrid_off_when_multi_replica():
    from ai.graphs.retrieve_graph import build_retrieve_graph

    store = _seed()
    emb = CountingEmbedder()
    cfg = _cfg(retrieve_multi_replica_safe=True)
    app_graph = build_retrieve_graph(embedder=emb, store=store, lexical=LexicalIndex(), cfg=cfg)
    out = app_graph.invoke({"question": "工单处理 SOP", "top_k": 2, "unit_ids": ["U-p2"]})
    assert out.get("hybrid") is False
    assert out.get("chunks") is not None
