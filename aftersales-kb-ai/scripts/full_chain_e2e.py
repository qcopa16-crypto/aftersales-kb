"""Full ingest chain real acceptance: async parse → embed → retrieve → (generate) → delete.

Uses shopkeeper-ai env + real Milvus + local BGE-M3 + MinerU + optional VLM/LLM.
"""

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
os.environ["LLM_API_BASE"] = env.get("LLM_API_BASE", env.get("VLM_API_BASE", ""))
os.environ["LLM_API_KEY"] = env.get("LLM_API_KEY", env.get("VLM_API_KEY", ""))
os.environ["LLM_MODEL"] = env.get("LLM_MODEL", "glm-4.5-air")
os.environ["MAX_VLM_IMAGES"] = "6"

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()
cfg = get_settings()
print(f"[cfg] store={cfg.vector_store}/{cfg.milvus_collection} embed={cfg.embed_provider}/{cfg.embed_dim}")

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


def poll(task_id: str, timeout: float = 360.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = client.get(f"/internal/tasks/{task_id}", headers=headers)
        if r.status_code != 200:
            return {"status": "failed", "error_message": r.text[:200]}
        body = r.json()
        line = f"{body['status']}/{body['stage']}/{body['progress']}% {body.get('detail','')}"
        if line != last:
            print(f"  [poll] {line}")
            last = line
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.4)
    return {"status": "failed", "error_message": "timeout"}


def full_chain(unit_id: str, file_type: str, path: Path, question: str, use_vlm: bool = False) -> None:
    t0 = time.time()
    # 1 init async parse
    init = client.post(
        "/internal/parse/async",
        headers=headers,
        data={"file_type": file_type, "unit_id": unit_id, "use_vlm": str(use_vlm).lower()},
        files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
    )
    if init.status_code != 200:
        mark(f"init.{unit_id}", False, f"{init.status_code} {init.text[:160]}")
        return
    task_id = init.json()["task_id"]
    mark(f"init.{unit_id}", True, f"task_id={task_id}")

    # 2 poll
    task = poll(task_id)
    if task["status"] != "done":
        mark(f"parse.{unit_id}", False, f"{task.get('error_code')}: {task.get('error_message')}")
        return
    result = task["result"]
    mark(
        f"parse.{unit_id}",
        True,
        f"chunks={len(result['chunks'])} q.suggest_review={result.get('quality',{}).get('suggest_review')} "
        f"images={len(result.get('images') or [])}",
    )

    # 3 embed
    er = client.post(
        "/internal/embed",
        headers=headers,
        json={
            "unit_id": unit_id,
            "chunks": result["chunks"],  # includes heading_path
        },
    )
    if er.status_code != 200:
        mark(f"embed.{unit_id}", False, er.text[:200])
        return
    mark(f"embed.{unit_id}", True, json.dumps(er.json(), ensure_ascii=False))

    # 4 retrieve
    rr = client.post(
        "/internal/retrieve",
        headers=headers,
        json={"question": question, "top_k": 3},
    )
    if rr.status_code != 200:
        mark(f"retrieve.{unit_id}", False, rr.text[:200])
        return
    hits = rr.json()["chunks"]
    hit_units = {h["unit_id"] for h in hits}
    mark(
        f"retrieve.{unit_id}",
        unit_id in hit_units,
        f"top_hit={hits[0]['unit_id'] if hits else None} score={hits[0]['score'] if hits else None:.4f} in_top={unit_id in hit_units}",
    )

    # 5 generate (optional cloud LLM)
    gen = client.post(
        "/internal/generate",
        headers=headers,
        json={
            "question": question,
            "contexts": [
                {"unit_id": h["unit_id"], "title": "资料", "content": h["content"][:500]}
                for h in hits[:2]
            ],
            "session_id": unit_id,
        },
    )
    if gen.status_code == 503:
        mark(f"generate.{unit_id}", True, "SKIP llm disabled")
    elif gen.status_code != 200:
        mark(f"generate.{unit_id}", False, f"HTTP {gen.status_code} {gen.text[:120]}")
    else:
        text = gen.text
        has_delta = "event: delta" in text or "event:done" in text.replace(" ", "")
        mark(f"generate.{unit_id}", has_delta, f"sse_len={len(text)} has_delta={has_delta}")

    # 6 delete vectors
    dr = client.delete(f"/internal/units/{unit_id}", headers=headers)
    mark(f"delete.{unit_id}", dr.status_code == 200 and dr.json().get("deleted") is True, dr.text[:120])

    print(f"  [elapsed] {unit_id}: {time.time()-t0:.1f}s")


def main() -> int:
    h = client.get("/health")
    mark("health", h.status_code == 200, json.dumps(h.json().get("circuits", {}), ensure_ascii=False)[:160])

    ts = int(time.time())
    cases = [
        (f"E2E-MD-{ts}", "md", samples / "sample.md", "备件库存怎么查", False),
        (f"E2E-TXT-{ts}", "txt", samples / "sample.txt", "备件库存怎么查", False),
        (f"E2E-DOCX-{ts}", "docx", samples / "sample.docx", "工单处理流程", False),
        (f"E2E-PDF-{ts}", "pdf", samples / "sample.pdf", "LA2608 指示灯含义", True),
    ]
    for uid, ft, path, q, vlm in cases:
        if not path.exists():
            mark(f"missing.{uid}", False, str(path))
            continue
        full_chain(uid, ft, path, q, use_vlm=vlm)

    print("\n=== FULL CHAIN SUMMARY ===")
    fails = 0
    for name, st, detail in results:
        print(f"{st:4}  {name}: {detail}")
        if st == "FAIL":
            fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
