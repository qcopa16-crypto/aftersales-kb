package com.shuidun.kb.org.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 角色实体，映射 roles 表（docs/services/database.md §2.3）。
 */
@Data
public class Role {

    /** 主键。 */
    private Long id;

    /** 角色名。 */
    private String roleName;

    /** 角色编码，如 admin / after_sales。 */
    private String roleCode;

    /** 描述。 */
    private String description;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
