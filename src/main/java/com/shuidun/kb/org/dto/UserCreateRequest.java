package com.shuidun.kb.org.dto;

import jakarta.validation.constraints.NotBlank;
import java.util.List;
import lombok.Data;

/**
 * 新增用户请求。
 */
@Data
public class UserCreateRequest {

    /** 登录名。 */
    @NotBlank(message = "用户名不能为空")
    private String username;

    /** 初始密码（明文，服务端 BCrypt 存储）。 */
    @NotBlank(message = "密码不能为空")
    private String password;

    /** 显示名。 */
    private String displayName;

    /** 所属部门 id。 */
    private Long departmentId;

    /** 角色 id 列表。 */
    private List<Long> roleIds;
}
