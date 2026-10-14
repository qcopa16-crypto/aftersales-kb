package com.shuidun.kb.faq;

import java.time.Duration;
import java.util.Optional;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * 基于 Redis 的 FAQ 答案存储（F-25）。
 *
 * <p>Redis 不可用时降级为“未命中”（读）/ 静默丢弃（写），绝不阻断问答链路；
 * 读路径未命中会由 {@link FaqCacheService} 回源 MySQL 并回填。
 */
@Slf4j
@Component
public class RedisFaqAnswerStore implements FaqAnswerStore {

    private final StringRedisTemplate redisTemplate;

    public RedisFaqAnswerStore(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    @Override
    public Optional<String> get(String key) {
        try {
            String value = redisTemplate.opsForValue().get(key);
            return (value == null || value.isBlank()) ? Optional.empty() : Optional.of(value);
        } catch (RuntimeException ex) {
            log.warn("faq cache lookup degraded, redis unavailable: {}", ex.getMessage());
            return Optional.empty();
        }
    }

    @Override
    public void put(String key, String answer, Duration ttl) {
        try {
            redisTemplate.opsForValue().set(key, answer, ttl);
        } catch (RuntimeException ex) {
            log.warn("faq cache put degraded, redis unavailable: {}", ex.getMessage());
        }
    }

    @Override
    public void evict(String key) {
        try {
            redisTemplate.delete(key);
        } catch (RuntimeException ex) {
            log.warn("faq cache evict degraded, redis unavailable: {}", ex.getMessage());
        }
    }
}
