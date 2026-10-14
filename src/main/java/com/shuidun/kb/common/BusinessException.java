package com.shuidun.kb.common;

import lombok.Getter;

/**
 * 业务异常（代码设计规范 §3.3）。由全局异常处理器统一转为 {@link Result}。
 */
@Getter
public class BusinessException extends RuntimeException {

    private final int code;
    private final int httpStatus;

    /**
     * 按错误码构造业务异常。
     *
     * @param errorCode 错误码
     */
    public BusinessException(ErrorCode errorCode) {
        super(errorCode.getDefaultMessage());
        this.code = errorCode.getCode();
        this.httpStatus = errorCode.getHttpStatus();
    }

    /**
     * 按错误码与自定义信息构造业务异常。
     *
     * @param errorCode 错误码
     * @param message   人类可读信息
     */
    public BusinessException(ErrorCode errorCode, String message) {
        super(message);
        this.code = errorCode.getCode();
        this.httpStatus = errorCode.getHttpStatus();
    }
}
