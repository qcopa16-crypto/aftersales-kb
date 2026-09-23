from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # auth
    internal_token: str = "change-me-long-random"

    # mineru
    mineru_enabled: bool = True
    mineru_api_base: str = "https://mineru.net"
    mineru_token: str = ""
    mineru_model_version: str = "vlm"
    mineru_language: str = "ch"
    mineru_enable_table: bool = True
    mineru_enable_formula: bool = True
    mineru_is_ocr: bool = False
    mineru_poll_interval_sec: float = 3.0
    mineru_poll_timeout_sec: float = 600.0
    mineru_batch_size: int = 20

    # embedding
    embed_provider: str = "local"  # local | cloud | fake
    embed_local_model: str = ""
    embed_local_device: str = "cpu"
    embed_cloud_base: str = ""
    embed_cloud_key: str = ""
    embed_cloud_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    embed_batch_size: int = 16

    # vector store
    vector_store: str = "memory"  # milvus | memory
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_collection: str = "kb_chunks"

    # server
    host: str = "0.0.0.0"
    port: int = 8000

    # chunker defaults
    chunk_size: int = 512
    chunk_overlap: int = 50

    # source_url download (MinIO presigned etc.)
    max_source_bytes: int = 209_715_200  # 200MB
    source_connect_timeout_sec: float = 10.0
    source_read_timeout_sec: float = 180.0
    # Test-only: allow localhost/private hosts so local fault-injection servers work.
    source_allow_private_hosts: bool = False

    # parse quality heuristics
    qc_pdf_page_review: int = 20
    qc_md_chars_review: int = 20_000
    qc_image_review: int = 5
    qc_empty_ratio_review: float = 0.05
    qc_low_heading_ratio: float = 0.3

    # VLM image captions (phase 2)
    vlm_enabled: bool = False
    vlm_api_base: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""
    max_vlm_images: int = 12
    vlm_timeout_sec: float = 60.0
    vlm_max_image_bytes: int = 2_000_000

    # concurrency / rate limits (aligned with vendor docs + product caps)
    vlm_max_concurrency: int = 10
    llm_max_concurrency: int = 5
    # MinerU 精准 API：提交 50 文件/分钟，轮询 1000 次/分钟，日上限 5000 文件
    mineru_max_concurrency: int = 1
    mineru_batch_link_limit: int = 50
    mineru_submit_per_minute: int = 50
    mineru_poll_per_minute: int = 1000
    mineru_daily_file_limit: int = 5000

    # LLM generate (cloud OpenAI-compatible)
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_timeout_sec: float = 120.0

    # P1: parse idempotency + VLM caption cache
    enable_parse_idempotency: bool = True
    parse_cache_max: int = 256
    vlm_caption_cache_max: int = 512
    retrieve_top_k_default: int = 8

    # Hybrid retrieve (dense + lexical + RRF)
    retrieve_overfetch_factor: int = 4
    retrieve_overfetch_max: int = 50
    retrieve_max_per_unit: int = 2
    # Rank by best-chunk-per-unit first (debias multi-chunk manuals).
    # Off by default: unit-level first-chunk order can bury the true answer chunk.
    retrieve_unit_agg: bool = False
    retrieve_enable_rerank: bool = True
    # Lightweight synonym/paraphrase expansion (query_expand.py).
    retrieve_query_expand: bool = True
    # Second-pass embed cosine rerank on top-N candidates.
    retrieve_embed_rerank: bool = True
    retrieve_embed_rerank_top: int = 20
    retrieve_embed_rerank_weight: float = 0.35
    retrieve_require_acl: bool = False
    retrieve_max_question_chars: int = 2000
    retrieve_hybrid_enabled: bool = True
    retrieve_hybrid_rrf_k: int = 60
    retrieve_lexical_rrf_weight: float = 0.35
    retrieve_hybrid_degrade_to_dense: bool = True
    retrieve_hybrid_lexical_mode: str = "memory_bm25"

    # Enterprise retrieve hardening (Python-side gaps after hybrid baseline)
    # Production must set retrieve_require_acl=true; this flag refuses startup if not.
    retrieve_strict_prod: bool = False
    # When true, /internal/embed rejects missing/blank acl_department.
    embed_require_acl_fields: bool = False
    # Single-writer process-local map for Milvus active versions (survives restart).
    milvus_active_map_path: str = ".data/milvus_active_versions.json"
    # Soft wall-clock for dense store.search (0 = disabled).
    retrieve_store_timeout_sec: float = 5.0
    # Full retrieve pipeline budget (0 = disabled); on exceed → 504 RETRIEVE_TIMEOUT.
    retrieve_overall_timeout_sec: float = 8.0
    # Cap documents scored by lexical path; score only first N in stable order.
    retrieve_lexical_max_docs: int = 20000
    # Cold-start load limit for sync_from_store (0 = no cap). Separate from query candidate cap.
    retrieve_lexical_sync_limit: int = 16384
    # When true, skip lexical path entirely if doc_count exceeds cap (default: still score first N).
    retrieve_lexical_skip_when_over_cap: bool = False
    # Multi-replica safety: dense-only when true (process-local lexical/active map unsafe).
    retrieve_multi_replica_safe: bool = False
    # Bulkhead around full retrieve pipeline (0 = disabled).
    retrieve_max_concurrency: int = 32
    # Process-local QPS limit for retrieve (0 = disabled).
    retrieve_qps_limit: float = 0.0
    # Shared thread pool size for store.search soft timeout.
    retrieve_store_pool_size: int = 4
    # Cold-start lexical sync_from_store wall-clock budget (0 = follow store timeout).
    retrieve_lexical_sync_timeout_sec: float = 0.0
    # Max length of unit_ids / deny_unit_ids lists on retrieve.
    retrieve_max_unit_ids: int = 500
    # When true, drop hits missing required ACL metadata fields (fail-closed; no full-corpus).
    retrieve_acl_fields_required: bool = False
    # Structured JSON log line per retrieve (no question/chunk text).
    retrieve_struct_log: bool = True
    # Process-local LRU for query embeddings (0 = disabled).
    retrieve_embed_cache_max: int = 0

    # P3: circuit breaker + embed resume
    circuit_failure_threshold: int = 3
    circuit_cooldown_sec: float = 60.0
    embed_resume_enabled: bool = True
    embed_partial_max: int = 32

    # Known-issues hardening (Python)
    parse_queue_max: int = 32
    embed_max_empty_ratio: float = 0.5
    embed_max_content_chars: int = 60000
    source_max_redirects: int = 5
    max_upload_bytes: int = 209_715_200
    upload_spool_threshold: int = 8_388_608
    mineru_batch_store_path: str = ".data/mineru_batches.json"

    # P1/P2 upgrade: versions, lease, cache key, parse hard-fail
    parser_version: str = "1.1"
    cache_config_version: str = "1"
    task_lease_sec: float = 120.0
    task_reap_on_read: bool = True
    parse_fail_on_empty: bool = True

    # Stateless hardening: sync parse overall budget (0 = disabled).
    # Stage timeouts remain source_*/mineru_poll_*/vlm_*; this is a request wall-clock cap.
    parse_overall_timeout_sec: float = 900.0

    # Input hardening (P0-P2)
    embed_max_chunks: int = 5000
    max_markdown_chars: int = 200_000
    generate_max_question_chars: int = 2000
    generate_max_contexts_total_chars: int = 120_000
    error_message_max_chars: int = 500
    metrics_public: bool = False
    refuse_on_default_internal_token: bool = False

    # Publish throughput (S2.1 ingest-publish-cache-uplift)
    milvus_insert_batch_size: int = 500
    milvus_count_validate_max: int = 200
    milvus_orphan_sweep_on_publish: bool = False

    # Parse / MinerU disk reuse (S2.2)
    parse_cache_disk_enabled: bool = True
    parse_cache_disk_dir: str = ".data/parse_cache"
    parse_cache_disk_max_entries: int = 128
    mineru_extract_cache_enabled: bool = True
    mineru_extract_cache_dir: str = ".data/mineru_extract_cache"
    mineru_extract_cache_max_entries: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def embed_model_id(cfg: Settings | None = None) -> str:
    c = cfg or get_settings()
    provider = (c.embed_provider or "fake").lower()
    if provider == "cloud":
        return f"cloud:{c.embed_cloud_model}"
    if provider == "local":
        return f"local:{c.embed_local_model}"
    return provider
