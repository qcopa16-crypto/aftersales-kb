package com.shuidun.kb.dashboard.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 最慢问题条目（耗时分析用）。
 */
@Data
@AllArgsConstructor
public class SlowQuestionVO {

    /** 问题。 */
    private String question;

    /** 端到端耗时（毫秒）。 */
    private long responseMs;

    /** 检索耗时（毫秒）。 */
    private long retrieveMs;

    /** 生成耗时（毫秒）。 */
    private long generateMs;

    /** 数据库耗时（毫秒）。 */
    private long dbMs;

    /** 缓存耗时（毫秒）。 */
    private long cacheMs;

    /** 是否命中 FAQ。 */
    private boolean faqHit;

    /** 答案来源：kb/faq/llm_general/clarify/refusal（看板来源标签用）。 */
    private String answerSource;

    /** 发生时间。 */
    private String createdAt;
}
