package com.shuidun.kb.auth;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

/**
 * 安全上下文工具（代码设计规范 §3.4）。统一获取当前登录用户。
 */
public final class SecurityUtils {

    private SecurityUtils() {
    }

    /**
     * 获取当前登录用户，未登录返回 null。
     *
     * @return 当前用户或 null
     */
    public static CurrentUser getCurrentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication != null && authentication.getPrincipal() instanceof CurrentUser currentUser) {
            return currentUser;
        }
        return null;
    }

    /**
     * 获取当前登录用户，未登录抛 401。
     *
     * @return 当前用户
     */
    public static CurrentUser requireCurrentUser() {
        CurrentUser currentUser = getCurrentUser();
        if (currentUser == null) {
            throw new BusinessException(ErrorCode.UNAUTHORIZED);
        }
        return currentUser;
    }
}
