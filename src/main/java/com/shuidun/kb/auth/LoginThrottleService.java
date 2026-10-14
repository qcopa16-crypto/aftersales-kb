package com.shuidun.kb.auth;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ConcurrentHashMap;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

/**
 * 登录限速服务（F-11 / R-14）：按 IP 与规范化账号双维度记录失败并渐进退避，
 * 并叠加不硬锁号的账号维度风险延迟。
 *
 * <p>设计要点：
 * <ul>
 *   <li><b>双维度</b>：{@code ip} 维度防单来源喷洒，{@code ip|user} 维度防针对单账号的持续爆破；
 *       不设「仅账号」硬锁，避免把正常用户锁在门外（定向锁号拒绝服务）；</li>
 *   <li><b>账号维度风险延迟</b>（R-14）：额外按「仅账号」聚合跨 IP 失败计数，达到阈值后施加
 *       封顶的递增延迟（不锁号）——分布式来源对同一账号的爆破会被整体拖慢，但攻击者无法
 *       借此把受害者账号长期锁死；</li>
 *   <li><b>Redis 优先、内存降级</b>：Redis 异常时切换进程内计数（冷却 30s 后重试），
 *       限速能力不因缓存故障而消失；降级状态经日志与 {@link #isRedisDegraded()} 可观测；</li>
 *   <li><b>fail-closed 方向</b>：读判定异常时默认按「已锁定」处理，宁可误拒也不放行；</li>
 *   <li><b>成功即清零</b>：登录成功后删除该维度计数与锁，正常用户无感。</li>
 * </ul>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class LoginThrottleService {

    private static final String KEY_PREFIX = "login:throttle:";
    private static final String FAIL_SUFFIX = ":fail";
    private static final String LOCK_SUFFIX = ":lock";
    private static final String LOCK_VALUE = "1";

    /** Redis 降级后的冷却时长：冷却期内直接用内存计数，避免每次登录都尝试连接拖慢请求。 */
    private static final long REDIS_DEGRADE_COOLDOWN_MS = 30_000L;

    /** 渐进退避最多翻倍的档数（2^20 已远超任何合理窗口，防止位移溢出）。 */
    private static final int MAX_BACKOFF_STEPS = 20;

    private final StringRedisTemplate redisTemplate;
    private final LoginThrottleProperties properties;

    /** 内存降级计数：scope -> 失败窗口与锁定截止。 */
    private final ConcurrentHashMap<String, MemoryRecord> memory = new ConcurrentHashMap<>();

    /** Redis 降级截止时间；之前视为健康。 */
    private volatile long redisDegradedUntilMs = 0L;

    /**
     * 校验当前是否允许尝试登录（锁定中直接拒绝，先于 BCrypt 校验，顺带缓解计算型 DoS）。
     *
     * @param ip       客户端 IP
     * @param username 登录名（原始输入，内部规范化为小写去空白）
     * @throws BusinessException 429 请求过于频繁（文案不泄露账号是否存在）
     */
    public void checkAllowed(String ip, String username) {
        if (!properties.isEnabled()) {
            return;
        }
        String user = normalizeUsername(username);
        String ipKey = scopeOf("ip", ip);
        String pairKey = scopeOf("pair", ip + "|" + user);
        boolean locked = isLocked(ipKey) || isLocked(pairKey);
        if (locked) {
            log.warn("login throttled ip={} username={}", ip, user);
            throw new BusinessException(ErrorCode.RATE_LIMITED);
        }
        // R-14：账号维度跨 IP 风险聚合——不硬锁号，改为封顶的递增延迟。
        // 分布式来源对同一账号的爆破会被整体拖慢，但攻击者无法借账号维度把受害者长期锁死。
        long accountFailures = currentCount(scopeOf("account", user));
        if (accountFailures >= properties.getAccountMaxFailures()) {
            long delayMs = accountDelayMillis(accountFailures);
            log.warn("login account risk throttle ip={} username={} failures={} delayMs={}",
                    ip, user, accountFailures, delayMs);
            sleepQuietly(delayMs);
        }
    }

    /**
     * 记录一次失败：对应维度计数 +1，达到阈值即按失败次数渐进加锁。
     *
     * <p>账号维度（{@code account}）只累计风险计数、不设锁，用于跨 IP 聚合与递增延迟。
     *
     * @param ip       客户端 IP
     * @param username 登录名
     */
    public void recordFailure(String ip, String username) {
        if (!properties.isEnabled()) {
            return;
        }
        String user = normalizeUsername(username);
        incrementAndMaybeLock(scopeOf("ip", ip));
        incrementAndMaybeLock(scopeOf("pair", ip + "|" + user));
        increment(scopeOf("account", user), properties.getAccountWindowSeconds());
    }

    /**
     * 记录一次成功：清零对应维度的失败计数与锁（含账号维度风险计数）。
     *
     * @param ip       客户端 IP
     * @param username 登录名
     */
    public void recordSuccess(String ip, String username) {
        if (!properties.isEnabled()) {
            return;
        }
        String user = normalizeUsername(username);
        clear(scopeOf("ip", ip));
        clear(scopeOf("pair", ip + "|" + user));
        clear(scopeOf("account", user));
    }

    /** 观测用：Redis 是否处于降级（改用进程内计数）状态。 */
    public boolean isRedisDegraded() {
        return redisDegradedUntilMs > System.currentTimeMillis();
    }

    /** 测试辅助：清空全部限速状态（内存与 Redis 键）。 */
    public void resetAll() {
        memory.clear();
        redisDegradedUntilMs = 0L;
        try {
            redisTemplate.delete(redisTemplate.keys(KEY_PREFIX + "*"));
        } catch (RuntimeException ex) {
            log.warn("login throttle reset skipped, redis unavailable: {}", ex.getMessage());
        }
    }

    private void incrementAndMaybeLock(String scope) {
        long count = increment(scope, properties.getWindowSeconds());
        if (count < properties.getMaxFailures()) {
            return;
        }
        long lockMs = lockMillisFor(count);
        lock(scope, lockMs);
        log.warn("login throttle armed scope={} failures={} lockMs={}", scope, count, lockMs);
    }

    private long increment(String scope, long windowSeconds) {
        String key = scope + FAIL_SUFFIX;
        try {
            Long count = redisTemplate.opsForValue().increment(key);
            if (count != null && count == 1L) {
                redisTemplate.expire(key, Duration.ofSeconds(windowSeconds));
            }
            markRedisHealthy();
            return count == null ? 0L : count;
        } catch (RuntimeException ex) {
            markRedisDegraded(ex);
            return memoryIncrement(scope, windowSeconds);
        }
    }

    /** 读取当前维度计数（Redis 优先，降级读内存）；异常一律按 0 处理，不影响登录主流程。 */
    private long currentCount(String scope) {
        String key = scope + FAIL_SUFFIX;
        try {
            String value = redisTemplate.opsForValue().get(key);
            markRedisHealthy();
            if (value == null) {
                return 0L;
            }
            try {
                return Long.parseLong(value.trim());
            } catch (NumberFormatException ex) {
                return 0L;
            }
        } catch (RuntimeException ex) {
            markRedisDegraded(ex);
            MemoryRecord record = memory.get(scope);
            return record == null ? 0L : record.failCount;
        }
    }

    /** 账号维度递增延迟：每超出阈值 1 次加一个步长，封顶 accountMaxDelayMs。 */
    private long accountDelayMillis(long failures) {
        long over = Math.max(1L, failures - properties.getAccountMaxFailures() + 1L);
        long delay = properties.getAccountBaseDelayMs() * over;
        return Math.min(delay, properties.getAccountMaxDelayMs());
    }

    private void sleepQuietly(long delayMs) {
        if (delayMs <= 0) {
            return;
        }
        try {
            Thread.sleep(delayMs);
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
        }
    }

    private boolean isLocked(String scope) {
        String key = scope + LOCK_SUFFIX;
        try {
            Boolean present = redisTemplate.hasKey(key);
            markRedisHealthy();
            return Boolean.TRUE.equals(present);
        } catch (RuntimeException ex) {
            markRedisDegraded(ex);
            return memoryIsLocked(scope);
        }
    }

    private void lock(String scope, long lockMs) {
        String key = scope + LOCK_SUFFIX;
        try {
            redisTemplate.opsForValue().set(key, LOCK_VALUE, Duration.ofMillis(lockMs));
            markRedisHealthy();
        } catch (RuntimeException ex) {
            markRedisDegraded(ex);
            memoryLock(scope, lockMs);
        }
    }

    private void clear(String scope) {
        String failKey = scope + FAIL_SUFFIX;
        String lockKey = scope + LOCK_SUFFIX;
        try {
            redisTemplate.delete(List.of(failKey, lockKey));
            markRedisHealthy();
        } catch (RuntimeException ex) {
            markRedisDegraded(ex);
            memory.remove(scope);
        }
    }

    /** 按超出阈值的档数翻倍，封顶 maxLockSeconds。 */
    private long lockMillisFor(long failCount) {
        long baseMs = Math.max(1L, properties.getLockSeconds()) * 1000L;
        long maxMs = Math.max(baseMs, properties.getMaxLockSeconds() * 1000L);
        long lockMs = baseMs;
        int steps = (int) Math.min(Math.max(0L, failCount - properties.getMaxFailures()), MAX_BACKOFF_STEPS);
        for (int i = 0; i < steps && lockMs < maxMs; i++) {
            lockMs = Math.min(lockMs * 2, maxMs);
        }
        return lockMs;
    }

    /* ---------------- 内存降级 ---------------- */

    private long memoryIncrement(String scope, long windowSeconds) {
        long now = System.currentTimeMillis();
        long windowMs = windowSeconds * 1000L;
        MemoryRecord updated = memory.compute(scope, (key, prev) -> {
            MemoryRecord record = prev;
            if (record == null || record.windowExpireAtMs <= now) {
                record = new MemoryRecord();
                record.windowExpireAtMs = now + windowMs;
            }
            record.failCount++;
            if (record.failCount >= properties.getMaxFailures()) {
                record.lockUntilMs = Math.max(record.lockUntilMs, now + lockMillisFor(record.failCount));
            }
            return record;
        });
        return updated.failCount;
    }

    private boolean memoryIsLocked(String scope) {
        MemoryRecord record = memory.get(scope);
        if (record == null) {
            return false;
        }
        long now = System.currentTimeMillis();
        if (record.lockUntilMs > now) {
            return true;
        }
        if (record.windowExpireAtMs <= now) {
            memory.remove(scope, record);
        }
        return false;
    }

    private void memoryLock(String scope, long lockMs) {
        memory.compute(scope, (key, prev) -> {
            MemoryRecord record = prev == null ? new MemoryRecord() : prev;
            record.lockUntilMs = Math.max(record.lockUntilMs, System.currentTimeMillis() + lockMs);
            return record;
        });
    }

    /* ---------------- 工具 ---------------- */

    private String scopeOf(String kind, String identity) {
        return KEY_PREFIX + kind + ":" + identity;
    }

    /** 规范化账号：去空白 + 小写，避免大小写/空白变体绕过计数。 */
    private String normalizeUsername(String username) {
        return username == null ? "" : username.strip().toLowerCase(Locale.ROOT);
    }

    private void markRedisDegraded(RuntimeException ex) {
        long now = System.currentTimeMillis();
        if (redisDegradedUntilMs < now) {
            redisDegradedUntilMs = now + REDIS_DEGRADE_COOLDOWN_MS;
            log.warn("login throttle redis degraded, fallback to in-memory: {}", ex.toString());
        }
    }

    private void markRedisHealthy() {
        if (redisDegradedUntilMs != 0L) {
            redisDegradedUntilMs = 0L;
            log.info("login throttle redis recovered");
        }
    }

    /** 内存计数记录：失败窗口与锁定截止时间。 */
    private static final class MemoryRecord {

        private int failCount;
        private long windowExpireAtMs;
        private long lockUntilMs;
    }
}
