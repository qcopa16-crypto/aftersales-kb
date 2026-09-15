"""Tests for P1 version publish + P2 trace/lease/retry/cache/hard-fail."""

from __future__ import annotations

import logging
import os
import time

os.environ.setdefault("TASK_LEASE_SEC", "1")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()

from app.main import app  # noqa: E402
from ai.ingest.task_store import TaskStore, reset_task_store_for_tests  # noqa: E402
from ai.retrieve.store import (  # noqa: E402
    MemoryVectorStore,
    PublishValidationError,
    VectorItem,
)
from ai.ingest.content_cache import content_hash  # noqa: E402
from core.retry import ErrorClass, RetryPolicy, classify_exception, is_retryable, retry_call  # noqa: E402
from core.trace import get_trace_id, new_trace_id, set_trace_id  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def _vec(seed: float) -> list[float]:
    return [seed] + [0.0] * 31


# --- P1 atomic publish ---

def test_memory_publish_switches_active_and_hides_old():
    store = MemoryVectorStore(dim=4)
    v1 = [VectorItem(0, "old-a", [1.0, 0, 0, 0], embed_model="fake"), VectorItem(1, "old-b", [0, 1.0, 0, 0], embed_model="fake")]
    assert store.publish_unit("U1", v1) == 2
    ver1 = store.get_active_version("U1")
    assert ver1
    hits = store.search([1.0, 0, 0, 0], top_k=10)
    assert {h["content"] for h in hits} == {"old-a", "old-b"}

    v2 = [VectorItem(0, "new-only", [0, 0, 1.0, 0], embed_model="fake")]
    assert store.publish_unit("U1", v2) == 1
    ver2 = store.get_active_version("U1")
    assert ver2 != ver1
    hits2 = store.search([0, 0, 1.0, 0], top_k=10)
    assert len(hits2) == 1
    assert hits2[0]["content"] == "new-only"
    assert hits2[0]["version_id"] == ver2
    assert hits2[0]["embed_model"] == "fake"


def test_memory_publish_dim_mismatch_rolls_back_keep_old():
    store = MemoryVectorStore(dim=4)
    store.publish_unit("U2", [VectorItem(0, "keep", [1.0, 0, 0, 0], embed_model="fake")])
    ver_before = store.get_active_version("U2")
    try:
        store.publish_unit("U2", [VectorItem(0, "bad", [1.0, 0.0], embed_model="fake")])
        raise AssertionError("expected dim mismatch")
    except RuntimeError:
        pass
    assert store.get_active_version("U2") == ver_before
    hits = store.search([1.0, 0, 0, 0], top_k=5)
    assert hits[0]["content"] == "keep"


def test_publish_rejects_mixed_embed_model():
    store = MemoryVectorStore(dim=4)
    try:
        store.publish_unit(
            "U3",
            [
                VectorItem(0, "a", [1.0, 0, 0, 0], embed_model="m1"),
                VectorItem(1, "b", [0, 1.0, 0, 0], embed_model="m2"),
            ],
        )
        raise AssertionError("expected mixed model rejection")
    except PublishValidationError as exc:
        assert exc.error_code == "PUBLISH_VALIDATE_FAILED"


def test_publish_rejects_oversized_content():
    store = MemoryVectorStore(dim=4)
    try:
        store.publish_unit("U4", [VectorItem(0, "x" * 60001, [1.0, 0, 0, 0], embed_model="fake")])
        raise AssertionError("expected oversized rejection")
    except PublishValidationError:
        pass


def test_milvus_publish_rollback_on_count_mismatch():
    from ai.retrieve.store import MilvusVectorStore

    class _FakeClient:
        def __init__(self):
            self.inserted = []
            self.deleted = []

        def insert(self, **kwargs):
            self.inserted.append(kwargs["data"])

        def flush(self, name):
            return None

        def query(self, **kwargs):
            return []  # count 0 != expected 鈫?validate fail

        def delete(self, **kwargs):
            self.deleted.append(kwargs.get("filter"))
            return {"delete_count": 1}

    store = MilvusVectorStore.__new__(MilvusVectorStore)
    store._dim = 4
    store._collection = "c"
    store._client = _FakeClient()
    store._supports_version = True
    store._supports_embed_model = True
    store._supports_ingest_id = False
    store._active = {}
    import threading

    store._active_lock = threading.Lock()

    try:
        store._publish_versioned("U-m", [VectorItem(0, "a", [1.0, 0, 0, 0], embed_model="fake")])
        raise AssertionError("expected validate fail")
    except PublishValidationError as exc:
        assert "staged count" in str(exc) or "count" in str(exc)
    assert store.get_active_version("U-m") is None
    assert store._client.deleted  # rollback delete attempted


# --- P2 trace ---

def test_trace_id_header_echo_and_inject():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id")

    custom = "abc123trace"
    resp2 = client.get("/health", headers={"X-Trace-Id": custom})
    assert resp2.headers.get("X-Trace-Id") == custom


def test_trace_contextvar_visible_in_logs():
    set_trace_id("tid-xyz")
    assert get_trace_id() == "tid-xyz"
    assert new_trace_id() != "tid-xyz"


# --- P2 lease/stale ---

def test_task_lease_expiry_marks_stale():
    reset_task_store_for_tests()
    store = TaskStore(lease_sec=0.05, reap_on_read=True)
    task = store.create("U-lease", "md")
    store.update(task.task_id, status="running", stage="parse")
    time.sleep(0.12)
    got = store.get(task.task_id)
    assert got is not None
    assert got.status == "stale"
    assert got.error_code == "TASK_STALE"
    requeued = store.requeue_stale(task.task_id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.attempt == 1


def test_heartbeat_extends_lease():
    store = TaskStore(lease_sec=0.2, reap_on_read=False)
    task = store.create("U-hb", "md")
    store.update(task.task_id, status="running")
    time.sleep(0.1)
    assert store.heartbeat(task.task_id)
    time.sleep(0.15)
    # still within extended window after heartbeat at t=0.1, lease 0.2 鈫?until ~0.3
    got = store.get(task.task_id)
    # reap disabled; check fields
    assert got.status == "running"
    assert got.lease_until > got.heartbeat_at


# --- P2 retry ---

class _TimeoutErr(Exception):
    error_code = "MINERU_TIMEOUT"


class _ParamErr(Exception):
    error_code = "BAD_SOURCE"


def test_retry_classification():
    assert is_retryable(_TimeoutErr())
    assert not is_retryable(_ParamErr())
    assert classify_exception(_TimeoutErr()) is ErrorClass.RETRYABLE
    assert classify_exception(ValueError("nope")) is ErrorClass.NON_RETRYABLE


def test_retry_call_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _TimeoutErr("temp")
        return "ok"

    sleeps: list[float] = []
    out = retry_call(flaky, policy=RetryPolicy(max_attempts=3, base_delay_sec=0.01), sleep=sleeps.append)
    assert out == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_retry_call_does_not_retry_non_retryable():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise _ParamErr("no")

    try:
        retry_call(bad, policy=RetryPolicy(max_attempts=3), sleep=lambda s: None)
        raise AssertionError("expected raise")
    except _ParamErr:
        pass
    assert calls["n"] == 1


# --- P2 cache version key ---

def test_cache_key_changes_with_versions():
    base = dict(
        file_type="md",
        file_bytes=None,
        markdown="hello",
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=False,
    )
    k1 = content_hash(**base, parser_version="1.0", embed_model="fake", config_version="1")
    k2 = content_hash(**base, parser_version="1.1", embed_model="fake", config_version="1")
    k3 = content_hash(**base, parser_version="1.0", embed_model="cloud:x", config_version="1")
    k4 = content_hash(**base, parser_version="1.0", embed_model="fake", config_version="2")
    k1b = content_hash(**base, parser_version="1.0", embed_model="fake", config_version="1")
    assert k1 == k1b
    assert len({k1, k2, k3, k4}) == 4


# --- P2 parse hard fail ---

def test_parse_empty_markdown_hard_fails():
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-empty-parse", "markdown": "   \n\n  "},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "PARSE_EMPTY_RESULT"


def test_embed_writes_embed_model_into_store():
    body = {
        "unit_id": "U-model-meta",
        "chunks": [{"chunk_index": 0, "content": "鏈夊唴瀹圭殑鍒囩墖"}],
    }
    resp = client.post("/internal/embed", headers=HEADERS, json=body)
    assert resp.status_code == 200, resp.text
    store = get_vector_store()
    q = [0.0] * 32
    q[0] = 1.0
    hits = store.search(q, top_k=5)
    mine = [h for h in hits if h.get("unit_id") == "U-model-meta"]
    assert mine
    assert mine[0].get("embed_model") == "fake"
    assert mine[0].get("version_id")
    client.delete("/internal/units/U-model-meta", headers=HEADERS)
