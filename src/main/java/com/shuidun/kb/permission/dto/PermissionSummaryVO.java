package com.shuidun.kb.permission.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 数据权限摘要项（用于知识详情展示）。
 */
@Data
@AllArgsConstructor
public class PermissionSummaryVO {

    /** 授权维度。 */
    private String targetType;

    /** 授权目标 id。 */
    private Long targetId;

    /** 人类可读标签，如「部门:售后部」。 */
    private String label;
}
