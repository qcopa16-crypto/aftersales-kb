package com.shuidun.kb.permission.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

/**
 * 单条数据权限项。
 */
@Data
public class PermissionItem {

    /** 授权维度：global / department / role / user。 */
    @NotBlank(message = "授权维度不能为空")
    private String targetType;

    /** 授权目标 id；global 传 0。 */
    @NotNull(message = "授权目标 id 不能为空")
    private Long targetId;
}
