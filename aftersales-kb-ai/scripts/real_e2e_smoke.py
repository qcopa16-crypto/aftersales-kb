"""Real-env smoke: one sample per type through parse/embed/delete against live Milvus."""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

# Live settings — env must be set BEFORE importing app.config
ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

env: dict[str, str] = {}
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

os.environ["INTERNAL_TOKEN"] = env.get("INTERNAL_TOKEN", "change-me-long-random")
os.environ["MINERU_ENABLED"] = env.get("MINERU_ENABLED", "true")
os.environ["MINERU_TOKEN"] = env.get("MINERU_TOKEN", "")
os.environ["MINERU_API_BASE"] = env.get("MINERU_API_BASE", "https://mineru.net")
os.environ["VECTOR_STORE"] = "milvus"
os.environ["MILVUS_URI"] = env.get("MILVUS_URI", "http://127.0.0.1:19530")
os.environ["MILVUS_COLLECTION"] = env.get("MILVUS_COLLECTION", "kb_ai_chunks")

# Embedding: honor .env; default local BGE-M3 when FlagEmbedding is available
provider = os.environ.get("EMBED_MODE") or env.get("EMBED_PROVIDER", "local")
if provider == "local":
    os.environ["EMBED_PROVIDER"] = "local"
    os.environ["EMBED_LOCAL_MODEL"] = env.get(
        "EMBED_LOCAL_MODEL",
        "D:/ai_models/modelscope_cache/models/BAAI--bge-m3/snapshots/master",
    )
    os.environ["EMBED_LOCAL_DEVICE"] = env.get("EMBED_LOCAL_DEVICE", "cuda")
    os.environ["EMBED_DIM"] = env.get("EMBED_DIM", "1024")
else:
    os.environ["EMBED_PROVIDER"] = "cloud"
    os.environ["EMBED_CLOUD_BASE"] = env.get("LLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
    os.environ["EMBED_CLOUD_KEY"] = env.get("LLM_API_KEY", "")
    os.environ["EMBED_CLOUD_MODEL"] = env.get("EMBED_CLOUD_MODEL", "embedding-3")
    os.environ["EMBED_DIM"] = "2048"
os.environ["EMBED_BATCH_SIZE"] = "4"

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()
cfg = get_settings()
print(f"[cfg] embed={cfg.embed_provider} dim={cfg.embed_dim} store={cfg.vector_store} milvus={cfg.milvus_uri}")
print(f"[cfg] mineru_enabled={cfg.mineru_enabled} token_set={bool(cfg.mineru_token)}")

results: list[tuple[str, str, str]] = []


def mark(name: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}: {detail}")


def main() -> int:
    from app.deps import get_embedder, get_mineru, get_vector_store
    from ai.ingest.chunker import split_markdown
    from ai.ingest.docx_parser import docx_to_markdown
    from ai.retrieve.store import VectorItem

    # 1) vector store real Milvus
    try:
        store = get_vector_store()
        store.ensure_collection()
        mark("milvus.ensure", True, f"collection ready type={type(store).__name__}")
    except Exception as e:
        mark("milvus.ensure", False, str(e))
        traceback.print_exc()
        return 1

    # 2) embedder
    try:
        emb = get_embedder()
        vecs = emb.encode(["测试向量 hello", "另一条"])
        mark("embedder", True, f"provider={cfg.embed_provider} dim={emb.dim} n={len(vecs)} d0={len(vecs[0])}")
    except Exception as e:
        mark("embedder", False, str(e))
        traceback.print_exc()
        # continue to parse-only tests if embedder missing
        emb = None

    samples = ROOT / "samples"

    # --- md ---
    unit_md = f"E2E-MD-{int(time.time())}"
    try:
        text = (samples / "sample.md").read_text(encoding="utf-8")
        chunks = split_markdown(text)
        assert chunks, "no chunks"
        if emb:
            vectors = emb.encode([c.content for c in chunks])
            items = [
                VectorItem(chunk_index=c.chunk_index, content=c.content, embedding=vectors[i])
                for i, c in enumerate(chunks)
            ]
            store.delete_by_unit(unit_md)
            n = store.upsert(unit_md, items)
            deleted = store.delete_by_unit(unit_md)
            mark("md.parse_embed_delete", True, f"chunks={len(chunks)} upsert={n} deleted={deleted}")
        else:
            mark("md.parse", True, f"chunks={len(chunks)} (embedder unavailable)")
    except Exception as e:
        mark("md.parse_embed_delete", False, str(e))
        traceback.print_exc()

    # --- txt ---
    unit_txt = f"E2E-TXT-{int(time.time())}"
    try:
        text = (samples / "sample.txt").read_text(encoding="utf-8")
        chunks = split_markdown(text)
        if emb:
            vectors = emb.encode([c.content for c in chunks])
            items = [
                VectorItem(chunk_index=c.chunk_index, content=c.content, embedding=vectors[i])
                for i, c in enumerate(chunks)
            ]
            store.delete_by_unit(unit_txt)
            n = store.upsert(unit_txt, items)
            deleted = store.delete_by_unit(unit_txt)
            mark("txt.parse_embed_delete", True, f"chunks={len(chunks)} upsert={n} deleted={deleted}")
        else:
            mark("txt.parse", True, f"chunks={len(chunks)}")
    except Exception as e:
        mark("txt.parse_embed_delete", False, str(e))
        traceback.print_exc()

    # --- docx ---
    unit_docx = f"E2E-DOCX-{int(time.time())}"
    try:
        text = docx_to_markdown(samples / "sample.docx")
        chunks = split_markdown(text)
        assert chunks, "docx produced no chunks"
        if emb:
            vectors = emb.encode([c.content for c in chunks])
            items = [
                VectorItem(chunk_index=c.chunk_index, content=c.content, embedding=vectors[i])
                for i, c in enumerate(chunks)
            ]
            store.delete_by_unit(unit_docx)
            n = store.upsert(unit_docx, items)
            deleted = store.delete_by_unit(unit_docx)
            mark("docx.parse_embed_delete", True, f"md_len={len(text)} chunks={len(chunks)} upsert={n} deleted={deleted}")
        else:
            mark("docx.parse", True, f"md_len={len(text)} chunks={len(chunks)}")
    except Exception as e:
        mark("docx.parse_embed_delete", False, str(e))
        traceback.print_exc()

    # --- pdf via MinerU ---
    unit_pdf = f"E2E-PDF-{int(time.time())}"
    try:
        mineru = get_mineru()
        md, page_count, images = mineru.pdf_to_markdown(samples / "sample.pdf")
        chunks = split_markdown(md)
        if emb:
            vectors = emb.encode([c.content for c in chunks])
            items = [
                VectorItem(chunk_index=c.chunk_index, content=c.content, embedding=vectors[i])
                for i, c in enumerate(chunks)
            ]
            store.delete_by_unit(unit_pdf)
            n = store.upsert(unit_pdf, items)
            deleted = store.delete_by_unit(unit_pdf)
            mark(
                "pdf.parse_embed_delete",
                True,
                f"pages={page_count} md_len={len(md)} chunks={len(chunks)} images={len(images)} upsert={n} deleted={deleted}",
            )
        else:
            mark(
                "pdf.parse",
                True,
                f"pages={page_count} md_len={len(md)} chunks={len(chunks)} images={len(images)}",
            )
        # preview
        preview = md[:400].replace("\n", " ")
        print(f"[pdf preview] {preview}")
    except Exception as e:
        mark("pdf.parse_embed_delete", False, str(e))
        traceback.print_exc()

    # --- API layer smoke via TestClient (real store + real embedder if available) ---
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        token = cfg.internal_token
        headers = {"X-Internal-Token": token}
        uid = f"E2E-API-{int(time.time())}"
        md_text = (samples / "sample.md").read_text(encoding="utf-8")
        r1 = client.post(
            "/internal/parse",
            headers=headers,
            data={"file_type": "md", "unit_id": uid, "markdown": md_text},
        )
        assert r1.status_code == 200, r1.text
        body = r1.json()
        r2 = client.post(
            "/internal/embed",
            headers=headers,
            json={
                "unit_id": uid,
                "chunks": [
                    {"chunk_index": c["chunk_index"], "content": c["content"]} for c in body["chunks"]
                ],
            },
        )
        assert r2.status_code == 200, r2.text
        r3 = client.delete(f"/internal/units/{uid}", headers=headers)
        assert r3.status_code == 200, r3.text
        mark(
            "api.parse_embed_delete",
            True,
            f"chunks={len(body['chunks'])} embed={r2.json()} delete={r3.json()}",
        )
    except Exception as e:
        mark("api.parse_embed_delete", False, str(e))
        traceback.print_exc()

    print("\n=== SUMMARY ===")
    fails = 0
    for name, status, detail in results:
        print(f"{status:4}  {name}: {detail}")
        if status == "FAIL":
            fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
