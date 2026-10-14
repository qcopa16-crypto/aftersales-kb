package com.shuidun.kb.knowledge.dto;

import com.shuidun.kb.permission.dto.PermissionItem;
import java.util.List;
import lombok.Data;

/**
 * 质检确认请求（docs/api/api.md §3 POST /api/knowledge/import/quality-gate/{unitId}）。
 */
@Data
public class QualityGateRequest {

    /** 动作：confirm 确认放行；adjust 调整分类/权限后放行。 */
    private String action;

    /** 调整后的分类（adjust 时可选）。 */
    private String category;

    /** 调整后的数据权限（adjust 时可选）。 */
    private List<PermissionItem> permissions;
}
