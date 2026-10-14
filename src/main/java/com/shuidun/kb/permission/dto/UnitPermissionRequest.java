package com.shuidun.kb.permission.dto;

import jakarta.validation.Valid;
import java.util.List;
import lombok.Data;

/**
 * 知识单元数据权限配置请求（docs/api/api.md §3）。
 */
@Data
public class UnitPermissionRequest {

    /** 配置模式：replace 覆盖；add 追加。默认 replace。 */
    private String mode = "replace";

    /** 权限项列表。 */
    @Valid
    private List<PermissionItem> items;
}
