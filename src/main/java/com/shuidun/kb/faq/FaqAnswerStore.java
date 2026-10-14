package com.shuidun.kb.faq;

import java.time.Duration;
import java.util.Optional;

/**
 * FAQ 答案缓存存储抽象（F-25）。
 *
 * <p>生产实现为 Redis（{@link RedisFaqAnswerStore}）；测试/降级可实现为内存存储。
 * 存储失败一律表现为“未命中”（{@link Optional#empty()}），由上层回源 MySQL。
 */
public interface FaqAnswerStore {

    /**
     * 读取缓存答案。
     *
     * @param key 规范化键
     * @return 命中返回答案；未命中或存储不可用返回 empty
     */
    Optional<String> get(String key);

    /**
     * 写入缓存答案（带 TTL，防止键无界增长）。
     *
     * @param key    规范化键
     * @param answer 答案
     * @param ttl    过期时间
     */
    void put(String key, String answer, Duration ttl);

    /**
     * 删除缓存键（驳回 / 下线 / 问题变更时调用）。
     *
     * @param key 规范化键
     */
    void evict(String key);
}
