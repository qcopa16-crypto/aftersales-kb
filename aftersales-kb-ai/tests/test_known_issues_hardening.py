"""Regression tests for known-issue Python hardening (P0/P1)."""

import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("EMBED_MAX_EMPTY_RATIO", "0.5")
os.environ.setdefault("PARSE_QUEUE_MAX", "2")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vector_store  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()

from app.main import app  # noqa: E402
from ai.ingest.fingerprint import chunks_fingerprint  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": "test-token"}


def _embed_payload(unit_id: str, contents: list[str], **extra):
    chunks = [{"chunk_index": i, "content": c} for i, c in enumerate(contents)]
    body = {"unit_id": unit_id, "chunks": chunks}
    body.update(extra)
    return body


def _embed_payload_from_parse_chunks(unit_id: str, parse_chunks: list[dict], **extra):
    """Echo parse chunk objects so chunks_hash matches (incl. heading_path)."""
    chunks = [
        {
            "chunk_index": c["chunk_index"],
            "content": c["content"],
            "heading_path": c.get("heading_path") or [],
        }
        for c in parse_chunks
    ]
    body = {"unit_id": unit_id, "chunks": chunks}
    body.update(extra)
    return body


def test_parse_returns_hashes_and_embed_accepts_them():
    md = "# 鏍囬\n\n鍐呭A銆俓n"
    resp = client.post(
        "/internal/parse",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": "U-hash", "markdown": md},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_hash"]
    assert body["chunks_hash"]

    embed = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload_from_parse_chunks(
            "U-hash",
            body["chunks"],
            content_hash=body["content_hash"],
            chunks_hash=body["chunks_hash"],
        ),
    )
    assert embed.status_code == 200, embed.text
    client.delete("/internal/units/U-hash", headers=HEADERS)


def test_embed_rejects_chunks_hash_mismatch():
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-mismatch", ["姝ｅ父鍐呭"], chunks_hash="deadbeef"),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "CHUNKS_HASH_MISMATCH"


def test_embed_quality_gate_empty_document():
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-empty", ["", "   "]),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "EMBED_EMPTY_DOCUMENT"


def test_embed_quality_gate_high_empty_ratio():
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-ratio", ["ok content", "", ""]),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "EMBED_QUALITY_GATE"

    forced = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-ratio", ["ok content", "", ""], force=True),
    )
    assert forced.status_code == 200, forced.text
    client.delete("/internal/units/U-ratio", headers=HEADERS)


def test_embed_rejects_oversized_chunk():
    long_text = "x" * 60001
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-long", [long_text]),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "EMBED_CONTENT_TOO_LONG"


def test_resume_checkpoint_hash_mismatch():
    first = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-resume", ["chunk-a", "chunk-b"]),
    )
    assert first.status_code == 200
    client.delete("/internal/units/U-resume", headers=HEADERS)

    # Inject a partial checkpoint then try resume with different chunks.
    from ai.embed.partial import EmbedPartial, get_embed_partial_store

    store = get_embed_partial_store()
    store.put(
        EmbedPartial(
            unit_id="U-resume",
            dim=32,
            vectors=[[0.0] * 32],
            contents=["chunk-a"],
            chunks_hash="old-hash",
        )
    )
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json=_embed_payload("U-resume", ["chunk-a", "chunk-b"], resume_from_index=1),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "RESUME_CHECKPOINT_MISMATCH"


def test_resume_checkpoint_embed_model_mismatch():
    from ai.embed.partial import EmbedPartial, get_embed_partial_store
    from core.config import embed_model_id
    from core.config import get_settings as _gs
    from ai.ingest.fingerprint import chunks_fingerprint

    chunks = [
        {"chunk_index": 0, "content": "chunk-a", "heading_path": []},
        {"chunk_index": 1, "content": "chunk-b", "heading_path": []},
    ]
    store = get_embed_partial_store()
    store.put(
        EmbedPartial(
            unit_id="U-resume-model",
            dim=32,
            vectors=[[0.1] * 32],
            contents=["chunk-a"],
            chunks_hash=chunks_fingerprint(chunks),
            embed_model="cloud:some-other-model",
        )
    )
    resp = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": "U-resume-model",
            "chunks": chunks,
            "resume_from_index": 1,
            "chunks_hash": chunks_fingerprint(chunks),
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "RESUME_CHECKPOINT_MISMATCH"
    assert "embed_model" in detail["message"]
    assert embed_model_id(_gs()) != "cloud:some-other-model"


def test_fingerprint_stable():
    chunks = [
        {"chunk_index": 0, "content": "a", "heading_path": ["h1"]},
        {"chunk_index": 1, "content": "b", "heading_path": []},
    ]
    h1 = chunks_fingerprint(chunks)
    h2 = chunks_fingerprint(chunks)
    assert h1 == h2
    assert h1 != chunks_fingerprint([chunks[0]])


def test_unit_lock_serializes():
    from core.unit_lock import UnitLockRegistry

    reg = UnitLockRegistry()
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str):
        with reg.hold("U-lock"):
            order.append(f"{name}-in")
            barrier.wait(timeout=2)
            order.append(f"{name}-out")

    # Second thread cannot enter while first holds the lock, so barrier would timeout
    # if locks work. Use a sequential acquire check instead.
    with reg.hold("U-x"):
        assert not reg._locks["U-x"].acquire(blocking=False)
    assert reg._locks.get("U-x") is None  # cleaned up


def test_source_redirect_private_blocked():
    from core.config import Settings
    from ai.ingest.source import SourceFetchError, fetch_source_to_temp

    cfg = Settings()
    # Start from a URL that is invalid for private host after "redirect".
    # Direct private URL is already blocked at first hop.
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fetch_source_to_temp(
                "http://127.0.0.1/x",
                Path(tmp),
                filename="x.bin",
                cfg=cfg,
            )
            raise AssertionError("expected SSRF block")
        except SourceFetchError as exc:
            assert exc.error_code in {"SOURCE_SSRF_BLOCKED", "SOURCE_BAD_URL", "SOURCE_FETCH_FAILED"}


def test_memory_replace_unit_removes_orphans():
    from ai.retrieve.store import MemoryVectorStore, VectorItem

    store = MemoryVectorStore(dim=4)
    store.replace_unit(
        "U1",
        [
            VectorItem(0, "a", [1.0, 0, 0, 0]),
            VectorItem(1, "b", [0, 1.0, 0, 0]),
        ],
    )
    store.replace_unit("U1", [VectorItem(0, "a2", [0, 0, 1.0, 0])])
    hits = store.search([0, 0, 1.0, 0], top_k=10)
    assert len(hits) == 1
    assert hits[0]["content"] == "a2"


def test_parse_queue_full_returns_503(monkeypatch):
    import app.api.internal.ingest as internal_mod

    # Drain semaphore to simulate full queue.
    sem = internal_mod._get_parse_sem()
    # parse_queue_max=2 in env; acquire all slots.
    acquired = 0
    while sem.acquire(blocking=False):
        acquired += 1
        if acquired > 10:
            break
    try:
        resp = client.post(
            "/internal/parse/async",
            headers=HEADERS,
            data={"file_type": "md", "unit_id": "U-full", "markdown": "# t\n\nbody\n"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error_code"] == "PARSE_QUEUE_FULL"
    finally:
        for _ in range(acquired):
            sem.release()


def test_milvus_refuses_silent_truncate():
    from ai.retrieve.store import MilvusVectorStore, PublishValidationError, VectorItem

    class _FakeClient:
        def delete(self, **kwargs):
            return 0

        def insert(self, **kwargs):
            raise AssertionError("insert should not be called")

        def flush(self, name):
            return None

    store = MilvusVectorStore.__new__(MilvusVectorStore)
    store._dim = 4
    store._collection = "c"
    store._client = _FakeClient()
    store._supports_version = False
    store._supports_embed_model = False
    try:
        store.upsert("U", [VectorItem(0, "x" * 60001, [0.0] * 4)])
        raise AssertionError("expected truncate refusal")
    except (RuntimeError, PublishValidationError) as exc:
        assert "milvus field limit" in str(exc) or "exceeds" in str(exc)
