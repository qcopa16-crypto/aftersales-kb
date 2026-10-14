package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 历史会话中的一轮问答（docs/api/api.md §4 GET /api/ai/chat/sessions/{sessionId}）。
 *
 * <p>字段与 SSE 的 delta/done/sources 事件对齐，前端可复用同一套消息渲染。
 */
@Data
@AllArgsConstructor
public class ChatRoundVO {

    /** 用户提问。 */
    private String question;

    /** 助手回答（已落库的最终文本）。 */
    private String answer;

    /** 该轮消耗 token（prompt + completion）。 */
    private long tokens;

    /** 端到端耗时（毫秒）。 */
    private long elapsedMs;

    /** 是否命中 FAQ 缓存。 */
    private boolean faqHit;

    /** 答案来源：kb/faq/llm_general/clarify/refusal（历史回看用）。 */
    private String answerSource;

    /** 提问时间（yyyy-MM-dd HH:mm）。 */
    private String createdAt;

    /** 澄清点选：本轮由用户从候选中选中的知识单元 id；未点选为 null。 */
    private Long chosenUnitId;

    /** 澄清点选：选中单元的标题（已删除的单元为 null）。 */
    private String chosenTitle;

    /** 来源卡片（按落库的 unit id 反查标题；已删除的单元会被跳过）。 */
    private SourcesEvent sources;
}
