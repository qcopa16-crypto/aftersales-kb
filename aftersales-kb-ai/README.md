# aftersales-kb-ai

售后知识库 **FastAPI AI 服务**。只做 AI 能力，不做登录与业务权限。

**AI 编排：LangGraph**（入库 ingest 图 + 问答 qa 图）；HTTP 契约保持不变。

## 模型策略

| 能力 | 方式 |
|---|---|
| Embedding | **本地 BGE-M3 或 云**，`EMBED_PROVIDER` 切换 |
| LLM | **仅云上**（OpenAI 兼容，如智谱 glm） |
| VLM | **仅云上**（扫描件图注、铭牌、故障图） |

推荐运行环境：Anaconda **`shopkeeper-ai`**（含 FlagEmbedding + torch CUDA）。

## 职责

| 做 | 不做 |
|---|---|
| 文档解析、切片、质检信号 | 用户登录 / JWT |
| Embedding 入库（local/cloud） | 数据权限判断 |
| 向量检索 | 知识单元主数据 CRUD |
| 云上 LLM 流式生成 | FAQ 审核 / 看板 |
| 云上 VLM 图文理解 | 业务日志落库（Java 记） |
| 删除向量、任务轮询/取消 | 业务 Job 持久化（Java `ingest_jobs`） |

## 在系统中的位置

```text
前端 → Java 业务服务 → 本服务 /internal/* → 向量库 + 云模型
```

前端 **不直连** 本服务；权限过滤在 Java 完成。

**状态语义**：AI **无业务主数据**（知识单元 / 权限 / 任务态归 Java）；但**存在需要备份与恢复的运行状态**——Milvus 活跃版本 map（`MILVUS_ACTIVE_MAP_PATH`）与索引修复 ledger，位于 compose 的 `ai-data` 卷（容器内 `/app/.data`）。这些状态**必须与 Milvus 数据版本保持一致**，否则重启后过滤失效或辅助索引状态误判。其余进程内状态（`unit_lock` / circuit / lexical/sparse 索引 / parse_cache / task_store 等）为 process-local，多副本需单写者或外置（见下文「权限与进程语义」）。

**交付状态**：入库 / hybrid 检索 / 企业级加固（限流、超时、multi-replica 门禁、可观测）已交付；离线回归 `pytest tests`（默认 663 passed，见 CI）。真实链路（真实 Milvus hybrid 三路检索、真实 BGE 嵌入 dim=1024、真实 MinerU 云解析、真实云 LLM/VLM 质检、真实 Mongo 会话、真实向量删除）已实测通过。

## LangGraph 流水线

```text
Ingest:  START → resolve_source → extract_text → vlm_captions → chunk_and_quality → END
QA:      START → retrieve → generate → END
```

- 节点实现复用 `ai/ingest/*` / `ai/retrieve/*` / `ai/generate/*` / `ai/embed/*`（MinerU / VLM / chunker / LLM / Milvus / circuit / limits）
- 入口：`ai/graphs/ingest_graph.py`、`ai/graphs/qa_graph.py`（另有调试路径 `ai/graphs/retrieve_graph.py`）
- `ai/ingest/parse.py::run_parse` 为兼容门面，内部走 graph
- 路由层：`app/api/*`；Pydantic 契约：`schemas/*`（`app` 与 `ai` 共用）

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | liveness 探针（恒 200；limits / circuits / metrics / index_repair） |
| GET | /ready | readiness 探针（受冷启动 preheat 门控；未就绪 503） |
| GET | /metrics | Prometheus 文本指标 |
| POST | /internal/parse | **生产主路径** 同步解析（Java 编排） |
| POST | /internal/parse/async | 调试/兼容 Init 异步解析，**非生产主路径** |
| GET | /internal/tasks/{task_id} | **Poll** 任务进度与结果（配合 async） |
| DELETE | /internal/tasks/{task_id} | 取消任务 |
| POST | /internal/review | 入库质检三档判定（规则 → LLM → VLM；仅回 verdict/reasons） |
| POST | /internal/embed | 向量化入库（支持 `resume_from_index` / `ingest_id`） |
| POST | /internal/retrieve | 问题 → Top-K **Hybrid** chunks（dense+lexical+sparse+RRF，可执行 acl_filter） |
| POST | /internal/generate | SSE 流式回答（仅用调用方 contexts） |
| POST | /internal/session/append | 追加一轮会话记忆（FAQ/澄清等非 generate 轮次；best-effort） |
| POST | /internal/vlm/describe | 单图 → 文字 |
| DELETE | /internal/units/{id} | 删向量 |

鉴权：`X-Internal-Token`（`/health`、`/ready` 除外；`/metrics` 默认同鉴权，`METRICS_PUBLIC=true` 时调试放行）

**生产主路径**：Java Worker 负责任务态/阶段机/质检编排 → 同步 `/internal/parse` → `/internal/embed`。`/internal/parse/async` 仅调试/兼容，任务存进程内存、重启即失。

**权限与进程语义（契约）**
- **权限决策在 Java**；Python retrieve 只**执行**调用方下发的 `acl_filter`（fail-closed），不做身份推断。
- `/internal/generate` 仅使用调用方传入的 authorized `contexts`，**禁止内部二次检索**。
- retrieve 主路径为 **多路 Hybrid**：dense + 进程内 lexical/BM25 + 进程内 sparse（BGE-M3 `lexical_weights`）+ RRF；默认开启，lexical/sparse 故障可配置降级为 dense（`degraded_to_dense` + `retrieve.hybrid_degraded`）。
- 响应审计字段：`score_dense` / `score_lexical` / `score_fused` / `retrieval_paths` / `acl_applied` / `hybrid` / `degraded_to_dense`。
- **生产 fail-closed**：`RETRIEVE_REQUIRE_ACL=true` + `RETRIEVE_STRICT_PROD=true`（启动硬门）+ `EMBED_REQUIRE_ACL_FIELDS=true`（Java 恒传后开启）。
- dense `store.search` 有软超时（`RETRIEVE_STORE_TIMEOUT_SEC`）与独立熔断 `retrieve_store`；全流程墙钟预算 `RETRIEVE_OVERALL_TIMEOUT_SEC`，超时 → 504 `RETRIEVE_TIMEOUT`。
- Milvus 活跃版本写入 `MILVUS_ACTIVE_MAP_PATH`（单写者 JSON），重启后可恢复过滤；lexical 评分上限 `RETRIEVE_LEXICAL_MAX_DOCS`。
- **多副本**：`RETRIEVE_MULTI_REPLICA_SAFE=true` → 仅 dense（`hybrid=false`），ACL post-filter 仍 fail-closed；生产多副本必须开启，直到 lexical/active 外置。默认 `false`（单副本/本地 hybrid）。
- **bulkhead / 限流**：`RETRIEVE_MAX_CONCURRENCY`（默认 32，0=关）与 `RETRIEVE_QPS_LIMIT`（默认 0=关）；超限 429 `RATE_LIMIT`（不可重试）。
- store 超时使用**共享**线程池（`RETRIEVE_STORE_POOL_SIZE`，默认 4），不再 per-request 新建；worker 可能仍被慢客户端占用（残留风险，文档化）。
- 分阶段耗时指标：`retrieve.stage_embed_ms` / `stage_store_ms` / `stage_lexical_ms` / `stage_fuse_ms`；cap 时 `retrieve.lexical_capped`；多副本模式 `retrieve.multi_replica_dense_only`。
- **P2 运维**：`RETRIEVE_ACL_FIELDS_REQUIRED` 缺 ACL 元数据丢弃；`RETRIEVE_STRUCT_LOG` 结构化 JSON 日志（无 question 全文）；`retrieve.acl_empty` / `retrieve.total_requests`；可选 `RETRIEVE_EMBED_CACHE_MAX` 查询向量 LRU（默认 0）。
- 生产 HTTP 检索走 flat `run_retrieve_pipeline`；`retrieve_graph` 为调试/组合路径，已对齐 multi_replica dense-only。
- 负载冒烟（非 CI）：`scripts/bench_retrieve_smoke.py`。
- 日志只记 `q_hash`（question SHA-256 前 12 hex），不落 question 全文。
- `unit_lock` / circuit / parse_cache / embed_partial / task_store / **lexical index** / **sparse index** 为 **process-local**；**Milvus active map** / **index repair ledger** 为**单写者持久化状态**（落 `ai-data` 卷、需与 Milvus 数据版本一致，见上文「状态语义」）。多副本需单写者或外置锁（或 `RETRIEVE_MULTI_REPLICA_SAFE=true`）。
- parse 缓存键 **不含 unit_id**；`cache_hit=true` 时响应里的 `unit_id` 为本次请求回显覆盖。

## 本地启动

```bash
cd aftersales-kb-ai
copy .env.example .env   # Windows；macOS/Linux 用 cp
# 编辑 .env（见 docs/config.md 与仓库根 README）

# 推荐 Anaconda shopkeeper-ai（FlagEmbedding + torch）
pip install -r requirements.txt

uvicorn main:app --host 0.0.0.0 --port 8000
# 或：python main.py
```

最小本地冒烟（无外部依赖）：

```bash
# .env 中 VECTOR_STORE=memory 且 EMBED_PROVIDER=fake 时
curl http://127.0.0.1:8000/health
```

## 测试

```bash
# 单元/接口回归（memory + fake embed，适合 CI/无外网）→ 默认离线 663 passed
# 真实链路 test_langgraph_real 由 conftest.py collect_ignore 默认不收集
python -m pytest tests -q

# 架构门禁：ai 不得 import app；core 不得依赖 app/ai
python -m pytest tests/test_architecture_boundaries.py -q

# Lint / 格式（CI 双命令，须通过）
ruff check .
ruff format --check .

# LangGraph 真实链路（无 Mock；需 Milvus + 本地 BGE + MinerU + 可选 VLM/LLM）
# REAL_CHAIN_ENABLED=1 时收集 670 / 运行 7（其余用例在真实链路 profile 下跳过）
REAL_CHAIN_ENABLED=1 python -m pytest tests/test_langgraph_real.py -v
```

CI：`.github/workflows/ai-ci.yml`（仓库根）在 push/PR 时跑上述离线回归。  
安装建议：`pip install -r requirements.txt -c requirements.lock.txt`（可重复构建）。

## 生产推荐环境

```text
INTERNAL_TOKEN=<long-random>
REFUSE_ON_DEFAULT_INTERNAL_TOKEN=true
RETRIEVE_REQUIRE_ACL=true
RETRIEVE_STRICT_PROD=true
RETRIEVE_HYBRID_ENABLED=true
RETRIEVE_HYBRID_DEGRADE_TO_DENSE=true
RETRIEVE_MULTI_REPLICA_SAFE=true
EMBED_REQUIRE_ACL_FIELDS=true
RETRIEVE_STORE_TIMEOUT_SEC=5
RETRIEVE_OVERALL_TIMEOUT_SEC=8
RETRIEVE_LEXICAL_MAX_DOCS=5000
RETRIEVE_MAX_CONCURRENCY=32
RETRIEVE_ACL_FIELDS_REQUIRED=true
RETRIEVE_STRUCT_LOG=true
RETRIEVE_EMBED_CACHE_MAX=0

# 检索效果固化配置（详见 docs/retrieval-regression-runbook.md §7）
RETRIEVE_QUERY_EXPAND_CODES=true
RETRIEVE_QUERY_EXPAND_CODES_RERANK=true
RETRIEVE_EMBED_RERANK_WEIGHT=0.65
RETRIEVE_UNIT_INTERLEAVE=true
RETRIEVE_LEXICAL_RRF_WEIGHT=0.8
# 可选 CE 单元重排（需本地 bge-reranker-base；缺失自动降级为「交错 + lex0.8」）
# RETRIEVE_RERANKER_ENABLED=true
# RETRIEVE_RERANKER_MODEL=<本地路径>
```

## 文档

| 文档 | 内容 |
|---|---|
| [架构设计](docs/architecture.md) | 模块、LangGraph、数据流、部署 |
| [AI 能力设计](docs/ai-design.md) | 落到演示语料的解析/切片/检索/生成/评测设计 |
| [内部接口](docs/ai-service-api.md) | `/internal/*` 契约（error_code / ingest_id / hash / 超时） |
| [配置说明](docs/config.md) | 环境变量 |
| [健康探针与 readiness](docs/readiness-and-health.md) | `/health` / `/ready` 语义、preheat、worker 身份 |
| [Milvus schema 兼容](docs/milvus-version-schema.md) | `version_id` / `embed_model` 与迁移 |
| [入库质检门禁](docs/ingest-quality-gate.md) | 内容覆盖指标与 soft/hard 门禁 |
| [检索回归 Runbook](docs/retrieval-regression-runbook.md) | 一键回归与门禁 |
| [检索门禁决策](docs/retrieval-gate-decision.md) | 多轮 A/B 与固化结论 |
| [检索分环节分析](docs/retrieval-analysis.md) | 早期分环节评估（历史记录） |
| [评测口径校准](docs/eval-credibility-calibration.md) | evidence_hit 等新口径设计 |
| [AI 优化分析](docs/ai-optimization-analysis.md) | 四层能力盘点与路线图 |

业务侧：`../docs/api/integration.md`、`../代码设计规范.md`、`../DESIGN.md`
