"""Unit tests for publish throughput + parse/MinerU disk caches (offline)."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.ingest.content_cache import (  # noqa: E402
    DiskParseCache,
    file_content_hash,
    get_disk_parse_cache,
)
from ai.ingest.mineru_extract_cache import (  # noqa: E402
    MinerUExtractCache,
    extract_cache_key,
)
from ai.retrieve.store import (  # noqa: E402
    MemoryVectorStore,
    MilvusVectorStore,
    PublishValidationError,
    VectorItem,
)


def test_extract_cache_key_stable_and_option_sensitive() -> None:
    a = extract_cache_key("h1", model_version="vlm", language="ch")
    b = extract_cache_key("h1", model_version="vlm", language="ch")
    c = extract_cache_key("h1", model_version="vlm", language="en")
    d = extract_cache_key("h2", model_version="vlm", language="ch")
    assert a == b
    assert a != c
    assert a != d


def test_mineru_extract_cache_roundtrip(tmp_path: Path) -> None:
    cache = MinerUExtractCache(tmp_path / "mx", max_entries=2)
    images = [{"image_id": "i1", "relative_path": "images/a.png", "status": "skipped", "caption": None, "bytes": b"\x89PNG"}]
    cache.put("k1", "# md\nbody", 3, images)
    out = cache.get("k1")
    assert out is not None
    md, pages, imgs = out
    assert md.startswith("# md")
    assert pages == 3
    assert imgs[0]["bytes"] == b"\x89PNG"


def test_parse_disk_cache_and_file_hash(tmp_path: Path) -> None:
    cache = DiskParseCache(tmp_path / "pc", max_entries=2)
    cache.put("abc123", {"unit_id": "U", "markdown": "x", "chunks": []})
    hit = cache.get("abc123")
    assert hit and hit["unit_id"] == "U"

    f = tmp_path / "up.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    k1 = file_content_hash(f, file_type="pdf", chunk_size=512, chunk_overlap=50, use_vlm=False)
    k2 = file_content_hash(f, file_type="pdf", chunk_size=1000, chunk_overlap=50, use_vlm=False)
    assert k1 != k2
    f.write_bytes(b"%PDF-1.4 fake2")
    k3 = file_content_hash(f, file_type="pdf", chunk_size=512, chunk_overlap=50, use_vlm=False)
    assert k3 != k1


def _fake_milvus_store() -> MilvusVectorStore:
    class _FakeClient:
        def __init__(self) -> None:
            self.inserted: list[list] = []
            self.deleted: list = []
            self.queried_filters: list = []

        def insert(self, **kwargs):
            self.inserted.append(kwargs["data"])

        def flush(self, name):
            return None

        def query(self, **kwargs):
            self.queried_filters.append(kwargs.get("filter"))
            if kwargs.get("limit") == 1:
                return [{"id": "x"}]
            # full count path: pretend all rows exist
            return [{"id": f"i{i}"} for i in range(5)]

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
    store._supports_acl = False
    store._active = {"U-big": "oldver"}
    store._active_lock = threading.RLock()
    store._active_map_path = None
    store._insert_batch_size = 2
    store._count_validate_max = 3
    store._orphan_sweep_on_publish = False
    return store


def test_milvus_large_publish_skips_full_count_and_deletes_prev_only() -> None:
    store = _fake_milvus_store()
    items = [VectorItem(i, f"c{i}", [1.0, 0, 0, 0], embed_model="fake") for i in range(5)]
    n = store._publish_versioned("U-big", items)
    assert n == 5
    # batched inserts: 2+2+1
    assert len(store._client.inserted) == 3
    # large N (>3): sample query limit=1, not full count
    assert any(f and "oldver" in str(f) for f in store._client.deleted)
    # no full orphan sweep queries of bare unit_id without version
    bare = [f for f in store._client.queried_filters if f and "version_id" not in str(f)]
    assert not bare
    assert store.get_active_version("U-big") not in (None, "oldver")


def test_milvus_small_publish_still_full_count() -> None:
    store = _fake_milvus_store()
    store._count_validate_max = 3
    items = [VectorItem(0, "a", [1.0, 0, 0, 0], embed_model="fake")]
    # full count returns 5 != 1 → validate fail + rollback
    try:
        store._publish_versioned("U-s", items)
        raise AssertionError("expected count mismatch")
    except PublishValidationError as exc:
        assert "count" in str(exc)


def test_memory_publish_cleans_prev_version() -> None:
    store = MemoryVectorStore(dim=4)
    store.replace_unit("U1", [VectorItem(0, "a", [1.0, 0, 0, 0], embed_model="fake")])
    v1 = store.get_active_version("U1")
    store.replace_unit("U1", [VectorItem(0, "b", [0, 1.0, 0, 0], embed_model="fake")])
    v2 = store.get_active_version("U1")
    assert v1 and v2 and v1 != v2
    hits = store.search([0.0, 1.0, 0.0, 0.0], top_k=5)
    assert hits and hits[0]["content"] == "b"
