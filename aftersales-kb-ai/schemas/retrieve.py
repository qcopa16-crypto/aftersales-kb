"""Retrieve /internal contract DTOs."""

from typing import Literal

from pydantic import BaseModel, Field


class AclFilter(BaseModel):
    """Executable filter conditions resolved by Java. Python never infers identity."""

    unit_ids: list[str] | None = None
    deny_unit_ids: list[str] | None = None
    department: str | None = Field(default=None, max_length=64)
    classification: str | None = Field(default=None, max_length=64)
    min_score: float | None = None

    def is_empty(self) -> bool:
        return not (
            self.unit_ids
            or self.deny_unit_ids
            or (self.department or "").strip()
            or (self.classification or "").strip()
            or self.min_score is not None
        )


class RetrieveRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)
    acl_filter: AclFilter | None = None
    unit_ids: list[str] | None = None
    overfetch: int | None = Field(default=None, ge=1, le=200)
    enable_rerank: bool | None = None
    enable_hybrid: bool | None = None
    # 会话记忆（检索期指代消解）。owner_id 是 Java 下发的不透明分区键，AI 只当 key、绝不解释。
    session_id: str = ""
    owner_id: str = ""
    enable_session_rewrite: bool = False


class RetrievedChunk(BaseModel):
    unit_id: str
    chunk_id: str
    score: float
    content: str
    version_id: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    score_dense: float | None = None
    score_lexical: float | None = None
    score_sparse: float | None = None
    score_fused: float | None = None
    retrieval_paths: list[Literal["dense", "lexical", "sparse"]] = Field(default_factory=list)


class RetrieveResponse(BaseModel):
    chunks: list[RetrievedChunk]
    took_ms: int | None = None
    acl_applied: bool = False
    hybrid: bool = True
    degraded_to_dense: bool = False
