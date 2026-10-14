package com.shuidun.kb.dashboard.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 耗时分析总览（按天聚合的分位 + 阶段拆分 + 最慢问题）。
 */
@Data
@AllArgsConstructor
public class LatencyVO {

    /** 统计区间（天）。 */
    private int days;

    /** 样本数。 */
    private long samples;

    /** 平均端到端耗时（毫秒）。 */
    private long avgMs;

    /** P50 端到端耗时（毫秒）。 */
    private long p50Ms;

    /** P90 端到端耗时（毫秒）。 */
    private long p90Ms;

    /** P95 端到端耗时（毫秒）。 */
    private long p95Ms;

    /** 最大端到端耗时（毫秒）。 */
    private long maxMs;

    /** 阶段拆分（平均值）。 */
    private LatencyBreakdownVO breakdown;

    /** 按小时/天/周/月的趋势。 */
    private List<DailyStatVO> trend;

    /** 最慢的问题 TOP。 */
    private List<SlowQuestionVO> slowest;
}
