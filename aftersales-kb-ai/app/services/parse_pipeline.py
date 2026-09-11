"""Parse pipeline facade: delegates to LangGraph ingest graph (same external contract)."""

from __future__ import annotations

import logging
from typing import Callable

from app.config import Settings
from app.graphs.ingest_graph import IngestGraphError, run_ingest_graph
from app.schemas.ingest import ParseChunk, ParseImage, ParseQuality, ParseResponse
from app.services.mineru_client import MinerUClient
from app.services.vlm import VlmClient

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, str], None]


class ParsePipelineError(Exception):
    def __init__(self, message: str, error_code: str = "PARSE_FAILED", status: int = 500) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


def run_parse(
    *,
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
    cfg: Settings,
    mineru: MinerUClient,
    vlm: VlmClient,
    progress: ProgressFn | None = None,
) -> ParseResponse:
    """Run LangGraph ingest and map state → ParseResponse."""
    try:
        state = run_ingest_graph(
            cfg=cfg,
            mineru=mineru,
            vlm=vlm,
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
            progress=progress,
        )
    except IngestGraphError as exc:
        raise ParsePipelineError(str(exc), exc.error_code, exc.status) from exc

    raw_chunks = state.get("chunks") or []
    raw_images = state.get("images") or []
    q = state.get("quality") or {}
    return ParseResponse(
        unit_id=unit_id.strip(),
        markdown=state.get("text") or "",
        chunks=[
            ParseChunk(
                chunk_index=int(c.get("chunk_index") or 0),
                content=str(c.get("content") or ""),
                heading_path=list(c.get("heading_path") or []),
            )
            for c in raw_chunks
        ],
        images=[
            ParseImage(
                image_id=str(i.get("image_id") or ""),
                relative_path=str(i.get("relative_path") or ""),
                status=str(i.get("status") or "skipped"),
                caption=i.get("caption"),
            )
            for i in raw_images
        ],
        page_count=state.get("page_count"),
        file_type=file_type,
        quality=ParseQuality(**{k: q[k] for k in ParseQuality.model_fields if k in q}),
        cache_hit=False,
    )
