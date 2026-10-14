package com.shuidun.kb.dashboard.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 耗时阶段拆分（毫秒，均为平均值）。
 *
 * <p>端到端 = 检索 + 生成 + 数据库 + 缓存 + 其他。
 */
@Data
@AllArgsConstructor
public class LatencyBreakdownVO {

    /** 检索阶段（Java 调 AI /internal/retrieve）。 */
    private long retrieveMs;

    /** 生成阶段（AI 流式 generate）。 */
    private long generateMs;

    /** 数据库阶段（MySQL：四维权限过滤 + 知识单元加载）。 */
    private long dbMs;

    /** 缓存阶段（Redis：FAQ 命中查询）。 */
    private long cacheMs;

    /** 其余开销（JWT 校验、SSE 透传、序列化、网络）。 */
    private long otherMs;

    /** 命中 FAQ 缓存时的平均端到端耗时（不走检索/生成，单独说明用）。 */
    private long faqMs;
}
