package com.shuidun.kb.config;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * AI 服务调用配置（代码设计规范 §3.8 / integration.md）。
 *
 * <p>Base URL、Internal Token 与各类超时全部来自配置，禁止硬编码。
 */
@Data
@ConfigurationProperties(prefix = "ai")
public class AiProperties {

    /** AI 服务 Base URL，本地 http://127.0.0.1:8000，部署 http://app-ai:8000。 */
    private String baseUrl = "http://127.0.0.1:8000";

    /** 内网鉴权 Token，与 AI 侧 INTERNAL_TOKEN 一致。 */
    private String internalToken;

    /** 连接超时（毫秒）。 */
    private int connectTimeoutMs = 5000;

    /** parse 超时（毫秒），大 PDF 需 >= 60s。 */
    private int parseTimeoutMs = 900000;

    /** embed 超时（毫秒），需 >= 60s。 */
    private int embedTimeoutMs = 120000;

    /** retrieve 超时（毫秒）。 */
    private int retrieveTimeoutMs = 15000;

    /** 删除单元向量（DELETE /internal/units/{id}）超时（毫秒）。 */
    private int deleteTimeoutMs = 15000;

    /** VLM 图注（POST /internal/vlm/describe）超时（毫秒）：单张图片描述，需比 retrieve 宽松。 */
    private int vlmTimeoutMs = 60000;

    /**
     * 入库质检（POST /internal/review）独立读超时（毫秒）。
     *
     * <p>必须大于 AI 侧最坏串行预算：LLM 阶段 {@code review_llm_timeout_sec=20} +
     * VLM 阶段 {@code review_vlm_timeout_sec=60} = 80s，再加网络/序列化余量。
     * 默认 95000（80s + 15s 余量）：低于该值会把可完成的质检误判为失败/降级，
     * 诱发重复调用昂贵模型与熔断误记（F-22）。仍然是有界值，不设无上限超时。
     */
    private int reviewTimeoutMs = 95000;

    /** generate 流式超时（毫秒），需 >= 120s。 */
    private int generateTimeoutMs = 180000;

    /** session/append 超时（毫秒）：会话记忆是 best-effort，必须短超时，绝不拖慢问答主链路。 */
    private int sessionTimeoutMs = 1000;

    /** owner_id 盐：{@code sha256(userId + 盐)[:16]}，避免直接暴露 userId（AI 只当分区键）。 */
    private String sessionOwnerSalt = "aftersales-kb-owner-salt";

    /**
     * 闲聊/通用问答兜底开关（默认关）。开启后，知识库没命中且问题**非业务性**时，
     * 用 AI 通用知识回答（全自动，无用户确认环节）。
     *
     * <p>关闭时行为与历史完全一致（拒答 + 参考资料），故可安全灰度与回滚。
     */
    private boolean chatFallbackEnabled = false;

    /**
     * 业务性关键词**补充**表（运营可配），与 {@code BusinessIntentClassifier} 的内置词表取并集。
     *
     * <p>只增不减：避免配置覆盖削弱默认安全网（宁枉勿纵）。
     */
    private List<String> chatBusinessKeywords = new ArrayList<>();
}
