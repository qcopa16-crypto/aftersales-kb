package com.shuidun.kb.auth;

import java.util.List;
import java.util.Set;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 当前登录用户上下文（代码设计规范 §3.4）。
 *
 * <p>由 JWT 解析并经数据库补全角色与权限码，作为 Spring Security 的 principal。
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class CurrentUser {

    /** 用户 id。 */
    private Long id;

    /** 登录名。 */
    private String username;

    /** 显示名。 */
    private String displayName;

    /** 部门 id。 */
    private Long departmentId;

    /** 角色编码列表。 */
    private List<String> roles;

    /** 操作权限码集合。 */
    private Set<String> permissions;

    /**
     * 判断是否拥有指定操作权限码。
     *
     * @param code 权限码
     * @return 拥有返回 true
     */
    public boolean hasPermission(String code) {
        return permissions != null && permissions.contains(code);
    }
}
