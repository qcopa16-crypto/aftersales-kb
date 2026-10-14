package com.shuidun.kb.org.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/**
 * 新增/更新角色请求。
 */
@Data
public class RoleRequest {

    /** 角色名。 */
    @NotBlank(message = "角色名不能为空")
    private String roleName;

    /** 角色编码；新增必填，更新时忽略。 */
    private String roleCode;

    /** 描述。 */
    private String description;
}
