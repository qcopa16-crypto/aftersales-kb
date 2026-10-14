package com.shuidun.kb.ai.dto;

import lombok.Data;

/**
 * embed 响应（aftersales-kb-ai/docs/ai-service-api.md POST /internal/embed）。
 */
@Data
public class EmbedResult {

    /** 回显 unit_id。 */
    private String unitId;

    /** 已写入向量数。 */
    private Integer embeddedCount;

    /** 向量维度。 */
    private Integer dim;

    /** 续传起点。 */
    private Integer resumedFrom;

    /** 是否为部分成功。 */
    private Boolean partial;

    /** Java 审计 id。 */
    private String ingestId;
}
