package com.shuidun.kb.permission;

import java.util.Set;

/**
 * 四维数据权限维度常量（代码设计规范 §3.5 / kb-design-spec.md §4.1）。
 */
public final class PermissionTargetType {

    /** 全局公开，targetId 固定 0。 */
    public static final String GLOBAL = "global";

    /** 部门维度。 */
    public static final String DEPARTMENT = "department";

    /** 角色维度。 */
    public static final String ROLE = "role";

    /** 个人维度。 */
    public static final String USER = "user";

    private static final Set<String> ALL = Set.of(GLOBAL, DEPARTMENT, ROLE, USER);

    private PermissionTargetType() {
    }

    /**
     * 判断是否为合法维度。
     *
     * @param type 维度值
     * @return 合法返回 true
     */
    public static boolean isValid(String type) {
        return type != null && ALL.contains(type);
    }
}
