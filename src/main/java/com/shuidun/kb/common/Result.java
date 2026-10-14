package com.shuidun.kb.common;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 统一响应包装（代码设计规范 §3.3）。所有对前端的响应必须使用本类型。
 *
 * @param <T> 业务数据类型
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class Result<T> {

    /** 业务码：0 成功；非 0 见 {@link ErrorCode}。 */
    private Integer code;

    /** 人类可读信息；成功固定 ok。 */
    private String message;

    /** 业务数据。 */
    private T data;

    /**
     * 构造成功响应。
     *
     * @param data 业务数据，可为 null
     * @return 成功结果
     */
    public static <T> Result<T> ok(T data) {
        return new Result<>(ErrorCode.SUCCESS.getCode(), ErrorCode.SUCCESS.getDefaultMessage(), data);
    }

    /**
     * 构造无数据的成功响应。
     *
     * @return 成功结果
     */
    public static <T> Result<T> ok() {
        return ok(null);
    }

    /**
     * 按错误码构造失败响应。
     *
     * @param errorCode 错误码
     * @return 失败结果
     */
    public static <T> Result<T> fail(ErrorCode errorCode) {
        return new Result<>(errorCode.getCode(), errorCode.getDefaultMessage(), null);
    }

    /**
     * 按错误码与自定义信息构造失败响应。
     *
     * @param errorCode 错误码
     * @param message   人类可读信息
     * @return 失败结果
     */
    public static <T> Result<T> fail(ErrorCode errorCode, String message) {
        return new Result<>(errorCode.getCode(), message, null);
    }
}
