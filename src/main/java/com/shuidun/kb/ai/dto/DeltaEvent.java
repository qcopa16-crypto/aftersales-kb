package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE delta 事件载荷。
 */
@Data
@AllArgsConstructor
public class DeltaEvent {

    /** 增量文本。 */
    private String text;
}
