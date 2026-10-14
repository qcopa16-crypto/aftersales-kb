package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.Data;

/**
 * 检索响应（aftersales-kb-ai/docs/ai-service-api.md）。
 */
@Data
public class RetrieveResult {

    /** 命中切片。 */
    private List<RetrievedChunk> chunks;

    /** 服务端耗时。 */
    private Integer tookMs;

    /** 是否已应用 ACL 过滤。 */
    private Boolean aclApplied;

    /** 是否启用 hybrid。 */
    private Boolean hybrid;

    /** 是否降级为 dense-only。 */
    private Boolean degradedToDense;
}
