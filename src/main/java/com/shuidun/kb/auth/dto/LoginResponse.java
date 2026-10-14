package com.shuidun.kb.auth.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 登录响应体（docs/api/api.md §1）。
 */
@Data
@AllArgsConstructor
public class LoginResponse {

    /** 访问令牌。 */
    private String accessToken;

    /** 令牌有效期（秒）。 */
    private long expiresIn;

    /** 用户信息与权限码。 */
    private UserInfoVO userInfo;
}
