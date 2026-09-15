"""Real acceptance: async parse+embed, one sample per file type."""

from __future__ import annotations

import json
import os
import sys
import time
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
os.environ["VLM_ENABLED"] = "true"
os.environ["VLM_API_BASE"] = env.get("VLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
os.environ["VLM_API_KEY"] = env.get("VLM_API_KEY", env.get("LLM_API_KEY", ""))
os.environ["VLM_MODEL"] = env.get("VLM_MODEL", "glm-4.6v")
os.environ["MAX_VLM_IMAGES"] = "6"

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()
cfg = get_settings()
print(f"[cfg] async ingest embed={cfg.embed_provider}/{cfg.embed_dim} vlm={cfg.vlm_enabled}")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
headers = {"X-Internal-Token": cfg.internal_token}
samples = ROOT / "samples"
results: list[tuple[str, str, str]] = []


def mark(name: str, ok: bool, detail: str) -> None:
    st = "PASS" if ok else "FAIL"
    results.append((name, st, detail))
    print(f"[{st}] {name}: {detail}")


def poll_task(task_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = client.get(f"/internal/tasks/{task_id}", headers=headers)
        if r.status_code != 200:
            return {"status": "failed", "error_message": r.text[:300]}
        body = r.json()
        line = f"{body['status']}/{body['stage']}/{body['progress']}% {body.get('detail','')}"
        if line != last:
            print(f"  [poll] {line}")
            last = line
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.5)
    return {"status": "failed", "error_message": "poll timeout"}


def ingest_one(unit_id: str, file_type: str, path: Path, use_vlm: bool = False) -> None:
    t0 = time.time()
    init = client.post(
        "/internal/parse/async",
        headers=headers,
        data={"file_type": file_type, "unit_id": unit_id, "use_vlm": str(use_vlm).lower()},
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
    )
    if init.status_code != 200:
        mark(f"async.init.{unit_id}", False, f"HTTP {init.status_code}: {init.text[:200]}")
        return
    task_id = init.json()["task_id"]
    mark(f"async.init.{unit_id}", True, f"task_id={task_id} status={init.json()['status']}")

    task = poll_task(task_id)
    if task["status"] != "done":
        mark(
            f"async.done.{unit_id}",
            False,
            f"{task.get('error_code')}: {task.get('error_message')}",
        )
        return

    result = task["result"]
    embed_r = client.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit_id,
            "chunks": [
                {"chunk_index": c["chunk_index"], "content": c["content"]}
                for c in result["chunks"]
            ],
        },
    )
    if embed_r.status_code != 200:
        mark(f"async.embed.{unit_id}", False, embed_r.text[:200])
        return
    emb = embed_r.json()
    imgs = result.get("images") or []
    described = sum(1 for i in imgs if i.get("status") == "described")
    mark(
        f"async.ingest.{unit_id}",
        True,
        f"chunks={len(result['chunks'])} embed={emb['embedded_count']}/dim={emb['dim']} "
        f"images={len(imgs)} described={described} caption_md={'图示' in (result.get('markdown') or '')} "
        f"elapsed={time.time()-t0:.1f}s",
    )
    out = samples / f"async_report_{unit_id}.json"
    out.write_text(
        json.dumps(
            {
                "unit_id": unit_id,
                "task_id": task_id,
                "chunk_count": len(result["chunks"]),
                "embedded_count": emb["embedded_count"],
                "quality": result.get("quality"),
                "images": [
                    {k: i.get(k) for k in ("image_id", "relative_path", "status", "caption")}
                    for i in imgs
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  [report] {out}")


def main() -> int:
    ts = int(time.time())
    jobs = [
        (f"A-MD-{ts}", "md", samples / "sample.md", False),
        (f"A-TXT-{ts}", "txt", samples / "sample.txt", False),
        (f"A-DOCX-{ts}", "docx", samples / "sample.docx", False),
        (f"A-PDF-{ts}", "pdf", samples / "sample.pdf", True),
    ]
    for uid, ft, path, vlm in jobs:
        if not path.exists():
            mark(f"missing.{uid}", False, str(path))
            continue
        ingest_one(uid, ft, path, use_vlm=vlm)

    print("\n=== SUMMARY ===")
    fails = 0
    for name, st, detail in results:
        print(f"{st:4}  {name}: {detail}")
        if st == "FAIL":
            fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
