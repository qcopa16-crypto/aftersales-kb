package com.shuidun.kb.dashboard.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 按日统计。
 */
@Data
@AllArgsConstructor
public class DailyStatVO {

    /** 日期（yyyy-MM-dd）。 */
    private String date;

    /** 问答次数。 */
    private long qaCount;

    /** Token 消耗。 */
    private long totalTokens;

    /** 平均响应耗时（毫秒）。 */
    private long avgResponseMs;
}
