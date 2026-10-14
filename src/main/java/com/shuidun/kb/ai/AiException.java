package com.shuidun.kb.ai;

import com.shuidun.kb.common.ErrorCode;
import lombok.Getter;

/**
 * AI 服务调用异常。携带可映射为业务错误码的语义。
 */
@Getter
public class AiException extends RuntimeException {

    private final ErrorCode errorCode;

    /** AI 侧返回的错误码，如 PARSE_FAILED / EMBED_FAILED / MINERU_TIMEOUT。 */
    private final String aiErrorCode;

    /**
     * 构造 AI 调用异常。
     *
     * @param errorCode  业务错误码（50001 / 50002）
     * @param aiErrorCode AI 侧错误码，可为 null
     * @param message    人类可读信息
     */
    public AiException(ErrorCode errorCode, String aiErrorCode, String message) {
        super(message);
        this.errorCode = errorCode;
        this.aiErrorCode = aiErrorCode;
    }
}
