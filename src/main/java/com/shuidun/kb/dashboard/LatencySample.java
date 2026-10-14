package com.shuidun.kb.dashboard;

/**
 * 一次问答的耗时采样（毫秒）。
 *
 * <p>用于把"端到端耗时"拆成可解释的阶段：检索 / 生成 / 数据库 / 缓存，
 * 剩余部分（权限过滤、SSE 透传、序列化、网络）由 {@code responseMs} 相减得到。
 *
 * @param responseMs 端到端耗时
 * @param retrieveMs 检索阶段（AI /internal/retrieve）
 * @param generateMs 生成阶段（AI /internal/generate）
 * @param dbMs       数据库阶段（MySQL：四维权限过滤 + 知识单元加载）
 * @param cacheMs    缓存阶段（Redis：FAQ 查询）
 */
public record LatencySample(int responseMs, int retrieveMs, int generateMs, int dbMs, int cacheMs) {
}
