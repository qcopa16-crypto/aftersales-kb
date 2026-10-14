package com.shuidun.kb.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 问答流式执行线程池配置（代码设计规范 §3.7）。
 *
 * <p>线程池必须有界：问答 SSE 是长连接，无界队列在流量突增时会无限堆积任务直至 OOM；
 * 有界队列 + 显式拒绝策略把过载转化为可识别的降级（服务繁忙），而非进程崩溃。
 */
@Data
@ConfigurationProperties(prefix = "app.chat.executor")
public class ChatExecutorProperties {

    /** 并发线程数（core=max），即同时处理的问答请求上限。 */
    private int concurrency = 16;

    /**
     * 有界队列容量。
     *
     * <p>保守取值：16 并发 + 100 排队，超过即拒绝并回「服务繁忙」，而不是无限堆积。
     */
    private int queueCapacity = 100;

    /**
     * 拒绝策略：{@code abort}（默认）/ {@code caller_runs}。
     *
     * <p>默认 {@code abort}：SSE 长连接场景下 {@code caller_runs} 会占用 Servlet 容器线程，
     * 反而拖垮整个服务，故不默认采用。
     *
     * <p><b>R-12</b>：{@code discard} / {@code discard_oldest} 会在池满时静默丢弃问答工作项，
     * 客户端收不到任何终态，启动时即被拒绝（见 {@code AsyncConfig.resolveRejectPolicy}）。
     */
    private String rejectPolicy = "abort";
}
