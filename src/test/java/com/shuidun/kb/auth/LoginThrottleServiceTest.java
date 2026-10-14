package com.shuidun.kb.auth;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.shuidun.kb.common.BusinessException;
import java.time.Duration;
import java.util.Collection;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;

/**
 * R-14 回归：登录限流在保留 IP 维度的同时，增加账号维度跨 IP 风险聚合。
 *
 * <p>账号维度只施加**封顶的递增延迟**、绝不硬锁号（避免攻击者定向锁死受害者账号）；
 * IP/IP+账号维度仍按原逻辑渐进加锁并拒绝；Redis 故障时降级为进程内计数且可观测。
 */
class LoginThrottleServiceTest {

    /** 用 Map 模拟 Redis 的原子计数/键存在/删除语义，避免依赖真实 Redis。 */
    private static final class FakeRedis {

        private final Map<String, String> store = new ConcurrentHashMap<>();

        private final StringRedisTemplate template = mock(StringRedisTemplate.class);

        @SuppressWarnings("unchecked")
        private FakeRedis() {
            ValueOperations<String, String> ops = mock(ValueOperations.class);
            when(ops.increment(anyString())).thenAnswer(inv -> {
                String key = inv.getArgument(0);
                long next = Long.parseLong(store.getOrDefault(key, "0")) + 1L;
                store.put(key, String.valueOf(next));
                return next;
            });
            when(ops.get(anyString())).thenAnswer(inv -> store.get(inv.getArgument(0)));
            doAnswer(inv -> {
                store.put(inv.getArgument(0), inv.getArgument(1));
                return null;
            }).when(ops).set(anyString(), anyString(), any(Duration.class));
            when(template.opsForValue()).thenReturn(ops);
            when(template.hasKey(anyString())).thenAnswer(inv -> store.containsKey(inv.getArgument(0)));
            when(template.expire(anyString(), any(Duration.class))).thenReturn(true);
            when(template.delete(anyCollection())).thenAnswer(inv -> {
                Collection<String> keys = inv.getArgument(0);
                keys.forEach(store::remove);
                return (long) keys.size();
            });
        }
    }

    private static LoginThrottleProperties props() {
        LoginThrottleProperties properties = new LoginThrottleProperties();
        properties.setEnabled(true);
        properties.setMaxFailures(3);
        properties.setWindowSeconds(600);
        properties.setLockSeconds(1);
        properties.setMaxLockSeconds(5);
        properties.setAccountMaxFailures(2);
        properties.setAccountWindowSeconds(600);
        properties.setAccountBaseDelayMs(50);
        properties.setAccountMaxDelayMs(200);
        return properties;
    }

    @Test
    void accountRiskAggregatesAcrossIpsAndDelaysWithoutHardLock() {
        FakeRedis redis = new FakeRedis();
        LoginThrottleProperties properties = props();
        // IP 阈值抬高，确保本用例只观察账号维度行为
        properties.setMaxFailures(1000);
        LoginThrottleService service = new LoginThrottleService(redis.template, properties);

        // 分散来源（不同 IP）对同一账号连续失败：账号维度计数聚合
        for (int i = 0; i < 5; i++) {
            service.recordFailure("10.0.0." + i, "victim");
        }

        // 达到账号阈值后：被延迟，但**不抛异常**（不硬锁号 → 攻击者无法定向 DoS 受害者）
        long start = System.nanoTime();
        assertDoesNotThrow(() -> service.checkAllowed("10.0.0.99", "victim"),
                "账号维度不得硬锁号");
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;
        assertTrue(elapsedMs >= 40L, "达到账号风险阈值后应施加递增延迟，实际 " + elapsedMs + "ms");

        // 未达阈值的其它账号不受影响（无延迟、无异常）
        long otherStart = System.nanoTime();
        assertDoesNotThrow(() -> service.checkAllowed("10.0.0.99", "other"));
        long otherElapsedMs = (System.nanoTime() - otherStart) / 1_000_000L;
        assertTrue(otherElapsedMs < 40L, "无关账号不应被延迟，实际 " + otherElapsedMs + "ms");
    }

    @Test
    void successClearsAccountRiskDelay() {
        FakeRedis redis = new FakeRedis();
        LoginThrottleProperties properties = props();
        properties.setMaxFailures(1000);
        LoginThrottleService service = new LoginThrottleService(redis.template, properties);

        for (int i = 0; i < 4; i++) {
            service.recordFailure("10.0.1." + i, "victim");
        }
        service.recordSuccess("10.0.1.0", "victim");

        long start = System.nanoTime();
        assertDoesNotThrow(() -> service.checkAllowed("10.0.1.0", "victim"));
        long elapsedMs = (System.nanoTime() - start) / 1_000_000L;
        assertTrue(elapsedMs < 40L, "成功登录后应清零账号风险计数，实际 " + elapsedMs + "ms");
    }

    @Test
    void ipDimensionStillLocksAndRejects() {
        FakeRedis redis = new FakeRedis();
        LoginThrottleProperties properties = props();
        LoginThrottleService service = new LoginThrottleService(redis.template, properties);

        String ip = "192.168.1.50";
        for (int i = 0; i < properties.getMaxFailures(); i++) {
            service.recordFailure(ip, "someone");
        }
        assertThrows(BusinessException.class, () -> service.checkAllowed(ip, "someone"),
                "IP 维度达到阈值仍应拒绝（R-14 未移除 IP 限流）");
    }

    @Test
    void redisFailureDegradesToMemoryAndIsObservable() {
        StringRedisTemplate brokenRedis = mock(StringRedisTemplate.class);
        when(brokenRedis.opsForValue()).thenThrow(new RuntimeException("redis down"));
        when(brokenRedis.hasKey(anyString())).thenThrow(new RuntimeException("redis down"));
        LoginThrottleProperties properties = props();
        LoginThrottleService service = new LoginThrottleService(brokenRedis, properties);

        for (int i = 0; i < properties.getMaxFailures(); i++) {
            service.recordFailure("172.16.0.9", "victim");
        }
        // 降级可观测 + 降级后限速仍然生效（内存计数锁定 IP 维度）
        assertTrue(service.isRedisDegraded(), "Redis 故障后应标记降级状态");
        assertThrows(BusinessException.class, () -> service.checkAllowed("172.16.0.9", "victim"),
                "降级为内存计数后 IP 维度限速仍需生效（fail-closed）");
    }
}
