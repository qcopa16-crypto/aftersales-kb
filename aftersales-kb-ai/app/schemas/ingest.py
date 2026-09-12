from pydantic import BaseModel, Field


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


class EmbedResponse(BaseModel):
    unit_id: str
    embedded_count: int
    dim: int
    resumed_from: int = 0
    partial: bool = False
    ingest_id: str | None = None


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
    # Stable enum (see app.services.quality.REVIEW_REASONS). Empty when suggest_review=false.
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


class RetrieveRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)


class RetrievedChunk(BaseModel):
    unit_id: str
    chunk_id: str
    score: float
    content: str


class RetrieveResponse(BaseModel):
    chunks: list[RetrievedChunk]


class GenerateContext(BaseModel):
    unit_id: str
    title: str = ""
    content: str = Field(min_length=1)


class GenerateRequest(BaseModel):
    question: str = Field(min_length=1)
    contexts: list[GenerateContext] = Field(default_factory=list)
    session_id: str = ""
