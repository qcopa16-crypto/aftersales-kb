import os

os.environ.setdefault("ENABLE_PARSE_IDEMPOTENCY", "true")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import (  # noqa: E402
    get_embedder,
    get_llm,
    get_mineru,
    get_vector_store,
    get_vlm,
)

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()
get_llm.cache_clear()

from app.main import app  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def test_parse_cache_hit_second_call():
    md = "# 幂等\n\n同一内容两次解析。\n"
    data = {"file_type": "md", "unit_id": "U-cache1", "markdown": md}
    r1 = client.post("/internal/parse", headers=HEADERS, data=data)
    assert r1.status_code == 200
    assert r1.json()["cache_hit"] is False

    data2 = {"file_type": "md", "unit_id": "U-cache2", "markdown": md}
    r2 = client.post("/internal/parse", headers=HEADERS, data=data2)
    assert r2.status_code == 200
    body = r2.json()
    assert body["cache_hit"] is True
    assert body["unit_id"] == "U-cache2"
    assert len(body["chunks"]) == len(r1.json()["chunks"])


def test_retrieve_after_embed():
    md = "# 保修\n\n屏幕碎裂属于人为损坏，不在免费保修范围。\n"
    pr = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-ret1", "markdown": md},
    )
    assert pr.status_code == 200
    chunks = pr.json()["chunks"]
    er = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": "U-ret1",
            "chunks": [{"chunk_index": c["chunk_index"], "content": c["content"]} for c in chunks],
        },
    )
    assert er.status_code == 200

    rr = client.post(
        "/internal/retrieve",
        headers=HEADERS,
        json={"question": "屏幕碎了能保修吗", "top_k": 3},
    )
    assert rr.status_code == 200, rr.text
    hits = rr.json()["chunks"]
    assert len(hits) >= 1
    assert any(h["unit_id"] == "U-ret1" for h in hits)
    assert isinstance(hits[0]["score"], float)

    client.delete("/internal/units/U-ret1", headers=HEADERS)


def test_retrieve_empty_question_400():
    resp = client.post("/internal/retrieve", headers=HEADERS, json={"question": "", "top_k": 3})
    assert resp.status_code == 422 or resp.status_code == 400


def test_generate_disabled_503():
    os.environ["LLM_API_BASE"] = ""
    os.environ["LLM_API_KEY"] = ""
    os.environ["LLM_MODEL"] = ""
    get_settings.cache_clear()
    get_llm.cache_clear()
    resp = client.post(
        "/internal/generate",
        headers=HEADERS,
        json={
            "question": "保修？",
            "contexts": [{"unit_id": "U-1", "title": "保修", "content": "人为损坏不保"}],
            "session_id": "s1",
        },
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "LLM_DISABLED"
