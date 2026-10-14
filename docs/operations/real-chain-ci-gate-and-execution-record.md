# 真实链路 CI 门禁与执行记录说明（P0-1）

> 适用对象：`.github/workflows/ai-nightly.yml`
> 目的：消除「真实链路未运行却判为通过」的假绿语义，明确离线验收与真实链路验收两个
> required check 的配置、执行记录与归档要求。
> 关联条目：上线方案 P0-1、放行清单「真实链路在目标自托管 runner 实际运行通过」。
>
> **状态（2026-10-15）**：硬门禁代码已在 `.github/workflows/ai-nightly.yml` 落地
> （`real-chain` 固定 `runs-on: [self-hosted, real-chain]`；未启用或依赖不可达一律 `exit 1`，无假绿分支）。
> **仍未完成**：注册自托管 runner、设仓库 Variable `REAL_CHAIN_ENABLED=1`、配 release 分支 required check，
> 并留存一次真实执行记录（见 §5/§6）。

## 1. 两个 required check 的分工

| check 名称 | runs-on | 语义 | 是否可设为必需 |
|---|---|---|---|
| `offline-acceptance` | `ubuntu-latest`（GitHub 托管） | 零外网离线回归（memory + fake/离线链路），命令 `python -m pytest tests -q` | 是，恒为必需 |
| `real-chain` | `[self-hosted, real-chain]`（自托管） | 真实 Milvus/BGE/MinerU/云 LLM 全链路（`test_langgraph_real.py` + `e2e-ai-chain.py`） | 是，自托管 runner 上线后设为必需 |

关键点：GitHub Actions 的 `runs-on` **不支持表达式**，无法按 runner 环境自动切换标签，
因此真实链路 job 固定写为 `runs-on: [self-hosted, real-chain]`。

## 2. 两种运行场景的区别（务必区分）

### A. GitHub 托管 runner 上的 nightly（说明性运行）

- 托管 runner 无法访问内网 Milvus/MySQL/Redis 与云 LLM。
- 若仓库尚未注册 `real-chain` 自托管 runner，`real-chain` job 会一直 **queued**，nightly
  无法完成——这本身就是「门禁尚未接线」的信号，**不是通过**。
- 托管 runner 上唯一可信的 nightly 信号是 `offline-acceptance`；它只证明离线回归通过，
  **不代表**真实链路已验证，**不得**据此放行上线。

### B. release 分支必需门禁（生产放行）

- 真实链路 job 在自托管 runner 上执行；**未显式启用或依赖不可达即失败**（非零退出）。
- 绝不存在 warning+exit 0 / skipped 的「假绿」分支。

## 3. 硬门禁行为（不得回退）

`real-chain` job 的三个步骤在 `REAL_CHAIN_ENABLED != 1` 时一律 `exit 1`：

1. `Preflight real-chain dependencies`：未启用 → `::error::` + `exit 1`；启用后探测
   Milvus/Redis/MySQL 可达性，任一不可达 → 列出缺失项 + `exit 1`。
2. `Real LangGraph chain`：未启用 → `exit 1`；否则 `python -m pytest tests/test_langgraph_real.py -v`。
3. `End-to-end AI chain`：未启用 → `exit 1`；否则 `python ../scripts/e2e-ai-chain.py`。

约束：**不得引入 `continue-on-error`**；**不得**把上述分支改回 `::warning::` + `exit 0`。

## 4. 配置步骤（release 分支必需门禁）

1. 上线可访问内网（如 `<内网主机>`）的自托管 runner，并打标签 `real-chain`。
2. 在该 runner 上导出仓库 Variable：`Settings → Secrets and variables → Actions →
   Variables → New repository variable`，名称 `REAL_CHAIN_ENABLED`，值 `1`。
   （未设置时 `real-chain` 会失败——这是有意设计。）
3. `Settings → Branches → Branch protection rules`（针对 release 分支）→
   `Require status checks to pass` → 勾选：
   - `offline-acceptance`
   - `real-chain`
4. 在该分支上确认 recent check 为 **pass（而非 skipped/queued）**。

## 5. 真实链路执行记录要求（必须归档）

每次放行必须留存至少一次真实链路执行记录，字段包括：

- **commit SHA**：被验证的确切提交；
- **runner 标识/标签**：`self-hosted` / `real-chain`，以及执行时间；
- **依赖版本摘要**：Milvus/BGE/MinerU/云 LLM 端点与模型版本、AI 服务镜像 digest；
  **不含任何 secret / token**；
- **结果**：`test_langgraph_real.py` 与 `e2e-ai-chain.py` 的通过数/失败数；
- **日志链接或归档路径**。

**禁止以 skipped / warning / queued 作为通过依据**；只有实际执行且 `exit 0` 才算通过。

## 6. 分支保护截图 / 审计记录归档

- 将 Branch protection 页面（required checks 列表含上述两项）**截图或导出 PDF**，
  连同本次真实链路执行记录，一并归档到发布证据目录：
  `docs/operations/release-evidence/<release-tag>/`（或团队审计库）。
- 脚本化留痕（便于事后审计比对）：

  ```bash
  gh api repos/:owner/:repo/branches/<release-branch>/protection \
    > docs/operations/release-evidence/<release-tag>/branch-protection.json
  ```

- 在发布审批单中标注归档路径与复核人。

## 7. 放行判定

- [ ] `offline-acceptance` 在 release commit 上真实通过（非 skipped）。
- [ ] `real-chain` 在目标自托管 runner 上**实际执行且通过**（非 skipped / warning / queued）。
- [ ] 真实链路执行记录已归档（含 commit SHA、runner、依赖版本、结果）。
- [ ] 分支保护截图 / `branch-protection.json` 已归档，required checks 含上述两项。
- [ ] 故意关闭 `REAL_CHAIN_ENABLED` 或断开关键依赖时，`real-chain` 呈现**失败**而非可放行状态。
