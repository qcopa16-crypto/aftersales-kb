"""LangGraph AI pipeline — single real-test entry (no mocks).

Run (shopkeeper-ai):
  D:/ProgramData/anaconda3/envs/shopkeeper-ai/python.exe -m pytest tests/test_langgraph_real.py -v

Covers: LangGraph ingest (md/txt/docx/pdf+VLM) → embed → retrieve → generate → delete.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

# ---- env: real backends from .env + shopkeeper-ai local BGE ----
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
os.environ["LLM_API_BASE"] = env.get("LLM_API_BASE", "")
os.environ["LLM_API_KEY"] = env.get("LLM_API_KEY", "")
os.environ["LLM_MODEL"] = env.get("LLM_MODEL", "glm-4.5-air")
os.environ["MAX_VLM_IMAGES"] = "4"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()
get_llm.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.graphs.ingest_graph import build_ingest_graph, run_ingest_graph  # noqa: E402
from app.graphs.qa_graph import run_qa_graph  # noqa: E402
from app.main import app  # noqa: E402
from app.services.parse_pipeline import run_parse  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": os.environ["INTERNAL_TOKEN"]}
SAMPLES = ROOT / "samples"
TS = str(int(time.time()))[-8:]
UNIQUE = f"LG{TS}"


def _uid(tag: str) -> str:
    return f"{UNIQUE}-{tag}-{uuid.uuid4().hex[:6]}"


def test_langgraph_import_and_compile():
    import langgraph  # noqa: F401
    from langgraph.graph import StateGraph  # noqa: F401

    cfg = get_settings()
    g = build_ingest_graph(cfg=cfg, mineru=get_mineru(), vlm=get_vlm())
    assert g is not None


def test_ingest_graph_md_real():
    cfg = get_settings()
    md = f"# LangGraph验收\n\n唯一内容标记 {UNIQUE} 备件库存查询步骤。\n"
    state = run_ingest_graph(
        cfg=cfg,
        mineru=get_mineru(),
        vlm=get_vlm(),
        unit_id=_uid("md"),
        file_type="md",
        markdown=md,
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=False,
    )
    assert state["stage"] == "done"
    assert len(state["chunks"]) >= 1
    assert UNIQUE in (state.get("text") or "")
    assert state.get("quality", {}).get("chunk_count") == len(state["chunks"])


def test_ingest_graph_txt_real():
    cfg = get_settings()
    state = run_ingest_graph(
        cfg=cfg,
        mineru=get_mineru(),
        vlm=get_vlm(),
        unit_id=_uid("txt"),
        file_type="txt",
        markdown=f"文本唯一标记 {UNIQUE} 工单状态机说明。",
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=False,
    )
    assert len(state["chunks"]) >= 1


def test_ingest_graph_docx_real():
    path = SAMPLES / "sample.docx"
    if not path.exists():
        import pytest

        pytest.skip("samples/sample.docx missing")
    cfg = get_settings()
    state = run_ingest_graph(
        cfg=cfg,
        mineru=get_mineru(),
        vlm=get_vlm(),
        unit_id=_uid("docx"),
        file_type="docx",
        file_bytes=path.read_bytes(),
        file_name=path.name,
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=False,
    )
    assert len(state["chunks"]) >= 1
    assert state["text"]


def test_full_chain_pdf_vlm_embed_retrieve_generate_delete():
    """PDF + VLM graph → embed → retrieve → generate → delete (real Milvus/BGE/MinerU/VLM)."""
    pdf = SAMPLES / "sample.pdf"
    if not pdf.exists():
        import pytest

        pytest.skip("samples/sample.pdf missing")
    uid = _uid("pdf")
    cfg = get_settings()

    # LangGraph parse
    state = run_ingest_graph(
        cfg=cfg,
        mineru=get_mineru(),
        vlm=get_vlm(),
        unit_id=uid,
        file_type="pdf",
        file_bytes=pdf.read_bytes(),
        file_name=pdf.name,
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=True,
    )
    assert state["stage"] == "done"
    assert len(state["chunks"]) >= 1
    images = state.get("images") or []
    assert images, "pdf should export images"
    assert any(i.get("status") == "described" for i in images), "VLM should describe at least one image"

    # facade contract
    parsed = run_parse(
        unit_id=uid,
        file_type="md",
        markdown=state["text"],
        chunk_size=512,
        chunk_overlap=50,
        use_vlm=False,
        cfg=cfg,
        mineru=get_mineru(),
        vlm=get_vlm(),
    )
    assert parsed.chunks

    # embed via API (real BGE + Milvus)
    er = client.post(
        "/internal/embed",
        headers=HEADERS,
        json={
            "unit_id": uid,
            "chunks": [
                {"chunk_index": c.chunk_index, "content": c.content, "heading_path": c.heading_path}
                for c in parsed.chunks
            ],
        },
    )
    assert er.status_code == 200, er.text
    assert er.json()["embedded_count"] == len(parsed.chunks)
    assert er.json()["dim"] == 1024

    # retrieve via LangGraph QA (skip cloud generate if LLM unset)
    qa = run_qa_graph(
        embedder=get_embedder(),
        store=get_vector_store(),
        llm=get_llm(),
        question="LA2608 指示灯",
        contexts=[],
        top_k=5,
    )
    hits = qa.get("chunks") or []
    assert hits, "retrieve should return chunks"
    assert any(h["unit_id"] == uid for h in hits), f"expected {uid} in { [h['unit_id'] for h in hits] }"

    # generate: only if LLM configured; still real path when available
    if get_llm().available:
        deltas: list[str] = []
        run_qa_graph(
            embedder=get_embedder(),
            store=get_vector_store(),
            llm=get_llm(),
            question="指示灯说明",
            contexts=[
                {"unit_id": h["unit_id"], "title": "pdf", "content": h["content"][:400]} for h in hits[:2]
            ],
            on_delta=deltas.append,
        )
        assert deltas, "LLM should stream deltas"
    else:
        print("[note] LLM disabled — generate graph skipped")

    # delete
    dr = client.delete(f"/internal/units/{uid}", headers=HEADERS)
    assert dr.status_code == 200
    assert dr.json()["deleted"] is True


def test_api_async_parse_uses_langgraph_pipeline():
    """HTTP async parse still works after graph refactor."""
    md = f"API唯一 {UNIQUE} 测试解析链路。\n"
    init = client.post(
        "/internal/parse/async",
        headers=HEADERS,
        data={"file_type": "md", "unit_id": _uid("api"), "markdown": md},
    )
    assert init.status_code == 200
    task_id = init.json()["task_id"]
    deadline = time.time() + 30
    body = None
    while time.time() < deadline:
        r = client.get(f"/internal/tasks/{task_id}", headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        if body["status"] in {"done", "failed"}:
            break
        time.sleep(0.05)
    assert body and body["status"] == "done", body
    assert body["result"]["chunks"]
