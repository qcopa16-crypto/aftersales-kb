import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Documented-sample INTERNAL_TOKEN values that are public in the repository and
# must never authenticate a prod process (F-05). Keep in sync with
# ``app.main._DEFAULT_TOKENS``.
_SAMPLE_INTERNAL_TOKENS = frozenset({"", "change-me-long-random"})

# F-14: 默认（本地）active-version 映射路径。多副本部署必须显式改为共享路径，否则拒绝启动。
_DEFAULT_ACTIVE_MAP_PATH = ".data/milvus_active_versions.json"


def explicit_worker_id() -> str:
    """Explicit index-repair worker identity from the environment (R-08).

    Canonical lookup shared by the config validator, the startup gate and
    ``ai.retrieve.index_health`` so the three never disagree on what counts as
    "explicitly configured". Empty when the operator relies on the default
    ``host:pid`` identity.
    """
    return (
        os.environ.get("RETRIEVE_INDEX_REPAIR_WORKER_ID") or os.environ.get("AI_REPLICA_ID") or ""
    ).strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Aggregate deployment profile (M2b). One switch that collapses the common
    # fail-closed production combination instead of juggling several flags:
    #   ""     -> legacy behaviour (no preset; existing startup gates still apply)
    #   "demo" -> explicitly permissive (strict_prod/require_acl off)
    #   "prod" -> fail-closed preset (strict_prod + require_acl on)
    # An explicitly-provided field always wins over the preset, so a profile never
    # silently overrides a deliberate override.
    app_profile: str = ""

    # auth
    internal_token: str = "change-me-long-random"
    # Minimum INTERNAL_TOKEN length enforced under APP_PROFILE=prod (F-05).
    # 0 disables the length floor (the missing/sample-value gate still applies).
    internal_token_min_length: int = 32

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

    # Ingest review gate (阶段1): AI 三档判定（规则 → LLM → VLM）。AI 只给结论，
    # Java 侧决策与落库。两个 enabled 都为 false 时 /internal/review 直接 503。
    review_llm_enabled: bool = True
    review_vlm_enabled: bool = True
    review_llm_timeout_sec: float = 20.0
    review_vlm_timeout_sec: float = 60.0
    review_vlm_max_images: int = 3
    review_llm_max_tokens: int = 512
    review_markdown_max_chars: int = 20_000
    review_chunks_max: int = 20
    # 图片字节磁盘缓存：parse 阶段把已抽取的图片按 content_hash 落盘，review 阶段
    # 反查做 VLM 抽检（免去调用方搬运字节）。物理位于 parse_cache_disk_dir 下，
    # 故同时受 parse_cache_disk_enabled 总开关约束。
    review_image_cache_enabled: bool = True
    review_image_cache_max: int = 12
    # images/ 下最多保留的文档目录数（有界 GC，按 mtime 最旧优先清理）。
    review_image_cache_max_docs: int = 200
    # /internal/review 线程池护栏（F-21）：build_review 是同步 LLM/VLM 链路，
    # 必须在受限线程池执行，不能占 FastAPI 事件循环。并发上限 + 队列上限
    # （上限和 = review_max_concurrency + review_queue_max），饱和即 503
    # REVIEW_BUSY；总截止 = review_llm_timeout_sec + review_vlm_timeout_sec。
    review_max_concurrency: int = 2
    review_queue_max: int = 8

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
    mineru_submit_per_minute: int = 50
    mineru_poll_per_minute: int = 1000
    mineru_daily_file_limit: int = 5000

    # LLM generate (cloud OpenAI-compatible)
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_timeout_sec: float = 120.0
    # Reasoning models (GLM-4.5 etc.) emit a hidden reasoning pass before the
    # answer, which inflates time-to-first-token. Set true to skip it for the
    # streaming answer path (verify answer quality with scripts/eval_answer.py).
    llm_disable_thinking: bool = False
    # 流式请求带上 stream_options.include_usage，让 provider 在最后一个 chunk 回传
    # prompt/completion token 数（OpenAI 兼容；智谱 glm 已验证支持）。
    # 若切换到不支持该参数的 provider，置 false 即可（此时 done 事件的 token 恒为 0）。
    llm_stream_include_usage: bool = True

    # P1: parse idempotency + VLM caption cache
    enable_parse_idempotency: bool = True
    parse_cache_max: int = 256
    vlm_caption_cache_max: int = 512
    retrieve_top_k_default: int = 8

    # Hybrid retrieve (dense + lexical + RRF)
    retrieve_overfetch_factor: int = 4
    retrieve_overfetch_max: int = 50
    # Base per-unit chunk budget. Raised 2 -> 4 (2026-10-06) after the v3
    # 85-item eval showed a budget of 2 truncated the answer chunk in
    # multi-section manuals (heading/summary chunks consumed the slots):
    # chunk_hit@5 0.9412 -> 0.9529, ndcg@5 0.8926 -> 0.9043, precision@5
    # 0.2729 -> 0.3600 (i.e. the extra slots carry *relevant*, not noise, chunks).
    retrieve_max_per_unit: int = 4
    # Adaptive per-unit chunk budget ceiling: when few distinct units are
    # recalled, allow more chunks per unit so multi-chunk manuals aren't
    # truncated out of the answer chunk (fixes "unit hit but chunk miss").
    retrieve_max_per_unit_cap: int = 8
    # Jaccard similarity above which overlapping chunks from the same unit are
    # deduplicated (near-duplicate window splits waste the per-unit budget).
    retrieve_chunk_dedup_jaccard: float = 0.85
    # Rank by best-chunk-per-unit first (debias multi-chunk manuals).
    # Off by default: unit-level first-chunk order can bury the true answer chunk.
    retrieve_unit_agg: bool = False
    # Unit-score aggregation used for unit ordering (and unit-only rerank):
    #   "best"  -> unit score = its single best chunk score (legacy)
    #   "topk"  -> weighted sum of top-k chunk scores (+ optional coverage bonus)
    # "topk" suppresses units whose only strength is one isolated high chunk.
    retrieve_unit_agg_mode: str = "best"
    retrieve_unit_agg_topk: int = 3
    retrieve_unit_agg_coverage_weight: float = 0.0
    retrieve_unit_agg_coverage_k: int = 3
    retrieve_enable_rerank: bool = True
    # Lightweight synonym/paraphrase expansion (query_expand.py).
    retrieve_query_expand: bool = True
    # Directed phenomenon -> error-code/jargon expansion (卡纸->E-01, WAN->光猫/网线, …).
    # Off by default; enabled only after the A/B in docs/retrieval-gate-decision.md §16.
    retrieve_query_expand_codes: bool = False
    # Additionally anchor the *rerank* stage (rule_rerank / intra-unit keyword order)
    # on the mapped error codes. High-precision: the demo E-01..E-08 codes exist
    # only in the error-code reference doc, so this cannot promote unrelated PDFs.
    retrieve_query_expand_codes_rerank: bool = False
    # rule_rerank boost weights (exact error-code/model/price tokens, Chinese bigram
    # overlap, generic token overlap). Defaults equal the historical hard-coded
    # constants so the default pipeline stays byte-identical to the baseline.
    retrieve_rule_rerank_exact_weight: float = 0.15
    retrieve_rule_rerank_bigram_weight: float = 0.01
    retrieve_rule_rerank_overlap_weight: float = 1e-3
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
    # BM25 parameters (k1 term saturation, b length normalization).
    lexical_k1: float = 1.5
    lexical_b: float = 0.75

    # Sparse retrieval via BGE-M3 lexical_weights (dense + sparse + lexical RRF).
    # Auto-degrades to empty when the embedder has no sparse support (cloud/fake).
    retrieve_sparse_enabled: bool = True
    retrieve_sparse_rrf_weight: float = 0.3
    retrieve_sparse_max_docs: int = 20000

    # Cold-start preheat + readiness (P0 availability fix).
    # The lexical/sparse indexes are process-local and built on first use; a
    # request that arrives before the index is built must NOT trigger an
    # unbounded in-request rebuild (that produced a guaranteed first-request
    # 504 on cold start). Instead we preheat on startup and degrade gracefully.
    retrieve_preheat_enabled: bool = True
    # Block startup until preheat finishes (or the timeout below). Default off:
    # the process serves degraded (dense/lexical) traffic immediately and /ready
    # gates orchestration instead of hanging boot.
    retrieve_preheat_blocking: bool = False
    retrieve_preheat_timeout_sec: float = 30.0
    # With blocking preheat, refuse startup if not ready within the timeout.
    retrieve_preheat_require_ready: bool = False
    # R-09: readiness policy when cold-start preheat does not complete (an index
    # ends in state "failed", e.g. the vector store was unreachable, or is still
    # running past ``retrieve_readiness_timeout_sec``).
    #   "fail"    -> /ready keeps returning 503: the instance is kept OUT of the
    #                load-balancer rotation until a later successful preheat.
    #                Refuse traffic rather than serve a half-built instance.
    #   "degrade" -> /ready returns 200 with status="degraded": dense (and any
    #                already-built lexical) still serve, and the router is allowed
    #                to send traffic to a degraded instance.
    # Default "fail" is the conservative choice for a cold replica; set "degrade"
    # when a warm dense path is preferable to dropping the replica.
    retrieve_readiness_on_preheat_failure: str = "fail"
    # R-09: bound (seconds) on how long an *incomplete* preheat keeps the instance
    # unready. 0 = unbounded (wait for preheat to finish). When exceeded, the
    # failure policy above applies. Ignored when preheat is disabled or the
    # deployment is multi-replica (both report ready without preheat).
    retrieve_readiness_timeout_sec: float = 0.0
    retrieve_preheat_lexical: bool = True
    retrieve_preheat_sparse: bool = True
    # Allow an in-request sparse cold sync (with timeout). Default False: an
    # unready sparse index is skipped (graceful degrade) rather than blocking.
    retrieve_sparse_sync_in_request: bool = False
    # Wall-clock budget for the in-request sparse sync (0 = follow store timeout).
    retrieve_sparse_sync_timeout_sec: float = 0.0
    # Skip the sparse path when the index is not ready (fail-open to dense/lexical).
    retrieve_sparse_skip_when_not_ready: bool = True
    # When the overall budget is exceeded, degrade optional stages to a partial
    # 200 instead of a hard 504 (default off: preserves the 504 contract).
    retrieve_overall_timeout_degrade: bool = False

    # Optional cross-encoder reranker (default off; heavy dependency, lazy load).
    retrieve_reranker_enabled: bool = False
    retrieve_reranker_model: str = "BAAI/bge-reranker-base"
    # Cross-encoder device ("cpu" / "cuda" / "cuda:0"); empty = library default.
    retrieve_reranker_device: str = ""
    retrieve_reranker_top_n: int = 20
    retrieve_reranker_weight: float = 0.4
    # When true (default) the cross-encoder only reorders *units*; each unit's
    # internal chunk order is preserved (no chunk-level scrambling). Set false to
    # restore the legacy behaviour that blends CE into score_fused globally
    # (measured net-negative: chunk_hit@5 -1.2pt, see §12).
    retrieve_reranker_unit_only: bool = True
    # Re-apply `keyword_aware_unit_order` before the unit-only cross-encoder
    # (restores intra-unit keyword order after embed_rerank's global sort).
    retrieve_reranker_regroup: bool = False
    # How the cross-encoder is blended into the unit ordering:
    #   "score" -> unit_score = agg(fused) + weight * max(0, CE_rep)
    #   "rank"  -> RRF of the fused unit ranking and the CE unit ranking
    #              (protects the fused top unit from a single weak CE verdict)
    retrieve_reranker_blend: str = "score"
    retrieve_reranker_rank_k: int = 60

    # Final structural pass: round-robin chunks across units so every top-k
    # prefix spans several units (a single mis-ranked unit can otherwise occupy
    # 4 of the 5 slots chunk_hit@5 looks at). Preserves unit + intra-unit order.
    retrieve_unit_interleave: bool = False
    # Chunks emitted per unit per interleave round: 1 = max unit diversity
    # (best chunk_hit@k, lowest precision@k); 2 = pairs (A1,A2,B1,B2,…).
    retrieve_unit_interleave_block: int = 1

    # Optional LLM query rewrite (default off; falls back to rule-based expansion).
    # Two modes (§15 / §16 of docs/retrieval-gate-decision.md):
    #   replace (default): the rewritten query *replaces* the original (legacy —
    #                      measured net-negative: @1 up but chunk_hit@5 down).
    #   extra:             the original stays the main path; the rewritten query is
    #                      added as an *extra recall path* fused into RRF with
    #                      `retrieve_query_rewrite_rrf_weight` (default 0.5;
    #                      set 0 to disable).
    retrieve_query_rewrite_enabled: bool = False
    retrieve_query_rewrite_extra_path: bool = False
    retrieve_query_rewrite_rrf_weight: float = 0.5
    retrieve_query_rewrite_timeout_sec: float = 3.0
    retrieve_query_rewrite_cache_max: int = 512

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
    # Dedicated cold-start rebuild pool size (lexical/sparse sync_from_store).
    # Isolated from the search pool so a stuck rebuild cannot occupy the threads
    # a live search needs (F-26).
    retrieve_sync_pool_size: int = 2
    # Bounded submission queue in front of the search/sync pools (F-26): when
    # the pool plus this queue is full, new retrieve work fails fast (503
    # RETRIEVE_STORE_OVERLOADED) instead of piling up behind a stuck downstream.
    retrieve_store_queue_max: int = 8
    # Bounded submission queue in front of the lexical/sparse branch pool
    # (F-37/R-09): when full, the optional branch degrades (dense still serves).
    retrieve_branch_queue_max: int = 16
    # R-04: bounded *isolation pool* for non-cancellable external calls (query
    # embedding, sparse embedding, cross-encoder rerank). Python cannot force-kill
    # a running thread, so these calls are given an explicit concurrency cap and
    # a bounded wait derived from the remaining request deadline. Saturation
    # fails fast instead of queueing unbounded behind a stuck downstream.
    retrieve_external_pool_size: int = 8
    retrieve_external_queue_max: int = 16
    # R-04: per-call wait budget for the query-embedding call (0 = derive only
    # from the remaining request deadline). Clamped by the remaining deadline so
    # a single slow embed cannot outlive `retrieve_overall_timeout_sec`.
    retrieve_embed_timeout_sec: float = 0.0
    # R-04: per-call wait budget for the cross-encoder rerank (0 = remaining only).
    retrieve_rerank_timeout_sec: float = 0.0
    # Durable ledger of lexical/sparse index repairs (R-10): a failed incremental
    # sync after a Milvus publish is recorded here (not silently swallowed) and a
    # full rebuild from the active chunks clears it. Shared path across replicas.
    #
    # R-03: markers are tagged with a worker identity and only the owning worker
    # clears them; persistence uses a cross-process lock + read-merge-write. Set
    # RETRIEVE_INDEX_REPAIR_WORKER_ID (or AI_REPLICA_ID) to a distinct value per
    # replica — otherwise workers on the same host share an identity. A ledger
    # write failure is surfaced as degraded (health) and never silently ignored.
    retrieve_index_repair_path: str = ".data/index_repair.json"
    # R-08: fail-closed startup gate for the index-repair worker identity. The
    # default identity is now ``host:pid`` (distinct per process), so concurrent
    # same-host workers no longer share marker ownership. Set this true to make
    # the process *prove* it was given a deliberate, deployment-level stable id
    # (``RETRIEVE_INDEX_REPAIR_WORKER_ID`` / ``AI_REPLICA_ID``): startup fails if
    # none is configured. Recommended for any multi-worker / multi-replica topology.
    retrieve_require_explicit_worker_id: bool = False
    # R-07: periodic, single-instance retry of the pending index-repair ledger.
    # A one-shot background rebuild is not enough: if the process restarts after
    # the marker is written (or the single attempt fails), the marker would sit
    # forever. Startup reads the pending ledger and this worker retries with
    # exponential backoff, recording last_attempt / last_error for health.
    retrieve_index_repair_retry_enabled: bool = True
    retrieve_index_repair_retry_base_sec: float = 5.0
    retrieve_index_repair_retry_max_sec: float = 300.0
    # 0 = retry forever (until success or the marker is cleared).
    retrieve_index_repair_retry_max_attempts: int = 0
    # R-06: the cross-process active-map lock must FAIL CLOSED when it cannot be
    # acquired (timeout / missing dir / permission) so a publish/delete errors and
    # the caller retries, instead of silently writing without mutual exclusion.
    # R-02: file write/replace failures likewise raise ActiveMapPersistenceError and
    # are never swallowed; publish persists the map before deleting the old version.
    # KNOWN LIMITATION: a lock file is mutual exclusion, not CAS — strict
    # multi-replica consistency needs MySQL/Redis CAS (documented in store.py).
    retrieve_active_map_fail_closed: bool = True
    # Cold-start lexical sync_from_store wall-clock budget (0 = follow store timeout).
    retrieve_lexical_sync_timeout_sec: float = 0.0
    # Max length of unit_ids / deny_unit_ids lists on retrieve.
    retrieve_max_unit_ids: int = 500
    # When true, drop hits missing required ACL metadata fields (fail-closed; no full-corpus).
    retrieve_acl_fields_required: bool = False
    # Structured JSON log line per retrieve (no question/chunk text).
    retrieve_struct_log: bool = True
    # Process-local LRU for query embeddings (0 = disabled). Enabled by default
    # to cut repeat-question embed latency and vendor calls. Also governs the
    # sparse query-weight cache (same key discipline, separate store).
    retrieve_embed_cache_max: int = 1024
    # Process-local LRU for rule-based query expansion (expand_query_texts /
    # expand_terms_for_lexical). Pure text transform; bounded to avoid unbounded
    # growth. 0 = disabled (recompute every call).
    retrieve_query_expand_cache_max: int = 1024

    # Data preprocessing: split wide markdown tables into row-granular chunks
    # (each carries the header), improving error-code / table-detail recall.
    table_row_chunk: bool = True
    # Max data rows per table chunk when row-level splitting is enabled.
    table_row_group: int = 3

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
    # Prompt-side context packing (dedup + budget + [n] citation numbering).
    generate_pack_max_chars: int = 6000
    generate_pack_per_chunk_max: int = 1500
    generate_pack_jaccard: float = 0.85
    # Phase A2.1: stricter citation instruction (only cite directly supporting
    # materials). Default False keeps the historical prompt byte-identical, so it
    # is safe to A/B in one process by flipping this flag.
    generate_citation_strict: bool = False
    # Phase A2.2: run the prompt-side context packer (dedup + per-chunk cap +
    # budget + optional item cap) inside /internal/generate. Default False keeps
    # the historical verbatim-context behaviour unchanged.
    generate_pack_enabled: bool = False
    generate_pack_max_items: int = 0  # 0 = no item cap
    # 闲聊/通用问答兜底：AI 侧的权威闸门（opt-in）。关闭时，即使上游 /internal/generate
    # 下发 mode="chat"，也一律按 "kb" 处理（与 Java 侧 ai.chat-fallback-enabled 双保险）。
    generate_chat_fallback_enabled: bool = False
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

    # ---- Session memory (multi-turn context; MongoDB) ----
    # 总开关（opt-in）。关闭时 AI 侧零会话状态、零 Mongo 依赖，行为与历史一致。
    session_memory_enabled: bool = False
    session_mongo_uri: str = "mongodb://127.0.0.1:27017"
    session_mongo_db: str = "aftersales_kb_ai"
    session_mongo_collection: str = "chat_sessions"
    # serverSelection / 操作超时；保持小值，Mongo 挂掉也不能拖慢问答主链路。
    session_mongo_timeout_ms: int = 800
    # 会话文档 TTL（每次写入刷新）。
    session_memory_ttl_days: int = 30
    # 回灌 Prompt 的最近轮数（**ACL 过滤之后**再截断）。
    session_history_max_turns: int = 4
    # 历史块字符预算；超预算从最旧轮开始丢。
    session_history_max_chars: int = 2000
    # 检索期用会话历史做 query 改写（指代消解）。
    retrieve_session_rewrite_enabled: bool = False
    retrieve_session_rewrite_timeout_sec: float = 3.0

    def _validate_internal_token(self) -> None:
        """F-05 prod credential gate: reject missing/sample/short INTERNAL_TOKEN.

        The documented sample value is public in the repository, so accepting
        it in prod exposes every /internal/* route (parse, retrieve, generate,
        vector delete) to anyone who can reach the port. Keep the sample set in
        sync with ``app.main._DEFAULT_TOKENS``.
        """
        token = (self.internal_token or "").strip()
        if not token or token in _SAMPLE_INTERNAL_TOKENS:
            raise ValueError(
                "APP_PROFILE=prod requires a strong INTERNAL_TOKEN: it is empty or "
                "the documented sample value; set a unique long random token"
            )
        min_len = int(self.internal_token_min_length or 0)
        if min_len > 0 and len(token) < min_len:
            raise ValueError(
                f"APP_PROFILE=prod requires INTERNAL_TOKEN length >= {min_len}, "
                f"got {len(token)}; set a unique long random token"
            )

    @model_validator(mode="after")
    def _validate_config_combos(self) -> "Settings":
        """Reject illegal *combinations* of flags (M2b), not just single values.

        Two invariants that individually-valid flags can still violate together:
          * preheat_require_ready needs preheat_blocking (readiness is only
            observable when preheat actually blocks);
          * strict_prod needs require_acl (fail-closed retrieval).

        The strict_prod/require_acl pair is also enforced at startup by
        ``app.main._enforce_strict_prod`` — that gate owns the historical
        ``RuntimeError`` wording asserted by the regression suite, so this
        validator only adds the check when an explicit ``APP_PROFILE`` is chosen.
        Without a profile the legacy startup gate remains the single source of
        truth, which keeps the existing construction contract intact.
        """
        profile = (self.app_profile or "").strip().lower()
        if profile not in {"", "demo", "prod"}:
            raise ValueError(
                f"invalid APP_PROFILE={self.app_profile!r}; expected one of: demo, prod"
            )

        # Aggregate profile presets. An explicitly-provided field (env or kwarg)
        # always wins, so a profile never silently overrides a deliberate value.
        if profile == "prod":
            if "retrieve_strict_prod" not in self.model_fields_set:
                self.retrieve_strict_prod = True
            if "retrieve_require_acl" not in self.model_fields_set:
                self.retrieve_require_acl = True
            # F-05: fail-closed credential gate. A prod process must never boot
            # with a missing / documented-sample / too-short INTERNAL_TOKEN —
            # the old posture only logged a warning (and depended on a separate
            # opt-in flag), leaving the repo-public default usable in production.
            self._validate_internal_token()
        elif profile == "demo":
            if "retrieve_strict_prod" not in self.model_fields_set:
                self.retrieve_strict_prod = False
            if "retrieve_require_acl" not in self.model_fields_set:
                self.retrieve_require_acl = False

        if self.retrieve_preheat_require_ready and not self.retrieve_preheat_blocking:
            raise ValueError(
                "invalid config combination: RETRIEVE_PREHEAT_REQUIRE_READY=true requires "
                "RETRIEVE_PREHEAT_BLOCKING=true"
            )
        # R-09: readiness failure policy must be one of the two documented modes.
        readiness_policy = (self.retrieve_readiness_on_preheat_failure or "").strip().lower()
        if readiness_policy not in {"fail", "degrade"}:
            raise ValueError(
                "invalid RETRIEVE_READINESS_ON_PREHEAT_FAILURE="
                f"{self.retrieve_readiness_on_preheat_failure!r}; expected 'fail' or 'degrade'"
            )
        if self.retrieve_readiness_timeout_sec < 0:
            raise ValueError("invalid RETRIEVE_READINESS_TIMEOUT_SEC: must be >= 0 (0 = unbounded)")
        if profile and self.retrieve_strict_prod and not self.retrieve_require_acl:
            raise ValueError(
                "invalid config combination: RETRIEVE_STRICT_PROD=true requires "
                "RETRIEVE_REQUIRE_ACL=true"
            )
        # F-14 / R-06: 多副本安全开关不能只靠"关闭词法分支"。active-version 映射必须是
        # 一个 *共享* 存储上的**绝对路径**：相对路径按各进程 cwd 解析，几乎必然不是同一份
        # 文件，却会被误当成"已共享"。这里拒绝默认本地路径与相对路径，避免
        # "以为开了 multi_replica_safe 就多副本安全"的静默错误部署。
        #
        # 注意：路径"看起来是共享"不等于它真的共享（无法在配置期验证挂载）。真正的
        # 多副本一致性由 store 侧保证：跨进程文件锁 + 读合并写（并发发布不丢映射）+ 读时
        # 按 mtime 重载（B 无重启即可见 A 的发布）；词法/稀疏索引在多副本下强制关闭
        # （见 pipeline 的 dense-only 门）。
        if self.retrieve_multi_replica_safe:
            map_path = (self.milvus_active_map_path or "").strip()
            if not map_path or map_path == _DEFAULT_ACTIVE_MAP_PATH:
                raise ValueError(
                    "invalid config combination: RETRIEVE_MULTI_REPLICA_SAFE=true requires a shared "
                    "active-version store — set MILVUS_ACTIVE_MAP_PATH to a path on shared storage "
                    "visible to every replica (closing the lexical branch alone is not multi-replica "
                    "support; see audit F-14/R-06)"
                )
            if not os.path.isabs(map_path):
                raise ValueError(
                    "invalid config combination: RETRIEVE_MULTI_REPLICA_SAFE=true requires an "
                    "ABSOLUTE MILVUS_ACTIVE_MAP_PATH; a relative path resolves per-process cwd and "
                    "is not a shared store (see audit R-06)"
                )
            # R-06: multi-replica sharing of the active map is only safe when a
            # lock failure aborts the write (fail-closed). Fail-open + shared file
            # silently loses active-version updates under contention.
            if not self.retrieve_active_map_fail_closed:
                raise ValueError(
                    "invalid config combination: RETRIEVE_MULTI_REPLICA_SAFE=true requires "
                    "RETRIEVE_ACTIVE_MAP_FAIL_CLOSED=true — a shared active-map file with a "
                    "fail-open lock can silently lose active-version updates (see audit R-06)"
                )
            # R-03/R-08: strict multi-replica also needs a DISTINCT index-repair
            # worker identity per replica. The default identity is now host:pid
            # (distinct per process), but a deliberate multi-replica deployment
            # must still prove it configured a *stable* deployment-level owner —
            # require an explicit id, fail closed, mirroring the active-map guard
            # above.
            if not explicit_worker_id():
                raise ValueError(
                    "invalid config combination: RETRIEVE_MULTI_REPLICA_SAFE=true requires an "
                    "explicit RETRIEVE_INDEX_REPAIR_WORKER_ID (or AI_REPLICA_ID) — a stable, "
                    "deployment-level identity is required so one replica never clears another's "
                    "index-repair markers (see audit R-03/R-08)"
                )
        # R-08: standalone fail-closed gate for multi-worker (single-host) topologies
        # that do NOT set RETRIEVE_MULTI_REPLICA_SAFE. When enabled, the process
        # refuses to start without an explicit, deployment-level stable worker id
        # (the host:pid default is unique but not stable across restarts).
        if self.retrieve_require_explicit_worker_id and not explicit_worker_id():
            raise ValueError(
                "RETRIEVE_REQUIRE_EXPLICIT_WORKER_ID=true requires an explicit "
                "RETRIEVE_INDEX_REPAIR_WORKER_ID (or AI_REPLICA_ID); the default host:pid "
                "identity is not a deployment-level stable owner (see audit R-08)"
            )
        return self


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
