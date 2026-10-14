package com.shuidun.kb.gap.dto;

import lombok.Data;

/**
 * 缺口状态更新请求（docs/api/api.md §6）。
 */
@Data
public class GapUpdateRequest {

    /** 目标状态：resolved / ignored。 */
    private String status;

    /** 关联解决的知识单元 id（resolved 时可选）。 */
    private Long resolvedUnitId;
}
