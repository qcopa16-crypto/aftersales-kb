"""LangGraph ingest graph: resolve → extract → vlm → chunk → quality."""

from __future__ import annotations

import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.graphs.state import IngestState
from app.services.chunker import split_markdown
from app.utils.circuit import get_circuits, get_metrics
from app.utils.docx_parser import docx_to_markdown
from app.utils.limits import get_limits
from app.services.mineru_client import MinerUClient, MinerUError
from app.services.quality import build_quality
from app.services.source_fetch import SourceFetchError, fetch_source_to_temp
from app.services.vlm import VlmClient, VlmError, guess_mime, inject_captions_into_markdown
from app.utils.vlm_cache import get_caption_cache

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]


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


def _images_from_markdown(text: str) -> list[dict[str, Any]]:
    import re

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
):
    """Compile LangGraph for document ingest. Services injected for testability."""
    on_progress = progress or _noop_progress

    def resolve_source(state: IngestState) -> IngestState:
        on_progress("fetch", 5, "resolving source")
        return {"stage": "fetch"}

    def extract_text(state: IngestState) -> IngestState:
        ft = state["file_type"]
        unit_id = state["unit_id"]
        on_progress("parse", 15, f"extract {ft}")
        text = ""
        page_count: int | None = None
        image_bytes_map: dict[str, bytes] = {}
        images: list[dict] = []

        try:
            if ft in {"md", "txt"}:
                md = state.get("markdown")
                raw = state.get("file_bytes")
                url = state.get("source_url")
                if md and md.strip():
                    text = md
                elif raw is not None:
                    text = raw.decode("utf-8", errors="replace")
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
                    md, page_count, raw_images = mineru.pdf_to_markdown(path)
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
            raise IngestGraphError(str(exc), exc.error_code, 502) from exc
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
        }

    def vlm_captions(state: IngestState) -> IngestState:
        use_vlm = bool(state.get("use_vlm"))
        image_bytes_map = state.get("image_bytes_map") or {}
        images = list(state.get("images") or [])
        text = state.get("text") or ""
        if not use_vlm or not vlm.available or not image_bytes_map:
            on_progress("vlm", 50, "skip vlm")
            return {"images": images, "text": text, "stage": "vlm"}

        circuits = get_circuits(cfg.circuit_failure_threshold, cfg.circuit_cooldown_sec)
        metrics = get_metrics()
        if circuits.is_open("vlm"):
            metrics.inc("vlm.circuit_open")
            on_progress("vlm", 50, "vlm circuit open, skip")
            return {"images": images, "text": text, "stage": "vlm"}

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

        def _one(img: dict) -> tuple[dict, str | None, str | None]:
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
            with ThreadPoolExecutor(max_workers=workers) as pool:
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
        return {"images": images, "text": text, "stage": "vlm"}

    def chunk_and_quality(state: IngestState) -> IngestState:
        on_progress("chunk", 85, "splitting")
        text = state.get("text") or ""
        images = state.get("images") or []
        page_count = state.get("page_count")
        ft = state["file_type"]
        chunk_objs = split_markdown(
            text,
            chunk_size=state.get("chunk_size") or cfg.chunk_size,
            chunk_overlap=state.get("chunk_overlap") if state.get("chunk_overlap") is not None else cfg.chunk_overlap,
        )
        chunks = [
            {"chunk_index": c.chunk_index, "content": c.content, "heading_path": c.heading_path}
            for c in chunk_objs
        ]
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
        on_progress("done", 100, f"chunks={len(chunks)}")
        return {
            "chunks": chunks,
            "quality": q.__dict__,
            "stage": "done",
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
    file_name: str | None = None,
    markdown: str | None = None,
    source_url: str | None = None,
    source_name: str | None = None,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    progress: ProgressFn | None = None,
) -> dict:
    """Invoke compiled ingest graph; return terminal state dict."""
    graph = build_ingest_graph(cfg=cfg, mineru=mineru, vlm=vlm, progress=progress)
    init: IngestState = {
        "unit_id": unit_id,
        "file_type": file_type,
        "file_bytes": file_bytes,
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
