package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE done 事件载荷。
 */
@Data
@AllArgsConstructor
public class DoneEvent {

    /** 会话 id。 */
    private String sessionId;

    /** 总 token。 */
    private int tokens;

    /** 端到端耗时（毫秒）。 */
    private long elapsedMs;

    /** 是否命中 FAQ 缓存。 */
    private boolean faqHit;
}
