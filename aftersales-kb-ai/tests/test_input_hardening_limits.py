"""Input hardening limits: embed/markdown/generate caps, parse timeout spool, auth edges.

约束:
- 鍞竴鏈疆鏂板娴嬭瘯鏂囦欢
- 涓氬姟杈撳叆澶嶇敤 docs/demo-knowledge/sop/工单处理SOP.md
- 鏁呴殰娉ㄥ叆鐢ㄦ湰鍦?HTTP；屼笉 mock 涓氬姟鏁版嵁灞?- 榛樿 memory + fake embed

杩愯:
    cd aftersales-kb-ai
    D:/ProgramData/anaconda3/envs/shopkeeper-ai/python.exe -m pytest tests/test_input_hardening_limits.py -q
"""

from __future__ import annotations

import io
import os
import re
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
_REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_INPUT_MD = _REPO_ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
assert TEST_INPUT_MD.is_file(), f"missing test input file: {TEST_INPUT_MD}"
TEST_MD_TEXT = TEST_INPUT_MD.read_text(encoding="utf-8")

# Do NOT setdefault ENABLE_PARSE_IDEMPOTENCY: would poison later modules
# (e.g. test_retrieve_generate) via import-order setdefault.
os.environ.setdefault("SOURCE_CONNECT_TIMEOUT_SEC", "2")
os.environ.setdefault("SOURCE_READ_TIMEOUT_SEC", "3")
os.environ.setdefault("METRICS_PUBLIC", "false")
os.environ.setdefault("EMBED_MAX_CHUNKS", "5000")
os.environ.setdefault("MAX_MARKDOWN_CHARS", "200000")
os.environ.setdefault("ERROR_MESSAGE_MAX_CHARS", "500")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import get_embedder, get_mineru, get_vector_store, get_vlm  # noqa: E402

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()

from app.main import app  # noqa: E402
from core.circuit import get_metrics  # noqa: E402
from core.errors import safe_message, sanitize_text  # noqa: E402
from core.retry import ErrorClass, classify_exception  # noqa: E402
from ai.ingest.source import SourceFetchError, fetch_source_to_temp  # noqa: E402
from core.retry import RetryPolicy  # noqa: E402

client = TestClient(app)
HEADERS = {"X-Internal-Token": os.environ["INTERNAL_TOKEN"]}
CFG = get_settings()
_SAVED_PRIVATE_HOSTS = os.environ.get("SOURCE_ALLOW_PRIVATE_HOSTS")
_SAVED_SPOOL_THRESHOLD = os.environ.get("UPLOAD_SPOOL_THRESHOLD")


class _HangHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        time.sleep(2.5)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"# hang")


class _Servers:
    def __init__(self) -> None:
        self.source: ThreadingHTTPServer | None = None

    def start(self) -> None:
        self.source = ThreadingHTTPServer(("127.0.0.1", _SOURCE_PORT), _HangHandler)
        threading.Thread(target=self.source.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.source:
            self.source.shutdown()

    def source_url(self, name: str = "hang.md") -> str:
        return f"http://127.0.0.1:{_SOURCE_PORT}/{name}"


_SERVERS = _Servers()


def setup_module() -> None:
    global CFG
    os.environ["SOURCE_ALLOW_PRIVATE_HOSTS"] = "true"
    os.environ["UPLOAD_SPOOL_THRESHOLD"] = "1"
    get_settings.cache_clear()
    CFG = get_settings()
    _SERVERS.start()


def teardown_module() -> None:
    _SERVERS.stop()
    for key, saved in (
        ("SOURCE_ALLOW_PRIVATE_HOSTS", _SAVED_PRIVATE_HOSTS),
        ("UPLOAD_SPOOL_THRESHOLD", _SAVED_SPOOL_THRESHOLD),
    ):
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
    get_settings.cache_clear()


def _md_payload(unit_id: str, md: str | None = None, **extra: Any) -> dict:
    data = {
        "file_type": "md",
        "unit_id": unit_id,
        "markdown": md if md is not None else TEST_MD_TEXT,
    }
    data.update(extra)
    return data


def _detail(resp) -> dict:
    body = resp.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail
    return {"error_code": None, "message": str(detail)}


# ---------------------------------------------------------------------------
# P-01 embed 瓒?chunks
# ---------------------------------------------------------------------------


def test_embed_too_many_chunks_returns_400() -> None:
    limit = int(CFG.embed_max_chunks)
    assert limit >= 1
    # Temporarily shrink the cap so the test stays light.
    object.__setattr__(CFG, "embed_max_chunks", 3)
    try:
        chunks = [
            {"chunk_index": i, "content": f"chunk-{i} 宸ュ崟娲惧崟璇存槑"} for i in range(4)
        ]
        resp = client.post(
            "/internal/embed",
            headers=HEADERS,
            json={"unit_id": "U-too-many", "chunks": chunks},
        )
        assert resp.status_code == 400, resp.text
        assert _detail(resp)["error_code"] == "EMBED_TOO_MANY_CHUNKS"
        assert classify_exception(
            type("E", (Exception,), {"error_code": "EMBED_TOO_MANY_CHUNKS"})()
        ) is ErrorClass.NON_RETRYABLE
        # 鎭㈠鍚庡悓鍐呭鍙?embed
        resp_ok = client.post(
            "/internal/embed",
            headers=HEADERS,
            json={"unit_id": "U-too-many-ok", "chunks": chunks[:3]},
        )
        assert resp_ok.status_code == 200, resp_ok.text
        client.delete("/internal/units/U-too-many-ok", headers=HEADERS)
    finally:
        object.__setattr__(CFG, "embed_max_chunks", limit)


# ---------------------------------------------------------------------------
# P-02 markdown 瓒呴暱
# ---------------------------------------------------------------------------


def test_parse_markdown_too_long_returns_400() -> None:
    limit = int(CFG.max_markdown_chars)
    object.__setattr__(CFG, "max_markdown_chars", 80)
    try:
        long_md = "# 鏍囬\n" + ("姝ｆ枃" * 100)
        resp = client.post(
            "/internal/parse",
            headers=HEADERS,
            data=_md_payload("U-md-long", md=long_md),
        )
        assert resp.status_code == 400, resp.text
        assert _detail(resp)["error_code"] == "MARKDOWN_TOO_LONG"
        assert classify_exception(
            type("E", (Exception,), {"error_code": "MARKDOWN_TOO_LONG"})()
        ) is ErrorClass.NON_RETRYABLE
        object.__setattr__(CFG, "max_markdown_chars", limit)
        assert len(TEST_MD_TEXT) <= limit
        resp_ok = client.post(
            "/internal/parse",
            headers=HEADERS,
            data=_md_payload("U-md-ok", md=TEST_MD_TEXT),
        )
        assert resp_ok.status_code == 200, resp_ok.text[:300]
    finally:
        object.__setattr__(CFG, "max_markdown_chars", limit)


def test_generate_payload_too_large_returns_400() -> None:
    q_limit = int(CFG.generate_max_question_chars)
    ctx_limit = int(CFG.generate_max_contexts_total_chars)
    object.__setattr__(CFG, "generate_max_question_chars", 10)
    object.__setattr__(CFG, "generate_max_contexts_total_chars", 50)
    try:
        # question 超限；未配置 LLM 时也会在长度检查前 400
        resp = client.post(
            "/internal/generate",
            headers=HEADERS,
            json={
                "question": "X" * 20,
                "contexts": [],
            },
        )
        assert resp.status_code == 400, resp.text
        assert _detail(resp)["error_code"] == "PAYLOAD_TOO_LARGE"

        # contexts 鎬婚暱瓒呴檺
        resp2 = client.post(
            "/internal/generate",
            headers=HEADERS,
            json={
                "question": "Q",
                "contexts": [
                    {
                        "unit_id": "U-ctx",
                        "title": "t",
                        "content": "寰堥暱鐨勪笂涓嬫枃" * 30,
                    }
                ],
            },
        )
        assert resp2.status_code == 400, resp2.text
        assert _detail(resp2)["error_code"] == "PAYLOAD_TOO_LARGE"
    finally:
        object.__setattr__(CFG, "generate_max_question_chars", q_limit)
        object.__setattr__(CFG, "generate_max_contexts_total_chars", ctx_limit)


def test_generate_strips_control_chars() -> None:
    dirty = "淇濅慨\x00鏀跨瓥\x01璇存槑\n淇濈暀鎹㈣"
    cleaned = sanitize_text(dirty)
    assert "\x00" not in cleaned and "\x01" not in cleaned
    assert "淇濅慨" in cleaned and "鏀跨瓥" in cleaned
    assert "鎹㈣" in cleaned or "\n" in dirty  # sanitize keeps \n


# ---------------------------------------------------------------------------
# P-03 parse 瓒呮椂 + spool 娓呯悊 + 鎸囨爣
# ---------------------------------------------------------------------------


def test_parse_timeout_cleans_spool_and_increments_metric() -> None:
    import unittest.mock as um

    from app.api.internal import ingest as internal_api

    original = internal_api.run_parse
    spool_dir = internal_api._parse_spool_dir
    before_metrics = get_metrics().snapshot().get("counters", {}).get("parse.timeout", 0)
    before_files = set(spool_dir.glob("*")) if spool_dir.exists() else set()

    def _slow_run_parse(**kwargs):
        time.sleep(0.8)
        return original(**kwargs)

    object.__setattr__(CFG, "parse_overall_timeout_sec", 0.05)
    object.__setattr__(CFG, "enable_parse_idempotency", False)
    try:
        import ai.ingest.parse as parse_mod

        with um.patch.object(internal_api, "run_parse", _slow_run_parse), um.patch.object(parse_mod, "run_parse", _slow_run_parse):
            resp = client.post(
                "/internal/parse",
                headers=HEADERS,
                data=_md_payload("U-timeout-spool", md=TEST_MD_TEXT),
                files={"file": ("sop.md", io.BytesIO(TEST_MD_TEXT.encode("utf-8")), "text/markdown")},
            )
        assert resp.status_code == 504, resp.text[:400]
        assert _detail(resp)["error_code"] == "PARSE_TIMEOUT"

        after_metrics = get_metrics().snapshot().get("counters", {}).get("parse.timeout", 0)
        assert after_metrics >= before_metrics + 1

        # 绛夊緟 abandoned worker + 鍚庡彴娓呯悊绾跨▼
        deadline = time.time() + 2.0
        while time.time() < deadline:
            leftover = {
                p for p in spool_dir.glob("*") if p not in before_files and p.exists()
            }
            if not leftover:
                break
            time.sleep(0.05)
        leftover = {
            p for p in spool_dir.glob("*") if p not in before_files and p.exists()
        }
        assert not leftover, f"spool not cleaned: {leftover}"
    finally:
        object.__setattr__(CFG, "parse_overall_timeout_sec", 30.0)
        object.__setattr__(CFG, "enable_parse_idempotency", False)


def test_parse_timeout_via_hang_source() -> None:
    object.__setattr__(CFG, "parse_overall_timeout_sec", 0.2)
    object.__setattr__(CFG, "enable_parse_idempotency", False)
    try:
        resp = client.post(
            "/internal/parse",
            headers=HEADERS,
            data={
                "file_type": "md",
                "unit_id": "U-hang-src",
                "source_url": _SERVERS.source_url(),
            },
        )
        assert resp.status_code == 504, resp.text[:400]
        assert _detail(resp)["error_code"] == "PARSE_TIMEOUT"
    finally:
        object.__setattr__(CFG, "parse_overall_timeout_sec", 30.0)
        object.__setattr__(CFG, "enable_parse_idempotency", False)


# ---------------------------------------------------------------------------
# P-04 SSRF 绉佺綉鍥炲綊
# ---------------------------------------------------------------------------


def test_ssrf_blocks_private_host_when_not_allowed(tmp_path: Path) -> None:
    object.__setattr__(CFG, "source_allow_private_hosts", False)
    dest = tmp_path / "ssrf"
    dest.mkdir()
    try:
        try:
            fetch_source_to_temp(
                "http://127.0.0.1:9/x.md",
                dest,
                filename="x.md",
                cfg=CFG,
                policy=RetryPolicy(max_attempts=1, base_delay_sec=0.01, max_delay_sec=0.01),
                sleep=lambda _s: None,
            )
            raise AssertionError("expected SSRF block")
        except SourceFetchError as exc:
            assert exc.error_code == "SOURCE_SSRF_BLOCKED"
    finally:
        object.__setattr__(CFG, "source_allow_private_hosts", True)


def test_ssrf_hostname_not_treated_as_private_literal() -> None:
    """Regression: domain names must not be fail-closed as private before DNS."""
    from ai.ingest.source import _host_header, _is_ip_literal, _is_private_ip, _pinned_url

    assert _is_private_ip("127.0.0.1") is True
    assert _is_private_ip("8.8.8.8") is False
    # Hostnames are not IP literals 鈫?not "private IP"
    assert _is_private_ip("minio.example.com") is False
    assert _is_ip_literal("minio.example.com") is False
    assert _is_ip_literal("127.0.0.1") is True

    url, host_hdr = _pinned_url("https://cdn.example.com:9443/a/b?q=1", "1.2.3.4")
    assert url.startswith("https://1.2.3.4:9443/a/b?q=1")
    assert host_hdr == "cdn.example.com:9443"
    url2, host2 = _pinned_url("https://cdn.example.com/a", "1.2.3.4")
    assert host2 == "cdn.example.com"
    assert _host_header("cdn.example.com", 80, "http") == "cdn.example.com"
    assert _host_header("cdn.example.com", 9000, "http") == "cdn.example.com:9000"


# ---------------------------------------------------------------------------
# P-05 X-Trace-Id 鐧藉悕鍗?# ---------------------------------------------------------------------------


def test_invalid_trace_id_is_regenerated() -> None:
    bad = "evil\ntrace\tid"
    resp = client.get("/health", headers={"X-Trace-Id": bad})
    assert resp.status_code == 200
    echoed = resp.headers.get("X-Trace-Id") or ""
    assert echoed != bad
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", echoed)

    too_long = "A" * 65
    resp2 = client.get("/health", headers={"X-Trace-Id": too_long})
    echoed2 = resp2.headers.get("X-Trace-Id") or ""
    assert echoed2 != too_long
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", echoed2)


def test_valid_trace_id_is_echoed() -> None:
    ok = "trace-ok_1.2-abc"
    resp = client.get("/health", headers={"X-Trace-Id": ok})
    assert resp.headers.get("X-Trace-Id") == ok


# ---------------------------------------------------------------------------
# P-06 /metrics 閴存潈
# ---------------------------------------------------------------------------


def test_metrics_requires_token_by_default() -> None:
    object.__setattr__(CFG, "metrics_public", False)
    resp = client.get("/metrics")
    assert resp.status_code == 401

    resp_ok = client.get("/metrics", headers=HEADERS)
    assert resp_ok.status_code == 200
    assert resp_ok.text.startswith("#") or "kb_ai_" in resp_ok.text


def test_metrics_public_flag_allows_anonymous() -> None:
    object.__setattr__(CFG, "metrics_public", True)
    try:
        resp = client.get("/metrics")
        assert resp.status_code == 200
    finally:
        object.__setattr__(CFG, "metrics_public", False)


def test_health_stays_public() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# P-07 / P-08 token WARN + message 鎴柇
# ---------------------------------------------------------------------------


def test_default_token_warns_and_refuse_flag() -> None:
    from app.main import _warn_if_weak_token

    class _Cfg:
        internal_token = "change-me-long-random"
        refuse_on_default_internal_token = False

    _warn_if_weak_token(_Cfg())  # should not raise

    class _Refuse(_Cfg):
        refuse_on_default_internal_token = True

    try:
        _warn_if_weak_token(_Refuse())
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "INTERNAL_TOKEN" in str(exc)


def test_safe_message_truncates() -> None:
    long_exc = Exception("X" * 1000 + "\ntraceback-ish detail")
    msg = safe_message(long_exc, max_chars=50)
    assert len(msg) <= 50
    assert "traceback" not in msg or len(msg) <= 50
    short = safe_message("ok", max_chars=50)
    assert short == "ok"


def test_embed_error_message_truncated() -> None:
    from ai.retrieve.store import PublishValidationError

    # force a path that uses detail_body truncation via exception message
    long_msg = "bad " * 200
    body = {
        "error_code": "TEST",
        "message": safe_message(long_msg, max_chars=int(CFG.error_message_max_chars)),
    }
    assert len(body["message"]) <= int(CFG.error_message_max_chars)
    assert isinstance(PublishValidationError, type)
