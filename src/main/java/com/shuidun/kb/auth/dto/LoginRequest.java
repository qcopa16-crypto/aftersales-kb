package com.shuidun.kb.auth.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/**
 * 登录请求体（docs/api/api.md §1）。
 */
@Data
public class LoginRequest {

    /** 登录名。 */
    @NotBlank(message = "用户名不能为空")
    private String username;

    /** 明文密码。 */
    @NotBlank(message = "密码不能为空")
    private String password;
}
