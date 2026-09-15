"""BGE-M3 (GPU) + real Milvus full-chain e2e for versioned publish.

Does NOT mutate production kb_ai_chunks. Uses dedicated collection
(default kb_ai_chunks_e2e) created with version_id/embed_model fields.

Chain: parse(md) → embed(local BGE-M3 GPU) → publish to Milvus → search →
       republish → verify version switch → delete.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="kb_ai_chunks_e2e")
    parser.add_argument("--drop-first", action="store_true", default=True)
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    # Prefer real GPU embed from .env; override collection only.
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ["MILVUS_COLLECTION"] = args.collection
    # EMBED_* loaded from .env (local BGE-M3 GPU dim=1024)

    from core.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    print(
        f"provider={cfg.embed_provider} dim={cfg.embed_dim} device={cfg.embed_local_device} "
        f"uri={cfg.milvus_uri} collection={cfg.milvus_collection}"
    )
    if cfg.embed_provider != "local":
        print("WARN: EMBED_PROVIDER is not local; expected GPU BGE-M3 from .env")

    from pymilvus import MilvusClient

    client = MilvusClient(uri=cfg.milvus_uri, timeout=20)
    if args.drop_first and client.has_collection(args.collection):
        client.drop_collection(args.collection)
        print("dropped", args.collection)

    # Warm embedder (loads FlagEmbedding/torch on GPU)
    from app.deps import get_embedder, get_vector_store

    get_embedder.cache_clear()
    get_vector_store.cache_clear()
    embedder = get_embedder()
    store = get_vector_store()
    store.ensure_collection()

    desc = client.describe_collection(args.collection)
    names = {f["name"] for f in desc.get("fields", [])}
    print("schema_fields", sorted(names))
    assert "version_id" in names and "embed_model" in names

    unit = f"U-e2e-{uuid.uuid4().hex[:8]}"
    md1 = "# 售后手册\n\n电源灯不亮时请检查电源线与适配器。\n\n网络口无法连接时请确认网线插紧。\n"
    md2 = "# 售后手册 v2\n\n电源灯不亮：先换适配器再检查插座。\n"

    # --- parse ---
    from fastapi.testclient import TestClient
    from app.main import app

    # ensure deps cache with real embedder/vector store already primed
    client_api = TestClient(app)
    headers = {"X-Internal-Token": cfg.internal_token}

    r1 = client_api.post(
        "/internal/parse",
        headers=headers,
        data={"file_type": "md", "unit_id": unit, "markdown": md1},
    )
    assert r1.status_code == 200, r1.text
    p1 = r1.json()
    chunks1 = p1["chunks"]
    print("parse1 chunks", len(chunks1), "hash", p1["chunks_hash"][:12])
    assert chunks1

    # --- embed v1 via API (uses GPU BGE + milvus publish) ---
    e1 = client_api.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit,
            "chunks": chunks1,
            "chunks_hash": p1["chunks_hash"],
        },
    )
    assert e1.status_code == 200, e1.text
    print("embed1", e1.json())
    ver1 = store.get_active_version(unit)
    print("active_v1", ver1)
    assert ver1

    q = embedder.encode(["电源灯不亮"])[0]
    hits = store.search(q, top_k=10)
    mine = [h for h in hits if h.get("unit_id") == unit]
    print("search_v1", [(h["content"][:40], h.get("version_id"), h.get("embed_model")) for h in mine])
    assert mine, "no hits after embed1"
    assert all(h.get("version_id") == ver1 for h in mine)
    assert any(h.get("embed_model") for h in mine)

    # --- embed v2 (replace) ---
    r2 = client_api.post(
        "/internal/parse",
        headers=headers,
        data={"file_type": "md", "unit_id": unit, "markdown": md2},
    )
    assert r2.status_code == 200, r2.text
    p2 = r2.json()
    e2 = client_api.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit,
            "chunks": p2["chunks"],
            "chunks_hash": p2["chunks_hash"],
        },
    )
    assert e2.status_code == 200, e2.text
    ver2 = store.get_active_version(unit)
    print("active_v2", ver2)
    assert ver2 and ver2 != ver1

    hits2 = store.search(q, top_k=10)
    mine2 = [h for h in hits2 if h.get("unit_id") == unit]
    print("search_v2", [(h["content"][:40], h.get("version_id")) for h in mine2])
    assert mine2 and all(h.get("version_id") == ver2 for h in mine2)

    safe = unit.replace("\\", "\\\\").replace('"', '\\"')
    rows = client.query(
        collection_name=args.collection,
        filter=f'unit_id == "{safe}"',
        output_fields=["version_id", "embed_model", "content"],
    )
    print("milvus_rows", rows)
    assert all(r.get("version_id") == ver2 for r in rows), "orphan versions after v2 publish"

    # delete
    d = client_api.delete(f"/internal/units/{unit}", headers=headers)
    assert d.status_code == 200, d.text
    print("delete", d.json())

    if os.environ.get("KEEP_E2E_COLLECTION") != "1" and client.has_collection(args.collection):
        client.drop_collection(args.collection)
        print("dropped", args.collection)

    print("BGE_MILVUS_E2E_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
