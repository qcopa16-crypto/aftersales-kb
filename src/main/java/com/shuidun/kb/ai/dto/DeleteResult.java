package com.shuidun.kb.ai.dto;

import lombok.Data;

/**
 * 删除向量响应（aftersales-kb-ai/docs/ai-service-api.md DELETE /internal/units/{unit_id}）。
 */
@Data
public class DeleteResult {

    /** 是否删除成功（失败不假成功）。 */
    private Boolean deleted;

    /** 删除的向量条数。 */
    private Integer deletedCount;
}
