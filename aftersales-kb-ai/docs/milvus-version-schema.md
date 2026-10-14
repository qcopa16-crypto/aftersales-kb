# Milvus schema 变更与兼容说明（version_id / embed_model）

> **状态更新**：`version_id` / `embed_model` 已进入代码主线——`ensure_collection()` 创建**新集合时默认带**这两字段；
> 存量集合缺字段时按下方「legacy 模式」运行（无版本原子切换）。当前部署的默认集合名以 `MILVUS_COLLECTION`
> 为准（compose 模板为 `kb_chunks`；评测/生产曾用 `kb_ai_chunks` / `kb_ai_chunks_v2` 等专用集合）。
> 下文 §「生产集合 `kb_ai_chunks` 评估」是当时一次**具体集合**的迁移记录，供同类迁移参考。

## 目标字段

| 字段 | 类型 | 用途 |
|------|------|------|
| `version_id` | VARCHAR(64) | 原子发布版本标识；search 只读 active |
| `embed_model` | VARCHAR(128) | 写入向量所用嵌入模型版本 |

## 新集合

`ensure_collection()` 创建时**默认带**上述两字段（可写 default 空串）。

## 存量集合兼容

1. 启动 `ensure_collection` 描述 schema。
2. 若缺可选字段，调用 **`add_collection_field(..., data_type=DataType.VARCHAR, ...)`**（pymilvus 3.x）。
3. **升级失败**：进入 **legacy 模式**（`delete → insert`），并打 warning；功能可用，但无版本原子切换。

### 生产集合 `kb_ai_chunks` 评估（2026-02-15，真机 127.0.0.1）

| 项 | 现状 |
|----|------|
| 行数 | 108 |
| 字段 | `id`, `unit_id`, `chunk_index`, `content`, `embedding(dim=1024)` |
| 缺字段 | `version_id`, `embed_model` |

**原地加字段探测结果**

- pymilvus 3.x `add_collection_field(data_type=...)` 调用格式正确。
- 服务器返回 **`UNIMPLEMENTED: unknown method AddCollectionField`** → **当前 Milvus 服务端不支持在线加字段**。

**结论**

- **唯一推荐路径**：新建 `kb_ai_chunks_v2`（创建时带 `version_id`/`embed_model`）→ 全量重 embed 或导出迁移 → 改 `.env` `MILVUS_COLLECTION=kb_ai_chunks_v2` → 观察后 drop 旧集合。
- **不可行**：对现有 `kb_ai_chunks` 原地加字段（服务端不支持）。
- **过渡期**：继续用 `kb_ai_chunks` 时服务自动 **legacy 删写**，功能可用但无原子版本切换。

**迁移检查清单**

1. 新建集合时 dim=1024，与 BGE-M3 一致
2. 跑 `scripts/verify_bge_milvus_e2e.py --collection kb_ai_chunks_v2`（GPU 真链路已验证脚本）
3. 切换配置后回归检索业务
4. 旧集合只读观察 ≥1 周期再删

## 发布协议（有版本字段时）

```text
1. write   插入 version_id=NEW 的全部向量
2. validate query count(unit_id, version_id=NEW) == expected
3. switch  进程内 active[unit_id]=NEW
4. cleanup delete unit_id and version_id != NEW
失败：delete version_id=NEW，active 保持旧值（回滚，服务可读）
```

## 运维注意

- 单副本进程内 active 指针；多副本需共享存储（本版 Out of Scope）。
- 重建索引后仍可按 `version_id` 过滤清理孤儿数据。
- 验证脚本可参考 `tests/test_p1_p2_upgrade.py` 中 memory/Milvus fake client 用例。
