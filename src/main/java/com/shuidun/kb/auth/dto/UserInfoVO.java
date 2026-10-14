package com.shuidun.kb.auth.dto;

import java.util.List;
import lombok.Data;

/**
 * 当前用户信息视图（docs/api/api.md §1）。禁止包含密码等敏感字段。
 */
@Data
public class UserInfoVO {

    /** 用户 id。 */
    private Long id;

    /** 登录名。 */
    private String username;

    /** 显示名。 */
    private String displayName;

    /** 部门 id。 */
    private Long departmentId;

    /** 部门名称。 */
    private String departmentName;

    /** 角色编码列表。 */
    private List<String> roles;

    /** 角色名称列表（与 roles 同序，前端直接展示中文）。 */
    private List<String> roleNames;

    /** 操作权限码列表。 */
    private List<String> permissions;
}
