# 企业 IT 设备售后知识库平台（aftersales-kb）

面向 IT 设备售后场景的企业知识库：文档导入、细粒度数据权限、AI 鉴权问答、FAQ 沉淀与知识缺口。

## 项目定位

| 维度 | 说明 |
|---|---|
| 业务场景 | IT 设备（路由器/交换机/笔记本/打印机）售后知识管理与智能问答 |
| 目标用户 | 系统管理员、知识管理员、售后工程师、销售人员、财务人员 |
| 核心价值 | 在数据权限安全的前提下，用自然语言获取售后制度、SOP、产品资料 |
| 架构 | **Java 业务服务 + FastAPI AI 服务** 双服务 |

## 仓库结构

```text
aftersales-kb/
├── src/main/java/com/shuidun/kb/     # Java Spring Boot（业务编排 / 权限 / 主数据）
├── docs/
│   ├── api/                          # 对前端接口、Java↔AI 集成契约
│   ├── services/                     # 系统架构、领域、功能、库表、路线
│   └── demo-knowledge/               # 演示知识语料
├── aftersales-kb-ai/                 # FastAPI AI 服务（独立 Python 工程）
│   ├── app/                          # HTTP 薄层（api/internal 按域拆分）
│   ├── ai/                           # 领域：ingest / retrieve / generate / embed / graphs
│   ├── schemas/                      # DTO 契约（app 与 ai 共用）
│   ├── core/                         # 配置、错误码、熔断、trace 等横切
│   ├── tests/                        # 离线回归 + 架构门禁
│   ├── scripts/                      # 真实验收 / 运维 / bench
│   └── requirements.txt + requirements.lock.txt
├── 代码设计规范.md                    # AI 代码生成契约（优先级高）
├── DESIGN.md                         # 入库流水线产品/流程规范
└── pom.xml
```

AI 服务细节见 `aftersales-kb-ai/README.md` 与 `aftersales-kb-ai/docs/`。

## 文档导航

| 文档 | 内容 |
|---|---|
| [代码设计规范.md](代码设计规范.md) | AI 代码生成契约（最高优先级） |
| [DESIGN.md](DESIGN.md) | 入库流水线产品/流程规范 |
| [docs/services/kb-design-spec.md](docs/services/kb-design-spec.md) | 领域模型、权限、状态机 |
| [docs/services/architecture.md](docs/services/architecture.md) | **系统**总体架构（Java+AI） |
| [docs/api/api.md](docs/api/api.md) / [integration.md](docs/api/integration.md) | 对前端 API / Java↔AI 契约 |
| [aftersales-kb-ai/docs/architecture.md](aftersales-kb-ai/docs/architecture.md) | **AI 服务**架构与硬约束 |
| [aftersales-kb-ai/docs/ai-service-api.md](aftersales-kb-ai/docs/ai-service-api.md) | `/internal/*` 契约 |
| [aftersales-kb-ai/docs/config.md](aftersales-kb-ai/docs/config.md) | 环境变量 |

## 核心能力

1. **组织权限**：用户 / 部门树 / 角色 / 操作权限（菜单与按钮级）
2. **知识维护**：单文件与批量导入、解析切片、知识单元 CRUD
3. **四维数据权限**：全局 / 部门 / 角色 / 个人，OR 组合放行
4. **AI 鉴权问答**：召回后强制权限过滤，无权限内容不进 Prompt
5. **FAQ 沉淀**：高频问题推荐、人工审核、缓存加速
6. **知识缺口**：未命中/低置信问题聚合
7. **数据看板**：访问量、热度榜、Token、响应耗时

## 服务分工

| 服务 | 技术 | 职责 |
|---|---|---|
| **业务** `aftersales-kb` | Spring Boot · Security · JWT · MyBatis | 登录、组织、知识 CRUD、数据权限、FAQ、缺口、看板、日志、编排 AI |
| **AI** `aftersales-kb-ai` | FastAPI · LangGraph · 向量库 · 云模型 | 解析切片、Embedding（本地/云）、检索、LLM 流式、VLM |

**前端只访问 Java**；Java 内网调用 FastAPI `/internal/*`（`X-Internal-Token`）。

## 模型策略

| 能力 | 方式 |
|---|---|
| Embedding | **本地 BGE-M3 或 云**，`EMBED_PROVIDER` 切换 |
| LLM | **仅云上**（OpenAI 兼容，如智谱 glm / DeepSeek） |
| VLM | **仅云上**（扫描件、设备图片） |

## 技术栈

| 层 | 技术 |
|---|---|
| 业务服务 | Spring Boot 4.0.x · Spring Security · JWT · MyBatis · Java 17 |
| AI 服务 | FastAPI · Pydantic v2 · LangGraph · pypdf/docx · BGE-M3 · Milvus |
| 数据 | MySQL 8 · Redis 7 · 向量库（Milvus 或 PGVector） |
| 模型 | 云上 OpenAI 兼容（LLM / VLM / 可选 Embedding） |
| 前端 | Vue3 · Element Plus · ECharts · SSE（独立仓库，本仓不包含） |

## 文档说明

本仓库 **仅上传代码与本 README**；详细设计文档（`docs/`、`DESIGN.md`、`代码设计规范.md`）与演示知识数据 **不随 Git 发布**，仅保留在本地工作区。下表链接在克隆仓库中可能不可用。

## 文档目录（本地）

### 业务服务

| 文档 | 内容 |
|---|---|
| [架构设计](docs/services/architecture.md) | 总体架构、职责边界、编排链路 |
| [领域设计规范](docs/services/kb-design-spec.md) | 分类体系、状态机、权限、审核、检索、验收 |
| [功能清单](docs/services/features.md) | 角色、功能、权限码、验收 |
| [数据库设计](docs/services/database.md) | 表结构与初始化数据 |
| [接口设计](docs/api/api.md) | 对前端 REST / SSE |
| [与 AI 集成](docs/api/integration.md) | Java 如何调 AI 服务 |
| [实施路线](docs/services/roadmap.md) | 里程碑与任务 |
| [演示数据说明](docs/demo-data.md) | 文档、账号、演示脚本 |
| [代码设计规范](代码设计规范.md) | AI 代码生成契约 |
| [入库流程设计](DESIGN.md) | 异步入库状态机与质检门 |

### AI 服务

| 文档 | 内容 |
|---|---|
| [AI README](aftersales-kb-ai/README.md) | AI 服务总览与启动 |
| [AI 架构](aftersales-kb-ai/docs/architecture.md) | 模块、LangGraph、数据流 |
| [AI 内部接口](aftersales-kb-ai/docs/ai-service-api.md) | `/internal/*` 契约 |
| [AI 配置说明](aftersales-kb-ai/docs/config.md) | 环境变量与切换注意 |

## 快速开始

> 前置：JDK 17+、Maven、Python 3.11+（推荐 Anaconda env `shopkeeper-ai`）、可选 MySQL/Redis/Milvus。

### 1. 启动 AI 服务（可独立运行）

```bash
cd aftersales-kb-ai

# Windows
copy .env.example .env
# macOS/Linux
# cp .env.example .env

# 编辑 .env：按需设置 INTERNAL_TOKEN、EMBED_*、VECTOR_STORE、LLM_*/VLM_*、MINERU_*
# 最小可跑：VECTOR_STORE=memory + EMBED_PROVIDER=fake（或 local BGE）

pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

### 2. 启动 Java 业务服务

```bash
# 配置 src/main/resources/application.yaml（数据源、Redis、ai.base-url、ai.internal-token）
mvn spring-boot:run
```

Java 调 AI 时，`ai.internal-token` 必须与 AI 侧 `INTERNAL_TOKEN` 一致。

### 3. 运行测试

```bash
cd aftersales-kb-ai

# 离线单元/接口（memory + fake embed，无需外网）
python -m pytest tests -q --ignore=tests/test_langgraph_real.py

# 真实链路（需可达 Milvus、本地 BGE、MinerU、可选云 LLM/VLM；无 Mock）
python -m pytest tests/test_langgraph_real.py -v
```

## 安全与仓库约定

- **不要提交** `.env`、API Key、数据库密码；仓库仅保留 [`.env.example`](aftersales-kb-ai/.env.example)。
- `docs/demo-knowledge/product/*.pdf` 体积大，默认 **gitignore**；本地演示需要时自行放置。
- `/internal/*` 仅内网 + `X-Internal-Token`；前端禁止直连 AI 服务。
- 权限过滤只在 Java；AI 服务不持有业务状态。

## 项目信息

- 业务包名：`com.shuidun.kb`
- AI 根包：`app`
- 定位：求职作品集 / AI 应用开发 · AI 全栈
