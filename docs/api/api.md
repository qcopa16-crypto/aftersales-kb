# 接口设计

> 本文件是 **对前端** 的业务 API。  
> AI 能力由 Java 编排后调用 FastAPI 内部接口，契约见 [integration.md](./integration.md)。

## 通用约定

- Base Path：`/api`
- 认证：`Authorization: Bearer <token>`
- 统一响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

- 错误码：
  - `0` 成功
  - `400` 参数校验失败
  - `401` 未登录/令牌无效
  - `403` 无操作权限
  - `40001` 数据权限不足（业务，**HTTP 200**，靠 body code 区分）
  - `500` 系统错误
  - `50001` AI 服务不可用（HTTP 502）
  - `50002` AI 解析/向量化失败
- 分页：`page`（从 1 起）、`size`（默认 20，最大 100）；data 字段为 `total`/`page`/`size`/`records`
- 领域约束见 [kb-design-spec.md](../services/kb-design-spec.md)

---

## 1. 认证

### POST /api/auth/login

**请求**

```json
{
  "username": "zhangsan",
  "password": "123456"
}
```

**响应**

```json
{
  "code": 0,
  "data": {
    "accessToken": "xxx",
    "expiresIn": 7200,
    "userInfo": {
      "id": 2,
      "username": "zhangsan",
      "displayName": "张三",
      "departmentId": 2,
      "departmentName": "售后部",
      "roles": ["after_sales"],
      "permissions": ["knowledge:list", "knowledge:ai"]
    }
  }
}
```

### GET /api/auth/me

返回当前登录用户信息与权限码。

---

## 2. 组织管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/org/departments | 部门树 |
| POST | /api/org/departments | 新增部门 |
| PUT | /api/org/departments/{id} | 更新部门 |
| GET | /api/org/users | 用户分页列表 |
| POST | /api/org/users | 新增用户 |
| PUT | /api/org/users/{id} | 更新用户 |
| POST | /api/org/users/{id}/reset-password | 重置密码 |
| GET | /api/org/roles | 角色列表 |
| POST | /api/org/roles | 新增角色 |
| PUT | /api/org/roles/{id} | 更新角色 |
| POST | /api/org/roles/{id}/permissions | 分配操作权限 |
| GET | /api/org/roles/{id}/permissions | 查询角色权限 |

---

## 3. 知识管理

### POST /api/knowledge/import

`multipart/form-data`，支持多文件。  
**异步**：只接收文件并创建批次/作业，不在 HTTP 内 parse+embed（对齐 DESIGN.md §6.2 与《代码设计规范.md》§5.2）。

字段：`files`（数组）、`category`（可选）

**响应**

```json
{
  "code": 0,
  "data": {
    "batchId": "B-20260214-01",
    "acceptedCount": 3,
    "rejectedCount": 0,
    "items": [
      {"unitId": 1, "title": "H3C路由器安装手册", "fileType": "pdf", "ingestStatus": "queued"}
    ]
  }
}
```

### GET /api/knowledge/import/batches

入库批次列表（分页 `page`/`size`）。返回批次摘要：batchId、文件数、成功/失败数、创建时间、创建人。

### GET /api/knowledge/import/batches/{batchId}

批次详情：逐文件 `unitId`、`title`、`category`、`ingestStatus`、`stage`、`progress`、`errorCode`、`errorMessage`。

### GET /api/knowledge/import/quality-metrics

质检度量汇总（默认窗口 30 天，`days` 夹取到 [1,365]）。需权限码 `knowledge:list`。

### POST /api/knowledge/import/quality-gate/{unitId}

质检确认（product 手册强制）。body：`{"action":"confirm"|"adjust","category":"product","permissions":[...]}`。  
确认或调整后进入 embed；拒绝/调整错误可触发重解析。

### POST /api/knowledge/import/retry/{unitId}

从失败 stage 重试。body 可选：`{"fromStage":"parse"}`。不可重试错误（加密 PDF 等）应返回可读原因与建议动作。

### GET /api/knowledge/units

查询参数：`page`、`size`、`keyword`、`category`、`status`  
`status` 支持：`active` / `disabled` / `deleting` / `superseded`（默认不展示 deleting）。

### GET /api/knowledge/units/{id}

详情（含权限配置摘要、ingestStatus）

### PUT /api/knowledge/units/{id}

更新标题/正文/分类/状态；更新正文触发 re-ingest，期间 `ingest_status=parsing`，单元不可被检索。

### DELETE /api/knowledge/units

批量删除，body：`{"ids":[1,2]}`  
顺序：`status=deleting` → AI 删向量（幂等）→ 删主数据与 unit_permissions；失败保持 deleting 并补偿。

### GET /api/knowledge/units/delete-tasks

未完成的删除补偿任务列表（失败态 + 最近错误 + 下次重试；`limit` 默认 50，夹取 [1,200]）。需权限码 `knowledge:delete`。

### POST /api/knowledge/units/delete-tasks/{unitId}/retry

重试失败的删除任务（带授权与审计），返回逐项删除结果。需权限码 `knowledge:delete`。

### POST /api/knowledge/units/{id}/permissions

**请求**

```json
{
  "mode": "replace",
  "items": [
    {"targetType": "department", "targetId": 2},
    {"targetType": "role", "targetId": 3}
  ]
}
```

`targetType=global` 时 `targetId` 传 0。

### POST /api/knowledge/check-permissions

**请求**

```json
{
  "userId": 2,
  "unitIds": [1, 2, 3, 4]
}
```

**响应**

```json
{
  "data": {
    "authorizedUnitIds": [1, 2],
    "unauthorizedUnitIds": [3, 4]
  }
}
```

---

## 4. AI 问答（SSE）

> 实现说明：Java 负责 JWT、FAQ 缓存、数据权限过滤与日志；检索与生成由 `aftersales-kb-ai` 完成。详见 [integration.md](./integration.md)。

### POST /api/ai/chat/stream

`Content-Type: application/json`  
响应类型：`text/event-stream`

**请求**

```json
{
  "sessionId": "s_001",
  "question": "客户屏幕碎了能保修吗？",
  "chosenUnitId": 5
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| sessionId | 否 | 会话 id，缺省由服务端生成 |
| question | 是 | 用户问题，≤ 2000 字符 |
| chosenUnitId | 否 | **澄清点选**：上一轮 `clarify` 候选里选中的知识单元 id |

> `chosenUnitId` 有值时**跳过向量检索**，直接以该单元正文作答。
> 服务端会校验该单元在当前用户的可访问集合内——不在则返回统一拒答话术，
> 不区分「不存在」与「无权限」，避免被用来探测知识存在性。

**SSE 事件**

```text
event: sources
data: {"authorized":[{"id":1,"title":"整机保修政策"}],"unauthorized":[{"id":9,"title":"供应商结算规范"}]}

event: delta
data: {"text":"根据保修政策，"}

event: delta
data: {"text":"人为损坏不在免费保修范围内。"}

event: done
data: {"sessionId":"s_001","tokens":420,"elapsedMs":1800,"faqHit":false}
```

**澄清（clarify）**：当**纯无召回**（检索无结果，且不是权限拦截）时，服务端会先判断**问题信息是否足够**，
再决定是"追问"还是"给参考"——**缺什么才问什么**，而不是一上来就甩一堆知识文档让用户挑。

> 关键区分：**「检索为空」不等于「需要澄清」**。检索为空最常见的原因是知识还没切片向量化，
> 跟用户问题是否完整无关。所以「H3C 路由器指示灯不亮」这种信息完整的问题**不该**被拦下来追问。

**形态一：`kind=ask` —— 信息不足，追问缺的那一项**

```text
event: clarify
data: {"kind":"ask",
       "prompt":"「路由器有问题」这个描述还不够定位问题，能补充下具体现象吗？（点选下面的选项，或直接描述都可以）",
       "options":[
         {"type":"hint","title":"无法开机 / 无反应"},
         {"type":"hint","title":"指示灯异常"},
         {"type":"hint","title":"无法联网 / 连接失败"},
         {"type":"hint","title":"频繁掉线 / 不稳定"},
         {"type":"hint","title":"报错 / 出现错误码"},
         {"type":"hint","title":"功能异常 / 结果不对"},
         {"type":"hint","title":"声音或发热异常"}
       ]}

event: delta
data: {"text":"「路由器有问题」这个描述还不够定位问题，能补充下具体现象吗？（点选下面的选项，或直接描述都可以）"}

event: done
data: {"sessionId":"s_001","tokens":0,"elapsedMs":120,"faqHit":false}
```

判定规则（纯规则，不调 LLM）：

| 条件 | 判定 |
|---|---|
| 含**笼统故障词**（有问题 / 坏了 / 异常 / 报错…）且不含**具体现象词** | 缺"现象" → 追问现象 |
| 问题很短且**没有任何知识库实体命中**（标题/摘要/正文都没命中） | 缺"对象" → 追问是哪个设备 |
| 其余 | 信息足够 → 走下面的"给参考" |

用户点选 `hint` 后，前端把 `原问题 + 提示文案` 拼成新问题重新提交
（例如「路由器有问题 指示灯异常」），不会再次追问同一件事。

**形态二：`kind=reference` —— 问题已问清楚，只是知识库没命中**

```text
event: clarify
data: {"kind":"reference","prompt":null,
       "options":[
         {"type":"unit","unitId":1,"unitCode":"U-1001","title":"整机保修政策",
          "category":"policy","snippet":"整机保修期为一年，人为损坏不在免费保修范围内。","score":7}
       ]}

event: delta
data: {"text":"知识库里没有找到直接匹配这个问题的资料。下面几份可能相关，点开我可以基于它作答；也可以换个说法再问。"}
```

| 字段 | 说明 |
|---|---|
| kind | `ask`（追问）/ `reference`（给参考） |
| prompt | `kind=ask` 时的追问文案；`reference` 时为 null |
| options[].type | `hint`（补充提示）/ `unit`（知识单元） |
| options[].title | 展示文案；`type=hint` 时同时是追加到原问题后的补充文本 |
| options[].snippet | `type=unit`：命中位置附近的正文片段 |
| options[].score | `type=unit`：匹配得分（标题×3 + 摘要×2 + 正文×1） |

- `reference` 形态下**不要求**用户先选才能得到回答——参考只是可选入口。
- 两种形态的选项都**只来自当前用户可访问的知识单元**。
- 追问形态**不计入知识缺口**（问题只是没说清）；`reference` 与完全无候选才计入。

**澄清点选后的落库**：该轮 `qa_access_logs.chosen_unit_id` 会记录选中的单元，
历史会话详情（§4 下方接口）里以 `chosenUnitId` / `chosenTitle` 返回，便于回看与审计。

失败时：

```text
event: error
data: {"code":"RETRIEVE_FAILED","message":"检索失败，请重试"}
```

### GET /api/ai/chat/sessions

历史会话列表（**仅当前用户**），按最近提问时间倒序。

> 实现说明：会话不单独建表，直接按 `qa_access_logs.session_id` 聚合重建，
> 避免「会话表」与「访问日志」两份真相不一致。

**查询参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| limit | 否 | 条数，默认 20，上限 50 |

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": [
    {
      "sessionId": "s_001",
      "title": "客户屏幕碎了能保修吗？",
      "messageCount": 3,
      "lastAskedAt": "2026-10-10 21:32"
    }
  ]
}
```

`title` 取该会话首问前 20 字（取不到时返回「未命名会话」）。

### GET /api/ai/chat/sessions/{sessionId}

历史会话详情：按时间正序返回全部问答轮次，字段与 SSE 事件对齐，前端可复用同一套渲染。

**响应**

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "sessionId": "s_001",
    "rounds": [
      {
        "question": "客户屏幕碎了能保修吗？",
        "answer": "人为损坏不在免费保修范围内。[1]",
        "tokens": 420,
        "elapsedMs": 1800,
        "faqHit": false,
        "createdAt": "2026-10-10 21:32",
        "chosenUnitId": null,
        "chosenTitle": null,
        "sources": {
          "authorized": [{ "id": 1, "title": "整机保修政策" }],
          "unauthorized": []
        }
      }
    ]
  }
}
```

**边界与安全**

- 只返回当前用户自己的会话：查询条件恒带 `user_id`，`sessionId` 被猜中也读不到他人问答；
  不属于该用户时返回**空 `rounds`**（而非 403），避免会话 id 被探测。
- `sessionId` 长度上限 64（与 `qa_access_logs.session_id` 对齐），超长返回 `400`。
- 来源卡片按落库的 unit id 反查标题；**知识单元已删除时该条来源被跳过**，不回退成 `#id`。
- 需要权限码 `knowledge:ai`。

---

## 5. FAQ

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/settlement/faqs/recommendations | 待审核推荐列表 |
| POST | /api/settlement/faqs/{id}/review | 审核 body: `{action, editedAnswer}` |
| GET | /api/settlement/faqs | 已发布/全部 FAQ 列表 |
| PUT | /api/settlement/faqs/{id} | 编辑已发布 FAQ |
| POST | /api/settlement/faqs/{id}/offline | 下线 |

审核 action：`approve` / `reject`

---

## 6. 知识缺口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/settlement/knowledge-gaps | 缺口列表 |
| PATCH | /api/settlement/knowledge-gaps/{id} | 更新状态 resolved/ignored |

---

## 7. 看板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/dashboard/metrics | 总次数/UV/知识数/Token/平均耗时 |
| GET | /api/dashboard/rankings/questions | 高频问题 TOP |
| GET | /api/dashboard/rankings/units | 热门知识 TOP |
| GET | /api/dashboard/stats/tokens | Token 与耗时趋势 |
| GET | /api/dashboard/latency | 耗时分析（`days` 默认 7、`granularity` 默认 day） |

**metrics 示例**

```json
{
  "data": {
    "qaCount": 1280,
    "uv": 36,
    "unitCount": 25,
    "totalTokens": 986000,
    "avgResponseMs": 2100
  }
}
```

---

## 8. 权限码与接口映射（示例）

| 接口 | 所需权限码 |
|---|---|
| /api/knowledge/import | knowledge:import |
| /api/knowledge/import/quality-metrics | knowledge:list |
| /api/knowledge/units/{id}/permissions | knowledge:perm |
| /api/knowledge/units/delete-tasks* | knowledge:delete |
| /api/ai/chat/stream | knowledge:ai |
| /api/settlement/faqs/*/review | faq:review |
| /api/dashboard/* | dashboard:view |
| /api/org/users* | org:user:list / org:user:edit |
| /api/org/departments* | org:dept:list / org:dept:edit |
| /api/org/roles* | org:role:list / org:role:edit |
