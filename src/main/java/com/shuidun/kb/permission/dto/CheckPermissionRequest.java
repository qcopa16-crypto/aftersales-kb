package com.shuidun.kb.permission.dto;

import jakarta.validation.constraints.NotNull;
import java.util.List;
import lombok.Data;

/**
 * 数据权限校验请求（docs/api/api.md §3 POST /api/knowledge/check-permissions）。
 */
@Data
public class CheckPermissionRequest {

    /** 待校验用户 id。 */
    @NotNull(message = "userId 不能为空")
    private Long userId;

    /** 待校验 unit id 列表。 */
    @NotNull(message = "unitIds 不能为空")
    private List<Long> unitIds;
}
