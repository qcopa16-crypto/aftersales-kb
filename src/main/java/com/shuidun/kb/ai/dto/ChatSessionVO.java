package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 历史会话列表项（docs/api/api.md §4 GET /api/ai/chat/sessions）。
 */
@Data
@AllArgsConstructor
public class ChatSessionVO {

    /** 会话 id。 */
    private String sessionId;

    /** 会话标题：取首问前若干字，避免前端再截断。 */
    private String title;

    /** 该会话的问答轮次数。 */
    private long messageCount;

    /** 最近一次提问时间（yyyy-MM-dd HH:mm）。 */
    private String lastAskedAt;
}
