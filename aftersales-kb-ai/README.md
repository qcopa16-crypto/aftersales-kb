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

**交付状态**：入库 / hybrid 检索 / 企业级加固（限流、超时、multi-replica 门禁、可观测）已交付；离线回归 `pytest tests --ignore=tests/test_langgraph_real.py`（见 CI）。

## LangGraph 流水线

```text
Ingest:  START → resolve_source → extract_text → vlm_captions → chunk_and_quality → END
QA:      START → retrieve → generate → END
```

- 节点实现复用 `app/services/*` 与 `app/utils/*`（MinerU / VLM / chunker / LLM / Milvus / circuit / limits）
- 入口：`app/graphs/ingest_graph.py`、`app/graphs/qa_graph.py`
- `parse_pipeline.run_parse` 为兼容门面，内部走 graph
- 路由层：`app/api/*`；Pydantic 契约：`app/schemas/*`

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 健康检查（limits / circuits / metrics） |
| GET | /metrics | Prometheus 文本指标 |
| POST | /internal/parse | **生产主路径** 同步解析（Java 编排） |
| POST | /internal/parse/async | 调试/兼容 Init 异步解析，**非生产主路径** |
| GET | /internal/tasks/{task_id} | **Poll** 任务进度与结果（配合 async） |
| DELETE | /internal/tasks/{task_id} | 取消任务 |
| POST | /internal/embed | 向量化入库（支持 `resume_from_index` / `ingest_id`） |
| POST | /internal/retrieve | 问题 → Top-K **Hybrid** chunks（dense+lexical+RRF，可执行 acl_filter） |
| POST | /internal/generate | SSE 流式回答（仅用调用方 contexts） |
| POST | /internal/vlm/describe | 单图 → 文字 |
| DELETE | /internal/units/{id} | 删向量 |

鉴权：`X-Internal-Token`（`/health` 除外；`/metrics` 默认同鉴权，`METRICS_PUBLIC=true` 时调试放行）

**生产主路径**：Java Worker 负责任务态/阶段机/质检编排 → 同步 `/internal/parse` → `/internal/embed`。`/internal/parse/async` 仅调试/兼容，任务存进程内存、重启即失。

**权限与进程语义（契约）**
- **权限决策在 Java**；Python retrieve 只**执行**调用方下发的 `acl_filter`（fail-closed），不做身份推断。
- `/internal/generate` 仅使用调用方传入的 authorized `contexts`，**禁止内部二次检索**。
- retrieve 主路径为 **多路 Hybrid**：dense + 进程内 lexical/BM25 + RRF；默认开启，lexical 故障可配置降级为 dense（`degraded_to_dense` + `retrieve.hybrid_degraded`）。
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
- `unit_lock` / circuit / parse_cache / embed_partial / task_store / **lexical index** / **Milvus active map** 均为 **process-local**；多副本需单写者或外置锁（或 `RETRIEVE_MULTI_REPLICA_SAFE=true`）。
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
# 单元/接口回归（memory + fake embed，适合 CI/无外网）
# 真实链路 test_langgraph_real 需要 Milvus+BGE，默认 ignore
python -m pytest tests -q --ignore=tests/test_langgraph_real.py

# 架构门禁：ai 不得 import app；core 不得依赖 app/ai
python -m pytest tests/test_architecture_boundaries.py -q

# LangGraph 真实链路（无 Mock；需 Milvus + 本地 BGE + MinerU + 可选 VLM/LLM）
python -m pytest tests/test_langgraph_real.py -v
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
```

## 文档

| 文档 | 内容 |
|---|---|
| [架构设计](docs/architecture.md) | 模块、LangGraph、数据流 |
| [内部接口](docs/ai-service-api.md) | `/internal/*` 契约（error_code / ingest_id / hash / 超时） |
| [配置说明](docs/config.md) | 环境变量 |
| [实施任务](docs/implementation.md) | 里程碑与验收 |
| [无状态加固规格](docs/compose/spec/ingest-python-stateless-hardening.md) | 本轮 P0–P2 交付 |

业务侧：`../docs/api/integration.md`、`../代码设计规范.md`、`../DESIGN.md`
