# 设备售后知识库平台（aftersales-kb）

面向 IT 设备售后场景的企业知识库：文档导入、细粒度数据权限、AI 鉴权问答、FAQ 沉淀与知识缺口。

## 生产主文档导航（唯一发布流程入口）

> **新同事只需从本节找到唯一上线手册。** 生产部署、迁移、备份恢复、回退、告警与上线放行的**唯一权威入口**是
> [`docs/operations/production-runbook.md`](docs/operations/production-runbook.md)；其余文档为**配套权威定义**，与 runbook 冲突时以 runbook 为准。

| 主文档 | 定位 |
|---|---|
| [production-runbook.md](docs/operations/production-runbook.md) | **唯一发布流程入口**：版本矩阵、端口/网络、secret、启动/探针、迁移、备份恢复、回退、上线门禁 |
| [security-and-data-retention.md](docs/operations/security-and-data-retention.md) | secret / 上传文件 / 日志 / 模型与向量状态的保留与删除规则 |
| [observability-and-alerting.md](docs/operations/observability-and-alerting.md) | 监控方案、告警阈值/通知/处置、敏感日志检查（P1-3） |
| [staging-drill-checklist.md](docs/operations/staging-drill-checklist.md) | staging 端到端演练清单与记录模板（P0-4） |
| [architecture.md](docs/services/architecture.md) | 服务边界、**状态所有权**（业务主数据 / 派生向量 / AI 运维状态）、网络、部署拓扑与单/多实例限制 |
| [database.md](docs/services/database.md) | schema、**迁移与回滚策略**、回滚风险分类、数据恢复策略 |
| [api.md](docs/api/api.md) / [integration.md](docs/api/integration.md) | 对前端 API / Java↔AI 内部契约 |
| 本 README | 快速入口、支持版本、开发/验证入口与文档导航 |

> **文档一致性约定**：每个配置项（端口、服务名、状态卷、TLS、迁移执行方式）只保留**一个权威定义**；改代码/Compose 时须同改文档。

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
├── frontend/                         # Vue3 + Vite + JS 管理端与 AI 工作台（已入库）
├── scripts/                          # 工程脚本：E2E 全链路 / 诊断 / 种子 / 前端代理校验（已入库）
├── .github/workflows/                # CI：java-ci / web-ci / ai-ci / ai-nightly
├── Dockerfile                        # Java 业务服务镜像（多阶段，非 root，Temurin 21）
├── docker-compose.yml                # 生产参考编排：MySQL/Redis/Milvus/Java/AI/frontend
├── .dockerignore                     # 镜像构建上下文裁剪
├── deploy/                           # 部署配置：nginx 反代、环境变量模板
├── mvnw + mvnw.cmd + .mvn/wrapper/   # Maven Wrapper（固定 3.9.16，克隆即可构建）
├── docs/
│   ├── api/                          # 对前端接口、Java↔AI 集成契约
│   ├── services/                     # 系统架构、领域、功能、库表、路线
│   ├── operations/                   # 生产运维手册、安全与数据保留（已入库）
│   ├── demo-data.md                  # 演示数据说明（文档、账号、演示脚本）
│   └── demo-knowledge/               # 演示知识语料（产品 PDF 不随代码分发）
├── aftersales-kb-ai/                 # FastAPI AI 服务（独立 Python 工程）
│   ├── Dockerfile                    # AI 服务镜像（Python 3.11，非 root）
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

> `frontend/`（Vue3 前端工程）与 `scripts/`（E2E / 诊断 / 种子脚本）已纳入版本库；
> 其下 `node_modules/`、`dist/` 等构建产物仍由 `frontend/.gitignore` 排除。

### 目录职责与新增约定

| 目录 | 职责 | 数据所有者 |
|---|---|---|
| `src/main/java/com/shuidun/kb/**` | Java 业务域（auth/org/permission/knowledge/job/faq/gap/dashboard/ai）+ common/config 横切 | 后端 |
| `src/main/resources/**` | 运行配置、schema/种子、`migration/V2–V11` 版本化迁移与回滚 | 后端 / DBA |
| `frontend/src/**` | api / router / stores / utils / constants / views / components / layouts | 前端 |
| `aftersales-kb-ai/{app,ai,core,schemas}/**` | AI HTTP 层、领域能力、横切、DTO 契约 | AI |
| `aftersales-kb-ai/scripts/**` | 真实验收 / 运维 / bench 脚本（后续可按 eval/ops/acceptance 分组，须保留旧入口兼容） | AI |
| `scripts/**` | 跨服务 E2E / 诊断 / 数据准备 | 平台 |
| `docs/operations/**` | 生产运维手册、安全与数据保留（生产必需） | 运维 |
| `deploy/**` + `docker-compose.yml` + `*/Dockerfile` | 部署工件（镜像 / 编排 / 反代 / 变量模板） | 运维 |
| `deliverables/**`、`data/uploads/**`、`aftersales-kb-ai/samples/**`、`aftersales-kb-ai/.data/**` | 一次性报告 / 上传语料 / 评测输出 / 运行时缓存（**不随 Git 发布**） | 见 `docs/operations/security-and-data-retention.md` |

> 新增文件请按上表放入对应领域目录，避免在仓库根新增未归类源码；新增依赖/配置/脚本时同步更新本 README、CI 路径过滤与文档导航。

AI 服务细节见 `aftersales-kb-ai/README.md` 与 `aftersales-kb-ai/docs/`。

## 文档导航

以下文档均已纳入版本库（`git clone` 后即可访问），链接在克隆仓库中有效。

| 文档 | 内容 |
|---|---|
| [代码设计规范.md](代码设计规范.md) | AI 代码生成契约（最高优先级） |
| [DESIGN.md](DESIGN.md) | 入库流水线产品/流程规范 |
| [docs/services/kb-design-spec.md](docs/services/kb-design-spec.md) | 领域模型、权限、状态机 |
| [docs/services/architecture.md](docs/services/architecture.md) | **系统**总体架构（Java+AI） |
| [docs/api/api.md](docs/api/api.md) / [integration.md](docs/api/integration.md) | 对前端 API / Java↔AI 契约 |
| [docs/operations/production-runbook.md](docs/operations/production-runbook.md) | **生产部署/运维手册**（版本矩阵、迁移、备份恢复、回滚） |
| [docs/operations/security-and-data-retention.md](docs/operations/security-and-data-retention.md) | secret / 上传文件 / 日志 / 模型数据的保留与删除 |
| [aftersales-kb-ai/docs/architecture.md](aftersales-kb-ai/docs/architecture.md) | **AI 服务**架构与硬约束 |
| [aftersales-kb-ai/docs/ai-service-api.md](aftersales-kb-ai/docs/ai-service-api.md) | `/internal/*` 契约 |
| [aftersales-kb-ai/docs/config.md](aftersales-kb-ai/docs/config.md) | 环境变量 |

> 文档同步状态：本 README 导航的文档均已完成核对，**当前无待同步清单**。
>
> 已核对、**不再**属于待同步清单：`docs/services/architecture.md`、`docs/services/database.md`（本轮上线准备批次：状态所有权、端口/服务名、迁移与回滚策略），`docs/api/api.md`、`docs/api/integration.md`、`docs/services/kb-design-spec.md`、`docs/services/features.md`、`docs/services/roadmap.md`，以及 `aftersales-kb-ai/README.md` 与 `aftersales-kb-ai/docs/*`（2026-10-15 按当前实现逐份核对）。

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
| 业务服务 | Spring Boot 4.0.x · Spring Security · JWT · MyBatis · **Java 21（生产基线）** |
| AI 服务 | FastAPI · Pydantic v2 · LangGraph · pypdf/docx · BGE-M3 · Milvus |
| 数据 | MySQL 8 · Redis 7 · 向量库 **Milvus 2.4.x**（standalone；AI 侧仅实现 `milvus | memory` 两种 `vector_store`，**PGVector 无实现分支、不在当前支持范围**；脚本/模板中的 `pgvector` 取值属历史预留） |
| 模型 | 云上 OpenAI 兼容（LLM / VLM / 可选 Embedding） |
| 前端 | Vue3 · Element Plus · ECharts · Pinia · SSE |

## 文档说明

本仓库发布 **业务/AI 源码、`frontend/` 前端工程、`scripts/` 工程脚本、CI 工作流、部署工件（`Dockerfile` / `docker-compose.yml` / `deploy/`）、生产必需文档（`docs/`、`DESIGN.md`、`代码设计规范.md`、`aftersales-kb-ai/docs/`）与本 README**。

以下内容**不随代码分发**（已在 `.gitignore` 中排除）：一次性交付/审计报告（`deliverables/`）、评测输出（`aftersales-kb-ai/samples/`）、真实上传语料（`data/uploads/`、`aftersales-kb-ai/uploads/`）、运行时缓存（`aftersales-kb-ai/.data/`）、工作区记忆（`.workbuddy-ai/`、`.codebuddy/`）以及演示知识中的产品手册 PDF（`docs/demo-knowledge/product/`）。这些数据的保留与清理见 [docs/operations/security-and-data-retention.md](docs/operations/security-and-data-retention.md)。

## 文档目录（已入库）

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

> 前置：**JDK 21**（生产与 CI 基线；`pom.xml` `java.version=21`）、Maven、Python 3.11+（推荐 Anaconda env `shopkeeper-ai`）、可选 MySQL/Redis/Milvus。
>
> 生产部署（镜像构建、Compose 编排、迁移/备份/回滚）见 [docs/operations/production-runbook.md](docs/operations/production-runbook.md)。

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
# 默认使用 H2 内存库（含建表与种子数据），无需外部依赖即可启动
mvn spring-boot:run

# 生产/联调切换 MySQL：先准备 application-mysql.yaml 的数据源，再指定 profile
# SPRING_PROFILES_ACTIVE=mysql mvn spring-boot:run
```

默认端口 `8080`。演示账号（H2 种子数据，密码统一 `123456`）：

| 账号 | 角色 | 权限概览 |
|---|---|---|
| `admin` | 系统管理员 | 全部权限 |
| `zhangsan` | 售后工程师 | knowledge:list / knowledge:ai / gap:view / dashboard:view |
| `lisi` | 销售人员 | knowledge:list / knowledge:ai / dashboard:view |
| `wangwu` | 财务人员 | knowledge:list / knowledge:ai / dashboard:view |

Java 调 AI 时，`ai.internal-token` 必须与 AI 侧 `INTERNAL_TOKEN` 一致。

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev            # 开发服务器，/api 已代理到 http://127.0.0.1:8080
npm run build          # 产出 dist/
npm run format         # Prettier 格式化（src/**/*.{js,vue,css} + 根配置）
npm run format:check   # 仅校验格式，不改文件
```

### 4. 运行测试

```bash
# Java 业务服务（H2 + 内存版 AI 替身，零外部依赖）
mvn test

# AI 服务
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
- 权限过滤只在 Java；AI 服务**不持有业务主数据**，但持有需备份/恢复的运行状态（active map / 修复 ledger，见 [architecture.md](docs/services/architecture.md) §2.1）。

## 项目信息

- 业务包名：`com.shuidun.kb`
- AI 根包：`app`
- 定位：求职作品集 / AI 应用开发 · AI 全栈
