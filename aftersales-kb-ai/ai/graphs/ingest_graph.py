"""LangGraph ingest graph: resolve → extract → vlm → chunk → quality."""

from __future__ import annotations

import logging
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from core.config import Settings
from ai.graphs.state import IngestState
from ai.ingest.chunker import split_markdown
from core.circuit import get_circuits, get_metrics
from ai.ingest.docx_parser import docx_to_markdown
from core.limits import get_limits
from ai.ingest.mineru_client import MinerUClient, MinerUError
from ai.ingest.quality import build_quality
from ai.ingest.source import SourceFetchError, fetch_source_to_temp
from ai.generate.vlm import VlmClient, VlmError, guess_mime, inject_captions_into_markdown
from core.trace import get_trace_id, set_trace_id
from ai.generate.vlm_cache import get_caption_cache

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]
CancelFn = Callable[[], bool]

_CATEGORY_RE = re.compile(r"^\*{0,2}(?:分类|category)\s*[:：]\*{0,2}\s*(\S+)", re.I)
_PERMISSION_RE = re.compile(r"^\*{0,2}(?:数据权限|权限|permission)\s*[:：]\*{0,2}\s*(.+)$", re.I)


class IngestGraphError(Exception):
    def __init__(self, message: str, error_code: str = "PARSE_FAILED", status: int = 500) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


def _noop_progress(stage: str, progress: int, detail: str) -> None:
    return


def _safe_upload_name(filename: str | None, fallback: str) -> str:
    name = Path(filename or fallback).name
    name = name.replace("\\", "_").replace("/", "_").strip() or fallback
    return name[:120]


def extract_classify_hints(markdown: str) -> tuple[str | None, str | None]:
    """Heuristic category/permission from front-matter-like meta lines. Java decides persist."""
    category: str | None = None
    permission: str | None = None
    for line in (markdown or "").splitlines()[:40]:
        stripped = line.strip()
        if category is None:
            m = _CATEGORY_RE.match(stripped)
            if m:
                category = m.group(1).strip()[:64]
        if permission is None:
            m = _PERMISSION_RE.match(stripped)
            if m:
                permission = m.group(1).strip()[:200]
        if category and permission:
            break
    return category, permission


def _images_from_markdown(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", text or ""):
        path = m.group(2).strip()
        if path in seen or path.startswith("http"):
            continue
        seen.add(path)
        out.append(
            {
                "image_id": Path(path).stem or path[-32:],
                "relative_path": path,
                "status": "skipped",
                "caption": None,
            }
        )
    return out


def build_ingest_graph(
    *,
    cfg: Settings,
    mineru: MinerUClient,
    vlm: VlmClient,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
):
    """Compile LangGraph for document ingest. Services injected for testability."""
    on_progress = progress or _noop_progress
    cancel_check = should_cancel

    def _check_cancel() -> None:
        if cancel_check and cancel_check():
            raise IngestGraphError("task cancelled", "TASK_CANCELLED", 499)

    def _merge_timings(state: IngestState, key: str, ms: float) -> dict[str, float]:
        timings = dict(state.get("stage_timings") or {})
        timings[key] = round(float(ms), 3)
        return timings

    def resolve_source(state: IngestState) -> IngestState:
        _check_cancel()
        on_progress("fetch", 5, "resolving source")
        return {"stage": "fetch"}

    def extract_text(state: IngestState) -> IngestState:
        ft = state["file_type"]
        unit_id = state["unit_id"]
        _check_cancel()
        on_progress("parse", 15, f"extract {ft}")
        extract_t0 = time.monotonic()
        text = ""
        page_count: int | None = None
        image_bytes_map: dict[str, bytes] = {}
        images: list[dict] = []

        try:
            if ft in {"md", "txt"}:
                md = state.get("markdown")
                raw = state.get("file_bytes")
                fpath = state.get("file_path")
                url = state.get("source_url")
                if md is not None:
                    # blank markdown is a valid source input; empty handled by hard-fail gate
                    text = md if md.strip() else ""
                elif raw is not None:
                    text = raw.decode("utf-8", errors="replace")
                elif fpath and Path(fpath).exists():
                    text = Path(fpath).read_text(encoding="utf-8", errors="replace")
                elif url:
                    with tempfile.TemporaryDirectory(prefix="kb_src_") as tmp:
                        path = fetch_source_to_temp(
                            url, Path(tmp), filename=state.get("source_name"), cfg=cfg
                        )
                        text = path.read_text(encoding="utf-8", errors="replace")
                else:
                    raise IngestGraphError("file, markdown, or source_url is required", "BAD_SOURCE", 400)
                images = _images_from_markdown(text)

            elif ft == "docx":
                raw = state.get("file_bytes")
                fpath = state.get("file_path")
                if raw is None and fpath and Path(fpath).exists():
                    raw = Path(fpath).read_bytes()
                if raw is None and state.get("source_url"):
                    with tempfile.TemporaryDirectory(prefix="kb_src_") as tmp:
                        path = fetch_source_to_temp(
                            state["source_url"] or "",
                            Path(tmp),
                            filename=state.get("source_name") or "upload.docx",
                            cfg=cfg,
                        )
                        raw = path.read_bytes()
                if raw is None:
                    raise IngestGraphError("file or source_url is required for docx", "BAD_SOURCE", 400)
                with tempfile.TemporaryDirectory(prefix="kb_docx_") as tmp:
                    path = Path(tmp) / _safe_upload_name(
                        state.get("file_name") or state.get("source_name"), "upload.docx"
                    )
                    path.write_bytes(raw)
                    text = docx_to_markdown(path)
                images = _images_from_markdown(text)

            elif ft == "pdf":
                raw = state.get("file_bytes")
                fname = state.get("file_name")
                fpath = state.get("file_path")
                if raw is None and fpath and Path(fpath).exists():
                    # Prefer on-disk path for large uploads (avoid full re-read when possible).
                    on_progress("parse", 20, "mineru extract")
                    circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
                    if circuits.is_open("mineru"):
                        raise IngestGraphError("MinerU circuit open", "MINERU_CIRCUIT_OPEN", 503)
                    md, page_count, raw_images = mineru.pdf_to_markdown(
                        Path(fpath),
                        on_progress=on_progress,
                        should_cancel=cancel_check,
                    )
                    text = md
                    images = []
                    for i, img in enumerate(raw_images):
                        rel = str(img.get("relative_path") or "")
                        blob = img.get("bytes")
                        if isinstance(blob, (bytes, bytearray)) and blob and rel:
                            image_bytes_map[rel] = bytes(blob)
                        images.append(
                            {
                                "image_id": str(img.get("image_id") or f"img_{i}"),
                                "relative_path": rel,
                                "status": "skipped",
                                "caption": None,
                            }
                        )
                    for extra in _images_from_markdown(text):
                        if all(extra["relative_path"] != x["relative_path"] for x in images):
                            images.append(extra)
                    on_progress("parse", 45, f"text_len={len(text)} images={len(images)}")
                    return {
                        "text": text,
                        "page_count": page_count,
                        "image_bytes_map": image_bytes_map,
                        "images": images,
                        "stage": "parse",
                        "stage_timings": _merge_timings(
                            state, "extract_ms", (time.monotonic() - extract_t0) * 1000
                        ),
                    }
                if raw is None and state.get("source_url"):
                    with tempfile.TemporaryDirectory(prefix="kb_src_") as tmp:
                        path = fetch_source_to_temp(
                            state["source_url"] or "",
                            Path(tmp),
                            filename=state.get("source_name") or "upload.pdf",
                            cfg=cfg,
                        )
                        raw = path.read_bytes()
                        fname = path.name
                if raw is None:
                    raise IngestGraphError("file or source_url is required for pdf", "BAD_SOURCE", 400)
                if not raw:
                    raise IngestGraphError("empty pdf upload", "EMPTY_PDF", 400)
                on_progress("parse", 20, "mineru extract")
                circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
                if circuits.is_open("mineru"):
                    raise IngestGraphError("MinerU circuit open", "MINERU_CIRCUIT_OPEN", 503)
                with tempfile.TemporaryDirectory(prefix="kb_pdf_") as tmp:
                    path = Path(tmp) / _safe_upload_name(fname, "upload.pdf")
                    path.write_bytes(raw)
                    md, page_count, raw_images = mineru.pdf_to_markdown(
                        path,
                        on_progress=on_progress,
                        should_cancel=cancel_check,
                    )
                text = md
                images = []
                for i, img in enumerate(raw_images):
                    rel = str(img.get("relative_path") or "")
                    blob = img.get("bytes")
                    if isinstance(blob, (bytes, bytearray)) and blob and rel:
                        image_bytes_map[rel] = bytes(blob)
                    images.append(
                        {
                            "image_id": str(img.get("image_id") or f"img_{i}"),
                            "relative_path": rel,
                            "status": "skipped",
                            "caption": None,
                        }
                    )
                for extra in _images_from_markdown(text):
                    if all(extra["relative_path"] != x["relative_path"] for x in images):
                        images.append(extra)
            else:
                raise IngestGraphError(f"unsupported file_type: {ft}", "BAD_TYPE", 400)
        except IngestGraphError:
            raise
        except SourceFetchError as exc:
            raise IngestGraphError(str(exc), exc.error_code, 502) from exc
        except MinerUError as exc:
            status = 499 if exc.error_code == "TASK_CANCELLED" else 502
            raise IngestGraphError(str(exc), exc.error_code, status) from exc
        except Exception as exc:
            logger.exception("ingest extract failed unit_id=%s", unit_id)
            raise IngestGraphError(str(exc), "PARSE_FAILED", 500) from exc

        on_progress("parse", 45, f"text_len={len(text)} images={len(images)}")
        return {
            "text": text,
            "page_count": page_count,
            "image_bytes_map": image_bytes_map,
            "images": images,
            "stage": "parse",
            "stage_timings": _merge_timings(
                state, "extract_ms", (time.monotonic() - extract_t0) * 1000
            ),
        }

    def vlm_captions(state: IngestState) -> IngestState:
        use_vlm = bool(state.get("use_vlm"))
        image_bytes_map = state.get("image_bytes_map") or {}
        images = list(state.get("images") or [])
        text = state.get("text") or ""
        vlm_t0 = time.monotonic()
        if not use_vlm or not vlm.available or not image_bytes_map:
            on_progress("vlm", 50, "skip vlm")
            return {
                "images": images,
                "text": text,
                "stage": "vlm",
                "stage_timings": _merge_timings(state, "vlm_ms", 0.0),
            }

        circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
        metrics = get_metrics()
        if circuits.is_open("vlm"):
            metrics.inc("vlm.circuit_open")
            on_progress("vlm", 50, "vlm circuit open, skip")
            return {
                "images": images,
                "text": text,
                "stage": "vlm",
                "stage_timings": _merge_timings(state, "vlm_ms", 0.0),
            }

        _check_cancel()
        on_progress("vlm", 50, "describing images")
        limits = get_limits()
        captions: dict[str, str] = {}
        todo: list[dict] = []
        for img in images:
            rel = img.get("relative_path") or ""
            if rel not in image_bytes_map:
                continue
            if len(todo) >= cfg.max_vlm_images:
                img["status"] = "skipped"
                continue
            todo.append(img)

        # Capture request-scoped trace before pool submit; worker threads do not inherit contextvars.
        parent_trace_id = get_trace_id()

        def _one(img: dict) -> tuple[dict, str | None, str | None]:
            if parent_trace_id and parent_trace_id != "-":
                set_trace_id(parent_trace_id)
            rel = img["relative_path"]
            blob = image_bytes_map[rel]
            cache = get_caption_cache(cfg.vlm_caption_cache_max)
            cached = cache.get(blob)
            if cached:
                return img, cached, None
            try:
                with limits.vlm_slot():
                    cap = vlm.describe_image(blob, mime=guess_mime(rel))
                cache.put(blob, cap)
                circuits.record_success("vlm")
                metrics.inc("vlm.ok")
                return img, cap, None
            except VlmError as exc:
                circuits.record_failure("vlm", str(exc))
                metrics.inc("vlm.fail")
                return img, None, str(exc)

        workers = max(1, min(cfg.vlm_max_concurrency, len(todo) or 1))
        done_n = 0
        if todo:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vlm-cap") as pool:
                futs = [pool.submit(_one, img) for img in todo]
                for fut in as_completed(futs):
                    img, cap, err = fut.result()
                    done_n += 1
                    on_progress("vlm", 50 + int(30 * done_n / max(1, len(todo))), f"vlm {done_n}/{len(todo)}")
                    if cap:
                        img["status"] = "described"
                        img["caption"] = cap
                        captions[img["relative_path"]] = cap
                    else:
                        logger.warning("vlm failed path=%s: %s", img.get("relative_path"), err)
                        img["status"] = "failed"
                        img["caption"] = None
        if captions:
            text = inject_captions_into_markdown(text, captions)
        return {
            "images": images,
            "text": text,
            "stage": "vlm",
            "stage_timings": _merge_timings(
                state, "vlm_ms", (time.monotonic() - vlm_t0) * 1000
            ),
        }

    def chunk_and_quality(state: IngestState) -> IngestState:
        _check_cancel()
        on_progress("chunk", 85, "splitting")
        chunk_t0 = time.monotonic()
        text = state.get("text") or ""
        images = state.get("images") or []
        page_count = state.get("page_count")
        ft = state["file_type"]
        # Hard-fail empty parse results: weak suggest_review must not mask empty content.
        if cfg.parse_fail_on_empty and not (text or "").strip():
            raise IngestGraphError(
                "parse produced empty markdown",
                "PARSE_EMPTY_RESULT",
                400,
            )
        chunk_objs = split_markdown(
            text,
            chunk_size=state.get("chunk_size") or cfg.chunk_size,
            chunk_overlap=state.get("chunk_overlap") if state.get("chunk_overlap") is not None else cfg.chunk_overlap,
        )
        chunks = [
            {"chunk_index": c.chunk_index, "content": c.content, "heading_path": c.heading_path}
            for c in chunk_objs
        ]
        if cfg.parse_fail_on_empty:
            non_empty = [c for c in chunks if (c.get("content") or "").strip()]
            if not non_empty:
                raise IngestGraphError(
                    "parse produced no non-empty chunks",
                    "PARSE_EMPTY_RESULT",
                    400,
                )
        md_img_count = len(_images_from_markdown(text))
        q = build_quality(
            markdown=text,
            chunks=[(c["content"], c["heading_path"]) for c in chunks],
            image_count=len(images),
            image_in_md_count=md_img_count,
            page_count=page_count,
            file_type=ft,
            cfg=cfg,
        )
        suggested_category, permission_hint = extract_classify_hints(text)
        on_progress("done", 100, f"chunks={len(chunks)}")
        return {
            "chunks": chunks,
            "quality": q.__dict__,
            "suggested_category": suggested_category,
            "permission_hint": permission_hint,
            "stage": "done",
            "stage_timings": _merge_timings(
                state, "chunk_ms", (time.monotonic() - chunk_t0) * 1000
            ),
        }

    def route_after_extract(state: IngestState) -> str:
        return "vlm"

    g = StateGraph(IngestState)
    g.add_node("resolve_source", resolve_source)
    g.add_node("extract_text", extract_text)
    g.add_node("vlm_captions", vlm_captions)
    g.add_node("chunk_and_quality", chunk_and_quality)
    g.add_edge(START, "resolve_source")
    g.add_edge("resolve_source", "extract_text")
    g.add_conditional_edges("extract_text", route_after_extract, {"vlm": "vlm_captions"})
    g.add_edge("vlm_captions", "chunk_and_quality")
    g.add_edge("chunk_and_quality", END)
    return g.compile()


def run_ingest_graph(
    *,
    cfg: Settings,
    mineru: MinerUClient,
    vlm: VlmClient,
    unit_id: str,
    file_type: str,
    file_bytes: bytes | None = None,
    file_path: str | None = None,
    file_name: str | None = None,
    markdown: str | None = None,
    source_url: str | None = None,
    source_name: str | None = None,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> dict:
    """Invoke compiled ingest graph; return terminal state dict."""
    graph = build_ingest_graph(
        cfg=cfg,
        mineru=mineru,
        vlm=vlm,
        progress=progress,
        should_cancel=should_cancel,
    )
    init: IngestState = {
        "unit_id": unit_id,
        "file_type": file_type,
        "file_bytes": file_bytes,
        "file_path": file_path,
        "file_name": file_name,
        "markdown": markdown,
        "source_url": source_url,
        "source_name": source_name,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "use_vlm": use_vlm,
    }
    try:
        return graph.invoke(init)
    except IngestGraphError:
        raise
