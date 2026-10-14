package com.shuidun.kb.dashboard.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 问答访问日志，映射 qa_access_logs 表（docs/services/database.md §2.9）。
 */
@Data
public class QaAccessLog {

    /** 主键。 */
    private Long id;

    /** 会话 id。 */
    private String sessionId;

    /** 用户 id。 */
    private Long userId;

    /** 问题。 */
    private String question;

    /** 答案。 */
    private String answer;

    /** 召回 unit id（JSON 数组）。 */
    private String recalledUnitIdsJson;

    /** 授权 unit id（JSON 数组）。 */
    private String authorizedUnitIdsJson;

    /** 未授权 unit id（JSON 数组）。 */
    private String unauthorizedUnitIdsJson;

    /** 输入 token。 */
    private Integer promptTokens;

    /** 输出 token。 */
    private Integer completionTokens;

    /** 总 token。 */
    private Integer totalTokens;

    /** 响应耗时（毫秒，端到端）。 */
    private Integer responseTimeMs;

    /** 检索阶段耗时（毫秒）。 */
    private Integer retrieveMs;

    /** 生成阶段耗时（毫秒）。 */
    private Integer generateMs;

    /** 数据库耗时（毫秒，MySQL）。 */
    private Integer dbMs;

    /** 缓存耗时（毫秒，Redis）。 */
    private Integer cacheMs;

    /** 澄清点选：本轮由用户从候选中选中的知识单元 id；未点选为 null。 */
    private Long chosenUnitId;

    /**
     * 答案来源，取值见 DESIGN.md：
     * {@code kb}（检索/点选生成）、{@code faq}（FAQ 缓存直出）、
     * {@code llm_general}（闲聊兜底）、{@code clarify}（澄清追问/参考资料）、{@code refusal}（拒答）。
     */
    private String answerSource;

    /** 是否命中 FAQ。 */
    private Integer faqHit;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
