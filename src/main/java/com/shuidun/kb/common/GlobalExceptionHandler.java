package com.shuidun.kb.common;

import jakarta.servlet.http.HttpServletRequest;
import java.util.stream.Collectors;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.validation.BindException;
import org.springframework.validation.FieldError;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;

/**
 * 全局异常处理（代码设计规范 §3.3）。
 *
 * <p>统一把业务异常、参数校验异常与系统异常转换为 {@link Result}，避免 Controller 手工 try-catch。
 */
@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

    /**
     * 处理业务异常，按错误码映射 HTTP 状态（40001 固定 200）。
     *
     * @param ex      业务异常
     * @param request 当前请求
     * @return 统一响应
     */
    @ExceptionHandler(BusinessException.class)
    public ResponseEntity<Result<Void>> handleBusiness(BusinessException ex, HttpServletRequest request) {
        log.warn("business error uri={} code={} message={}", request.getRequestURI(), ex.getCode(), ex.getMessage());
        return ResponseEntity.status(ex.getHttpStatus())
                .body(new Result<>(ex.getCode(), ex.getMessage(), null));
    }

    /**
     * 处理请求体参数校验失败。
     *
     * @param ex 校验异常
     * @return 400 统一响应
     */
    @ExceptionHandler({MethodArgumentNotValidException.class, BindException.class})
    public ResponseEntity<Result<Void>> handleValidation(BindException ex) {
        String message = ex.getBindingResult().getFieldErrors().stream()
                .map(this::formatFieldError)
                .collect(Collectors.joining("; "));
        if (message.isEmpty()) {
            message = ErrorCode.PARAM_INVALID.getDefaultMessage();
        }
        return ResponseEntity.status(ErrorCode.PARAM_INVALID.getHttpStatus())
                .body(Result.fail(ErrorCode.PARAM_INVALID, message));
    }

    /**
     * 处理操作权限不足（Spring Security 抛出）。
     *
     * @param ex 访问拒绝异常
     * @return 403 统一响应
     */
    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<Result<Void>> handleAccessDenied(AccessDeniedException ex) {
        return ResponseEntity.status(ErrorCode.FORBIDDEN.getHttpStatus()).body(Result.fail(ErrorCode.FORBIDDEN));
    }

    /**
     * 处理上传文件超过大小限制（Spring/Tomcat 在进入 Controller 之前抛出）。
     *
     * <p>必须单独处理：否则会被 {@link #handleUnexpected} 兜底成 500「系统错误」，
     * 用户看到"系统错误"完全不知道是自己文件太大。
     *
     * @param ex 超限异常
     * @return 413 统一响应（带可读的大小上限提示）
     */
    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<Result<Void>> handleMaxUploadSize(MaxUploadSizeExceededException ex) {
        long max = ex.getMaxUploadSize();
        String limit = max > 0 ? (max / (1024 * 1024)) + "MB" : "服务端配置上限";
        log.warn("upload rejected: file exceeds max upload size {} bytes", max);
        return ResponseEntity.status(ErrorCode.UPLOAD_TOO_LARGE.getHttpStatus())
                .body(Result.fail(ErrorCode.UPLOAD_TOO_LARGE,
                        "上传文件过大（单文件上限 " + limit + "），请压缩或拆分后重试"));
    }

    /**
     * 处理请求体无法解析（畸形 JSON、类型不匹配等，Spring 在进入 Controller 前抛出）。
     *
     * <p>安全审计 S6：若不单独处理会被 {@link #handleUnexpected} 兜底成 500「系统错误」，
     * 客户端无法区分"自己请求体写错了"还是"服务端崩了"。
     *
     * @param ex 请求体解析异常
     * @return 400 统一响应
     */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<Result<Void>> handleNotReadable(HttpMessageNotReadableException ex) {
        log.warn("malformed request body: {}", ex.getMessage());
        return ResponseEntity.status(ErrorCode.PARAM_INVALID.getHttpStatus())
                .body(Result.fail(ErrorCode.PARAM_INVALID, "请求体格式错误（JSON 解析失败），请检查后重试"));
    }

    /**
     * 处理请求参数类型不匹配（如 {@code ?page=abc} 无法转换为 int）。
     *
     * <p>安全审计 S6：避免被兜底成 500，明确返回 400 并指明出错参数名。
     *
     * @param ex 类型不匹配异常
     * @return 400 统一响应
     */
    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    public ResponseEntity<Result<Void>> handleTypeMismatch(MethodArgumentTypeMismatchException ex) {
        log.warn("argument type mismatch: name={} value={}", ex.getName(), ex.getValue());
        return ResponseEntity.status(ErrorCode.PARAM_INVALID.getHttpStatus())
                .body(Result.fail(ErrorCode.PARAM_INVALID, "参数 " + ex.getName() + " 类型不正确"));
    }

    /**
     * 处理请求方法不支持（如对只读接口使用了 POST）。
     *
     * <p>安全审计 S6：返回标准 405，而不是被兜底成 500。
     *
     * @param ex 方法不支持异常
     * @return 405 统一响应
     */
    @ExceptionHandler(HttpRequestMethodNotSupportedException.class)
    public ResponseEntity<Result<Void>> handleMethodNotSupported(HttpRequestMethodNotSupportedException ex) {
        log.warn("method not supported: {}", ex.getMethod());
        return ResponseEntity.status(ErrorCode.METHOD_NOT_ALLOWED.getHttpStatus())
                .body(Result.fail(ErrorCode.METHOD_NOT_ALLOWED));
    }

    /**
     * 兜底处理未预期异常。
     *
     * @param ex      异常
     * @param request 当前请求
     * @return 500 统一响应
     */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<Result<Void>> handleUnexpected(Exception ex, HttpServletRequest request) {
        log.error("unexpected error uri={}", request.getRequestURI(), ex);
        return ResponseEntity.status(ErrorCode.SYSTEM_ERROR.getHttpStatus())
                .body(Result.fail(ErrorCode.SYSTEM_ERROR));
    }

    private String formatFieldError(FieldError fieldError) {
        return fieldError.getField() + ": " + fieldError.getDefaultMessage();
    }
}
