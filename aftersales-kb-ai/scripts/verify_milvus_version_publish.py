"""Real Milvus integration: versioned atomic publish + embed_model + rollback.

Uses shopkeeper-ai env and .env MILVUS_URI. Creates a dedicated collection
(default kb_ai_chunks_p1_verify) so production kb_ai_chunks is not mutated
unless --collection is passed.

Embedder: fake with dim matching collection (1024) to isolate vector-store protocol.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="kb_ai_chunks_p1_verify")
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--drop-first", action="store_true", default=True)
    parser.add_argument("--keep", action="store_true", help="keep collection after run")
    args = parser.parse_args()

    # Load project .env via pydantic settings after env defaults
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ["EMBED_PROVIDER"] = "fake"
    os.environ["EMBED_DIM"] = str(args.dim)
    os.environ["MILVUS_COLLECTION"] = args.collection

    from core.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    print(f"MILVUS_URI={cfg.milvus_uri} collection={cfg.milvus_collection} dim={cfg.embed_dim}")

    from pymilvus import MilvusClient

    client = MilvusClient(uri=cfg.milvus_uri, timeout=15)
    if args.drop_first and client.has_collection(args.collection):
        client.drop_collection(args.collection)
        print(f"dropped existing {args.collection}")

    from ai.retrieve.store import (
        PublishValidationError,
        VectorItem,
        create_vector_store,
    )

    store = create_vector_store(cfg)
    store.ensure_collection()

    desc = client.describe_collection(args.collection)
    names = {f["name"] for f in desc.get("fields", [])}
    print("schema_fields", sorted(names))
    assert "version_id" in names and "embed_model" in names, "version fields missing after ensure"

    unit = f"U-p1-{uuid.uuid4().hex[:8]}"
    dim = args.dim

    def item(idx: int, content: str, seed: float) -> VectorItem:
        vec = [0.0] * dim
        vec[idx % dim] = seed
        vec[0] = seed
        return VectorItem(
            chunk_index=idx,
            content=content,
            embedding=vec,
            embed_model="fake:p1-verify",
        )

    # 1) first publish
    n1 = store.publish_unit(unit, [item(0, "v1-a", 1.0), item(1, "v1-b", 0.5)])
    ver1 = store.get_active_version(unit)
    print("publish1", n1, "active", ver1)
    assert n1 == 2 and ver1

    hits = store.search([1.0] + [0.0] * (dim - 1), top_k=20)
    mine = [h for h in hits if h.get("unit_id") == unit]
    assert mine, "search returned no rows for unit after publish1"
    assert all(h.get("version_id") == ver1 for h in mine)
    assert all(h.get("embed_model") == "fake:p1-verify" for h in mine)
    print("search1 ok", [(h["content"], h["version_id"]) for h in mine])

    # 2) second publish replaces
    n2 = store.publish_unit(unit, [item(0, "v2-only", 0.8)])
    ver2 = store.get_active_version(unit)
    print("publish2", n2, "active", ver2)
    assert n2 == 1 and ver2 != ver1

    hits2 = store.search([0.8] + [0.0] * (dim - 1), top_k=20)
    mine2 = [h for h in hits2 if h.get("unit_id") == unit]
    assert mine2 and all(h["content"] == "v2-only" for h in mine2), mine2
    assert all(h["version_id"] == ver2 for h in mine2)
    print("search2 ok", mine2)

    # 3) rollback on mixed model
    try:
        store.publish_unit(
            unit,
            [
                item(0, "x", 0.1),
                VectorItem(1, "y", [0.2] * dim, embed_model="other-model"),
            ],
        )
        raise SystemExit("FAIL: mixed model should reject")
    except PublishValidationError as exc:
        print("mixed model rejected:", exc)

    active_after = store.get_active_version(unit)
    assert active_after == ver2, "active changed after rejected publish"
    hits3 = store.search([0.8] + [0.0] * (dim - 1), top_k=20)
    mine3 = [h for h in hits3 if h.get("unit_id") == unit]
    assert mine3 and mine3[0]["content"] == "v2-only"
    print("rollback keeps active", active_after)

    # 4) count query orphans
    safe = unit.replace("\\", "\\\\").replace('"', '\\"')
    rows = client.query(
        collection_name=args.collection,
        filter=f'unit_id == "{safe}"',
        output_fields=["id", "version_id", "embed_model", "content"],
    )
    print("all rows for unit", rows)
    assert all(r.get("version_id") == ver2 for r in rows), "orphan versions remain"

    deleted = store.delete_by_unit(unit)
    print("deleted", deleted)
    assert deleted >= 1

    if not args.keep and client.has_collection(args.collection):
        client.drop_collection(args.collection)
        print("cleanup dropped", args.collection)

    print("MILVUS_P1_VERIFY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
