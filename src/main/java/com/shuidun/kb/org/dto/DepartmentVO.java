package com.shuidun.kb.org.dto;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;

/**
 * 部门树节点视图。
 */
@Data
public class DepartmentVO {

    /** 部门 id。 */
    private Long id;

    /** 父部门 id，0 为根。 */
    private Long parentId;

    /** 部门名称。 */
    private String name;

    /** 负责人 user_id。 */
    private Long leaderId;

    /** 负责人姓名（前端直接展示，避免只显示 id）。 */
    private String leaderName;

    /** 排序值。 */
    private Integer sortOrder;

    /** 子部门。 */
    private List<DepartmentVO> children = new ArrayList<>();
}
