package com.shuidun.kb.knowledge.dto;

import lombok.Data;

/**
 * 失败重试请求（docs/api/api.md §3 POST /api/knowledge/import/retry/{unitId}）。
 */
@Data
public class RetryRequest {

    /** 从指定阶段重试，如 parse / embed；不传则从失败阶段重试。 */
    private String fromStage;
}
