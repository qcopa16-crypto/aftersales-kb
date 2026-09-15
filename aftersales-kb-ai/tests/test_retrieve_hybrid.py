"""Hybrid retrieve tests: multi-path, RRF, degrade, lexical path.



约束:

- 业务夹具鍙娇鐢?docs/demo-knowledge/sop/工单处理SOP.md

- 榛樿 memory + fake embed

- 涓?setdefault ENABLE_PARSE_IDEMPOTENCY

"""



from __future__ import annotations



import os

from pathlib import Path





from fastapi.testclient import TestClient  # noqa: E402



from core.config import get_settings  # noqa: E402



get_settings.cache_clear()



from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm  # noqa: E402



get_embedder.cache_clear()

get_vector_store.cache_clear()

get_mineru.cache_clear()

get_vlm.cache_clear()

get_llm.cache_clear()



from app.main import app  # noqa: E402

from ai.retrieve.lexical import get_lexical_index, tokenize  # noqa: E402

from ai.retrieve.pipeline import fuse_rrf  # noqa: E402



client = TestClient(app)

HEADERS = {"X-Internal-Token": "test-token"}

SOP = Path(__file__).resolve().parents[2] / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"

assert SOP.is_file(), f"missing fixture: {SOP}"

SOP_TEXT = SOP.read_text(encoding="utf-8")





def _embed_unit(unit_id: str, markdown: str, **kwargs) -> list[dict]:

    pr = client.post(

        "/internal/parse",

        headers=HEADERS,

        data={"file_type": "md", "unit_id": unit_id, "markdown": markdown},

    )

    assert pr.status_code == 200, pr.text

    chunks = pr.json()["chunks"]

    payload = {

        "unit_id": unit_id,

        "chunks": [{"chunk_index": c["chunk_index"], "content": c["content"]} for c in chunks],

    }

    payload.update(kwargs)

    er = client.post("/internal/embed", headers=HEADERS, json=payload)

    assert er.status_code == 200, er.text

    return chunks





def _cleanup(*unit_ids: str) -> None:

    for uid in unit_ids:

        client.delete(f"/internal/units/{uid}", headers=HEADERS)





def test_tokenize_chinese_bigrams_and_ascii():

    toks = tokenize("工单 U-1234故障码 E01")

    assert "工" in toks

    assert "工单" in toks

    assert "u" in toks and "1234" in toks

    assert "e01" in toks





def test_fuse_rrf_formula_and_missing_path():

    # A ranks 1 in dense; B ranks 1 in lexical; C ranks 2 in dense only

    scores = fuse_rrf([["A", "C"], ["B"]], k=60)

    assert abs(scores["A"] - 1 / 61) < 1e-9

    assert abs(scores["B"] - 1 / 61) < 1e-9

    assert abs(scores["C"] - 1 / 62) < 1e-9

    assert scores["A"] == scores["B"] > scores["C"]





def test_t_hyb_01_main_path_hybrid():

    _cleanup("U-hyb-main")

    _embed_unit("U-hyb-main", SOP_TEXT)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理标准步骤有哪些", "top_k": 5},

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["hybrid"] is True

    assert body["degraded_to_dense"] is False

    assert body["took_ms"] is not None

    assert len(body["chunks"]) >= 1

    paths = set()

    for c in body["chunks"]:

        assert c["score_fused"] is not None

        assert c["retrieval_paths"]

        paths.update(c["retrieval_paths"])

    assert "dense" in paths

    _cleanup("U-hyb-main")





def test_t_hyb_02_both_paths_same_chunk():

    _cleanup("U-hyb-both")

    _embed_unit("U-hyb-both", SOP_TEXT)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "waiting_parts 状态含义", "top_k": 8},

    )

    assert rr.status_code == 200, rr.text

    dual = [c for c in rr.json()["chunks"] if "dense" in c["retrieval_paths"] and "lexical" in c["retrieval_paths"]]

    assert dual, "expected at least one chunk hit by both paths"

    c = dual[0]

    assert c["score_dense"] is not None

    assert c["score_lexical"] is not None

    assert c["score_fused"] is not None

    assert c["score"] == c["score_fused"]

    _cleanup("U-hyb-both")





def test_t_hyb_03_lexical_exact_token():

    _cleanup("U-hyb-lex")

    # Distinctive token unlikely in generic semantic paraphrase alone

    md = (

        "# 鏁呴殰鐮侀€熸煡\n\n"

        "鍨嬪彿 XJ-8800 鍑虹幇鏁呴殰鐮?E77 鏃讹紝鍏堟柇鐢靛啀鏇存崲涓绘澘銆俓n"

        "鍏朵粬閫氱敤淇濅慨璇存槑瑙佸伐鍗曞鐞?SOP銆俓n"

    )

    _embed_unit("U-hyb-lex", md)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "XJ-8800 E77", "top_k": 5},

    )

    assert rr.status_code == 200, rr.text

    hits = rr.json()["chunks"]

    assert hits, "lexical path should surface exact model/fault token"

    assert any("XJ-8800" in h["content"] or "E77" in h["content"] for h in hits)

    assert any("lexical" in h["retrieval_paths"] for h in hits)

    _cleanup("U-hyb-lex")





def test_t_hyb_04_semantic_only_dense_ok():

    _cleanup("U-hyb-sem")

    _embed_unit("U-hyb-sem", SOP_TEXT)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "璁惧鍑洪棶棰樹箣鍚庝竴鑸簲璇ユ€庝箞澶勭悊", "top_k": 5},

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["hybrid"] is True

    assert len(body["chunks"]) >= 1

    # Missing lexical hits must not fail the request

    for c in body["chunks"]:

        assert "dense" in c["retrieval_paths"] or "lexical" in c["retrieval_paths"]

    _cleanup("U-hyb-sem")





def test_t_hyb_06_lexical_degrade_to_dense(monkeypatch):

    _cleanup("U-hyb-deg")

    _embed_unit("U-hyb-deg", SOP_TEXT)

    index = get_lexical_index()



    def boom():

        raise RuntimeError("lexical offline")



    monkeypatch.setattr(index, "search", boom)

    # Force empty index so sync is skipped after we keep docs; search itself raises

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "宸ュ崟鐘舵€佹満", "top_k": 3},

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["degraded_to_dense"] is True

    assert body["hybrid"] is True

    assert len(body["chunks"]) >= 1

    metrics = client.get("/metrics", headers=HEADERS)

    assert metrics.status_code == 200

    assert "kb_ai_retrieve_hybrid_degraded" in metrics.text

    monkeypatch.undo()

    _cleanup("U-hyb-deg")





def test_t_hyb_07_forbid_degrade_raises(monkeypatch):

    _cleanup("U-hyb-nod")

    _embed_unit("U-hyb-nod", SOP_TEXT)

    cfg = get_settings()

    monkeypatch.setattr(cfg, "retrieve_hybrid_degrade_to_dense", False)

    index = get_lexical_index()



    def boom():

        raise RuntimeError("lexical offline")



    monkeypatch.setattr(index, "search", boom)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "宸ュ崟鐘舵€佹満", "top_k": 3},

    )

    assert rr.status_code == 500, rr.text

    assert rr.json()["detail"]["error_code"] == "RETRIEVE_FAILED"

    monkeypatch.undo()

    _cleanup("U-hyb-nod")





def test_t_hyb_08_disable_hybrid():

    _cleanup("U-hyb-off")

    _embed_unit("U-hyb-off", SOP_TEXT)

    rr = client.post(

        "/internal/retrieve",

        headers=HEADERS,

        json={"question": "工单处理娴佺▼", "top_k": 3, "enable_hybrid": False},

    )

    assert rr.status_code == 200, rr.text

    body = rr.json()

    assert body["hybrid"] is False

    assert body["degraded_to_dense"] is False

    assert len(body["chunks"]) >= 1

    for c in body["chunks"]:

        assert c["retrieval_paths"] == ["dense"] or "lexical" not in c["retrieval_paths"]

    _cleanup("U-hyb-off")

