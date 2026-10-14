package com.shuidun.kb.common;

import lombok.Getter;

/**
 * 统一错误码枚举（代码设计规范 §3.3）。
 *
 * <p>禁止自造 code；40001 的业务语义要求 HTTP 固定 200，靠 body 的 code 区分。
 */
@Getter
public enum ErrorCode {

    /** 成功。 */
    SUCCESS(0, 200, "ok"),
    /** 参数校验失败。 */
    PARAM_INVALID(400, 400, "参数校验失败"),
    /** 未登录或令牌无效。 */
    UNAUTHORIZED(401, 401, "未登录或令牌无效"),
    /** 无操作权限（操作权限码不足）。 */
    FORBIDDEN(403, 403, "无操作权限"),
    /** 登录尝试过于频繁（限速，F-11）。文案不泄露账号是否存在。 */
    RATE_LIMITED(429, 429, "登录尝试过于频繁，请稍后再试"),
    /** 请求方法不支持（如对只读接口使用了 POST）。 */
    METHOD_NOT_ALLOWED(405, 405, "请求方法不支持"),
    /** 数据权限不足（业务语义，HTTP 固定 200）。 */
    DATA_PERMISSION_DENIED(40001, 200, "数据权限不足"),
    /** 上传文件超过大小限制。 */
    UPLOAD_TOO_LARGE(40002, 413, "上传文件过大"),
    /** 系统错误。 */
    SYSTEM_ERROR(500, 500, "系统错误"),
    /** AI 服务不可用。 */
    AI_UNAVAILABLE(50001, 502, "AI 服务不可用"),
    /** AI 解析 / 向量化失败。 */
    AI_PROCESS_FAILED(50002, 500, "AI 解析或向量化失败"),
    /** 服务繁忙（问答线程池饱和，请求被拒绝，客户端可稍后重试）。 */
    SERVICE_BUSY(50003, 503, "服务繁忙，请稍后重试");

    private final int code;
    private final int httpStatus;
    private final String defaultMessage;

    ErrorCode(int code, int httpStatus, String defaultMessage) {
        this.code = code;
        this.httpStatus = httpStatus;
        this.defaultMessage = defaultMessage;
    }
}
