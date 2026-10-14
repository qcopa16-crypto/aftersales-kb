package com.shuidun.kb.knowledge.dto;

import lombok.Data;

/**
 * 更新知识单元请求（docs/api/api.md §3 PUT /api/knowledge/units/{id}）。
 */
@Data
public class KnowledgeUnitUpdateRequest {

    /** 标题。 */
    private String title;

    /** 正文；变更将触发 re-ingest。 */
    private String content;

    /** 摘要。 */
    private String summary;

    /** 分类。 */
    private String category;

    /** 业务状态：active / disabled。 */
    private String status;

    /**
     * 可选乐观锁版本（R-02）：客户端读取详情时拿到的 {@code lifecycleVersion}。
     *
     * <p>提供时服务端会校验其与当前版本一致，否则返回并发冲突要求重读；不提供时以服务端
     * 本次读取到的版本作为 CAS 基准（仍可检测读取后被并发删除/修改）。
     */
    private Long lifecycleVersion;
}
