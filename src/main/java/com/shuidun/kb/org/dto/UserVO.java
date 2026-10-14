package com.shuidun.kb.org.dto;

import java.util.List;
import lombok.Data;

/**
 * 用户列表/详情视图。禁止包含密码等敏感字段（代码设计规范 §5.1）。
 */
@Data
public class UserVO {

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

    /** 状态：1 正常，0 停用。 */
    private Integer status;

    /** 角色 id 列表。 */
    private List<Long> roleIds;

    /** 角色编码列表。 */
    private List<String> roles;

    /** 角色名称列表（与 roles 同序，前端直接展示中文）。 */
    private List<String> roleNames;
}
