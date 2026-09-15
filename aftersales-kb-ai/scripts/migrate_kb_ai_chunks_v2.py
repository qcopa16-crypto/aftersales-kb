"""One-off migration: kb_ai_chunks → kb_ai_chunks_v2 (full re-embed).

Independent of FastAPI business routes. Supports --resume via local checkpoint.

Usage (shopkeeper-ai):
  set PYTHONPATH=<repo>/aftersales-kb-ai
  python scripts/migrate_kb_ai_chunks_v2.py
  python scripts/migrate_kb_ai_chunks_v2.py --switch-env   # also patch .env
  python scripts/migrate_kb_ai_chunks_v2.py --verify-only  # validation only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CKPT_PATH = ROOT / ".data" / "migrate_kb_ai_chunks_v2.ckpt.json"
DEFAULT_SRC = "kb_ai_chunks"
DEFAULT_DST = "kb_ai_chunks_v2"


def load_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def apply_runtime_env(env: dict[str, str]) -> None:
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ.setdefault("MILVUS_URI", env.get("MILVUS_URI", "http://127.0.0.1:19530"))
    os.environ["EMBED_PROVIDER"] = "local"
    os.environ.setdefault(
        "EMBED_LOCAL_MODEL",
        env.get("EMBED_LOCAL_MODEL", "D:/ai_models/modelscope_cache/models/BAAI--bge-m3/snapshots/master"),
    )
    device = env.get("EMBED_LOCAL_DEVICE", "cuda").lower()
    os.environ["EMBED_LOCAL_DEVICE"] = "cuda" if device in {"gpu", "cuda"} else device
    os.environ.setdefault("EMBED_DIM", env.get("EMBED_DIM", "1024"))
    os.environ.setdefault("INTERNAL_TOKEN", env.get("INTERNAL_TOKEN", "change-me-long-random"))


def load_ckpt() -> dict:
    if CKPT_PATH.exists():
        try:
            return json.loads(CKPT_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_ckpt(data: dict) -> None:
    CKPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CKPT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CKPT_PATH)


def ensure_dst_collection(client, dst: str, dim: int) -> None:
    from pymilvus import DataType

    if client.has_collection(dst):
        desc = client.describe_collection(dst)
        names = {f["name"] for f in desc.get("fields", [])}
        required = {"id", "unit_id", "chunk_index", "content", "embedding", "version_id", "embed_model"}
        missing = required - names
        if missing:
            raise RuntimeError(f"dst collection {dst} exists but missing fields: {sorted(missing)}")
        print(f"[create] reuse existing {dst}")
        return

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("unit_id", DataType.VARCHAR, max_length=64)
    schema.add_field("chunk_index", DataType.INT64)
    schema.add_field("content", DataType.VARCHAR, max_length=65535)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("version_id", DataType.VARCHAR, max_length=64, default_value="")
    schema.add_field("embed_model", DataType.VARCHAR, max_length=128, default_value="")
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
    client.create_collection(collection_name=dst, schema=schema, index_params=index_params)
    print(f"[create] created {dst} dim={dim} with version_id/embed_model")


def export_source(client, src: str) -> list[dict]:
    """Paginate export of source rows (no vectors — will re-embed)."""
    rows: list[dict] = []
    offset = 0
    page = 100
    while True:
        batch = client.query(
            collection_name=src,
            filter="chunk_index >= 0",
            output_fields=["id", "unit_id", "chunk_index", "content"],
            offset=offset,
            limit=page,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=DEFAULT_SRC)
    parser.add_argument("--dst", default=DEFAULT_DST)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--switch-env", action="store_true", help="patch MILVUS_COLLECTION in .env")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--drop-dst-first", action="store_true", help="recreate dst from scratch")
    args = parser.parse_args()

    env = load_env_file()
    apply_runtime_env(env)
    os.environ["MILVUS_COLLECTION"] = args.dst

    from core.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    print(
        f"[cfg] uri={cfg.milvus_uri} src={args.src} dst={args.dst} "
        f"embed={cfg.embed_provider}/{cfg.embed_local_device} dim={cfg.embed_dim}"
    )

    from pymilvus import MilvusClient

    client = MilvusClient(uri=cfg.milvus_uri, timeout=20)
    if not client.has_collection(args.src):
        print(f"[error] source collection {args.src} not found")
        return 1

    if args.drop_dst_first and client.has_collection(args.dst):
        client.drop_collection(args.dst)
        print(f"[create] dropped {args.dst}")

    ensure_dst_collection(client, args.dst, cfg.embed_dim)

    src_rows = export_source(client, args.src)
    print(f"[export] source rows={len(src_rows)}")
    if not src_rows:
        print("[error] source empty; abort")
        return 1

    ckpt = load_ckpt()
    done_ids: set[str] = set(ckpt.get("done_ids") or [])
    embed_model_id = f"local:{cfg.embed_local_model}"
    migration_version = ckpt.get("migration_version") or uuid.uuid4().hex
    ckpt["migration_version"] = migration_version
    ckpt["embed_model"] = embed_model_id
    ckpt["src"] = args.src
    ckpt["dst"] = args.dst
    save_ckpt(ckpt)

    if args.verify_only:
        return verify(client, args.src, args.dst, src_rows, embed_model_id)

    # Load BGE-M3 only when migrating
    from app.deps import get_embedder

    get_embedder.cache_clear()
    embedder = get_embedder()
    print(f"[embed] model ready dim={embedder.dim}")

    pending = [r for r in src_rows if str(r.get("id")) not in done_ids]
    print(f"[migrate] pending={len(pending)} done={len(done_ids)} total={len(src_rows)}")

    batch_size = max(1, args.batch_size)
    t0 = time.time()
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [(r.get("content") or "") for r in batch]
        # skip empty content but record as done to avoid loop
        vectors = embedder.encode(texts)
        data = []
        for row, vec in zip(batch, vectors):
            if len(vec) != cfg.embed_dim:
                raise RuntimeError(f"dim mismatch {len(vec)} != {cfg.embed_dim}")
            data.append(
                {
                    "id": str(row.get("id")),
                    "unit_id": str(row.get("unit_id") or ""),
                    "chunk_index": int(row.get("chunk_index") or 0),
                    "content": str(row.get("content") or ""),
                    "embedding": vec,
                    "version_id": migration_version,
                    "embed_model": embed_model_id,
                }
            )
        try:
            client.insert(collection_name=args.dst, data=data)
            client.flush(args.dst)
        except Exception as exc:
            print(f"[error] insert batch failed at offset {i}: {exc}")
            save_ckpt(ckpt)
            return 2
        for row in batch:
            done_ids.add(str(row.get("id")))
        ckpt["done_ids"] = sorted(done_ids)
        save_ckpt(ckpt)
        pct = 100.0 * len(done_ids) / len(src_rows)
        print(f"[migrate] inserted {len(done_ids)}/{len(src_rows)} ({pct:.1f}%) elapsed={time.time()-t0:.1f}s")

    print("[migrate] all rows written; verifying...")
    rc = verify(client, args.src, args.dst, src_rows, embed_model_id)
    if rc != 0:
        return rc

    if args.switch_env:
        patch_env_collection(args.dst)
        print(f"[env] MILVUS_COLLECTION -> {args.dst} (restart AI service to apply)")

    print("MIGRATION_OK")
    print("NOTE: old collection kept for rollback. Drop kb_ai_chunks only after gray observation.")
    return 0


def verify(client, src: str, dst: str, src_rows: list[dict], embed_model_id: str) -> int:
    from pymilvus import MilvusClient  # noqa: F401

    desc = client.describe_collection(dst)
    names = {f["name"] for f in desc.get("fields", [])}
    required = {"id", "unit_id", "chunk_index", "content", "embedding", "version_id", "embed_model"}
    missing = required - names
    if missing:
        print(f"[verify] FAIL schema missing {sorted(missing)}")
        return 3
    print(f"[verify] schema OK fields={sorted(names)}")

    # count all dst rows via unit_id wildcard is hard; query all ids in pages
    dst_rows: list[dict] = []
    offset = 0
    while True:
        batch = client.query(
            collection_name=dst,
            filter="chunk_index >= 0",
            output_fields=["id", "unit_id", "chunk_index", "content", "version_id", "embed_model"],
            offset=offset,
            limit=100,
        )
        if not batch:
            break
        dst_rows.extend(batch)
        offset += len(batch)
        if len(batch) < 100:
            break

    src_ids = {str(r.get("id")) for r in src_rows}
    dst_ids = {str(r.get("id")) for r in dst_rows}
    print(f"[verify] counts src={len(src_ids)} dst={len(dst_ids)}")
    if len(src_ids) != len(dst_ids):
        print("[verify] FAIL count mismatch")
        return 3
    only_src = src_ids - dst_ids
    only_dst = dst_ids - src_ids
    if only_src or only_dst:
        print(f"[verify] FAIL id sets differ only_src={list(only_src)[:5]} only_dst={list(only_dst)[:5]}")
        return 3

    versions = {r.get("version_id") or "" for r in dst_rows}
    models = {r.get("embed_model") or "" for r in dst_rows}
    if not all(versions) or len(versions) != 1:
        print(f"[verify] FAIL version_id not uniform: {versions}")
        return 3
    if models != {embed_model_id}:
        print(f"[verify] FAIL embed_model unexpected: {models}")
        return 3

    # sample dim + content match
    sample = dst_rows[: min(5, len(dst_rows))]
    src_map = {str(r.get("id")): r for r in src_rows}
    for r in sample:
        s = src_map.get(str(r.get("id")))
        if not s or (s.get("content") or "") != (r.get("content") or ""):
            print(f"[verify] FAIL content mismatch id={r.get('id')}")
            return 3
    # query one vector via search? skip heavy; check insert used dim from encode
    print(f"[verify] OK version={list(versions)[0]} model={list(models)[0]} sample={len(sample)}")
    return 0


def patch_env_collection(dst: str) -> None:
    p = ROOT / ".env"
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith("MILVUS_COLLECTION="):
            out.append(f"MILVUS_COLLECTION={dst}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"MILVUS_COLLECTION={dst}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
