"""Internal ingest APIs: parse / async parse tasks / embed / delete. Java-only via X-Internal-Token."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.config import Settings, get_settings
from app.deps import get_embedder, get_llm, get_mineru, get_vector_store, get_vlm
from app.schemas.ingest import (
    DeleteUnitResponse,
    EmbedRequest,
    EmbedResponse,
    GenerateRequest,
    ParseResponse,
    ParseTaskAccepted,
    ParseTaskStatus,
    RetrieveRequest,
    RetrieveResponse,
    RetrievedChunk,
    VlmDescribeResponse,
)
from app.services.chunker import enrich_chunk_for_embed, split_markdown
from app.services.content_cache import content_hash, get_parse_cache
from app.services.embedder import Embedder
from app.utils.limits import get_limits
from app.services.llm import LlmClient, LlmError
from app.services.mineru_client import MinerUClient
from app.services.parse_pipeline import ParsePipelineError, run_parse
from app.services.task_store import get_task_store
from app.services.vector_store import VectorItem, VectorStore
from app.services.vlm import VlmClient, VlmError, guess_mime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal", tags=["internal"])

_ALLOWED_TYPES = {"pdf", "docx", "md", "txt", "markdown"}
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# single-worker pool: parse jobs are IO-heavy (MinerU/VLM); avoid stampede
_parse_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="parse-task")


def _normalize_file_type(file_type: str) -> str:
    ft = (file_type or "").lower().strip()
    if ft == "markdown":
        return "md"
    if ft not in _ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported file_type: {file_type}")
    return ft


def _require_unit_id(unit_id: str) -> str:
    uid = (unit_id or "").strip()
    if not uid or not _UNIT_ID_RE.match(uid):
        raise HTTPException(
            status_code=400,
            detail="unit_id is required and must match [A-Za-z0-9._:-]{1,64}",
        )
    return uid


def _chunk_params(cfg: Settings, chunk_size: int | None, chunk_overlap: int | None) -> tuple[int, int]:
    size = chunk_size if chunk_size is not None else cfg.chunk_size
    overlap = chunk_overlap if chunk_overlap is not None else cfg.chunk_overlap
    if size < 32 or size > 8000:
        raise HTTPException(status_code=400, detail="chunk_size must be between 32 and 8000")
    if overlap < 0 or overlap >= size:
        raise HTTPException(status_code=400, detail="chunk_overlap must be >= 0 and < chunk_size")
    return size, overlap


def _require_source(
    *,
    file: UploadFile | None,
    markdown: str | None,
    source_url: str | None,
    file_type: str,
) -> None:
    has_file = file is not None
    has_md = markdown is not None and markdown.strip()
    has_url = bool(source_url and source_url.strip())
    if has_file or has_md or has_url:
        return
    if file_type in {"docx", "pdf"} and has_md:
        raise HTTPException(status_code=400, detail=f"markdown is not valid for {file_type}")
    raise HTTPException(status_code=400, detail="file, markdown, or source_url is required")


@router.post("/parse", response_model=ParseResponse)
async def parse(
    file: UploadFile | None = File(default=None),
    markdown: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    source_name: str | None = Form(default=None),
    file_type: str = Form(...),
    unit_id: str = Form(...),
    chunk_size: int | None = Form(default=None),
    chunk_overlap: int | None = Form(default=None),
    use_vlm: bool = Form(default=False),
    cfg: Settings = Depends(get_settings),
    mineru: MinerUClient = Depends(get_mineru),
    vlm: VlmClient = Depends(get_vlm),
) -> ParseResponse:
    """Synchronous parse (small docs / debug). Large PDFs should use /parse/async."""
    ft = _normalize_file_type(file_type)
    uid = _require_unit_id(unit_id)
    size, overlap = _chunk_params(cfg, chunk_size, chunk_overlap)
    _require_source(file=file, markdown=markdown, source_url=source_url, file_type=ft)

    file_bytes = await file.read() if file is not None else None
    cache = get_parse_cache(cfg.parse_cache_max)
    cache_key: str | None = None
    if cfg.enable_parse_idempotency and source_url is None:
        cache_key = content_hash(
            file_type=ft,
            file_bytes=file_bytes,
            markdown=markdown,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
        )
        hit = cache.get(cache_key)
        if hit is not None:
            payload = dict(hit)
            payload["unit_id"] = uid
            payload["cache_hit"] = True
            return ParseResponse(**payload)

    try:
        result = run_parse(
            unit_id=uid,
            file_type=ft,
            file_bytes=file_bytes,
            file_name=file.filename if file else None,
            markdown=markdown,
            source_url=source_url,
            source_name=source_name,
            chunk_size=size,
            chunk_overlap=overlap,
            use_vlm=use_vlm,
            cfg=cfg,
            mineru=mineru,
            vlm=vlm,
        )
        if cache_key is not None:
            cache.put(cache_key, result.model_dump())
        return result
    except ParsePipelineError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc


def _run_parse_job(
    task_id: str,
    *,
    unit_id: str,
    file_type: str,
    file_bytes: bytes | None,
    file_name: str | None,
    markdown: str | None,
    source_url: str | None,
    source_name: str | None,
    chunk_size: int,
    chunk_overlap: int,
    use_vlm: bool,
    cfg: Settings,
) -> None:
    """Background worker body for async parse tasks."""
    store = get_task_store()
    mineru = get_mineru()
    vlm = get_vlm()
    store.update(task_id, status="running", stage="fetch", progress=1, detail="started")

    def on_progress(stage: str, progress: int, detail: str) -> None:
        if store.is_cancelled(task_id):
            raise ParsePipelineError("task cancelled", "TASK_CANCELLED", 499)
        store.update(task_id, stage=stage, progress=progress, detail=detail)

    try:
        result = run_parse(
            unit_id=unit_id,
            file_type=file_type,
            file_bytes=file_bytes,
            file_name=file_name,
            markdown=markdown,
            source_url=source_url,
            source_name=source_name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            use_vlm=use_vlm,
            cfg=cfg,
            mineru=mineru,
            vlm=vlm,
            progress=on_progress,
        )
        payload: dict[str, Any] = result.model_dump()
        store.update(
            task_id,
            status="done",
            stage="done",
            progress=100,
            detail="completed",
            result=payload,
        )
    except ParsePipelineError as exc:
        logger.warning("async parse failed task_id=%s unit_id=%s: %s", task_id, unit_id, exc)
        store.update(
            task_id,
            status="failed",
            stage="failed",
            detail=str(exc),
            error_code=exc.error_code,
            error_message=str(exc),
        )
    except Exception as exc:
        logger.exception("async parse unexpected error task_id=%s", task_id)
        store.update(
            task_id,
            status="failed",
            stage="failed",
            detail=str(exc),
            error_code="PARSE_FAILED",
            error_message=str(exc),
        )


@router.post("/parse/async", response_model=ParseTaskAccepted)
async def parse_async(
    file: UploadFile | None = File(default=None),
    markdown: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    source_name: str | None = Form(default=None),
    file_type: str = Form(...),
    unit_id: str = Form(...),
    chunk_size: int | None = Form(default=None),
    chunk_overlap: int | None = Form(default=None),
    use_vlm: bool = Form(default=False),
    cfg: Settings = Depends(get_settings),
) -> ParseTaskAccepted:
    """Init async parse: returns task_id immediately; poll GET /tasks/{task_id}."""
    ft = _normalize_file_type(file_type)
    uid = _require_unit_id(unit_id)
    size, overlap = _chunk_params(cfg, chunk_size, chunk_overlap)
    _require_source(file=file, markdown=markdown, source_url=source_url, file_type=ft)

    file_bytes = await file.read() if file is not None else None
    store = get_task_store()
    task = store.create(unit_id=uid, file_type=ft)
    _parse_pool.submit(
        _run_parse_job,
        task.task_id,
        unit_id=uid,
        file_type=ft,
        file_bytes=file_bytes,
        file_name=file.filename if file else None,
        markdown=markdown,
        source_url=source_url,
        source_name=source_name,
        chunk_size=size,
        chunk_overlap=overlap,
        use_vlm=use_vlm,
        cfg=cfg,
    )
    return ParseTaskAccepted(
        task_id=task.task_id,
        unit_id=uid,
        file_type=ft,
        status="queued",
    )


@router.get("/tasks/{task_id}", response_model=ParseTaskStatus)
def get_task(task_id: str) -> ParseTaskStatus:
    """Poll async parse task status and result."""
    task = get_task_store().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": task_id})
    public = task.to_public()
    result = public.get("result")
    return ParseTaskStatus(
        task_id=public["task_id"],
        unit_id=public["unit_id"],
        file_type=public["file_type"],
        status=public["status"],
        stage=public["stage"],
        progress=public["progress"],
        detail=public["detail"],
        error_code=public["error_code"],
        error_message=public["error_message"],
        result=ParseResponse(**result) if result else None,
        created_at=public["created_at"],
        updated_at=public["updated_at"],
    )


@router.delete("/tasks/{task_id}")
def cancel_task(task_id: str) -> dict:
    """Request cancel of queued/running parse task."""
    store = get_task_store()
    if not store.get(task_id):
        raise HTTPException(status_code=404, detail={"error_code": "TASK_NOT_FOUND", "message": task_id})
    ok = store.request_cancel(task_id)
    return {"task_id": task_id, "cancel_requested": ok}


@router.post("/embed", response_model=EmbedResponse)
def embed(
    body: EmbedRequest,
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    cfg: Settings = Depends(get_settings),
) -> EmbedResponse:
    """Idempotent embed; supports resume_from_index after partial failure."""
    from app.utils.circuit import get_metrics
    from app.services.embed_partial import EmbedPartial, get_embed_partial_store

    unit_id = _require_unit_id(body.unit_id)
    texts = [enrich_chunk_for_embed(c.content, c.heading_path) for c in body.chunks]
    partial_store = get_embed_partial_store(cfg.embed_partial_max)
    resume_from = body.resume_from_index if cfg.embed_resume_enabled else 0
    if resume_from >= len(body.chunks):
        raise HTTPException(status_code=400, detail="resume_from_index out of range")

    try:
        if resume_from == 0:
            store.delete_by_unit(unit_id)
            partial_store.clear(unit_id)
            vectors: list[list[float]] = []
        else:
            prev = partial_store.get(unit_id)
            if prev is None or len(prev.vectors) < resume_from:
                raise HTTPException(
                    status_code=400,
                    detail="no partial checkpoint to resume; start from 0",
                )
            vectors = list(prev.vectors[:resume_from])

        batch = max(1, cfg.embed_batch_size)
        for i in range(resume_from, len(texts), batch):
            vectors.extend(embedder.encode(texts[i : i + batch]))
            # checkpoint after each batch
            if cfg.embed_resume_enabled:
                partial_store.put(
                    EmbedPartial(unit_id=unit_id, dim=embedder.dim, vectors=list(vectors), contents=texts[: len(vectors)])
                )

        if len(vectors) != len(body.chunks):
            raise RuntimeError(f"encode count mismatch: {len(vectors)} != {len(body.chunks)}")
        items = [
            VectorItem(chunk_index=c.chunk_index, content=texts[i], embedding=vectors[i])
            for i, c in enumerate(body.chunks)
        ]
        count = store.upsert(unit_id, items)
        partial_store.clear(unit_id)
        get_metrics().inc("embed.ok")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("embed failed unit_id=%s resume_from=%s", unit_id, resume_from)
        get_metrics().inc("embed.fail")
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "EMBED_FAILED",
                "message": str(exc),
                "resume_hint": f"retry with resume_from_index={resume_from}",
            },
        ) from exc

    return EmbedResponse(
        unit_id=unit_id,
        embedded_count=count,
        dim=embedder.dim,
        resumed_from=resume_from,
        partial=False,
    )


@router.delete("/units/{unit_id}", response_model=DeleteUnitResponse)
def delete_unit(
    unit_id: str,
    store: VectorStore = Depends(get_vector_store),
) -> DeleteUnitResponse:
    """Delete vectors for unit; idempotent. Never fake success on failure."""
    uid = _require_unit_id(unit_id)
    try:
        deleted_count = store.delete_by_unit(uid)
    except Exception as exc:
        logger.exception("delete unit failed unit_id=%s", uid)
        raise HTTPException(
            status_code=500,
            detail={"error_code": "DELETE_FAILED", "message": str(exc)},
        ) from exc
    return DeleteUnitResponse(deleted=True, deleted_count=deleted_count)


@router.post("/vlm/describe", response_model=VlmDescribeResponse)
async def vlm_describe(
    file: UploadFile = File(...),
    cfg: Settings = Depends(get_settings),
    vlm: VlmClient = Depends(get_vlm),
) -> VlmDescribeResponse:
    """Describe one image with cloud VLM (optional capability)."""
    if not vlm.available:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "VLM_DISABLED", "message": "VLM disabled or not configured"},
        )
    raw = await file.read()
    mime = file.content_type or guess_mime(file.filename or "")
    if not mime.startswith("image/"):
        mime = guess_mime(file.filename or "")
    try:
        with get_limits().vlm_slot():
            caption = vlm.describe_image(raw, mime=mime)
    except VlmError as exc:
        status = 413 if exc.error_code == "VLM_IMAGE_TOO_LARGE" else 502
        raise HTTPException(
            status_code=status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return VlmDescribeResponse(caption=caption, model=cfg.vlm_model)


@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    cfg: Settings = Depends(get_settings),
) -> RetrieveResponse:
    """Question → Top-K chunks. Permission filter stays in Java."""
    top_k = body.top_k or cfg.retrieve_top_k_default
    try:
        qvec = embedder.encode([body.question])[0]
        hits = store.search(qvec, top_k=top_k)
    except Exception as exc:
        logger.exception("retrieve failed")
        raise HTTPException(
            status_code=500,
            detail={"error_code": "RETRIEVE_FAILED", "message": str(exc)},
        ) from exc
    chunks = [
        RetrievedChunk(
            unit_id=str(h.get("unit_id") or ""),
            chunk_id=str(h.get("id") or h.get("chunk_index") or ""),
            score=float(h.get("score") or 0.0),
            content=str(h.get("content") or ""),
        )
        for h in hits
    ]
    return RetrieveResponse(chunks=chunks)


@router.post("/generate")
def generate(
    body: GenerateRequest,
    llm: LlmClient = Depends(get_llm),
):
    """SSE stream from authorized contexts only; never re-retrieves."""
    from fastapi.responses import StreamingResponse

    import json as _json
    import time as _time

    if not llm.available:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "LLM_DISABLED", "message": "LLM not configured"},
        )

    started = _time.monotonic()

    def sse_gen():
        n_chars = 0
        try:
            with get_limits().llm_slot():
                for delta in llm.stream_answer(body.question, body.contexts):
                    n_chars += len(delta)
                    yield f"event: delta\ndata: {_json.dumps({'text': delta}, ensure_ascii=False)}\n\n"
            elapsed_ms = int((_time.monotonic() - started) * 1000)
            # token counts unavailable without usage in stream; expose elapsed only
            done = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "elapsed_ms": elapsed_ms,
                "faqHit": False,
                "session_id": body.session_id,
            }
            yield f"event: done\ndata: {_json.dumps(done, ensure_ascii=False)}\n\n"
        except LlmError as exc:
            err = {"code": exc.error_code, "message": str(exc)}
            yield f"event: error\ndata: {_json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("generate stream failed session_id=%s", body.session_id)
            err = {"code": "GENERATE_FAILED", "message": str(exc)}
            yield f"event: error\ndata: {_json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
