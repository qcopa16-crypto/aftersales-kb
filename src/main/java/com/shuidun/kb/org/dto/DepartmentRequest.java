package com.shuidun.kb.org.dto;

import jakarta.validation.constraints.NotBlank;
import lombok.Data;

/**
 * 新增/更新部门请求。
 */
@Data
public class DepartmentRequest {

    /** 父部门 id，0 为根；不传默认 0。 */
    private Long parentId;

    /** 部门名称。 */
    @NotBlank(message = "部门名称不能为空")
    private String name;

    /** 负责人 user_id。 */
    private Long leaderId;

    /** 排序值。 */
    private Integer sortOrder;
}
