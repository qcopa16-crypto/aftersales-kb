package com.shuidun.kb.job;

import com.shuidun.kb.config.QualityReviewProperties;
import java.util.concurrent.atomic.AtomicInteger;
import org.springframework.stereotype.Component;

/**
 * 轻量质检熔断器（P7 语义：失败即转人工，不做阻塞式重试）。
 *
 * <p>连续失败达到阈值后进入冷却期；冷却期内直接 fail-closed 转人工，不再调用 AI，
 * 避免 AI 侧持续故障时拖垮单线程 Worker。成功调用即清零。冷却期结束进入 half-open，
 * 放行一次探测。
 *
 * <p>线程安全（Worker 单线程 + 单元测试并发），状态用原子量/volatile 保护。
 */
@Component
public class ReviewCircuitBreaker {

    private final int failureThreshold;
    private final long cooldownMs;
    private final AtomicInteger consecutiveFailures = new AtomicInteger();
    private volatile long openUntilMillis;

    /**
     * 构造熔断器。
     *
     * @param props 质检配置（阈值与冷却时长）
     */
    public ReviewCircuitBreaker(QualityReviewProperties props) {
        this.failureThreshold = Math.max(1, props.getReviewCircuitFailureThreshold());
        this.cooldownMs = Math.max(0, props.getReviewCircuitCooldownMs());
    }

    /**
     * 熔断是否打开（冷却期内为 true）。
     *
     * @return true 表示应直接 fail-closed，不再调用 AI
     */
    public boolean isOpen() {
        long until = openUntilMillis;
        if (until == 0) {
            return false;
        }
        if (System.currentTimeMillis() < until) {
            return true;
        }
        // 冷却期结束：half-open，放行一次探测
        openUntilMillis = 0;
        return false;
    }

    /** 记录一次成功：清零失败计数并关闭熔断。 */
    public void recordSuccess() {
        consecutiveFailures.set(0);
        openUntilMillis = 0;
    }

    /** 记录一次失败：连续失败达阈值即打开熔断并清零计数。 */
    public void recordFailure() {
        if (consecutiveFailures.incrementAndGet() >= failureThreshold) {
            openUntilMillis = System.currentTimeMillis() + cooldownMs;
            consecutiveFailures.set(0);
        }
    }

    /** 重置（测试用）。 */
    public void reset() {
        consecutiveFailures.set(0);
        openUntilMillis = 0;
    }
}
