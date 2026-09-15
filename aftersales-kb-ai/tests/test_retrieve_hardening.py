"""Enterprise retrieve hardening tests (Python-side gaps after hybrid baseline).



约束:

- 业务夹具鍙娇鐢?docs/demo-knowledge/sop/工单处理SOP.md

- 榛樿 memory + fake embed

- 涓?setdefault ENABLE_PARSE_IDEMPOTENCY

- pytest -k 鏃跺繀椤?--ignore=tests/test_langgraph_real.py

"""



from __future__ import annotations



import os

import time

from pathlib import Path

from types import SimpleNamespace





from fastapi.testclient import TestClient  # noqa: E402



from core.config import Settings, get_settings  # noqa: E402



get_settings.cache_clear()



from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm  # noqa: E402



get_embedder.cache_clear()

get_vector_store.cache_clear()

get_mineru.cache_clear()

get_vlm.cache_clear()

get_llm.cache_clear()



from app.main import app  # noqa: E402

from ai.retrieve.lexical import LexicalIndex  # noqa: E402

from ai.retrieve.pipeline import (  # noqa: E402

    RetrieveError,

    RetrieveRequestView,

    run_retrieve_pipeline,

)

from ai.retrieve.store import (  # noqa: E402

    MemoryVectorStore,

    SearchFilter,

    VectorItem,

)

from core.circuit import get_circuits, get_metrics  # noqa: E402



client = TestClient(app)

HEADERS = {"X-Internal-Token": "test-token"}

SOP = Path(__file__).resolve().parents[2] / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"

assert SOP.is_file(), f"missing fixture: {SOP}"

SOP_TEXT = SOP.read_text(encoding="utf-8")





def _embed_unit(unit_id: str, markdown: str, **kwargs) -> list[dict]:

    pr = client.post(

        "/internal/parse",

        headers=HEADERS,

        data={"file_type": "md", "unit_id": unit_id, "markdown": markdown},

    )

    assert pr.status_code == 200, pr.text

    chunks = pr.json()["chunks"]

    payload = {

        "unit_id": unit_id,

        "chunks": [{"chunk_index": c["chunk_index"], "content": c["content"]} for c in chunks],

    }

    payload.update(kwargs)

    er = client.post("/internal/embed", headers=HEADERS, json=payload)

    assert er.status_code == 200, er.text

    return chunks





def _cleanup(*unit_ids: str) -> None:

    for uid in unit_ids:

        client.delete(f"/internal/units/{uid}", headers=HEADERS)





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





# ---------------------------------------------------------------------------

# T-REQACL-01 / T-REQACL-02

# ---------------------------------------------------------------------------





def test_t_reqacl_01_require_acl_rejects_empty_filter():

    cfg = get_settings()

    original = cfg.retrieve_require_acl

    cfg.retrieve_require_acl = True

    try:

        rr = client.post(

            "/internal/retrieve",

            headers=HEADERS,

            json={"question": "工单处理鏍囧噯姝ラ", "top_k": 3},

        )

        assert rr.status_code == 400, rr.text

        detail = rr.json()["detail"]

        assert detail["error_code"] == "RETRIEVE_ACL_REQUIRED"

    finally:

        cfg.retrieve_require_acl = original





def test_t_reqacl_02_require_acl_accepts_valid_filter():

    _cleanup("U-hard-acl")

    _embed_unit("U-hard-acl", SOP_TEXT)

    cfg = get_settings()

    original = cfg.retrieve_require_acl

    cfg.retrieve_require_acl = True

    try:

        rr = client.post(

            "/internal/retrieve",

            headers=HEADERS,

            json={

                "question": "工单处理标准步骤有哪些",

                "top_k": 5,

                "acl_filter": {"unit_ids": ["U-hard-acl"]},

            },

        )

        assert rr.status_code == 200, rr.text

        assert rr.json()["acl_applied"] is True

        assert rr.json()["chunks"]

    finally:

        cfg.retrieve_require_acl = original

        _cleanup("U-hard-acl")





# ---------------------------------------------------------------------------

# T-EMBACL-01 / T-EMBACL-02

# ---------------------------------------------------------------------------





def test_t_embacl_01_embed_require_acl_fields_missing_department():

    cfg = get_settings()

    original = cfg.embed_require_acl_fields

    cfg.embed_require_acl_fields = True

    try:

        er = client.post(

            "/internal/embed",

            headers=HEADERS,

            json={

                "unit_id": "U-hard-embacl-miss",

                "chunks": [{"chunk_index": 0, "content": "工单创建与流转是基本流程"}],

            },

        )

        assert er.status_code == 400, er.text

        detail = er.json()["detail"]

        assert detail["error_code"] == "EMBED_ACL_FIELDS_REQUIRED"

    finally:

        cfg.embed_require_acl_fields = original





def test_t_embacl_02_embed_require_acl_fields_with_department():

    cfg = get_settings()

    original = cfg.embed_require_acl_fields

    cfg.embed_require_acl_fields = True

    try:

        er = client.post(

            "/internal/embed",

            headers=HEADERS,

            json={

                "unit_id": "U-hard-embacl-ok",

                "chunks": [{"chunk_index": 0, "content": "工单创建与流转是基本流程"}],

                "acl_department": "service",

                "acl_classification": "internal",

            },

        )

        assert er.status_code == 200, er.text

        assert er.json()["embedded_count"] == 1

    finally:

        cfg.embed_require_acl_fields = original

        _cleanup("U-hard-embacl-ok")





# ---------------------------------------------------------------------------

# T-VER-01: memory/active filter still excludes inactive (regression)

# ---------------------------------------------------------------------------





def test_t_ver_01_memory_active_excludes_inactive():

    store = MemoryVectorStore(dim=8)

    items_v1 = [

        VectorItem(chunk_index=0, content="v1 content", embedding=[1.0] * 8, version_id="v1")

    ]

    store.publish_unit("U-ver", items_v1)

    v1 = store.get_active_version("U-ver")

    items_v2 = [

        VectorItem(chunk_index=0, content="v2 content", embedding=[0.0, 1.0] * 4, version_id="v2")

    ]

    store.publish_unit("U-ver", items_v2)

    v2 = store.get_active_version("U-ver")

    assert v1 and v2 and v1 != v2

    hits = store.search([0.0, 1.0] * 4, top_k=10)

    assert hits

    assert all(h["version_id"] == v2 for h in hits)

    assert all("v1" not in h["content"] for h in hits)





# ---------------------------------------------------------------------------

# T-STORE-01: store timeout path returns RETRIEVE_FAILED or RETRIEVE_TIMEOUT

# ---------------------------------------------------------------------------





class _HangingStore(MemoryVectorStore):

    def search(self, query_vector, top_k=8, *, filter=None):

        time.sleep(1.5)

        return super().search(query_vector, top_k=top_k, filter=filter)





def test_t_store_01_store_timeout_path():

    cfg = Settings(

        vector_store="memory",

        embed_provider="fake",

        embed_dim=8,

        retrieve_store_timeout_sec=0.05,

        retrieve_overall_timeout_sec=0,

        retrieve_hybrid_enabled=False,

    )

    store = _HangingStore(dim=8)

    store.publish_unit(

        "U-timeout",

        [VectorItem(chunk_index=0, content="x", embedding=[1.0] * 8)],

    )

    request = RetrieveRequestView(question="宸ュ崟", top_k=1)

    try:

        run_retrieve_pipeline(

            request=request,

            embedder=_fake_embedder(8),

            store=store,

            lexical=LexicalIndex(),

            cfg=cfg,

        )

        raise AssertionError("expected RetrieveError on store timeout")

    except RetrieveError as exc:

        assert exc.error_code in {"RETRIEVE_TIMEOUT", "RETRIEVE_FAILED"}

        assert exc.status_code in {500, 504}

    finally:

        get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)





# ---------------------------------------------------------------------------

# T-BUDGET-01: overall timeout flag reachable (unit-level)

# ---------------------------------------------------------------------------





class _SlowStore(MemoryVectorStore):

    def search(self, query_vector, top_k=8, *, filter=None):

        time.sleep(0.2)

        return super().search(query_vector, top_k=top_k, filter=filter)





def test_t_budget_01_overall_timeout_flag_reachable():

    cfg = Settings(

        vector_store="memory",

        embed_provider="fake",

        embed_dim=8,

        retrieve_store_timeout_sec=0,

        retrieve_overall_timeout_sec=0.01,

        retrieve_hybrid_enabled=False,

    )

    store = _SlowStore(dim=8)

    store.publish_unit(

        "U-budget",

        [VectorItem(chunk_index=0, content="x", embedding=[1.0] * 8)],

    )

    request = RetrieveRequestView(question="宸ュ崟", top_k=1)

    try:

        run_retrieve_pipeline(

            request=request,

            embedder=_fake_embedder(8),

            store=store,

            lexical=LexicalIndex(),

            cfg=cfg,

        )

        raise AssertionError("expected RetrieveError on overall budget")

    except RetrieveError as exc:

        assert exc.error_code == "RETRIEVE_TIMEOUT"

        assert exc.status_code == 504





# ---------------------------------------------------------------------------

# T-LEXCAP-01: lexical_max_docs does not 500

# ---------------------------------------------------------------------------





def test_t_lexcap_01_lexical_max_docs_does_not_500():

    index = LexicalIndex()

    docs = [

        {

            "id": f"c{i}",

            "chunk_index": i,

            "content": f"工单说明第{i}段 waiting_parts 故障",

            "acl_department": "service",

        }

        for i in range(20)

    ]

    index.replace_unit("U-lexcap", "v1", docs)

    hits = index.search("宸ュ崟 waiting_parts", top_k=5, max_docs=5)

    assert isinstance(hits, list)

    assert len(hits) <= 5



    # API path with tiny lexical cap must still 200

    _cleanup("U-lexcap-api")

    _embed_unit("U-lexcap-api", SOP_TEXT)

    cfg = get_settings()

    original_cap = cfg.retrieve_lexical_max_docs

    cfg.retrieve_lexical_max_docs = 2

    try:

        rr = client.post(

            "/internal/retrieve",

            headers=HEADERS,

            json={"question": "工单处理鏍囧噯姝ラ", "top_k": 3},

        )

        assert rr.status_code == 200, rr.text

    finally:

        cfg.retrieve_lexical_max_docs = original_cap

        _cleanup("U-lexcap-api")





# ---------------------------------------------------------------------------

# Startup hard gate: retrieve_strict_prod

# ---------------------------------------------------------------------------





def test_retrieve_strict_prod_refuses_when_require_acl_false():

    from app.main import _enforce_strict_prod



    cfg = Settings(retrieve_strict_prod=True, retrieve_require_acl=False)

    try:

        _enforce_strict_prod(cfg)

        raise AssertionError("expected RuntimeError")

    except RuntimeError as exc:

        assert "retrieve_require_acl" in str(exc)



    cfg_ok = Settings(retrieve_strict_prod=True, retrieve_require_acl=True)

    _enforce_strict_prod(cfg_ok)  # should not raise





# ---------------------------------------------------------------------------

# Observability: question hash + metrics on ACL reject

# ---------------------------------------------------------------------------





def test_question_hash_and_acl_metrics():

    from ai.retrieve.pipeline import _question_hash_prefix



    h = _question_hash_prefix("工单处理标准步骤有哪些")

    assert len(h) == 12

    assert all(c in "0123456789abcdef" for c in h)

    h = _question_hash_prefix("工单处理标准步骤有哪些")



    cfg = get_settings()

    original = cfg.retrieve_require_acl

    cfg.retrieve_require_acl = True

    metrics = get_metrics()

    before = metrics.snapshot()["counters"].get("retrieve.acl_required_reject", 0)

    try:

        rr = client.post(

            "/internal/retrieve",

            headers=HEADERS,

            json={"question": "工单处理", "top_k": 1},

        )

        assert rr.status_code == 400

        after = metrics.snapshot()["counters"].get("retrieve.acl_required_reject", 0)

        assert after == before + 1

    finally:

        cfg.retrieve_require_acl = original





# ---------------------------------------------------------------------------

# Milvus active map load/save (unit-level, no real Milvus required)

# ---------------------------------------------------------------------------





def test_milvus_active_map_persist_and_load(tmp_path, monkeypatch):

    import ai.retrieve.store as vs



    class _FakeMilvusClient:

        def __init__(self, uri=None):

            self.uri = uri



    monkeypatch.setattr(vs, "MilvusVectorStore", vs.MilvusVectorStore)

    # Patch pymilvus import inside __init__

    import sys



    fake_pymilvus = SimpleNamespace(MilvusClient=_FakeMilvusClient)

    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)



    map_path = tmp_path / "active.json"

    store = vs.MilvusVectorStore(

        uri="http://127.0.0.1:1",

        collection="nope",

        dim=8,

        active_map_path=str(map_path),

    )

    store._active["U-a"] = "ver-a"

    store._save_active_map()

    assert map_path.is_file()



    store2 = vs.MilvusVectorStore(

        uri="http://127.0.0.1:1",

        collection="nope",

        dim=8,

        active_map_path=str(map_path),

    )

    store2._load_active_map()

    assert store2.get_active_version("U-a") == "ver-a"





def test_milvus_fail_closed_when_acl_required_and_columns_missing(monkeypatch):

    import sys



    import ai.retrieve.store as vs



    class _FakeMilvusClient:

        def __init__(self, uri=None):

            self.uri = uri



        def has_collection(self, name):

            return True



        def describe_collection(self, name):

            return {

                "fields": [

                    {"name": "id"},

                    {"name": "unit_id"},

                    {"name": "chunk_index"},

                    {"name": "content"},

                    {"name": "embedding"},

                    {"name": "version_id"},

                    {"name": "embed_model"},

                ]

            }



        def add_collection_field(self, **kwargs):

            raise RuntimeError("cannot add field")



    fake_pymilvus = SimpleNamespace(MilvusClient=_FakeMilvusClient, DataType=object)

    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)



    store = vs.MilvusVectorStore(

        uri="http://127.0.0.1:1",

        collection="legacy",

        dim=8,

        embed_require_acl_fields=True,

    )

    try:

        store.ensure_collection()

        raise AssertionError("expected RuntimeError fail-closed")

    except RuntimeError as exc:

        assert "acl" in str(exc).lower()

