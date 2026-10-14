package com.shuidun.kb.org.dto;

import lombok.Data;

/**
 * 重置密码请求（F-12）：必须显式提供新密码，服务端不再生成明文回显。
 */
@Data
public class ResetPasswordRequest {

    /** 新密码（明文，必填，最少 8 位）；经带外渠道告知用户，用户首次登录后自行修改。 */
    private String password;
}
