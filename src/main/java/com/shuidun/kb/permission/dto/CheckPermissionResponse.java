package com.shuidun.kb.permission.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 数据权限校验响应（docs/api/api.md §3）。
 */
@Data
@AllArgsConstructor
public class CheckPermissionResponse {

    /** 允许访问的 unit id。 */
    private List<Long> authorizedUnitIds;

    /** 不允许访问的 unit id。 */
    private List<Long> unauthorizedUnitIds;
}
