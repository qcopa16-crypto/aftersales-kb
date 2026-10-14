"""Ingest /internal contract DTOs.

Also re-exports retrieve/generate DTOs for backward compatibility
(`from schemas.ingest import RetrieveRequest, ...`).
"""

from pydantic import BaseModel, Field

from schemas.generate import GenerateContext, GenerateRequest  # noqa: F401
from schemas.retrieve import (  # noqa: F401
    AclFilter,
    RetrieveRequest,
    RetrieveResponse,
    RetrievedChunk,
)


class ChunkIn(BaseModel):
    chunk_index: int = Field(ge=0)
    # Allow empty strings so embed quality gate can reject with 400 (not 422).
    content: str = Field(default="")
    heading_path: list[str] = Field(default_factory=list)


class EmbedRequest(BaseModel):
    unit_id: str = Field(min_length=1)
    # Hard upper bound is also enforced in the endpoint via Settings.embed_max_chunks
    # so the error body can carry EMBED_TOO_MANY_CHUNKS instead of 422.
    chunks: list[ChunkIn] = Field(min_length=1)
    # P3: resume after partial embed; default 0 = full replace
    resume_from_index: int = Field(default=0, ge=0)
    # batch consistency (optional; validated when provided)
    content_hash: str | None = None
    chunks_hash: str | None = None
    force: bool = False
    # Java-generated audit id; Python only echoes/stores, never generates.
    ingest_id: str | None = Field(default=None, max_length=128)
    # Optional ACL metadata for retrieve filters (Java-resolved; Python only stores).
    acl_department: str | None = Field(default=None, max_length=64)
    acl_classification: str | None = Field(default=None, max_length=64)


class EmbedResponse(BaseModel):
    unit_id: str
    embedded_count: int
    dim: int
    resumed_from: int = 0
    partial: bool = False
    ingest_id: str | None = None
    # encode_ms / publish_ms wall-clock (observability; additive).
    stage_timings: dict[str, float] = Field(default_factory=dict)


class ParseChunk(BaseModel):
    chunk_index: int
    content: str
    heading_path: list[str] = Field(default_factory=list)


class ParseImage(BaseModel):
    image_id: str
    relative_path: str
    status: str = "skipped"
    caption: str | None = None


class VlmDescribeResponse(BaseModel):
    caption: str
    model: str


class ParseQuality(BaseModel):
    chunk_count: int = 0
    empty_chunk_count: int = 0
    empty_chunk_ratio: float = 0.0
    avg_chunk_chars: float = 0.0
    max_chunk_chars: int = 0
    heading_count: int = 0
    chunks_with_heading_ratio: float = 0.0
    image_count: int = 0
    image_in_md_count: int = 0
    markdown_chars: int = 0
    page_count: int | None = None
    suggest_review: bool = False
    # Stable enum (see ai.ingest.quality.REVIEW_REASONS). Empty when suggest_review=false.
    review_reasons: list[str] = Field(default_factory=list)


class ParseResponse(BaseModel):
    unit_id: str
    markdown: str
    chunks: list[ParseChunk]
    images: list[ParseImage] = Field(default_factory=list)
    page_count: int | None = None
    file_type: str
    quality: ParseQuality = Field(default_factory=ParseQuality)
    cache_hit: bool = False
    content_hash: str = ""
    chunks_hash: str = ""
    # Echo of request ingest_id (Java-generated); None if not provided.
    ingest_id: str | None = None
    # Heuristic only; Java decides whether to persist.
    suggested_category: str | None = None
    permission_hint: str | None = None
    # extract_ms / vlm_ms / chunk_ms wall-clock (observability; additive).
    stage_timings: dict[str, float] = Field(default_factory=dict)


class DeleteUnitResponse(BaseModel):
    deleted: bool
    deleted_count: int


class ParseTaskAccepted(BaseModel):
    """Init response for async parse (snake_case internal contract)."""

    task_id: str
    unit_id: str
    file_type: str
    status: str = "queued"


class ParseTaskStatus(BaseModel):
    task_id: str
    unit_id: str
    file_type: str
    status: str
    stage: str
    progress: int
    detail: str = ""
    error_code: str | None = None
    error_message: str | None = None
    result: ParseResponse | None = None
    created_at: float
    updated_at: float


# --------------------------------------------------------------------------- #
# Ingest review gate (/internal/review) — 阶段1 AI 三档判定契约
# --------------------------------------------------------------------------- #
class ReviewImage(BaseModel):
    relative_path: str
    # 调用方可直接给图片字节（base64）；不给则 AI 侧尝试按 content_hash 解析缓存。
    data_b64: str | None = None


class ReviewRequest(BaseModel):
    unit_id: str = Field(min_length=1)
    category: str | None = None
    markdown: str = ""
    # [{chunk_index, content, heading_path?}]
    chunks: list[dict] = Field(default_factory=list)
    # ParseQuality 的 dict；不给则由 AI 用 build_quality 重算。
    quality: dict | None = None
    images: list[ReviewImage] = Field(default_factory=list)
    enable_llm: bool = True
    enable_vlm: bool = True
    content_hash: str | None = None
    ingest_id: str | None = None


class VlmFinding(BaseModel):
    relative_path: str
    usable: bool
    reason: str = ""


class ReviewResponse(BaseModel):
    # "pass" | "review"（稳定枚举，见 ai.ingest.review.REVIEW_VERDICTS）
    verdict: str
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    # {"rules": "pass|review", "llm": "pass|review|skipped|error",
    #  "vlm": "pass|review|skipped|unavailable|error"}
    tiers: dict[str, str] = Field(default_factory=dict)
    coverage: dict = Field(default_factory=dict)
    vlm_findings: list[VlmFinding] = Field(default_factory=list)
    model: str = ""
    # llm_ms / vlm_ms wall-clock（observability；additive）。
    stage_timings: dict[str, float] = Field(default_factory=dict)
    ingest_id: str | None = None
