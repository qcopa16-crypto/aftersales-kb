package com.shuidun.kb.permission;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

/**
 * 操作权限码拦截器（代码设计规范 §3.4）。
 *
 * <p>读取 {@link RequirePermission} 注解，校验当前用户权限码；不足抛 403（HTTP 403，非 40001）。
 */
@Component
public class PermissionInterceptor implements HandlerInterceptor {

    /**
     * 在进入 Controller 前校验权限码。
     *
     * @param request  当前请求
     * @param response 当前响应
     * @param handler  处理器
     * @return 放行返回 true
     */
    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) {
        // SSE/异步请求完成后容器会以 ASYNC 类型重新派发并再次经过拦截器；此时 SecurityContext 已清空，
        // 若重复鉴权会抛 401 到已提交的响应上，导致连接被硬关闭、chunked 流缺少结束符。
        // 首次派发（REQUEST）已鉴权，异步派发直接放行。
        if (request.getDispatcherType() == DispatcherType.ASYNC) {
            return true;
        }
        if (!(handler instanceof HandlerMethod handlerMethod)) {
            return true;
        }
        RequirePermission requirePermission = handlerMethod.getMethodAnnotation(RequirePermission.class);
        if (requirePermission == null) {
            requirePermission = handlerMethod.getBeanType().getAnnotation(RequirePermission.class);
        }
        if (requirePermission == null) {
            return true;
        }
        CurrentUser currentUser = SecurityUtils.getCurrentUser();
        if (currentUser == null) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED);
        }
        for (String code : requirePermission.value()) {
            if (currentUser.hasPermission(code)) {
                return true;
            }
        }
        throw new BusinessException(ErrorCode.FORBIDDEN);
    }
}
