package com.shuidun.kb.auth;

import com.shuidun.kb.auth.dto.LoginRequest;
import com.shuidun.kb.auth.dto.LoginResponse;
import com.shuidun.kb.auth.dto.UserInfoVO;
import com.shuidun.kb.common.Result;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 认证接口（docs/api/api.md §1）。
 */
@RestController
@RequestMapping("/api/auth")
@RequiredArgsConstructor
public class AuthController {

    private final AuthService authService;

    /**
     * 用户名密码登录（按 IP + 账号限速，F-11）。
     *
     * @param request    登录请求
     * @param httpRequest 原始请求（提取客户端 IP；经可信反向代理部署时应在网关注入并校验来源）
     * @return 令牌与用户信息
     */
    @PostMapping("/login")
    public Result<LoginResponse> login(@Valid @RequestBody LoginRequest request,
                                       HttpServletRequest httpRequest) {
        return Result.ok(authService.login(request, httpRequest.getRemoteAddr()));
    }

    /**
     * 获取当前登录用户信息与权限码。
     *
     * @return 用户信息
     */
    @GetMapping("/me")
    public Result<UserInfoVO> me() {
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return Result.ok(authService.buildUserInfo(currentUser));
    }
}
