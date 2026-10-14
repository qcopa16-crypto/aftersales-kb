package com.shuidun.kb.permission.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 知识单元数据权限记录，映射 unit_permissions 表（docs/services/database.md §2.7）。
 *
 * <p>四维：global / department / role / user；global 时 targetId 固定为 0。
 */
@Data
public class UnitPermission {

    /** 主键。 */
    private Long id;

    /** 知识单元 id。 */
    private Long unitId;

    /** 授权维度：global / department / role / user。 */
    private String targetType;

    /** 授权目标 id；global 为 0。 */
    private Long targetId;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
