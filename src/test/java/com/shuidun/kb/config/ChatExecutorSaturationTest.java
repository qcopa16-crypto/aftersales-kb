package com.shuidun.kb.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

/**
 * P4 回归：问答线程池饱和时必须「拒绝」，绝不能无限堆积（OOM）。
 *
 * <p>用极小容量实例（并发 1 + 队列 1）把池打满，断言第 N+1 个任务抛
 * {@link RejectedExecutionException}——即饱和被显式暴露给调用方，而非静默堆积。
 */
class ChatExecutorSaturationTest {

    @Test
    void saturatedPoolRejectsInsteadOfAccumulating() throws Exception {
        ThreadPoolExecutor executor = tinyExecutor("abort");
        try {
            CountDownLatch started = new CountDownLatch(1);
            CountDownLatch release = new CountDownLatch(1);
            // 占住唯一工作线程
            executor.execute(() -> {
                started.countDown();
                awaitQuietly(release);
            });
            assertTrue(started.await(2, TimeUnit.SECONDS), "工作线程应已启动");
            // 占满唯一队列位
            executor.execute(() -> awaitQuietly(release));
            assertEquals(1, executor.getQueue().size(), "队列应有界且被占满");

            // 第 3 个任务：池满 + 队列满 → 必须被拒绝
            assertThrows(RejectedExecutionException.class, () -> executor.execute(() -> { }));

            release.countDown();
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    void workerThreadsAreNamedChatSseAndDaemon() throws Exception {
        ThreadPoolExecutor executor = tinyExecutor("abort");
        try {
            AtomicReference<Thread> captured = new AtomicReference<>();
            CountDownLatch done = new CountDownLatch(1);
            executor.execute(() -> {
                captured.set(Thread.currentThread());
                done.countDown();
            });
            assertTrue(done.await(2, TimeUnit.SECONDS), "任务应被执行");
            assertEquals("chat-sse", captured.get().getName());
            assertTrue(captured.get().isDaemon(), "chat-sse 必须是 daemon 线程");
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    void rejectPolicyParsingIsFailSafe() {
        assertInstanceOf(ThreadPoolExecutor.CallerRunsPolicy.class,
                AsyncConfig.resolveRejectPolicy("caller-runs"));
        assertInstanceOf(ThreadPoolExecutor.AbortPolicy.class,
                AsyncConfig.resolveRejectPolicy("abort"));
        // R-12：会静默丢弃工作项的策略在启动期被拒绝
        assertThrows(IllegalStateException.class, () -> AsyncConfig.resolveRejectPolicy("discard"));
        assertThrows(IllegalStateException.class, () -> AsyncConfig.resolveRejectPolicy("discard_oldest"));
        assertThrows(IllegalStateException.class, () -> AsyncConfig.resolveRejectPolicy("discard-oldest"));
        // 未知值 fail-safe 回退 abort（而非静默接受）
        assertInstanceOf(ThreadPoolExecutor.AbortPolicy.class,
                AsyncConfig.resolveRejectPolicy("no-such-policy"));
        assertInstanceOf(ThreadPoolExecutor.AbortPolicy.class,
                AsyncConfig.resolveRejectPolicy(null));
    }

    @Test
    void buildingExecutorWithDiscardPolicyFailsFast() {
        // R-12：部署把 reject-policy 切成 discard/discard_oldest 时，Bean 创建即失败（拒绝启动），
        // 而不是等到线上过载才发现 SSE 被静默丢弃。
        assertThrows(IllegalStateException.class, () -> tinyExecutor("discard"));
        assertThrows(IllegalStateException.class, () -> tinyExecutor("discard_oldest"));
    }

    private ThreadPoolExecutor tinyExecutor(String rejectPolicy) {
        ChatExecutorProperties properties = new ChatExecutorProperties();
        properties.setConcurrency(1);
        properties.setQueueCapacity(1);
        properties.setRejectPolicy(rejectPolicy);
        return AsyncConfig.buildChatExecutor(properties);
    }

    private static void awaitQuietly(CountDownLatch latch) {
        try {
            latch.await(10, TimeUnit.SECONDS);
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
        }
    }
}
