"""Real Milvus probe: insert/flush/query + active map + ACL surface."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

os.environ["VECTOR_STORE"] = "milvus"
os.environ["EMBED_PROVIDER"] = "fake"
os.environ["EMBED_DIM"] = "32"
os.environ["MILVUS_URI"] = "http://127.0.0.1:19530"
os.environ["MILVUS_COLLECTION"] = "kb_ai_chunks_hardening_verify"
MAP = Path(r"D:/code_project/aftersales-kb/aftersales-kb-ai/.data/hardening_active_verify.json")
os.environ["MILVUS_ACTIVE_MAP_PATH"] = str(MAP)
if MAP.exists():
    MAP.unlink()

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()
cfg = get_settings()

from pymilvus import DataType, MilvusClient  # noqa: E402

client = MilvusClient(uri=cfg.milvus_uri, timeout=10)
COLL = "kb_ai_chunks_hardening_probe"
if client.has_collection(COLL):
    client.drop_collection(COLL)
    print("dropped probe collection", flush=True)

schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
schema.add_field("unit_id", DataType.VARCHAR, max_length=64)
schema.add_field("chunk_index", DataType.INT64)
schema.add_field("content", DataType.VARCHAR, max_length=65535)
schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=32)
schema.add_field("version_id", DataType.VARCHAR, max_length=64, default_value="")
schema.add_field("embed_model", DataType.VARCHAR, max_length=128, default_value="")
schema.add_field("ingest_id", DataType.VARCHAR, max_length=128, default_value="")
schema.add_field("acl_department", DataType.VARCHAR, max_length=64, default_value="")
schema.add_field("acl_classification", DataType.VARCHAR, max_length=64, default_value="")
ip = client.prepare_index_params()
ip.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
client.create_collection(collection_name=COLL, schema=schema, index_params=ip)
print("created probe collection", flush=True)

row = {
    "id": uuid.uuid4().hex,
    "unit_id": "U-probe",
    "chunk_index": 0,
    "content": "hello",
    "embedding": [0.1] * 32,
    "version_id": "v1",
    "embed_model": "fake",
    "ingest_id": "",
    "acl_department": "service",
    "acl_classification": "internal",
}
print("inserting", flush=True)
r = client.insert(collection_name=COLL, data=[row])
print("inserted", r, flush=True)
print("flushing", flush=True)
try:
    client.flush(COLL)
    print("flushed", flush=True)
except Exception as e:
    print("flush failed", type(e).__name__, e, flush=True)
print("querying", flush=True)
q = client.query(
    collection_name=COLL,
    filter='unit_id == "U-probe"',
    output_fields=["id", "version_id", "acl_department"],
    limit=10,
)
print("query", q, flush=True)
print("searching", flush=True)
s = client.search(
    collection_name=COLL,
    data=[[0.1] * 32],
    limit=5,
    output_fields=["unit_id", "version_id", "acl_department", "content"],
    search_params={"metric_type": "COSINE"},
)
print("search", s, flush=True)

# Active map + store path with real client
if client.has_collection(cfg.milvus_collection):
    client.drop_collection(cfg.milvus_collection)
    print("dropped verify collection", flush=True)

from ai.retrieve.store import SearchFilter, VectorItem, create_vector_store  # noqa: E402

store = create_vector_store(cfg)
store.ensure_collection()
print("store ensure ok", flush=True)

unit = "U-hard-real-" + uuid.uuid4().hex[:8]


def item(idx: int, content: str, seed: float) -> VectorItem:
    vec = [0.0] * 32
    vec[0] = seed
    vec[idx % 32] = seed
    return VectorItem(
        chunk_index=idx,
        content=content,
        embedding=vec,
        embed_model="fake:hard",
        acl_department="service",
        acl_classification="internal",
    )


print("publish1 start", flush=True)
n1 = store.publish_unit(unit, [item(0, "v1-a", 1.0), item(1, "v1-b", 0.5)])
ver1 = store.get_active_version(unit)
print("publish1", n1, ver1, flush=True)
assert n1 == 2 and ver1
assert MAP.is_file(), "active map missing"
data = json.loads(MAP.read_text(encoding="utf-8"))
assert data.get(unit) == ver1
print("active map written", flush=True)

store2 = create_vector_store(cfg)
store2.ensure_collection()
assert store2.get_active_version(unit) == ver1
hits = store2.search([1.0] + [0.0] * 31, top_k=20)
mine = [h for h in hits if h.get("unit_id") == unit]
print("restart search", [(h["content"], h["version_id"], h.get("acl_department")) for h in mine], flush=True)
assert mine and all(h["version_id"] == ver1 for h in mine)
assert all(h.get("acl_department") == "service" for h in mine)

n2 = store2.publish_unit(unit, [item(0, "v2-only", 0.8)])
ver2 = store2.get_active_version(unit)
print("publish2", n2, ver2, flush=True)
assert ver2 != ver1

store3 = create_vector_store(cfg)
store3.ensure_collection()
assert store3.get_active_version(unit) == ver2
hits3 = store3.search([0.8] + [0.0] * 31, top_k=20)
mine3 = [h for h in hits3 if h.get("unit_id") == unit]
print("restart2 search", [(h["content"], h["version_id"]) for h in mine3], flush=True)
assert mine3 and all(h["content"] == "v2-only" for h in mine3)

hits_ok = store3.search([0.8] + [0.0] * 31, top_k=20, filter=SearchFilter(department="service"))
assert any(h["unit_id"] == unit for h in hits_ok)
hits_no = store3.search([0.8] + [0.0] * 31, top_k=20, filter=SearchFilter(department="nope"))
assert not any(h["unit_id"] == unit for h in hits_no)
print("acl filter ok", flush=True)

deleted = store3.delete_by_unit(unit)
print("deleted", deleted, flush=True)
data2 = json.loads(MAP.read_text(encoding="utf-8"))
assert unit not in data2
print("active map cleaned", flush=True)

for coll in (COLL, cfg.milvus_collection):
    if client.has_collection(coll):
        client.drop_collection(coll)
        print("cleanup", coll, flush=True)

print("REAL_MILVUS_HARDENING_VERIFY_OK", flush=True)
