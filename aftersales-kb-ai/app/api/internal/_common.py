"""Shared helpers for /internal routes."""
from __future__ import annotations

import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from core.config import Settings, get_settings
from core.errors import detail_body

_ALLOWED_TYPES = {"pdf", "docx", "md", "txt", "markdown"}
_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_parse_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="parse-task")
_parse_sem: threading.BoundedSemaphore | None = None
_parse_sem_lock = threading.Lock()
_parse_spool_dir = Path(tempfile.gettempdir()) / "kb_upload_spool"
_parse_spool_dir.mkdir(parents=True, exist_ok=True)

def _get_parse_sem() -> threading.BoundedSemaphore:
    global _parse_sem
    with _parse_sem_lock:
        if _parse_sem is None:
            _parse_sem = threading.BoundedSemaphore(max(1, get_settings().parse_queue_max))
        return _parse_sem


async def _spool_upload(file: UploadFile, cfg: Settings) -> tuple[bytes | None, Path | None]:
    """Stream upload with size cap. Large files go to disk; small stay in memory."""
    max_bytes = cfg.max_upload_bytes
    threshold = cfg.upload_spool_threshold
    buf = bytearray()
    dest: Any = None
    dest_path: Path | None = None
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_code": "UPLOAD_TOO_LARGE",
                        "message": f"upload exceeds max_upload_bytes={max_bytes}",
                    },
                )
            if dest is None and len(buf) >= threshold:
                # spill to disk once we cross the in-memory threshold
                suffix = Path(file.filename or "upload.bin").suffix[:16]
                tmp = tempfile.NamedTemporaryFile(
                    dir=_parse_spool_dir, suffix=suffix, delete=False
                )
                dest = tmp
                dest_path = Path(tmp.name)
                dest.write(buf)
                buf.clear()
            elif dest is not None:
                dest.write(chunk)
        if dest is not None:
            dest.close()
            return None, dest_path
        return bytes(buf), None
    except Exception:
        if dest is not None:
            dest.close()
        if dest_path is not None:
            dest_path.unlink(missing_ok=True)
        raise


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
    # Presence (even blank) is a source; empty content is rejected by parse hard-fail gate.
    has_md = markdown is not None
    has_url = bool(source_url and source_url.strip())
    if has_file or has_md or has_url:
        return
    if file_type in {"docx", "pdf"} and has_md:
        raise HTTPException(status_code=400, detail=f"markdown is not valid for {file_type}")
    raise HTTPException(status_code=400, detail="file, markdown, or source_url is required")


def _enforce_markdown_limit(markdown: str | None, cfg: Settings) -> None:
    if markdown is None:
        return
    limit = int(cfg.max_markdown_chars or 0)
    if limit > 0 and len(markdown) > limit:
        raise HTTPException(
            status_code=400,
            detail=detail_body(
                "MARKDOWN_TOO_LONG",
                f"markdown length {len(markdown)} exceeds max_markdown_chars={limit}",
                max_chars=cfg.error_message_max_chars,
            ),
        )


def _max_msg(cfg: Settings) -> int:
    return int(cfg.error_message_max_chars or 500)

