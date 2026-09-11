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
| POST | /internal/parse | 同步解析（小文档） |
| POST | /internal/parse/async | **Init** 异步解析，返回 `task_id` |
| GET | /internal/tasks/{task_id} | **Poll** 任务进度与结果 |
| DELETE | /internal/tasks/{task_id} | 取消任务 |
| POST | /internal/embed | 向量化入库（支持 `resume_from_index`） |
| POST | /internal/retrieve | 问题 → Top-K chunks |
| POST | /internal/generate | SSE 流式回答（仅用调用方 contexts） |
| POST | /internal/vlm/describe | 单图 → 文字 |
| DELETE | /internal/units/{id} | 删向量 |

鉴权：`X-Internal-Token`（`/health` 除外）

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
python -m pytest tests -q --ignore=tests/test_langgraph_real.py

# LangGraph 真实链路（无 Mock；需 Milvus + 本地 BGE + MinerU + 可选 VLM/LLM）
python -m pytest tests/test_langgraph_real.py -v
```

真实链路依赖见 `.env` 与 [配置说明](docs/config.md)。密钥勿提交 Git。

## 文档

| 文档 | 内容 |
|---|---|
| [架构设计](docs/architecture.md) | 模块、LangGraph、数据流 |
| [内部接口](docs/ai-service-api.md) | `/internal/*` 契约 |
| [配置说明](docs/config.md) | 环境变量 |
| [实施任务](docs/implementation.md) | 里程碑与验收 |

业务侧：`../docs/api/integration.md`、`../代码设计规范.md`、`../DESIGN.md`
