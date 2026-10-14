package com.shuidun.kb.org.dto;

import java.util.List;
import lombok.Data;

/**
 * 更新用户请求（不含登录名与密码）。
 */
@Data
public class UserUpdateRequest {

    /** 显示名。 */
    private String displayName;

    /** 所属部门 id。 */
    private Long departmentId;

    /** 状态：1 正常，0 停用。 */
    private Integer status;

    /** 角色 id 列表；为 null 表示不改动。 */
    private List<Long> roleIds;
}
