package com.shuidun.kb.config;

import java.util.Locale;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.RejectedExecutionHandler;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 异步执行配置。
 *
 * <p>问答 SSE 需要在独立线程中执行"检索 → 过滤 → 生成"，避免占用 Servlet 容器线程。
 */
@Configuration
public class AsyncConfig {

    private final ChatExecutorProperties properties;

    public AsyncConfig(ChatExecutorProperties properties) {
        this.properties = properties;
    }

    /**
     * 问答流式执行线程池：有界队列 + 显式拒绝策略。
     *
     * <p>过载时由 {@link ThreadPoolExecutor} 抛出 {@link java.util.concurrent.RejectedExecutionException}，
     * 调用方（{@code ChatOrchestrator.stream}）捕获后向客户端回「服务繁忙」的 SSE error 事件，
     * 而不是让任务无限堆积至 OOM。
     *
     * <p>R-12：拒绝策略在 Bean 创建期解析，配置为 {@code discard}/{@code discard_oldest}
     * 时直接抛异常拒绝启动，避免"静默丢弃工作项、SSE 挂到超时"的部署事故。
     *
     * @return 有界线程池
     */
    @Bean(name = "chatExecutor", destroyMethod = "shutdown")
    public ExecutorService chatExecutor() {
        return buildChatExecutor(properties);
    }

    /**
     * 按配置构建线程池（抽为静态方法便于单测直接构造极小容量实例验证拒绝行为）。
     *
     * @param properties 线程池配置
     * @return 有界线程池
     */
    public static ThreadPoolExecutor buildChatExecutor(ChatExecutorProperties properties) {
        int concurrency = Math.max(1, properties.getConcurrency());
        int queueCapacity = Math.max(1, properties.getQueueCapacity());
        return new ThreadPoolExecutor(
                concurrency,
                concurrency,
                0L,
                TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(queueCapacity),
                runnable -> {
                    Thread thread = new Thread(runnable, "chat-sse");
                    thread.setDaemon(true);
                    return thread;
                },
                resolveRejectPolicy(properties.getRejectPolicy()));
    }

    /**
     * 解析拒绝策略配置（R-12：启动即拒绝会静默丢弃工作项的策略）。
     *
     * <p>问答 SSE 一旦被静默丢弃，客户端会一直挂到 emitter 超时也收不到任何终态，
     * 无法与"服务繁忙"区分。因此只允许两类**不会静默丢任务**的策略：
     * <ul>
     *   <li>{@code abort}（默认）：向提交方抛 {@link java.util.concurrent.RejectedExecutionException}，
     *       由 {@code ChatOrchestrator.stream} 捕获后回显式 error 终态；</li>
     *   <li>{@code caller_runs}：在提交线程内联执行，任务不会被丢弃（代价是占用容器线程）。</li>
     * </ul>
     * {@code discard} / {@code discard_oldest} 会在池满时**静默丢弃**工作项，直接拒绝启动；
     * 未知值 fail-safe 回退 {@code abort}（而非静默接受）。
     *
     * @param policy 策略名（大小写、连字符/下划线不敏感）
     * @return 拒绝策略
     * @throws IllegalStateException 配置了 discard / discard_oldest 等会静默丢弃的策略
     */
    static RejectedExecutionHandler resolveRejectPolicy(String policy) {
        if (policy == null || policy.isBlank()) {
            return new ThreadPoolExecutor.AbortPolicy();
        }
        String normalized = policy.trim().toLowerCase(Locale.ROOT).replace('-', '_');
        return switch (normalized) {
            case "abort" -> new ThreadPoolExecutor.AbortPolicy();
            case "caller_runs" -> new ThreadPoolExecutor.CallerRunsPolicy();
            case "discard", "discard_oldest" -> throw new IllegalStateException(
                    "app.chat.executor.reject-policy=" + policy + " 会在过载时静默丢弃问答工作项，"
                            + "导致 SSE 客户端收不到任何终态。生产仅允许 abort 或 caller_runs。");
            default -> new ThreadPoolExecutor.AbortPolicy();
        };
    }
}
