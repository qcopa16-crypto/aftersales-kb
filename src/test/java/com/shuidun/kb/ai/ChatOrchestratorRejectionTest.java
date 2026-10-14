package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.ChatRequest;
import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.config.AsyncConfig;
import com.shuidun.kb.config.ChatExecutorProperties;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * P4 回归：线程池饱和被拒绝时，{@code ChatOrchestrator.stream} 必须降级收尾（发 error 事件 + complete），
 * 而不是把 {@link java.util.concurrent.RejectedExecutionException} 抛给容器变成裸 500。
 */
class ChatOrchestratorRejectionTest {

    @Test
    void saturatedExecutorDegradesToCompletedEmitter() throws Exception {
        ChatExecutorProperties properties = new ChatExecutorProperties();
        properties.setConcurrency(1);
        properties.setQueueCapacity(1);
        ThreadPoolExecutor executor = AsyncConfig.buildChatExecutor(properties);
        try {
            CountDownLatch release = new CountDownLatch(1);
            executor.execute(() -> awaitQuietly(release));   // 占住工作线程
            executor.execute(() -> awaitQuietly(release));   // 占满队列

            // 其余依赖在 stream() 被拒绝路径上不会被触达，传 null 即可
            ChatOrchestrator orchestrator = new ChatOrchestrator(
                    null, null, null, null, null, null, null, null, null, executor);

            CurrentUser user = new CurrentUser();
            user.setId(1L);
            ChatRequest request = new ChatRequest();
            request.setQuestion("保修多久");

            SseEmitter emitter = orchestrator.stream(request, user);

            // 已降级：emitter 被 complete（再次 send 抛「already completed」），且未向外抛异常
            IllegalStateException ex = assertThrows(IllegalStateException.class,
                    () -> emitter.send(SseEmitter.event().name("x").data("y")));
            assertTrue(ex.getMessage().contains("already completed"),
                    "饱和时应已 complete 发射器，实际: " + ex.getMessage());

            release.countDown();
        } finally {
            executor.shutdownNow();
        }
    }

    private static void awaitQuietly(CountDownLatch latch) {
        try {
            latch.await(10, TimeUnit.SECONDS);
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
        }
    }
}
