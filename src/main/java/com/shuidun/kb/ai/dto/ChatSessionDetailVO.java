package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 历史会话详情（docs/api/api.md §4 GET /api/ai/chat/sessions/{sessionId}）。
 */
@Data
@AllArgsConstructor
public class ChatSessionDetailVO {

    /** 会话 id。 */
    private String sessionId;

    /** 按时间正序的问答轮次。 */
    private List<ChatRoundVO> rounds;
}
