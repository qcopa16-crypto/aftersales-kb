package com.shuidun.kb.org.dto;

import java.util.List;
import lombok.Data;

/**
 * 角色视图（含已分配的操作权限码）。
 */
@Data
public class RoleVO {

    /** 角色 id。 */
    private Long id;

    /** 角色名。 */
    private String roleName;

    /** 角色编码。 */
    private String roleCode;

    /** 描述。 */
    private String description;

    /** 操作权限码列表。 */
    private List<String> permissions;
}
