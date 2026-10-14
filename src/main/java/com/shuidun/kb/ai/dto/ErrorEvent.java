package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE error 事件载荷。
 */
@Data
@AllArgsConstructor
public class ErrorEvent {

    /** 错误码。 */
    private String code;

    /** 人类可读信息。 */
    private String message;
}
