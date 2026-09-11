import os

# Force test-friendly settings before app import
os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()

from app.main import app  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def test_health_open():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_internal_requires_token():
    resp = client.post("/internal/embed", json={"unit_id": "u1", "chunks": [{"chunk_index": 0, "content": "x"}]})
    assert resp.status_code == 401


def test_parse_md_and_embed_delete_roundtrip():
    md = """# 安装手册

## 电源

上电后 PWR 常亮绿色。

## 网络

WAN 口接光猫。
"""
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-1", "markdown": md},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unit_id"] == "U-1"
    assert len(body["chunks"]) >= 2
    assert body["images"] == []
    assert body["file_type"] == "md"

    embed_resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": "U-1",
            "chunks": [
                {"chunk_index": c["chunk_index"], "content": c["content"]}
                for c in body["chunks"]
            ],
        },
    )
    assert embed_resp.status_code == 200, embed_resp.text
    emb = embed_resp.json()
    assert emb["embedded_count"] == len(body["chunks"])
    assert emb["dim"] == 32

    # re-embed same unit is idempotent
    embed2 = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": "U-1",
            "chunks": [
                {"chunk_index": c["chunk_index"], "content": c["content"]}
                for c in body["chunks"]
            ],
        },
    )
    assert embed2.status_code == 200
    assert embed2.json()["embedded_count"] == len(body["chunks"])

    del_resp = client.delete("/internal/units/U-1", headers=HEADERS)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True
    assert del_resp.json()["deleted_count"] >= 1

    # idempotent delete
    del2 = client.delete("/internal/units/U-1", headers=HEADERS)
    assert del2.status_code == 200
    assert del2.json()["deleted"] is True


def test_parse_md_file_upload_with_image_paths():
    md = "# 图\n\n![](images/pwr.jpg)\n\n电源灯说明。\n"
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-img"},
        files={"file": ("a.md", md.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["images"]) == 1
    assert body["images"][0]["relative_path"] == "images/pwr.jpg"
    assert body["images"][0]["status"] == "skipped"


def test_parse_rejects_bad_type():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "exe", "unit_id": "U-x", "markdown": "x"},
    )
    assert resp.status_code == 400


def test_parse_rejects_invalid_chunk_params():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-bad", "markdown": "# t\n\nbody", "chunk_size": -1},
    )
    assert resp.status_code == 400

    resp2 = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-bad", "markdown": "# t\n\nbody", "chunk_overlap": 9999},
    )
    assert resp2.status_code == 400


def test_embed_rejects_blank_unit_id():
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={"unit_id": "   ", "chunks": [{"chunk_index": 0, "content": "x"}]},
    )
    assert resp.status_code == 400


def test_parse_rejects_path_traversal_filename():
    md = "hello"
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-safe"},
        files={"file": ("../../evil.md", md.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200
    assert "evil" not in (resp.json().get("unit_id") or "")
