package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE {@code notice} 事件载荷：给前端的显著提示横幅。
 *
 * <p>闲聊/通用问答兜底时，先发 notice 再发 delta，让用户在答案之前就知道
 * "以下为 AI 通用知识，非公司规定"（安全模型的第 3 层：UI 显著标注）。
 */
@Data
@AllArgsConstructor
public class NoticeEvent {

    /** 提示类型，如 {@code llm_general}。 */
    private String kind;

    /** 提示文案。 */
    private String text;
}
