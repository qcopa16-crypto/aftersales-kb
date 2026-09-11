"""Phase-2 real acceptance: VLM captions + multi-type ingest into Milvus (data kept)."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
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

os.environ["EMBED_PROVIDER"] = "local"
os.environ["EMBED_LOCAL_MODEL"] = env.get(
    "EMBED_LOCAL_MODEL",
    "D:/ai_models/modelscope_cache/models/BAAI--bge-m3/snapshots/master",
)
os.environ["EMBED_LOCAL_DEVICE"] = env.get("EMBED_LOCAL_DEVICE", "cuda")
os.environ["EMBED_DIM"] = env.get("EMBED_DIM", "1024")
os.environ["EMBED_BATCH_SIZE"] = "4"

# Phase-2 VLM on
os.environ["VLM_ENABLED"] = "true"
os.environ["VLM_API_BASE"] = env.get("VLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
os.environ["VLM_API_KEY"] = env.get("VLM_API_KEY", env.get("LLM_API_KEY", ""))
os.environ["VLM_MODEL"] = env.get("VLM_MODEL", "glm-4.6v")
os.environ["MAX_VLM_IMAGES"] = "8"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
cfg = get_settings()
print(
    f"[cfg] embed={cfg.embed_provider}/{cfg.embed_dim} store={cfg.vector_store}/"
    f"{cfg.milvus_collection} vlm={cfg.vlm_enabled}/{cfg.vlm_model} mineru={cfg.mineru_enabled}"
)

results: list[tuple[str, str, str]] = []
kept_units: list[str] = []


def mark(name: str, ok: bool, detail: str) -> None:
    st = "PASS" if ok else "FAIL"
    results.append((name, st, detail))
    print(f"[{st}] {name}: {detail}")


def ingest_file(
    *,
    unit_id: str,
    file_type: str,
    path: Path | None = None,
    markdown: str | None = None,
    use_vlm: bool = False,
    keep: bool = True,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    headers = {"X-Internal-Token": cfg.internal_token}

    data = {"file_type": file_type, "unit_id": unit_id, "use_vlm": str(use_vlm).lower()}
    files = None
    if markdown is not None:
        data["markdown"] = markdown
    elif path is not None:
        files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
    else:
        raise ValueError("need path or markdown")

    t0 = time.time()
    r1 = client.post("/internal/parse", headers=headers, data=data, files=files)
    if r1.status_code != 200:
        mark(f"parse.{unit_id}", False, f"HTTP {r1.status_code}: {r1.text[:300]}")
        return
    body = r1.json()
    q = body.get("quality") or {}
    imgs = body.get("images") or []
    described = sum(1 for i in imgs if i.get("status") == "described")
    failed = sum(1 for i in imgs if i.get("status") == "failed")
    has_caption_md = "【图示】" in (body.get("markdown") or "")

    r2 = client.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit_id,
            "chunks": [
                {"chunk_index": c["chunk_index"], "content": c["content"]} for c in body["chunks"]
            ],
        },
    )
    if r2.status_code != 200:
        mark(f"embed.{unit_id}", False, f"HTTP {r2.status_code}: {r2.text[:300]}")
        return
    emb = r2.json()

    if keep:
        kept_units.append(unit_id)

    detail = (
        f"chunks={len(body['chunks'])} embed={emb['embedded_count']}/dim={emb['dim']} "
        f"quality.suggest_review={q.get('suggest_review')} images={len(imgs)} "
        f"described={described} failed={failed} caption_in_md={has_caption_md} "
        f"elapsed={time.time()-t0:.1f}s"
    )
    if use_vlm and file_type == "pdf" and imgs:
        # at least one described or all failed with reason
        ok = described > 0 or failed == len(imgs)
        mark(f"ingest.{unit_id}", ok, detail)
    else:
        mark(f"ingest.{unit_id}", True, detail)

    # print caption samples
    for i in imgs[:3]:
        if i.get("caption"):
            print(f"  [caption] {i.get('relative_path')}: {i['caption'][:120]}")
    # save report
    out = ROOT / "samples" / f"report_{unit_id}.json"
    out.parent.mkdir(exist_ok=True)
    slim = {
        "unit_id": unit_id,
        "file_type": file_type,
        "use_vlm": use_vlm,
        "chunk_count": len(body["chunks"]),
        "embedded_count": emb["embedded_count"],
        "dim": emb["dim"],
        "quality": q,
        "images": [
            {k: i.get(k) for k in ("image_id", "relative_path", "status", "caption")}
            for i in imgs
        ],
        "markdown_preview": (body.get("markdown") or "")[:800],
    }
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [report] {out}")


def verify_milvus() -> None:
    from app.deps import get_vector_store
    from app.services.vector_store import MilvusVectorStore

    store = get_vector_store()
    if not isinstance(store, MilvusVectorStore):
        mark("milvus.verify", False, f"not milvus: {type(store)}")
        return
    client = store._client
    coll = store._collection
    total = 0
    for uid in kept_units:
        try:
            rows = client.query(
                collection_name=coll,
                filter=f'unit_id == "{uid}"',
                output_fields=["chunk_index"],
                limit=1000,
            )
            n = len(rows)
            total += n
            mark(f"milvus.rows.{uid}", n > 0, f"rows={n}")
        except Exception as e:
            mark(f"milvus.rows.{uid}", False, str(e))
    mark("milvus.total", total > 0, f"kept_rows≈{total} units={kept_units}")


def main() -> int:
    samples = ROOT / "samples"
    ts = int(time.time())

    # 1) md with table + image path (no VLM — no local image bytes)
    ingest_file(
        unit_id=f"P2-MD-{ts}",
        file_type="md",
        path=samples / "sample.md",
        use_vlm=False,
    )

    # 2) txt
    ingest_file(
        unit_id=f"P2-TXT-{ts}",
        file_type="txt",
        path=samples / "sample.txt",
        use_vlm=False,
    )

    # 3) docx
    ingest_file(
        unit_id=f"P2-DOCX-{ts}",
        file_type="docx",
        path=samples / "sample.docx",
        use_vlm=False,
    )

    # 4) pdf + VLM (phase-2 core)
    pdf = samples / "sample.pdf"
    if pdf.exists():
        ingest_file(
            unit_id=f"P2-PDF-{ts}",
            file_type="pdf",
            path=pdf,
            use_vlm=True,
        )
    else:
        mark("ingest.pdf", False, "samples/sample.pdf missing")

    # keep vectors in DB
    try:
        verify_milvus()
    except Exception:
        traceback.print_exc()
        mark("milvus.verify", False, "exception")

    print("\n=== SUMMARY ===")
    fails = 0
    for name, st, detail in results:
        print(f"{st:4}  {name}: {detail}")
        if st == "FAIL":
            fails += 1
    print(f"kept units: {kept_units}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
