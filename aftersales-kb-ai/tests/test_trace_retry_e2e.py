"""异步 parse trace_id 继承 + retry_call 全量覆盖 E2E 验证脚本.

约束（硬性）:
- 运行环境: Anaconda shopkeeper-ai
- 向量断言: 真实 Milvus（独立测试 collection，结束后清理）
- 任务态/日志: 真实 TaskStore + TraceIdFilter 捕获，不 mock 业务层
- 唯一业务输入文件: docs/demo-knowledge/sop/工单处理SOP.md
- 故障注入: 本地 HTTP 服务 + source_allow_private_hosts 测试旁路
- 关联性: 重试链路日志均校验 trace_id 连续一致

运行:
    cd aftersales-kb-ai
    D:/ProgramData/anaconda3/envs/shopkeeper-ai/python.exe -m pytest tests/test_trace_retry_e2e.py -v -s
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 环境初始化: 必须在导入 app.* 之前完成
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_SOURCE_PORT = _free_port()
_EMBED_PORT = _free_port()
_REPO_ROOT = Path(__file__).resolve().parents[2]
# 唯一测试业务输入文件（全场景复用）
TEST_INPUT_MD = _REPO_ROOT / "docs" / "demo-knowledge" / "sop" / "工单处理SOP.md"
assert TEST_INPUT_MD.is_file(), f"missing test input file: {TEST_INPUT_MD}"
TEST_MD_TEXT = TEST_INPUT_MD.read_text(encoding="utf-8")
TEST_FILE_NAME = TEST_INPUT_MD.name

# 独跑（pytest tests/test_trace_retry_e2e.py）时强制真实 Milvus + 专用 token；
# 全量 suite 导入时不得抢占 env，避免 INTERNAL_TOKEN/VECTOR_STORE 污染其他模块。
import sys  # noqa: E402

_SOLO = any("test_trace_retry_e2e" in a for a in sys.argv) and not any(
    a.replace("\\", "/").rstrip("/").endswith("tests") for a in sys.argv
)

if _SOLO:
    # 真实 Milvus + 独立测试 collection，避免污染业务 collection
    os.environ["VECTOR_STORE"] = "milvus"
    os.environ.setdefault("MILVUS_URI", "http://127.0.0.1:19530")
    os.environ["MILVUS_COLLECTION"] = "kb_ai_chunks_trace_retry_e2e"
    os.environ["EMBED_PROVIDER"] = "cloud"
    os.environ["EMBED_CLOUD_BASE"] = f"http://127.0.0.1:{_EMBED_PORT}"
    os.environ["EMBED_CLOUD_KEY"] = "e2e-test-key"
    os.environ["EMBED_CLOUD_MODEL"] = "e2e-fake-bge"
    os.environ["EMBED_DIM"] = "32"
    os.environ["INTERNAL_TOKEN"] = "e2e-trace-retry-token"
    os.environ["MINERU_ENABLED"] = "false"
    os.environ["PARSE_FAIL_ON_EMPTY"] = "true"
    # 允许 127.0.0.1 故障注入服务（生产默认 false）
    os.environ["SOURCE_ALLOW_PRIVATE_HOSTS"] = "true"
    # 收紧重试退避，加速测试
    os.environ.setdefault("SOURCE_CONNECT_TIMEOUT_SEC", "2")
    os.environ.setdefault("SOURCE_READ_TIMEOUT_SEC", "5")
else:
    # suite 模式: 仅收紧超时，不抢 token/store；私网旁路只在 setup_module 生效
    os.environ.setdefault("SOURCE_CONNECT_TIMEOUT_SEC", "2")
    os.environ.setdefault("SOURCE_READ_TIMEOUT_SEC", "5")

from fastapi.testclient import TestClient  # noqa: E402

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.deps import (  # noqa: E402
    get_embedder,
    get_llm,
    get_mineru,
    get_vector_store,
    get_vlm,
)

get_embedder.cache_clear()
get_vector_store.cache_clear()
get_mineru.cache_clear()
get_vlm.cache_clear()
get_llm.cache_clear()

from app.main import app  # noqa: E402
from ai.ingest.task_store import get_task_store, reset_task_store_for_tests  # noqa: E402
from core.retry import RetryPolicy, retry_call  # noqa: E402
from core.trace import TraceIdFilter, get_trace_id, set_trace_id  # noqa: E402

HEADERS = {"X-Internal-Token": os.environ.get("INTERNAL_TOKEN", "test-token")}
CFG = get_settings()
DIM = int(os.environ.get("EMBED_DIM", "32"))

# ---------------------------------------------------------------------------
# 日志捕获: 真实 handler + TraceIdFilter，记录 (message, trace_id, thread)
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self.records.append(record)

    def snapshot(self) -> list[logging.LogRecord]:
        with self._lock:
            return list(self.records)

    def clear(self) -> None:
        with self._lock:
            self.records.clear()

    def for_trace(self, tid: str) -> list[logging.LogRecord]:
        return [
            r
            for r in self.snapshot()
            if getattr(r, "trace_id", None) == tid
        ]

    def messages_for_trace(self, tid: str, substr: str = "") -> list[str]:
        out = []
        for r in self.for_trace(tid):
            msg = r.getMessage()
            if substr in msg:
                out.append(msg)
        return out


_LOG_CAPTURE = _CapturingHandler()
_LOG_CAPTURE.addFilter(TraceIdFilter())


def _install_log_capture() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if _LOG_CAPTURE not in root.handlers:
        root.addHandler(_LOG_CAPTURE)
    for h in root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in h.filters):
            h.addFilter(TraceIdFilter())


def _uninstall_log_capture() -> None:
    root = logging.getLogger()
    if _LOG_CAPTURE in root.handlers:
        root.removeHandler(_LOG_CAPTURE)


# ---------------------------------------------------------------------------
# 本地 HTTP 故障注入服务（非 Mock 数据层，真实 socket HTTP）
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    """fail_times 次 500，之后 200 返回 markdown 或 embeddings JSON。"""

    lock = threading.Lock()
    # mode: "md" | "embed" | "always_500"
    mode: str = "md"
    fail_times: int = 0
    hits: int = 0
    body_payload: bytes = b""
    _last_post_body: bytes = b""

    def log_message(self, fmt: str, *args: Any) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        with type(self).lock:
            type(self).hits += 1
            n = type(self).hits
            mode = type(self).mode
            fail_times = type(self).fail_times
            payload = type(self).body_payload
        if mode == "always_500" or n <= fail_times:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
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
        with type(self).lock:
            type(self).hits += 1
            n = type(self).hits
            mode = type(self).mode
            fail_times = type(self).fail_times
        if mode == "always_500" or n <= fail_times:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"injected"}')
            return
        if mode == "embed":
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
            return
        self.send_response(200)
        self.end_headers()


def _start_server(port: int, handler: type[_ScriptedHandler]) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class _Servers:
    def __init__(self) -> None:
        self.source: ThreadingHTTPServer | None = None
        self.embed: ThreadingHTTPServer | None = None

    def start(self) -> None:
        _ScriptedHandler.mode = "md"
        _ScriptedHandler.fail_times = 0
        _ScriptedHandler.hits = 0
        _ScriptedHandler.body_payload = TEST_MD_TEXT.encode("utf-8")
        self.source = _start_server(_SOURCE_PORT, _ScriptedHandler)

        class _EmbedHandler(_ScriptedHandler):
            pass

        # 独立 handler 类，避免 source/embed 计数互相干扰
        _EmbedHandler.mode = "embed"
        _EmbedHandler.fail_times = 0
        _EmbedHandler.hits = 0
        _EmbedHandler.body_payload = b""
        self.embed = _start_server(_EMBED_PORT, _EmbedHandler)
        self._embed_handler = _EmbedHandler

    def reset_source(self, *, mode: str = "md", fail_times: int = 0) -> None:
        with _ScriptedHandler.lock:
            _ScriptedHandler.mode = mode
            _ScriptedHandler.fail_times = fail_times
            _ScriptedHandler.hits = 0
            _ScriptedHandler.body_payload = TEST_MD_TEXT.encode("utf-8")

    def reset_embed(self, *, mode: str = "embed", fail_times: int = 0) -> None:
        h = self._embed_handler
        with h.lock:
            h.mode = mode
            h.fail_times = fail_times
            h.hits = 0

    def source_hits(self) -> int:
        with _ScriptedHandler.lock:
            return _ScriptedHandler.hits

    def embed_hits(self) -> int:
        with self._embed_handler.lock:
            return self._embed_handler.hits

    def stop(self) -> None:
        for srv in (self.source, self.embed):
            if srv is not None:
                srv.shutdown()
                srv.server_close()


SERVERS = _Servers()
CLIENT: TestClient | None = None
_TRACKED_UNITS: set[str] = set()


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------


def _new_unit_id(prefix: str) -> str:
    uid = f"{prefix}-{uuid.uuid4().hex[:10]}"
    _TRACKED_UNITS.add(uid)
    return uid


def _headers(trace_id: str | None = None) -> dict[str, str]:
    h = dict(HEADERS)
    if trace_id:
        h["X-Trace-Id"] = trace_id
    return h


def _poll_task(task_id: str, *, timeout: float = 30.0, trace_id: str | None = None) -> dict:
    assert CLIENT is not None
    deadline = time.time() + timeout
    body: dict | None = None
    while time.time() < deadline:
        resp = CLIENT.get(f"/internal/tasks/{task_id}", headers=_headers(trace_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in {"done", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} not terminal: {body}")


def _print_point(name: str, ok: bool, **info: Any) -> None:
    status = "PASS" if ok else "FAIL"
    extra = " ".join(f"{k}={v}" for k, v in info.items())
    print(f"  [{status}] {name} {extra}".rstrip())


def _assert_point(name: str, ok: bool, **info: Any) -> None:
    _print_point(name, ok, **info)
    assert ok, name


# ---------------------------------------------------------------------------
# 环境初始化 / 清理
# ---------------------------------------------------------------------------


_SAVED_ENV: dict[str, str | None] = {}
_ENV_KEYS = (
    "EMBED_PROVIDER",
    "EMBED_CLOUD_BASE",
    "EMBED_CLOUD_KEY",
    "EMBED_CLOUD_MODEL",
    "EMBED_DIM",
    "VECTOR_STORE",
    "SOURCE_ALLOW_PRIVATE_HOSTS",
    "INTERNAL_TOKEN",
)


def setup_module() -> None:
    """pytest module setup: 起服务、装日志、清任务态。"""
    global CLIENT, HEADERS, CFG, DIM, _SAVED_ENV
    _SAVED_ENV = {k: os.environ.get(k) for k in _ENV_KEYS}
    # 本模块需要 cloud embed 指向本地故障注入端口（强制，teardown 恢复）
    os.environ["EMBED_PROVIDER"] = "cloud"
    os.environ["EMBED_CLOUD_BASE"] = f"http://127.0.0.1:{_EMBED_PORT}"
    os.environ["EMBED_CLOUD_KEY"] = "e2e-test-key"
    os.environ["EMBED_CLOUD_MODEL"] = "e2e-fake-bge"
    os.environ["SOURCE_ALLOW_PRIVATE_HOSTS"] = "true"
    HEADERS = {"X-Internal-Token": os.environ.get("INTERNAL_TOKEN", "test-token")}
    get_settings.cache_clear()
    get_embedder.cache_clear()
    get_vector_store.cache_clear()
    get_mineru.cache_clear()
    get_vlm.cache_clear()
    get_llm.cache_clear()
    CFG = get_settings()
    DIM = int(os.environ.get("EMBED_DIM", CFG.embed_dim or 32))
    _install_log_capture()
    SERVERS.start()
    reset_task_store_for_tests()
    CLIENT = TestClient(app)
    # 触发 collection ensure（memory/milvus 均可）
    try:
        get_vector_store().ensure_collection()
    except Exception as exc:  # noqa: BLE001
        print(f"  vector store ensure skipped/failed: {exc}")
    print("\n=== E2E setup done ===")
    print(f"  test_input={TEST_INPUT_MD}")
    print(f"  vector_store={CFG.vector_store} milvus={CFG.milvus_uri} collection={CFG.milvus_collection}")
    print(f"  source_server=127.0.0.1:{_SOURCE_PORT} embed_server=127.0.0.1:{_EMBED_PORT}")
    print(f"  source_allow_private_hosts={CFG.source_allow_private_hosts}")
    print(f"  internal_token={HEADERS['X-Internal-Token'][:8]}…")


def teardown_module() -> None:
    """清理任务数据、向量、日志 handler、HTTP 服务，并恢复 suite 环境。"""
    global _SAVED_ENV
    print("\n=== E2E teardown: cleanup ===")
    try:
        store = get_vector_store()
        deleted = 0
        for uid in list(_TRACKED_UNITS):
            try:
                n = store.delete_by_unit(uid)
                deleted += n
                print(f"  deleted unit={uid} count={n}")
            except Exception as exc:  # noqa: BLE001
                print(f"  delete failed unit={uid}: {exc}")
        print(f"  cleanup done deleted_vectors={deleted}")
    except Exception as exc:  # noqa: BLE001
        print(f"  vector cleanup skipped: {exc}")
    _TRACKED_UNITS.clear()
    reset_task_store_for_tests()
    _LOG_CAPTURE.clear()
    _uninstall_log_capture()
    SERVERS.stop()
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    get_embedder.cache_clear()
    get_vector_store.cache_clear()
    get_mineru.cache_clear()
    get_vlm.cache_clear()
    get_llm.cache_clear()
    _SAVED_ENV = {}
    print("  suite env restored")


# ---------------------------------------------------------------------------
# 场景 1: 异步 parse 链路 trace_id 继承
# ---------------------------------------------------------------------------


def test_s1_async_parse_worker_inherits_trace_id():
    """场景1: 异步任务全阶段日志携带与请求侧一致的 trace_id。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s1")
    SERVERS.reset_source(mode="md", fail_times=0)

    init = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid),
        data={"file_type": "md", "unit_id": unit_id, "markdown": TEST_MD_TEXT},
    )
    _assert_point(
        "S1.1 异步提交接受",
        init.status_code == 200,
        status_code=init.status_code,
        trace_id=tid,
        unit_id=unit_id,
    )
    resp_trace = init.headers.get("X-Trace-Id")
    _assert_point("S1.2 响应回显 trace_id", resp_trace == tid, echo=resp_trace)

    body = _poll_task(init.json()["task_id"], trace_id=tid)
    _assert_point(
        "S1.3 任务完成",
        body["status"] == "done",
        task_status=body["status"],
        task_id=body["task_id"],
        chunks=len((body.get("result") or {}).get("chunks") or []),
    )

    starts = _LOG_CAPTURE.messages_for_trace(tid, "async parse start")
    dones = _LOG_CAPTURE.messages_for_trace(tid, "async parse done")
    _assert_point(
        "S1.4 worker start/done 日志携带请求 trace_id",
        len(starts) >= 1 and len(dones) >= 1,
        start_logs=len(starts),
        done_logs=len(dones),
        trace_id=tid,
    )

    # 反证: 该 trace 的日志不应出现另一个 tid 前缀串扰（同批仅一个任务时用 "-" 默认值排除）
    foreign = [
        r
        for r in _LOG_CAPTURE.snapshot()
        if "async parse" in r.getMessage()
        and getattr(r, "trace_id", None) not in {tid, "-", None}
    ]
    _assert_point(
        "S1.5 无其它 trace 串扰 start/done",
        len(foreign) == 0,
        foreign=len(foreign),
    )


def test_s1_concurrent_workers_trace_isolation():
    """场景1: 多 worker 并发下不同任务 trace_id 互不串扰。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid_a = "trace-" + uuid.uuid4().hex[:12]
    tid_b = "trace-" + uuid.uuid4().hex[:12]
    unit_a = _new_unit_id("U-s1a")
    unit_b = _new_unit_id("U-s1b")

    ra = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid_a),
        data={"file_type": "md", "unit_id": unit_a, "markdown": TEST_MD_TEXT},
    )
    rb = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid_b),
        data={"file_type": "md", "unit_id": unit_b, "markdown": TEST_MD_TEXT},
    )
    assert ra.status_code == 200 and rb.status_code == 200
    body_a = _poll_task(ra.json()["task_id"], timeout=40)
    body_b = _poll_task(rb.json()["task_id"], timeout=40)
    _assert_point(
        "S1.6 并发任务均完成",
        body_a["status"] == "done" and body_b["status"] == "done",
        a=body_a["status"],
        b=body_b["status"],
    )

    a_msgs = _LOG_CAPTURE.messages_for_trace(tid_a, "async parse")
    b_msgs = _LOG_CAPTURE.messages_for_trace(tid_b, "async parse")
    _assert_point(
        "S1.7 任务A链路完整",
        any("start" in m for m in a_msgs) and any("done" in m for m in a_msgs),
        trace_id=tid_a,
        logs=len(a_msgs),
    )
    _assert_point(
        "S1.8 任务B链路完整",
        any("start" in m for m in b_msgs) and any("done" in m for m in b_msgs),
        trace_id=tid_b,
        logs=len(b_msgs),
    )

    # 串扰: A 的日志不应记录 B 的任务 id，反之亦然
    a_task = ra.json()["task_id"]
    b_task = rb.json()["task_id"]
    cross_ab = [m for m in a_msgs if b_task in m]
    cross_ba = [m for m in b_msgs if a_task in m]
    _assert_point(
        "S1.9 并发 trace/task 不串扰",
        len(cross_ab) == 0 and len(cross_ba) == 0,
        cross_ab=len(cross_ab),
        cross_ba=len(cross_ba),
        tid_a=tid_a,
        tid_b=tid_b,
    )


def test_s1_parse_failure_keeps_trace_id():
    """场景1: 解析异常时错误日志仍携带对应任务的 trace_id。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s1err")

    init = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid),
        data={"file_type": "md", "unit_id": unit_id, "markdown": "   \n\n  "},
    )
    assert init.status_code == 200, init.text
    body = _poll_task(init.json()["task_id"], trace_id=tid)
    _assert_point(
        "S1.10 空内容任务失败",
        body["status"] == "failed",
        status=body["status"],
        error_code=body.get("error_code"),
    )
    fail_logs = _LOG_CAPTURE.messages_for_trace(tid, "async parse failed")
    start_logs = _LOG_CAPTURE.messages_for_trace(tid, "async parse start")
    _assert_point(
        "S1.11 失败日志携带 trace_id",
        len(fail_logs) >= 1 and len(start_logs) >= 1,
        fail_logs=len(fail_logs),
        start_logs=len(start_logs),
        trace_id=tid,
    )


# ---------------------------------------------------------------------------
# 场景 2.1: source_fetch 重试
# ---------------------------------------------------------------------------


def test_s21_source_fetch_retries_then_succeeds():
    """场景2.1: 源文件 5xx 后自动重试，成功下载并完成解析；重试日志携带 trace_id。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s21ok")
    SERVERS.reset_source(mode="md", fail_times=2)

    url = f"http://127.0.0.1:{_SOURCE_PORT}/{TEST_FILE_NAME}"
    init = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid),
        data={
            "file_type": "md",
            "unit_id": unit_id,
            "source_url": url,
            "source_name": TEST_FILE_NAME,
        },
    )
    _assert_point("S21.1 提交 source_url 任务", init.status_code == 200, status_code=init.status_code)
    body = _poll_task(init.json()["task_id"], timeout=45, trace_id=tid)
    hits = SERVERS.source_hits()
    _assert_point(
        "S21.2 失败2次后成功完成任务",
        body["status"] == "done",
        task_status=body["status"],
        http_hits=hits,
        chunks=len((body.get("result") or {}).get("chunks") or []),
    )
    _assert_point(
        "S21.3 HTTP 调用次数符合重试(至少3次)",
        hits >= 3,
        hits=hits,
    )
    retry_logs = _LOG_CAPTURE.messages_for_trace(tid, "retryable failure")
    _assert_point(
        "S21.4 重试日志携带 trace_id",
        len(retry_logs) >= 2,
        retry_logs=len(retry_logs),
        trace_id=tid,
        sample=(retry_logs[0] if retry_logs else ""),
    )


def test_s21_source_fetch_exhausts_retries():
    """场景2.1: 达到最大重试后抛出最终异常，任务标记 failed。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s21fail")
    SERVERS.reset_source(mode="always_500", fail_times=0)

    url = f"http://127.0.0.1:{_SOURCE_PORT}/always-fail.md"
    init = CLIENT.post(
        "/internal/parse/async",
        headers=_headers(tid),
        data={
            "file_type": "md",
            "unit_id": unit_id,
            "source_url": url,
            "source_name": "always-fail.md",
        },
    )
    assert init.status_code == 200, init.text
    body = _poll_task(init.json()["task_id"], timeout=45, trace_id=tid)
    hits = SERVERS.source_hits()
    retry_logs = _LOG_CAPTURE.messages_for_trace(tid, "retryable failure")
    fail_logs = _LOG_CAPTURE.messages_for_trace(tid, "async parse failed")
    _assert_point(
        "S21.5 重试耗尽后任务 failed",
        body["status"] == "failed",
        status=body["status"],
        error_code=body.get("error_code"),
        http_hits=hits,
    )
    _assert_point(
        "S21.6 默认策略最多约 max_attempts 次",
        1 <= hits <= 3,
        hits=hits,
        expected_max_attempts=3,
    )
    _assert_point(
        "S21.7 重试与失败日志均带 trace_id",
        len(retry_logs) >= 1 and len(fail_logs) >= 1,
        retry_logs=len(retry_logs),
        fail_logs=len(fail_logs),
        trace_id=tid,
    )


# ---------------------------------------------------------------------------
# 场景 2.2: embedder 重试 + 真实 Milvus
# ---------------------------------------------------------------------------


def test_s22_embedder_retries_then_publishes_to_milvus():
    """场景2.2: embedder 5xx 后重试成功，向量写入真实 Milvus；日志带 trace_id。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s22")
    SERVERS.reset_embed(mode="embed", fail_times=2)

    set_trace_id(tid)
    try:
        # 使用唯一测试文件的解析内容作为向量化输入
        parse = CLIENT.post(
            "/internal/parse",
            headers=_headers(tid),
            data={"file_type": "md", "unit_id": unit_id, "markdown": TEST_MD_TEXT},
        )
        assert parse.status_code == 200, parse.text
        chunks = parse.json()["chunks"]
        assert chunks, "parse produced no chunks"

        embed_body = {
            "unit_id": unit_id,
            "chunks": [
                {
                    "chunk_index": c["chunk_index"],
                    "content": c["content"],
                    "heading_path": c["heading_path"],
                }
                for c in chunks
            ],
        }
        embed_resp = CLIENT.post("/internal/embed", headers=_headers(tid), json=embed_body)
        hits = SERVERS.embed_hits()
        _assert_point(
            "S22.1 embed 接口成功",
            embed_resp.status_code == 200,
            status_code=embed_resp.status_code,
            body=embed_resp.text[:200],
            http_hits=hits,
        )
        _assert_point(
            "S22.2 embedder 至少触发重试(总调用>=3)",
            hits >= 3,
            hits=hits,
            embedded_count=embed_resp.json().get("embedded_count"),
        )

        # 真实 Milvus 校验
        store = get_vector_store()
        vec = [0.0] * DIM
        vec[0] = 1.0
        hits_search = store.search(vec, top_k=50)
        unit_hits = [h for h in hits_search if str(h.get("unit_id")) == unit_id]
        _assert_point(
            "S22.3 真实 Milvus 可检索到测试 unit",
            len(unit_hits) >= 1,
            unit_id=unit_id,
            hits=len(unit_hits),
            collection=CFG.milvus_collection,
        )

        # 任务态真路径: embed 本身无 task，但 trace 仍应出现在 embed/retry 相关日志
        # retry_call 的 warning 在请求上下文中发出，应带 tid
        retry_logs = _LOG_CAPTURE.messages_for_trace(tid, "retryable failure")
        _assert_point(
            "S22.4 embed 重试日志携带 trace_id",
            len(retry_logs) >= 2,
            retry_logs=len(retry_logs),
            trace_id=tid,
            sample=(retry_logs[0] if retry_logs else ""),
        )
    finally:
        set_trace_id("-")


def test_s22_embedder_exhausts_retries_fails_request():
    """场景2.2: embedder 持续 5xx 时重试耗尽，接口返回失败且日志有 trace_id。"""
    assert CLIENT is not None
    _LOG_CAPTURE.clear()
    tid = uuid.uuid4().hex
    unit_id = _new_unit_id("U-s22fail")
    SERVERS.reset_embed(mode="always_500", fail_times=0)

    set_trace_id(tid)
    try:
        payload = {
            "unit_id": unit_id,
            "chunks": [{"chunk_index": 0, "content": "测试向量重试耗尽", "heading_path": ["t"]}],
        }
        resp = CLIENT.post("/internal/embed", headers=_headers(tid), json=payload)
        hits = SERVERS.embed_hits()
        retry_logs = _LOG_CAPTURE.messages_for_trace(tid, "retryable failure")
        _assert_point(
            "S22.5 持续 5xx 时 embed 失败",
            resp.status_code >= 400,
            status_code=resp.status_code,
            http_hits=hits,
        )
        _assert_point(
            "S22.6 失败路径重试日志带 trace_id",
            len(retry_logs) >= 1,
            retry_logs=len(retry_logs),
            trace_id=tid,
            hits=hits,
        )
    finally:
        set_trace_id("-")


# ---------------------------------------------------------------------------
# 直接单元级联动: retry_call + trace 上下文（关联性约束）
# ---------------------------------------------------------------------------


def test_s2_retry_call_preserves_trace_context():
    """关联性: retry 休眠前后 get_trace_id 保持不变。"""
    tid = uuid.uuid4().hex
    set_trace_id(tid)
    sleeps: list[float] = []
    calls = {"n": 0}

    class _Tmp(Exception):
        error_code = "SOURCE_FETCH_FAILED"

    def flaky() -> str:
        calls["n"] += 1
        assert get_trace_id() == tid
        if calls["n"] < 3:
            raise _Tmp("temp down")
        return "ok"

    try:
        out = retry_call(
            flaky,
            policy=RetryPolicy(max_attempts=3, base_delay_sec=0.01),
            sleep=sleeps.append,
        )
        _assert_point(
            "S2.0 retry 过程 trace_id 连续一致",
            out == "ok" and get_trace_id() == tid,
            result=out,
            attempts=calls["n"],
            sleeps=len(sleeps),
            trace_id=tid,
        )
    finally:
        set_trace_id("-")
