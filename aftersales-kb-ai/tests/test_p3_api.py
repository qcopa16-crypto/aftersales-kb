"""P3 API tests: health metrics, embed resume, mineru circuit error surface."""

import os

os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")
os.environ.setdefault("EMBED_RESUME_ENABLED", "true")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

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


def test_health_includes_circuits_and_metrics():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "circuits" in body
    assert "metrics" in body
    assert "limits" in body


def test_embed_full_then_resume_without_partial_400():
    chunks = [{"chunk_index": i, "content": f"c{i}"} for i in range(4)]
    r = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={"unit_id": "U-resume", "chunks": chunks},
    )
    assert r.status_code == 200
    assert r.json()["resumed_from"] == 0

    r2 = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={"unit_id": "U-resume", "chunks": chunks, "resume_from_index": 2},
    )
    assert r2.status_code == 400
    assert "partial" in str(r2.json()["detail"]).lower() or "resume" in str(r2.json()["detail"]).lower()
    client.delete("/internal/units/U-resume", headers=HEADERS)


def test_embed_resume_after_partial_checkpoint():
    from app.services.embed_partial import EmbedPartial, get_embed_partial_store

    chunks = [{"chunk_index": i, "content": f"body{i}"} for i in range(3)]
    # simulate checkpoint after 1 chunk encoded
    store = get_embed_partial_store()
    store.put(
        EmbedPartial(
            unit_id="U-rp",
            dim=32,
            vectors=[[0.01] * 32],
            contents=["[h]\nbody0"],
        )
    )
    r = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={"unit_id": "U-rp", "chunks": chunks, "resume_from_index": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["embedded_count"] == 3
    assert r.json()["resumed_from"] == 1
    client.delete("/internal/units/U-rp", headers=HEADERS)
