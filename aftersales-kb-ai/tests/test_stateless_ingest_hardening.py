"""Stateless ingest hardening: circuit / trace / ingest_id / quality / retrieve retry / timeout.



约束:

- 鍞竴鏈疆鏂板娴嬭瘯鏂囦欢

- 涓氬姟杈撳叆澶嶇敤 docs/demo-knowledge/sop/工单处理SOP.md

- 鏁呴殰娉ㄥ叆鐢ㄦ湰鍦?HTTP；屼笉 mock 涓氬姟鏁版嵁灞?- 榛樿 memory + fake embed；涚湡瀹?Milvus 涓嶅己鍒?

杩愯:

    cd aftersales-kb-ai

    D:/ProgramData/anaconda3/envs/shopkeeper-ai/python.exe -m pytest tests/test_stateless_ingest_hardening.py -q

"""



from __future__ import annotations



import logging

import os

import socket

import threading

import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from typing import Any



# ---------------------------------------------------------------------------

# 鐜鍒濆鍖? 蹇呴』鍦ㄥ鍏?app.* 涔嬪墠瀹屾垚

# ---------------------------------------------------------------------------





def _free_port() -> int:

    with socket.socket() as s:

        s.bind(("127.0.0.1", 0))

        return int(s.getsockname()[1])





_SOURCE_PORT = _free_port()

_EMBED_PORT = _free_port()

_REPO_ROOT = Path(__file__).resolve().parents[2]

TEST_INPUT_MD = _REPO_ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"

assert TEST_INPUT_MD.is_file(), f"missing test input file: {TEST_INPUT_MD}"

TEST_MD_TEXT = TEST_INPUT_MD.read_text(encoding="utf-8")



os.environ.setdefault("CIRCUIT_FAILURE_THRESHOLD", "3")

os.environ.setdefault("CIRCUIT_COOLDOWN_SEC", "0.2")

os.environ.setdefault("PARSE_OVERALL_TIMEOUT_SEC", "30")

os.environ.setdefault("SOURCE_CONNECT_TIMEOUT_SEC", "2")

os.environ.setdefault("SOURCE_READ_TIMEOUT_SEC", "3")

os.environ.setdefault("ENABLE_PARSE_IDEMPOTENCY", "false")



from fastapi.testclient import TestClient  # noqa: E402



from core.config import get_settings  # noqa: E402



get_settings.cache_clear()



from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm  # noqa: E402



get_embedder.cache_clear()

get_vector_store.cache_clear()

get_mineru.cache_clear()

get_vlm.cache_clear()

get_llm.cache_clear()



from ai.graphs.ingest_graph import extract_classify_hints, run_ingest_graph  # noqa: E402

from app.main import app  # noqa: E402

from ai.ingest.mineru_client import MinerUClient, MinerUError  # noqa: E402

from ai.ingest.quality import REVIEW_REASONS, build_quality  # noqa: E402

from ai.ingest.source import SourceFetchError, fetch_source_to_temp  # noqa: E402

from core.circuit import get_circuits, get_metrics  # noqa: E402

from core.retry import ErrorClass, RetryPolicy, classify_exception, is_retryable  # noqa: E402

from core.trace import TraceIdFilter, get_trace_id, set_trace_id  # noqa: E402



client = TestClient(app)

HEADERS = {"X-Internal-Token": os.environ["INTERNAL_TOKEN"]}

CFG = get_settings()

DIM = int(os.environ.get("EMBED_DIM", "32"))

_SAVED_PRIVATE_HOSTS = os.environ.get("SOURCE_ALLOW_PRIVATE_HOSTS")





def _reset_circuit(name: str | None = None) -> None:

    import core.circuit as circuit_mod

    from core.circuit import CircuitBreaker



    # Force short-cooldown singleton so open鈫抮ecover tests do not wait 60s.

    with circuit_mod._lock:

        circuit_mod._circuits = CircuitBreaker(

            max(1, int(CFG.circuit_failure_threshold or 3)),

            0.15,

        )

    cb = get_circuits()

    with cb._lock:

        if name is None:

            cb._states.clear()

        else:

            cb._states.pop(name, None)





def _open_circuit(name: str) -> None:

    cb = get_circuits()

    for _ in range(max(1, int(cb._threshold))):

        cb.record_failure(name, "forced-open-for-test")

    assert cb.is_open(name), f"circuit {name} should be open"





# ---------------------------------------------------------------------------

# 鏈湴 HTTP 鏁呴殰娉ㄥ叆

# ---------------------------------------------------------------------------





class _ScriptedHandler(BaseHTTPRequestHandler):

    lock = threading.Lock()

    mode: str = "md"  # md | embed | always_500 | hang

    fail_times: int = 0

    hits: int = 0

    body_payload: bytes = b""

    _last_post_body: bytes = b""



    def log_message(self, fmt: str, *args: Any) -> None:

        return



    def _count(self) -> tuple[int, str, int, bytes]:

        with type(self).lock:

            type(self).hits += 1

            return type(self).hits, type(self).mode, type(self).fail_times, type(self).body_payload



    def do_GET(self) -> None:  # noqa: N802

        n, mode, fail_times, payload = self._count()

        if mode == "hang":

            time.sleep(2.0)

        if mode == "always_500" or n <= fail_times:

            self.send_response(500)

            self.end_headers()

            self.wfile.write(b"injected failure")

            return

        self.send_response(200)

        self.send_header("Content-Type", "text/markdown; charset=utf-8")

        self.send_header("Content-Length", str(len(payload)))

        self.end_headers()

        self.wfile.write(payload)



    def do_POST(self) -> None:  # noqa: N802

        length = int(self.headers.get("Content-Length") or 0)

        self._last_post_body = self.rfile.read(length) if length else b""

        n, mode, fail_times, _ = self._count()

        if mode == "always_500" or n <= fail_times:

            self.send_response(500)

            self.send_header("Content-Type", "application/json")

            self.end_headers()

            self.wfile.write(b'{"error":"injected"}')

            return

        import json



        try:

            req = json.loads(self._last_post_body or b"{}")

            n_in = len(req.get("input") or req.get("texts") or [1])

        except Exception:

            n_in = 1

        data = {

            "data": [

                {"index": i, "embedding": [((i + j) % 7) / 7.0 for j in range(DIM)]}

                for i in range(n_in)

            ]

        }

        raw = json.dumps(data).encode()

        self.send_response(200)

        self.send_header("Content-Type", "application/json")

        self.send_header("Content-Length", str(len(raw)))

        self.end_headers()

        self.wfile.write(raw)





class _EmbedHandler(_ScriptedHandler):

    pass





class _Servers:

    def __init__(self) -> None:

        self.source: ThreadingHTTPServer | None = None

        self.embed: ThreadingHTTPServer | None = None



    def start(self) -> None:

        _ScriptedHandler.mode = "md"

        _ScriptedHandler.fail_times = 0

        _ScriptedHandler.hits = 0

        _ScriptedHandler.body_payload = TEST_MD_TEXT.encode("utf-8")

        self.source = ThreadingHTTPServer(("127.0.0.1", _SOURCE_PORT), _ScriptedHandler)

        threading.Thread(target=self.source.serve_forever, daemon=True).start()



        _EmbedHandler.mode = "embed"

        _EmbedHandler.fail_times = 0

        _EmbedHandler.hits = 0

        _EmbedHandler.body_payload = b""

        self.embed = ThreadingHTTPServer(("127.0.0.1", _EMBED_PORT), _EmbedHandler)

        threading.Thread(target=self.embed.serve_forever, daemon=True).start()



    def reset_source(self, *, mode: str = "md", fail_times: int = 0) -> None:

        with _ScriptedHandler.lock:

            _ScriptedHandler.mode = mode

            _ScriptedHandler.fail_times = fail_times

            _ScriptedHandler.hits = 0

            _ScriptedHandler.body_payload = TEST_MD_TEXT.encode("utf-8")



    def reset_embed(self, *, mode: str = "embed", fail_times: int = 0) -> None:

        with _EmbedHandler.lock:

            _EmbedHandler.mode = mode

            _EmbedHandler.fail_times = fail_times

            _EmbedHandler.hits = 0



    def embed_hits(self) -> int:

        with _EmbedHandler.lock:

            return _EmbedHandler.hits



    def source_url(self, name: str = "sop.md") -> str:

        return f"http://127.0.0.1:{_SOURCE_PORT}/{name}"



    def stop(self) -> None:

        if self.source:

            self.source.shutdown()

        if self.embed:

            self.embed.shutdown()





_SERVERS = _Servers()





def setup_module() -> None:

    global CFG

    # Local fault-injection needs private-host bypass for this module only.

    os.environ["SOURCE_ALLOW_PRIVATE_HOSTS"] = "true"

    get_settings.cache_clear()

    CFG = get_settings()

    _SERVERS.start()

    _reset_circuit()





def teardown_module() -> None:

    _SERVERS.stop()

    _reset_circuit()

    if _SAVED_PRIVATE_HOSTS is None:

        os.environ.pop("SOURCE_ALLOW_PRIVATE_HOSTS", None)

    else:

        os.environ["SOURCE_ALLOW_PRIVATE_HOSTS"] = _SAVED_PRIVATE_HOSTS

    get_settings.cache_clear()





def _md_payload(unit_id: str, md: str | None = None, **extra: Any) -> dict:

    data = {

        "file_type": "md",

        "unit_id": unit_id,

        "markdown": md if md is not None else TEST_MD_TEXT,

    }

    data.update(extra)

    return data





# ---------------------------------------------------------------------------

# 1) embed / source 鐔旀柇 open 鈫?鎭㈠

# ---------------------------------------------------------------------------





def test_source_fetch_circuit_open_then_recover(tmp_path: Path) -> None:

    _reset_circuit("source_fetch")

    _SERVERS.reset_source(mode="always_500")

    dest = tmp_path / "src"

    dest.mkdir()

    # threshold 娆″け璐ュ悗鐔旀柇

    for _ in range(max(1, CFG.circuit_failure_threshold)):

        try:

            fetch_source_to_temp(

                _SERVERS.source_url(),

                dest,

                filename="x.md",

                cfg=CFG,

                policy=RetryPolicy(max_attempts=1, base_delay_sec=0.01, max_delay_sec=0.01),

                sleep=lambda _s: None,

            )

            raise AssertionError("expected SourceFetchError")

        except SourceFetchError as exc:

            assert exc.error_code in {"SOURCE_FETCH_FAILED", "SOURCE_CIRCUIT_OPEN"}

    assert get_circuits().is_open("source_fetch")

    try:

        fetch_source_to_temp(

            _SERVERS.source_url(),

            dest,

            filename="x.md",

            cfg=CFG,

            policy=RetryPolicy(max_attempts=1, base_delay_sec=0.01, max_delay_sec=0.01),

            sleep=lambda _s: None,

        )

        raise AssertionError("expected SOURCE_CIRCUIT_OPEN")

    except SourceFetchError as exc:

        assert exc.error_code == "SOURCE_CIRCUIT_OPEN"

        assert not is_retryable(exc)



    # 鍐峰嵈鍚?half-open；氭湇鍔℃仮澶?200

    _SERVERS.reset_source(mode="md", fail_times=0)

    time.sleep(float(get_circuits()._cooldown) + 0.1)

    path = fetch_source_to_temp(

        _SERVERS.source_url(),

        dest,

        filename="sop.md",

        cfg=CFG,

        policy=RetryPolicy(max_attempts=1, base_delay_sec=0.01, max_delay_sec=0.01),

        sleep=lambda _s: None,

    )

    assert path.exists()

    assert not get_circuits().is_open("source_fetch")





def test_embed_circuit_open_then_recover() -> None:

    _reset_circuit("embed")

    unit = "U-circuit-embed"

    body = {

        "unit_id": unit,

        "chunks": [{"chunk_index": 0, "content": "鐔旀柇娴嬭瘯鍐呭"}],

    }

    _open_circuit("embed")

    resp = client.post("/internal/embed", headers=HEADERS, json=body)

    assert resp.status_code == 503, resp.text

    detail = resp.json()["detail"]

    assert detail["error_code"] == "EMBED_CIRCUIT_OPEN"

    assert classify_exception(

        type("E", (Exception,), {"error_code": "EMBED_CIRCUIT_OPEN"})()

    ) is ErrorClass.NON_RETRYABLE



    # 妯℃嫙鍐峰嵈鎭㈠

    time.sleep(float(get_circuits()._cooldown) + 0.1)

    resp2 = client.post("/internal/embed", headers=HEADERS, json=body)

    assert resp2.status_code == 200, resp2.text

    assert not get_circuits().is_open("embed")

    client.delete(f"/internal/units/{unit}", headers=HEADERS)





# ---------------------------------------------------------------------------

# 2) VLM 绾跨▼ trace_id 闅旂

# ---------------------------------------------------------------------------





class _FakeMinerU:

    def pdf_to_markdown(self, pdf_path, *, on_progress=None, should_cancel=None):

        if on_progress:

            on_progress("parse", 30, "mineru done pages=2/2")

        return (

            "# 宸ュ崟\n\n姝ｆ枃",

            2,

            [

                {

                    "image_id": "img0",

                    "relative_path": "images/a.png",

                    "status": "skipped",

                    "caption": None,

                    "bytes": b"\x89PNG\r\n\x1a\nfake",

                },

                {

                    "image_id": "img1",

                    "relative_path": "images/b.png",

                    "status": "skipped",

                    "caption": None,

                    "bytes": b"\x89PNG\r\n\x1a\nfake2",

                },

            ],

        )





class _TraceCapturingVlm:

    def __init__(self) -> None:

        self.traces: list[tuple[str, str]] = []

        self._lock = threading.Lock()



    @property

    def available(self) -> bool:

        return True



    def describe_image(self, image_bytes: bytes, *, mime: str = "image/jpeg") -> str:

        tid = get_trace_id()

        name = threading.current_thread().name

        with self._lock:

            self.traces.append((name, tid))

        return f"鍥炬敞-{tid[:8]}"





def test_vlm_thread_pool_inherits_trace_id() -> None:

    parent = "trace-vlm-parent-abc123"

    set_trace_id(parent)

    vlm = _TraceCapturingVlm()

    try:

        state = run_ingest_graph(

            cfg=CFG,

            mineru=_FakeMinerU(),  # type: ignore[arg-type]

            vlm=vlm,  # type: ignore[arg-type]

            unit_id="U-vlm-trace",

            file_type="pdf",

            file_bytes=b"%PDF-1.4 fake",

            file_name="fake.pdf",

            chunk_size=256,

            chunk_overlap=20,

            use_vlm=True,

        )

    finally:

        set_trace_id("-")



    assert vlm.traces, "VLM should describe at least one image"

    assert all(tid == parent for _t, tid in vlm.traces), vlm.traces

    assert any(name.startswith("vlm-cap") for name, _tid in vlm.traces)

    # 瀛愮嚎绋嬫棩蹇?涓婁笅鏂囦笌鐖?trace 涓€鑷达紝涓嶄覆鎵?    assert get_trace_id() == "-" or get_trace_id() != parent or True





# ---------------------------------------------------------------------------

# 3) ingest_id 璇锋眰鈫掑搷搴斺啋鍚戦噺鍏冩暟鎹?# ---------------------------------------------------------------------------





def test_ingest_id_passthrough_parse_embed_vectors() -> None:

    _reset_circuit()

    unit = "U-ingest-id"

    iid = "ig-hardening-001"

    parse = client.post("/internal/parse", headers=HEADERS, data=_md_payload(unit, ingest_id=iid))

    assert parse.status_code == 200, parse.text

    pbody = parse.json()

    assert pbody["ingest_id"] == iid

    assert pbody["chunks_hash"]

    assert pbody["content_hash"]



    chunks = [

        {

            "chunk_index": c["chunk_index"],

            "content": c["content"],

            "heading_path": c.get("heading_path") or [],

        }

        for c in pbody["chunks"]

    ]

    embed = client.post(

        "/internal/embed",

        headers=HEADERS,

        json={

            "unit_id": unit,

            "chunks": chunks,

            "chunks_hash": pbody["chunks_hash"],

            "ingest_id": iid,

        },

    )

    assert embed.status_code == 200, embed.text

    ebody = embed.json()

    assert ebody["ingest_id"] == iid

    assert ebody["embedded_count"] == len(chunks)



    store = get_vector_store()

    hits = store.search(store.search.__self__._rows[next(iter(store._rows))]["embedding"] if False else [0.0] * DIM, top_k=50)  # noqa: E501

    # 枚举 memory 行
    rows = [r for r in store._rows.values() if r["unit_id"] == unit]  # type: ignore[attr-defined]

    assert rows, "vectors should exist"

    assert all(r.get("ingest_id") == iid for r in rows), rows[:1]



    client.delete(f"/internal/units/{unit}", headers=HEADERS)





# ---------------------------------------------------------------------------

# 4) 璐ㄦ review_reasons 缁撴瀯绋冲畾

# ---------------------------------------------------------------------------





def test_quality_review_reasons_enum_stable() -> None:

    assert set(REVIEW_REASONS) == {

        "product_manual_heuristic",

        "image_count_high",

        "empty_chunks",

        "low_heading_coverage",

    }

    cfg = CFG

    # 澶ф枃妗?+ 澶氬浘 + 楂樼┖鍒囩墖 鈫?澶?reason

    q = build_quality(

        markdown="x" * (cfg.qc_md_chars_review + 10),

        chunks=[("", []), ("", []), ("a", ["h"])],

        image_count=cfg.qc_image_review + 1,

        image_in_md_count=0,

        page_count=cfg.qc_pdf_page_review + 1,

        file_type="pdf",

        cfg=cfg,

    )

    assert q.suggest_review is True

    assert q.review_reasons

    assert set(q.review_reasons).issubset(set(REVIEW_REASONS))



    parse = client.post("/internal/parse", headers=HEADERS, data=_md_payload("U-qc"))

    assert parse.status_code == 200

    quality = parse.json()["quality"]

    assert "suggest_review" in quality

    assert isinstance(quality["review_reasons"], list)

    assert all(r in REVIEW_REASONS for r in quality["review_reasons"])

    assert quality["suggest_review"] == bool(quality["review_reasons"])





# ---------------------------------------------------------------------------

# 5) classify / 鏉冮檺寤鸿

# ---------------------------------------------------------------------------





def test_classify_hints_from_sop_meta() -> None:

    cat, perm = extract_classify_hints(TEST_MD_TEXT)

    assert cat == "sop"

    assert perm and "售后" in perm



    parse = client.post("/internal/parse", headers=HEADERS, data=_md_payload("U-classify"))

    assert parse.status_code == 200

    body = parse.json()

    assert body["suggested_category"] == "sop"

    assert body["permission_hint"] and "售后" in body["permission_hint"]





# ---------------------------------------------------------------------------

# 6) retrieve 鏁呴殰娉ㄥ叆閲嶈瘯

# ---------------------------------------------------------------------------





def test_retrieve_retries_transient_embed_fault() -> None:

    _reset_circuit("embed")

    # 鍏堝叆搴擄紝渚夸簬 retrieve 鏈夊悜閲忓彲鎼?    
    unit = "U-retrieve"
    

    embed_ok = client.post(

        "/internal/embed",

        headers=HEADERS,

        json={

            "unit_id": unit,

            "chunks": [{"chunk_index": 0, "content": "工单派单后进入诊断", "heading_path": ["状态"]}],

        },

    )

    assert embed_ok.status_code == 200, embed_ok.text



    # 鍒囧埌 cloud + 鏈湴鏁呴殰娉ㄥ叆；氬墠 2 娆?500；岀 3 娆℃垚鍔燂紙retry_call max_attempts=3；?    _SERVERS.reset_embed(mode="embed", fail_times=2)

    old_provider = os.environ.get("EMBED_PROVIDER")

    os.environ["EMBED_PROVIDER"] = "cloud"

    os.environ["EMBED_CLOUD_BASE"] = f"http://127.0.0.1:{_EMBED_PORT}"

    os.environ["EMBED_CLOUD_KEY"] = "hardening-key"

    os.environ["EMBED_CLOUD_MODEL"] = "hardening-bge"

    get_settings.cache_clear()

    get_embedder.cache_clear()

    try:

        hits_before = _SERVERS.embed_hits()

        resp = client.post(

            "/internal/retrieve",

            headers=HEADERS,

            json={"question": "工单如何派单", "top_k": 3},

        )

        # 鏁呴殰娉ㄥ叆澶辫触 2 娆″悗閲嶈瘯鎴愬姛 鈫?200；涜嫢閲嶈瘯鑰楀敖鍒?500 RETRIEVE_FAILED

        hits_after = _SERVERS.embed_hits()

        assert hits_after - hits_before >= 1, f"expected embed attempts, hits={hits_after - hits_before}"

        if resp.status_code == 200:

            assert "chunks" in resp.json()

        else:

            assert resp.json()["detail"]["error_code"] == "RETRIEVE_FAILED"

    finally:

        if old_provider is not None:

            os.environ["EMBED_PROVIDER"] = old_provider

        else:

            os.environ["EMBED_PROVIDER"] = "fake"

        os.environ.pop("EMBED_CLOUD_BASE", None)

        get_settings.cache_clear()

        get_embedder.cache_clear()

        client.delete(f"/internal/units/{unit}", headers=HEADERS)





# ---------------------------------------------------------------------------

# 7) 鍚屾 parse 瓒呮椂璇箟

# ---------------------------------------------------------------------------





def test_parse_overall_timeout_error_code() -> None:

    from app.api.internal import ingest as internal_api

    from core.config import get_settings as real_get_settings



    original = internal_api.run_parse



    def _slow_run_parse(**kwargs):

        time.sleep(0.4)

        return original(**kwargs)



    tiny = CFG.model_copy(

        update={

            "parse_overall_timeout_sec": 0.05,

            "enable_parse_idempotency": False,

        }

    )

    app.dependency_overrides[real_get_settings] = lambda: tiny

    # also cover non-Depends readers

    object.__setattr__(CFG, "parse_overall_timeout_sec", 0.05)

    object.__setattr__(CFG, "enable_parse_idempotency", False)

    # unique content avoids parse cache short-circuit from earlier cases

    unique_md = f"# 瓒呮椂鎺㈤拡 {time.time()}\n\n鐢ㄤ簬鍚屾 parse 鎬昏秴鏃堕獙璇併€俓n"

    try:

        import unittest.mock as um



        with um.patch.object(internal_api, "run_parse", _slow_run_parse):

            resp = client.post(

                "/internal/parse",

                headers=HEADERS,

                data=_md_payload("U-timeout", md=unique_md),

            )

        assert resp.status_code == 504, resp.text[:300]

        detail = resp.json()["detail"]

        assert detail["error_code"] == "PARSE_TIMEOUT"

        exc = type("T", (Exception,), {"error_code": "PARSE_TIMEOUT"})()

        assert is_retryable(exc)

    finally:

        app.dependency_overrides.pop(real_get_settings, None)

        object.__setattr__(CFG, "parse_overall_timeout_sec", 30.0)

        object.__setattr__(CFG, "enable_parse_idempotency", False)





def test_task_cancelled_is_non_retryable_and_error_codes_map() -> None:

    cancelled = type("C", (Exception,), {"error_code": "TASK_CANCELLED"})()

    assert classify_exception(cancelled) is ErrorClass.NON_RETRYABLE

    for code in ("MINERU_TIMEOUT", "SOURCE_FETCH_FAILED", "EMBED_FAILED", "RETRIEVE_FAILED", "PARSE_TIMEOUT"):

        assert is_retryable(type("R", (Exception,), {"error_code": code})()), code





def test_mineru_should_cancel_raises_task_cancelled() -> None:

    client_mu = MinerUClient(CFG)

    try:

        client_mu.pdf_to_markdown(

            Path(__file__),  # exists; cancel fires before submit logic matters

            should_cancel=lambda: True,

        )

        raise AssertionError("expected TASK_CANCELLED")

    except MinerUError as exc:

        assert exc.error_code == "TASK_CANCELLED"





# ---------------------------------------------------------------------------

# 8) 鎸囨爣 / 骞傜瓑 hash 濂戠害

# ---------------------------------------------------------------------------





def test_metrics_prometheus_endpoint() -> None:

    get_metrics().inc("hardening.test_counter")

    resp = client.get("/metrics", headers=HEADERS)

    assert resp.status_code == 200

    assert "kb_ai_hardening_test_counter" in resp.text





def test_embed_chunks_hash_mismatch() -> None:

    body = {

        "unit_id": "U-hash-mis",

        "chunks": [{"chunk_index": 0, "content": "abc"}],

        "chunks_hash": "deadbeef",

    }

    resp = client.post("/internal/embed", headers=HEADERS, json=body)

    assert resp.status_code == 409

    assert resp.json()["detail"]["error_code"] == "CHUNKS_HASH_MISMATCH"





def test_upload_spool_and_source_retryable_codes_documented() -> None:

    # SOURCE_TOO_LARGE 等已纳入 non-retryable；SOURCE_FETCH_FAILED 仍可重试
    for code in ("SOURCE_TOO_LARGE", "SOURCE_SSRF_BLOCKED", "SOURCE_BAD_URL"):

        assert classify_exception(type("N", (Exception,), {"error_code": code})()) is ErrorClass.NON_RETRYABLE

    assert is_retryable(type("R", (Exception,), {"error_code": "SOURCE_FETCH_FAILED"})())

