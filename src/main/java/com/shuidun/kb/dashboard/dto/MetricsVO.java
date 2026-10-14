package com.shuidun.kb.dashboard.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 看板总览指标（docs/api/api.md §7）。
 */
@Data
@AllArgsConstructor
public class MetricsVO {

    /** 问答总次数。 */
    private long qaCount;

    /** 独立访问人数。 */
    private long uv;

    /** 知识单元总数。 */
    private long unitCount;

    /** Token 总消耗。 */
    private long totalTokens;

    /** 平均响应耗时（毫秒）。 */
    private long avgResponseMs;
}
