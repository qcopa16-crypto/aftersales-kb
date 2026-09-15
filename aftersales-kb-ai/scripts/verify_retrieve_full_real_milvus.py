"""Full retrieve chain on real Milvus (dedicated collection, fake embed).

Covers: hybrid path + ACL fail-closed + multi-replica dense-only + bulkhead/QPS
signals + stage metrics. Never touches production kb_chunks.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

COLL = "kb_ai_chunks_retrieve_full_verify"
MAP = ROOT / ".data" / "retrieve_full_active_verify.json"

os.environ["VECTOR_STORE"] = "milvus"
os.environ["EMBED_PROVIDER"] = "fake"
os.environ["EMBED_DIM"] = "32"
os.environ["MILVUS_URI"] = "http://127.0.0.1:19530"
os.environ["MILVUS_COLLECTION"] = COLL
os.environ["MILVUS_ACTIVE_MAP_PATH"] = str(MAP)
os.environ["INTERNAL_TOKEN"] = "test-token"
os.environ["MINERU_ENABLED"] = "false"
if MAP.exists():
    MAP.unlink()

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
from ai.retrieve.store import VectorItem, create_vector_store  # noqa: E402
from core.circuit import get_circuits, get_metrics  # noqa: E402

SOP = ROOT.parent / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
if not SOP.is_file():
    SOP = ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
assert SOP.is_file(), f"missing fixture: {SOP}"
SOP_TEXT = SOP.read_text(encoding="utf-8")


class FakeEmbedder:
    def encode(self, texts):
        import hashlib

        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([((h[i % len(h)] + i) % 255) / 255.0 - 0.5 for i in range(32)])
        return out


def cfg(**over) -> Settings:
    base = dict(
        vector_store="milvus",
        embed_provider="fake",
        embed_dim=32,
        milvus_uri="http://127.0.0.1:19530",
        milvus_collection=COLL,
        milvus_active_map_path=str(MAP),
        internal_token="test-token",
        retrieve_require_acl=False,
        retrieve_hybrid_enabled=True,
        retrieve_multi_replica_safe=False,
        retrieve_max_concurrency=32,
        retrieve_qps_limit=0.0,
        retrieve_store_timeout_sec=5.0,
        retrieve_overall_timeout_sec=8.0,
        retrieve_lexical_max_docs=5000,
        retrieve_max_unit_ids=500,
    )
    base.update(over)
    return Settings(**base)


def main() -> int:
    reset_store_pool_for_tests()
    reset_bulkhead_for_tests()
    reset_qps_for_tests()
    get_circuits.cache_clear() if hasattr(get_circuits, "cache_clear") else None

    c = cfg()
    store = create_vector_store(c)
    store.ensure_collection()
    print("store ready", COLL, flush=True)

    unit = "U-full-" + uuid.uuid4().hex[:8]
    unit_other = "U-other-" + uuid.uuid4().hex[:8]
    emb = FakeEmbedder()
    texts = [SOP_TEXT[:600], "工单处理 SOP 退换货", "屏幕碎了保修政策"]
    vecs = emb.encode(texts)

    def item(idx: int, content: str, vec: list[float]) -> VectorItem:
        return VectorItem(
            chunk_index=idx,
            content=content,
            embedding=vec,
            embed_model="fake:full",
            version_id="v1",
            acl_department="售后部",
            acl_classification="internal",
        )

    n1 = store.publish_unit(unit, [item(0, texts[0], vecs[0]), item(1, texts[1], vecs[1])])
    n2 = store.publish_unit(unit_other, [item(0, texts[2], vecs[2])])
    print("published", n1, n2, store.get_active_version(unit), flush=True)
    assert n1 == 2 and MAP.is_file()

    # 1) hybrid + ACL unit allow
    lexical = LexicalIndex()
    hybrid = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP 退换货",
            top_k=3,
            acl_filter=AclFilter(unit_ids=[unit], department="售后部", classification="internal"),
        ),
        embedder=emb,
        store=store,
        lexical=lexical,
        cfg=cfg(),
    )
    assert hybrid["hybrid"] is True
    assert hybrid["acl_applied"] is True
    assert hybrid["chunks"], "hybrid retrieve returned empty"
    assert all(ch["unit_id"] == unit for ch in hybrid["chunks"])
    print("hybrid+acl ok", len(hybrid["chunks"]), "took_ms", hybrid["took_ms"], flush=True)

    # 2) ACL deny (wrong unit) → empty, not full corpus
    deny = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP 退换货",
            top_k=3,
            acl_filter=AclFilter(unit_ids=["U-NOPE"]),
        ),
        embedder=emb,
        store=store,
        lexical=lexical,
        cfg=cfg(),
    )
    assert deny["chunks"] == []
    print("acl deny ok", flush=True)

    # 3) multi-replica dense-only
    mr = run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP 退换货",
            top_k=3,
            acl_filter=AclFilter(unit_ids=[unit]),
        ),
        embedder=emb,
        store=store,
        lexical=lexical,
        cfg=cfg(retrieve_multi_replica_safe=True),
    )
    assert mr["hybrid"] is False
    assert mr["chunks"]
    assert all(ch["unit_id"] == unit for ch in mr["chunks"])
    print("multi-replica dense-only ok", flush=True)

    # 4) QPS reject
    try:
        run_retrieve_pipeline(
            request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=[unit]),
            embedder=emb,
            store=store,
            lexical=lexical,
            cfg=cfg(retrieve_qps_limit=1.0),
        )
        run_retrieve_pipeline(
            request=RetrieveRequestView(question="工单处理 SOP", top_k=2, unit_ids=[unit]),
            embedder=emb,
            store=store,
            lexical=lexical,
            cfg=cfg(retrieve_qps_limit=1.0),
        )
        raise AssertionError("expected QPS 429")
    except RetrieveError as exc:
        assert exc.status_code == 429 and exc.error_code == "RATE_LIMIT"
        print("qps limit ok", flush=True)
    finally:
        reset_qps_for_tests()

    # 5) unit_ids cap
    try:
        run_retrieve_pipeline(
            request=RetrieveRequestView(
                question="工单处理 SOP",
                top_k=2,
                unit_ids=[f"U{i}" for i in range(10)],
            ),
            embedder=emb,
            store=store,
            lexical=lexical,
            cfg=cfg(retrieve_max_unit_ids=3),
        )
        raise AssertionError("expected unit_ids cap 400")
    except RetrieveError as exc:
        assert exc.status_code == 400 and exc.error_code == "RETRIEVE_INVALID_FILTER"
        print("unit_ids cap ok", flush=True)

    # 6) stage metrics present
    snap = get_metrics().snapshot()
    for key in (
        "retrieve.stage_embed_ms",
        "retrieve.stage_store_ms",
        "retrieve.stage_fuse_ms",
        "retrieve.latency",
    ):
        assert key in snap["timings"], f"missing {key}"
    print("stage metrics ok", list(snap["timings"].keys()), flush=True)

    # 7) restart recovery via active map
    store2 = create_vector_store(cfg())
    store2.ensure_collection()
    assert store2.get_active_version(unit) == store.get_active_version(unit)
    hits = store2.search(vecs[0], top_k=20, filter=None)
    mine = [h for h in hits if h.get("unit_id") == unit]
    assert mine and all(h["version_id"] == store.get_active_version(unit) for h in mine)
    print("active map restart ok", len(mine), flush=True)

    # 8) P2: embed cache on real Milvus path
    from ai.retrieve.pipeline import reset_embed_cache_for_tests

    reset_embed_cache_for_tests()

    class CountingEmb:
        def __init__(self):
            self.n = 0

        def encode(self, texts):
            self.n += 1
            return emb.encode(texts)

    cemb = CountingEmb()
    cache_cfg = cfg(retrieve_embed_cache_max=16, retrieve_hybrid_enabled=False)
    run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP 退换货",
            top_k=2,
            acl_filter=AclFilter(unit_ids=[unit]),
        ),
        embedder=cemb,
        store=store2,
        lexical=LexicalIndex(),
        cfg=cache_cfg,
    )
    n1 = cemb.n
    run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP 退换货",
            top_k=2,
            acl_filter=AclFilter(unit_ids=[unit]),
        ),
        embedder=cemb,
        store=store2,
        lexical=LexicalIndex(),
        cfg=cache_cfg,
    )
    assert cemb.n == n1, "embed cache should skip second encode"
    assert get_metrics().snapshot()["counters"].get("retrieve.embed_cache_hit", 0) >= 1
    print("embed cache ok", flush=True)
    reset_embed_cache_for_tests()

    # 9) P2: total_requests counter moves
    t0 = get_metrics().snapshot()["counters"].get("retrieve.total_requests", 0)
    run_retrieve_pipeline(
        request=RetrieveRequestView(
            question="工单处理 SOP",
            top_k=2,
            acl_filter=AclFilter(unit_ids=[unit]),
        ),
        embedder=emb,
        store=store2,
        lexical=LexicalIndex(),
        cfg=cfg(retrieve_hybrid_enabled=False),
    )
    t1 = get_metrics().snapshot()["counters"].get("retrieve.total_requests", 0)
    assert t1 > t0
    print("total_requests ok", flush=True)

    # cleanup dedicated units + collection
    store2.delete_by_unit(unit)
    store2.delete_by_unit(unit_other)
    from pymilvus import MilvusClient

    mc = MilvusClient(uri=c.milvus_uri, timeout=10)
    if mc.has_collection(COLL):
        mc.drop_collection(COLL)
        print("cleanup", COLL, flush=True)
    if MAP.exists():
        MAP.unlink()

    print("REAL_MILVUS_RETRIEVE_FULL_VERIFY_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
