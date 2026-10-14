package com.shuidun.kb.org.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 用户实体，映射 users 表（docs/services/database.md §2.1）。
 *
 * <p>passwordHash 属于敏感字段，禁止出现在任何对前端的响应中。
 */
@Data
public class User {

    /** 主键。 */
    private Long id;

    /** 登录名。 */
    private String username;

    /** BCrypt 密码散列。 */
    private String passwordHash;

    /** 显示名。 */
    private String displayName;

    /** 所属部门 id。 */
    private Long departmentId;

    /** 状态：1 正常，0 停用。 */
    private Integer status;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;
}
