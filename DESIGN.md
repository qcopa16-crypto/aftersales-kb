# DESIGN.md — 售后知识库 AI 入库流程

> Taste 层规范。后续入库相关 UI、状态机、接口与作业模型均以本文件为源。
> 范围：知识文档从上传到可检索的完整链路；不覆盖问答对话本身的视觉规范。

---

## 1. Objective

把「知识管理员把一份文档丢进系统，直到 AI 问答能检索到它」做成**可观察、可恢复、可审计**的流水线，而不是一次 HTTP 请求里的黑盒。

**成功标准**
- 200 页 PDF 入库不阻塞浏览器、不超时，管理员能离开页面再回来看进度。
- 任一阶段失败可定位到 stage + 文件 + 原因，并能重试该阶段而不是整单重来。
- 入库结果可解释：切了多少片、建议了什么分类/权限、嵌入了多少向量。
- 删除知识时 MySQL 与向量库一致，无孤儿向量。

**非目标**
- 不做跨库联邦检索、不做在线编辑器实时协作、不做多租户 SaaS 化。

---

## 2. Product Context

| 维度 | 事实 |
|---|---|
| 用户 | `knowledge_admin`（主）、`admin`；上传后配置权限 |
| 频次 | 低频高价值：一次批量导入手册，日常偶发补录 FAQ/政策 |
| 源形态 | PDF 整本手册（扫描件/电子版）、Markdown、DOCX、TXT |
| 现有架构 | 前端 → Java（编排/权限/主数据）→ FastAPI（parse/embed）→ Milvus（生产向量库；AI 侧运行状态存 `ai-data` 卷） |
| 现有接口 | `POST /api/knowledge/import` 历史上同步完成 parse+embed（大文件必超时）；现已按本设计改为**异步批次**（落存储 → unit(draft) → job(queued) → 返回 batch 摘要，见 §6 / §9） |
| 权限模型 | 四维：global / department / role / user，入库时需建议或指定 |
| 分类枚举 | `product` / `policy` / `sop` / `sales` / `finance` |

**关键产品判断**：入库不是「上传按钮」，是**知识管理员的工作台状态**。90% 的时间用户看到的不是上传表单，而是「这批任务现在到哪一步了」。

---

## 3. Visual Foundations

本规范主要约束流程与信息架构，视觉 token 沿用管理端既有体系；入库页专用补充：

| Token | 值 | 用途 |
|---|---|---|
| `--stage-queued` | `#94A3B8` | 排队中 |
| `--stage-running` | `#3B82F6` | 进行中（含脉冲） |
| `--stage-review` | `#F59E0B` | 待质检确认 |
| `--stage-ready` | `#10B981` | 已可检索 |
| `--stage-failed` | `#EF4444` | 失败 |
| 阶段条高度 | 4px | 进度条细条，非大圆环 |
| 状态文案字号 | 12px / mono 可选 | 与标题 16px 形成层级 |

**类型**：标题 Noto Sans SC 600 / 16–18px；正文 14px；文件名与 stage 码 13px；辅助说明 12px `#64748B`。

**布局骨架（入库工作台）**
```
┌──────────────────────────────────────────────────┐
│  入库中心                    [上传文件] [重试失败] │
├──────────────┬───────────────────────────────────┤
│ 批次列表      │  当前批次详情                      │
│ · 今天 3 个   │  文件行：名称 | 分类 | 阶段 | 进度  │
│ · 昨天 1 个   │  选中文件 → 阶段时间线 + 错误原因   │
└──────────────┴───────────────────────────────────┘
```

---

## 4. Accessibility

- 阶段状态不仅靠颜色：每条状态带文字标签（排队/解析中/待确认/已就绪/失败）。
- 进度更新用 `aria-live="polite"`，失败用 `assertive` 且可聚焦到错误详情。
- 键盘可达：文件行、重试、确认质检均为可 Tab 焦点控件。
- 色弱友好：失败/成功除颜色外用图标区分（× / ✓）。

---

## 5. Voice & Tone

| 场景 | 语气 | 示例 |
|---|---|---|
| 排队 | 事实、平静 | 「排队中 · 前方 2 个任务」 |
| 解析中 | 事实 + 可量化 | 「解析中 · 第 12/86 页」 |
| 待质检 | 引导、不指责 | 「解析完成，建议确认目录结构后再入库」 |
| 失败 | 可行动 | 「解析失败：PDF 加密，无法读取。请提供未加密文件后重试。」 |
| 完成 | 简短闭环 | 「已就绪 · 切片 142 · 向量 142」 |

禁止：「哎呀出错了」「AI 正在努力」等人格化空话。

---

## 6. Implementation Practices

### 6.1 状态机（`knowledge_units.ingest_status`）

```
draft ──► queued ──► parsing ──► parsed ──► embedding ──► active
            │           │          │           │
            └───────────┴──────────┴───────────┴──► failed
                                                     │
                                              (retry 回到 queued 或失败前阶段)

active ──► disabled（软下线，向量保留）
disabled ──► active
任意 ──► deleting ──► （物理删，级联删向量）
```

`status`（active/disabled）与 `ingest_status` 分离：前者是业务可用性，后者是流水线进度。问答检索只看 `status=active AND ingest_status=active`。

### 6.2 异步作业模型

- Java 侧新增 `ingest_jobs` 表：`batch_id` / `unit_id` / `stage` / `progress` / `error_code` / `error_message` / `attempt` / `created_by`。
- 上传接口 **只做**：落 MinIO → 建 `knowledge_units(draft)` → 建 job(queued) → 返回 batch 摘要。**不再**在 HTTP 内 parse/embed。
- 后台 Worker（Java `@Async` 或独立线程池，单线程串行避免打爆 MinerU）消费 job：
  1. `parse`：调 AI `/internal/parse`（大 PDF 可分片/按章）
  2. `chunk_qc`：切片数、平均长度、空片率阈值检查
  3. `classify`：调 AI 分类/摘要建议（可跳过）
  4. `embed`：调 AI `/internal/embed`，分批写向量
  5. `finalize`：`ingest_status=active`，写统计
- 失败策略：指数退避重试 ≤3 次；不可重试错误（加密 PDF、格式不支持）直接 failed 并给用户动作建议。

### 6.3 质检门（Quality Gate）

| category | 质检策略 |
|---|---|
| `product`（整本手册） | **强制人工确认**：解析完成后停在 `parsed`，管理员确认标题/目录/切片抽样后再 embed |
| `sop` / `policy` | 建议确认，可 10s 内点「跳过」或超时自动继续 |
| `sales` / `finance` / `md/txt` | 自动通过，失败才介入 |

质检页提供：文件元信息、章节结构树、随机 3 个切片全文、AI 建议分类与权限。

### 6.4 权限建议

- Markdown 源若含「数据权限：售后部」等元行，解析时抽取并预填 `unit_permissions`。
- 无元信息时：按 category 映射默认权限（product→global，sop→售后部，finance→财务部…），入库前在质检/预览中可改。
- 权限写入发生在 **embed 成功后、active 前**，避免无权限向量先上线。

### 6.5 幂等与更新

- 同一 `source_file_name` + 同一 uploader 再次上传 → 识别为更新：新建 unit 新版本，旧 unit 标记 `superseded` 或删除（默认替换，保留 7 天可撤销）。
- `PUT /units/{id}` 改正文 → 触发 **re-ingest** job（parse→embed），期间该 unit 从检索中摘除（`ingest_status=parsing`）。

### 6.6 删除一致性

```
Java: status → deleting
  → AI DELETE /internal/units/{id}  (删向量，幂等)
  → MySQL 删 chunks / unit_permissions / knowledge_units
  → 失败则 unit 保持 deleting，后台补偿任务重试
```

禁止先删 MySQL 再删向量而不记录 in-flight 删除意图。

---

## 7. Anti-Patterns

| 禁止 | 原因 |
|---|---|
| 上传 HTTP 内同步 parse+embed | 大 PDF 超时、连接占用、无法离开页面 |
| 只用转圈「处理中…」无阶段 | 管理员无法判断卡死还是正常 |
| 失败只给 500 | 无法行动；必须有 stage + 可读原因 + 重试入口 |
| 质检页只显示「解析成功」 | 无法发现页序错乱、目录丢失、表格变纯文本 |
| 向量删了但 unit 还在 active | 检索空窗或报错 |
| 用 `status=disabled` 代替 `failed` | 语义混淆，看板与运维无法区分 |
| 批量导入部分失败却返回整体 success | 必须逐文件结果 |

---

## 8. Decision-Making

冲突时的优先级：

1. **数据正确性**（权限与向量一致）> 吞吐
2. **可恢复性**（失败可重试定位）> 实现简单
3. **管理员可离开**（异步）> 实时看到完成
4. **手册质量**（强制质检）> 全自动
5. 与现有「Java 编排 / AI 不持有业务主数据」边界一致，不把业务状态塞进 FastAPI（AI 侧仅持有需备份的运行状态——active map、索引修复 ledger，见 [architecture.md](docs/services/architecture.md) §2.1）

拿不准时：选更慢但可解释的路径；演示环境可降级质检为建议，但状态机与错误模型不降级。

---

## 9. Workflow

```
1. 上传（多文件）     → MinIO + unit(draft) + job(queued)     [立即返回]
2. Worker 领取        → job(parsing)
3. AI parse           → chunks 草稿 + 页级进度回传
4. 切片 QC            → 过 / 挂起警告
5. 分类与权限建议     → 写入 unit 建议字段
6. 质检（按策略）     → parsed 等待确认 / 自动放行
7. AI embed（分批）   → 向量入库，progress 更新
8. Finalize           → ingest_status=active，可检索
9. 失败路径           → failed + error + 重试（从失败 stage）
10. 删除路径          → deleting → 向量删 → 主数据删
```

**前端必做表面**
1. 上传入口（拖拽多文件 + category 可选）
2. 入库中心（批次列表 + 文件行状态）
3. 质检抽屉/页（product 手册必达）
4. 失败详情 + 重试
5. 知识列表中的 ingest 状态角标

**后端必做表面**
1. 上传 API（异步化）
2. Job 查询 API（batch 详情）
3. 质检确认 API
4. 重试 API
5. Worker + 阶段调用 AI 内部接口
6. 删除补偿

---

## Decision Trace 摘要

见对话中的 Decision Trace 段；关键决策：
- 同步 → 异步 job
- 状态拆分 `status` vs `ingest_status`
- product 手册强制质检
- 权限在 embed 后写入
- 删除先意向后级联
