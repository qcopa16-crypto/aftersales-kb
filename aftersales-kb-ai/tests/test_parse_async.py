import os
import time

os.environ.setdefault("VECTOR_STORE", "memory")
os.environ.setdefault("EMBED_PROVIDER", "fake")
os.environ.setdefault("EMBED_DIM", "32")
os.environ.setdefault("INTERNAL_TOKEN", "test-token")
os.environ.setdefault("MINERU_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vlm, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()

from app.main import app  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def test_parse_async_md_poll_done():
    md = "# 安装\n\n上电后 PWR 常亮。\n"
    init = client.post(
        "/internal/parse/async",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-async1", "markdown": md},
    )
    assert init.status_code == 200, init.text
    task_id = init.json()["task_id"]
    assert init.json()["status"] == "queued"

    deadline = time.time() + 10
    body = None
    while time.time() < deadline:
        poll = client.get(f"/internal/tasks/{task_id}", headers=HEADERS)
        assert poll.status_code == 200
        body = poll.json()
        if body["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)

    assert body is not None
    assert body["status"] == "done", body
    assert body["progress"] == 100
    assert body["result"] is not None
    assert body["result"]["unit_id"] == "U-async1"
    assert len(body["result"]["chunks"]) >= 1


def test_task_not_found():
    resp = client.get("/internal/tasks/missing-task-id", headers=HEADERS)
    assert resp.status_code == 404


def test_parse_async_rejects_bad_type():
    resp = client.post(
        "/internal/parse/async",
        headers=HEADERS,
        data={"file_type": "exe", "unit_id": "U-x", "markdown": "x"},
    )
    assert resp.status_code == 400
