package com.shuidun.kb.ai.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/**
 * 问答请求（docs/api/api.md §4 POST /api/ai/chat/stream）。
 */
@Data
public class ChatRequest {

    /**
     * 问题长度上限，与 AI 侧 {@code RETRIEVE_MAX_QUESTION_CHARS} / {@code GENERATE_MAX_QUESTION_CHARS}
     * 默认值 2000 对齐，避免超长入参打到下游（安全审计 F-013）。
     */
    public static final int MAX_QUESTION_CHARS = 2000;

    /**
     * 会话 id 长度上限，与 {@code qa_access_logs.session_id} 的 VARCHAR(64) 对齐。
     *
     * <p>与历史会话详情接口（GET /chat/sessions/{sessionId}）共用同一上限，避免两处限制互相矛盾。
     */
    public static final int MAX_SESSION_ID_CHARS = 64;

    /** 会话 id。 */
    @Size(max = MAX_SESSION_ID_CHARS, message = "会话 id 长度不能超过 64")
    private String sessionId;

    /** 用户问题。 */
    @NotBlank(message = "问题不能为空")
    @Size(max = MAX_QUESTION_CHARS, message = "问题长度不能超过 2000 字符")
    private String question;

    /**
     * 澄清选择：用户从上一轮 clarify 候选里点选的知识单元 id。
     *
     * <p>有值时跳过向量检索，直接用该单元正文作答。<b>服务端必须校验该单元在当前用户的可访问集合内</b>，
     * 否则等于开放「传任意 unitId 读任意知识」的越权读取。
     */
    private Long chosenUnitId;
}
