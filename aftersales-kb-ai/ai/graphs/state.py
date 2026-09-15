"""LangGraph shared state types for AI pipelines."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _last(prev: Any, new: Any) -> Any:
    return new if new is not None else prev


class IngestState(TypedDict, total=False):
    """State for parse/ingest graph."""

    unit_id: Annotated[str, _last]
    file_type: Annotated[str, _last]
    file_bytes: Annotated[bytes | None, _last]
    file_path: Annotated[str | None, _last]
    file_name: Annotated[str | None, _last]
    markdown: Annotated[str | None, _last]
    source_url: Annotated[str | None, _last]
    source_name: Annotated[str | None, _last]
    chunk_size: Annotated[int, _last]
    chunk_overlap: Annotated[int, _last]
    use_vlm: Annotated[bool, _last]

    # intermediates
    text: Annotated[str, _last]
    page_count: Annotated[int | None, _last]
    image_bytes_map: Annotated[dict[str, bytes], _last]
    images: Annotated[list[dict], _last]
    chunks: Annotated[list[dict], _last]
    quality: Annotated[dict, _last]
    suggested_category: Annotated[str | None, _last]
    permission_hint: Annotated[str | None, _last]
    stage: Annotated[str, _last]
    error_code: Annotated[str | None, _last]
    error_message: Annotated[str | None, _last]


class RetrieveState(TypedDict, total=False):
    """State for hybrid retrieve graph (dense + lexical + RRF)."""

    question: Annotated[str, _last]
    top_k: Annotated[int, _last]
    overfetch: Annotated[int | None, _last]
    unit_ids: Annotated[list[str] | None, _last]
    acl: Annotated[Any, _last]
    search_filter: Annotated[Any, _last]
    acl_applied: Annotated[bool, _last]
    hybrid: Annotated[bool, _last]
    enable_rerank: Annotated[bool | None, _last]
    degraded_to_dense: Annotated[bool, _last]
    dense_hits: Annotated[list[dict], _last]
    lexical_hits: Annotated[list[dict], _last]
    fused_hits: Annotated[list[Any], _last]
    filtered_out: Annotated[int, _last]
    chunks: Annotated[list[dict], _last]
    stage: Annotated[str, _last]
    error_code: Annotated[str | None, _last]
    error_message: Annotated[str | None, _last]


class QaState(TypedDict, total=False):
    """State for retrieve + generate graph (contexts already authorized by caller)."""

    question: Annotated[str, _last]
    top_k: Annotated[int, _last]
    contexts: Annotated[list[dict], _last]
    session_id: Annotated[str, _last]
    chunks: Annotated[list[dict], _last]
    answer_deltas: Annotated[list[str], _last]
    error_code: Annotated[str | None, _last]
    error_message: Annotated[str | None, _last]
