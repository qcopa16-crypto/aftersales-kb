package com.shuidun.kb.org.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 部门实体，映射 departments 表（docs/services/database.md §2.2）。
 */
@Data
public class Department {

    /** 主键。 */
    private Long id;

    /** 父部门 id，0 为根。 */
    private Long parentId;

    /** 部门名称。 */
    private String name;

    /** 负责人 user_id。 */
    private Long leaderId;

    /** 排序值。 */
    private Integer sortOrder;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
