"""Retrieve pipeline contract tests: ACL, normalize, top_k, auth, generate no re-retrieve.



约束:

- 业务夹具鍙娇鐢?docs/demo-knowledge/sop/工单处理SOP.md

- 榛樿 memory + fake embed

- 涓?setdefault ENABLE_PARSE_IDEMPOTENCY

"""



from __future__ import annotations



import os

from pathlib import Path





from fastapi.testclient import TestClient  # noqa: E402



from core.config import get_settings  # noqa: E402



get_settings.cache_clear()



from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm  # noqa: E402



get_embedder.cache_clear()

get_vector_store.cache_clear()

get_mineru.cache_clear()

get_vlm.cache_clear()

get_llm.cache_clear()



from app.main import app  # noqa: E402



client = TestClient(app)

HEADERS = {"X-Internal-Token": "test-token"}

SOP = Path(__file__).resolve().parents[2] / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"

assert SOP.is_file()

SOP_TEXT = SOP.read_text(encoding="utf-8")





def _embed_unit(unit_id: str, markdown: str, **kwargs) -> None:

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





def _cleanup(*unit_ids: str) -> None:

    for uid in unit_ids:

        client.delete(f"/internal/units/{uid}", headers=HEADERS)





def _setup_acl_fixtures():

    _cleanup("U-acl-allow", "U-acl-deny")

    _embed_unit(

        "U-acl-allow",

        SOP_TEXT,

        acl_department="售后部",

        acl_classification="internal",

    )

    # Second unit: finance department, same domain content (from SOP)

    _embed_unit(

        "U-acl-deny",

        SOP_TEXT,

        acl_department="财务部",

        acl_classification="internal",

    )





def test_t_acl_01_no_filter_compat():

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理", "top_k": 10},

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["acl_applied"] is False

    units = {c["unit_id"] for c in body["chunks"]}

    assert units  # non-empty

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_02_unit_allowlist():

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "工单处理标准步骤有哪些",

            "top_k": 10,

            "acl_filter": {"unit_ids": ["U-acl-allow"]},

        },

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["acl_applied"] is True

    assert body["chunks"]

    assert {c["unit_id"] for c in body["chunks"]} <= {"U-acl-allow"}

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_03_unit_denylist():

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "工单处理标准步骤有哪些",

            "top_k": 10,

            "acl_filter": {"deny_unit_ids": ["U-acl-deny"]},

        },

    )

    assert rr.status_code == 200, rr.text

    units = {c["unit_id"] for c in rr.json()["chunks"]}

    assert "U-acl-deny" not in units

    assert "U-acl-allow" in units

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_04_department_filter():

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "工单处理标准步骤有哪些",

            "top_k": 10,

            "acl_filter": {"department": "售后部"},

        },

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["acl_applied"] is True

    assert body["chunks"]

    assert {c["unit_id"] for c in body["chunks"]} <= {"U-acl-allow"}



    rr2 = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "工单处理标准步骤有哪些",

            "top_k": 10,

            "acl_filter": {"department": "涓嶅瓨鍦ㄧ殑閮ㄩ棬"},

        },

    )

    assert rr2.status_code == 200, rr2.text

    assert rr2.json()["chunks"] == []

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_05_post_filter_drops_violation():

    _setup_acl_fixtures()

    # Filter to allow only; deny unit must never appear even if store had a bug

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "宸ュ崟",

            "top_k": 20,

            "acl_filter": {"unit_ids": ["U-acl-allow"], "department": "售后部"},

        },

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["acl_applied"] is True

    assert all(c["unit_id"] != "U-acl-deny" for c in body["chunks"])

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_06_require_acl(monkeypatch):

    _setup_acl_fixtures()

    cfg = get_settings()

    monkeypatch.setattr(cfg, "retrieve_require_acl", True)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理", "top_k": 3},

    )

    assert rr.status_code == 400, rr.text

    assert rr.json()["detail"]["error_code"] == "RETRIEVE_ACL_REQUIRED"

    monkeypatch.undo()

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_07_invalid_unit_id():

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "宸ュ崟",

            "top_k": 3,

            "acl_filter": {"unit_ids": ["bad id!"]},

        },

    )

    assert rr.status_code == 400, rr.text

    assert rr.json()["detail"]["error_code"] == "RETRIEVE_INVALID_FILTER"





def test_t_acl_09_empty_intersection_denies_all():

    """unit_ids 鈭?acl_filter.unit_ids = 鈭?must return zero hits, never full corpus."""

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "工单处理",

            "top_k": 10,

            "unit_ids": ["U-acl-allow"],

            "acl_filter": {"unit_ids": ["U-acl-deny"]},

        },

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["acl_applied"] is True

    assert body["chunks"] == []

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_acl_08_both_paths_same_filter():

    _setup_acl_fixtures()

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "waiting_parts waiting_customer",

            "top_k": 10,

            "acl_filter": {"unit_ids": ["U-acl-allow"]},

        },

    )

    assert rr.status_code == 200, rr.text

    for c in rr.json()["chunks"]:

        assert c["unit_id"] == "U-acl-allow"

        # If lexical contributed, it still had to pass the same unit filter

        if "lexical" in c["retrieval_paths"]:

            assert c["unit_id"] == "U-acl-allow"

    _cleanup("U-acl-allow", "U-acl-deny")





def test_t_norm_01_whitespace_question():

    rr = client.post("/internal/retrieve", headers=HEADERS, json={"question": "   ", "top_k": 3})

    assert rr.status_code in (400, 422)





def test_t_norm_02_too_long_question():

    q = "工" * 2001

    rr = client.post("/internal/retrieve", headers=HEADERS, json={"question": q, "top_k": 3})

    assert rr.status_code == 400, rr.text

    assert rr.json()["detail"]["error_code"] == "PAYLOAD_TOO_LARGE"





def test_t_norm_03_control_chars_not_500():

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "宸ュ崟\x00澶勭悊", "top_k": 3},

    )

    assert rr.status_code != 500





def test_t_dedup_01_max_per_unit(monkeypatch):

    _cleanup("U-dedup")

    _embed_unit("U-dedup", SOP_TEXT)

    cfg = get_settings()

    monkeypatch.setattr(cfg, "retrieve_max_per_unit", 1)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理鐘舵€佷笌姝ラ", "top_k": 20},

    )

    assert rr.status_code == 200, rr.text

    units = [c["unit_id"] for c in rr.json()["chunks"]]

    assert units.count("U-dedup") <= 1

    monkeypatch.undo()

    _cleanup("U-dedup")





def test_t_topk_01():

    _cleanup("U-topk")

    _embed_unit("U-topk", SOP_TEXT)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理", "top_k": 1},

    )

    assert rr.status_code == 200, rr.text

    assert len(rr.json()["chunks"]) <= 1

    _cleanup("U-topk")





def test_t_empty_01():

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={

            "question": "瀹屽叏娌℃湁鍛戒腑鐨勮瘽棰榵yzabc12345",

            "top_k": 5,

            "acl_filter": {"unit_ids": ["U-not-exist-000"]},

        },

    )

    assert rr.status_code == 200, rr.text

    assert rr.json()["chunks"] == []





def test_t_auth_01_missing_token():

    rr = client.post("/internal/retrieve", json={"question": "宸ュ崟", "top_k": 1})

    assert rr.status_code == 401





def test_t_auth_02_wrong_token():

    rr = client.post(

        "/internal/retrieve",

        headers={"X-Internal-Token": "wrong"},

        json={"question": "宸ュ崟", "top_k": 1},

    )

    assert rr.status_code == 401





def test_t_gen_01_generate_skips_retrieve(monkeypatch):

    calls = {"n": 0}

    store = get_vector_store()

    orig_search = store.search



    def counting_search(*args, **kwargs):

        calls["n"] += 1

        return orig_search(*args, **kwargs)



    monkeypatch.setattr(store, "search", counting_search)

    rr = client.post(

        "/internal/generate",

        headers=HEADERS,

        json={

            "question": "保修",

            "contexts": [{"unit_id": "U-x", "title": "t", "content": "屏幕碎屏不在保修"}],

        },

    )

    # LLM may be disabled (503) 鈥?either way store.search must not be called.

    assert calls["n"] == 0

    assert rr.status_code in (200, 503)

    monkeypatch.undo()





def test_t_rerr_01_encode_failure(monkeypatch):

    from app.deps import get_embedder



    embedder = get_embedder()



    def boom(texts):

        raise RuntimeError("encode down")



    monkeypatch.setattr(embedder, "encode", boom)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理", "top_k": 3},

    )

    assert rr.status_code == 500, rr.text

    assert rr.json()["detail"]["error_code"] == "RETRIEVE_FAILED"

    monkeypatch.undo()

