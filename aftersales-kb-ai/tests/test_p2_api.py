"""P2 API tests: task cancel, source_url SSRF, token middleware."""

import os
import time


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


def test_token_wrong_401():
    resp = client.post(
        "/internal/embed",
        headers={"X-Internal-Token": "wrong"},
        json={"unit_id": "u", "chunks": [{"chunk_index": 0, "content": "x"}]},
    )
    assert resp.status_code == 401


def test_cancel_queued_task():
    # create task then cancel immediately
    init = client.post(
        "/internal/parse/async",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-cancel", "markdown": "# t\n\nbody\n"},
    )
    assert init.status_code == 200
    task_id = init.json()["task_id"]
    cr = client.delete(f"/internal/tasks/{task_id}", headers=HEADERS)
    assert cr.status_code == 200
    # may already be terminal (tiny md finishes first); cancel is best-effort
    assert "cancel_requested" in cr.json()

    poll = client.get(f"/internal/tasks/{task_id}", headers=HEADERS)
    assert poll.status_code == 200
    body = poll.json()
    # may be cancelled-as-failed or already done (race on tiny md); both valid terminal
    assert body["status"] in {"failed", "done"}


def test_cancel_missing_404():
    resp = client.delete("/internal/tasks/no-such-id", headers=HEADERS)
    assert resp.status_code == 404


def test_parse_source_url_ssrf_blocked():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={
            "file_type": "md",
            "unit_id": "U-ssrf",
            "source_url": "http://192.168.1.1/secret.md",
        },
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error_code"] == "SOURCE_SSRF_BLOCKED"


def test_embed_with_heading_path():
    md = "# 电源\n\nPWR 常亮绿。\n"
    pr = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-head", "markdown": md},
    )
    assert pr.status_code == 200
    chunks = pr.json()["chunks"]
    assert any(c.get("heading_path") for c in chunks)
    er = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": "U-head",
            "chunks": chunks,  # includes heading_path
        },
    )
    assert er.status_code == 200, er.text
    rr = client.post(
        "/internal/retrieve",
        headers=HEADERS,
        json={"question": "电源灯", "top_k": 2},
    )
    assert rr.status_code == 200
    assert rr.json()["chunks"][0]["unit_id"] == "U-head"
    client.delete("/internal/units/U-head", headers=HEADERS)
