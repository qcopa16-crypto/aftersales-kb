package com.shuidun.kb.knowledge.dto;

import lombok.Data;

/**
 * 导入批次中的单个文件结果（docs/api/api.md §3 POST /api/knowledge/import）。
 */
@Data
public class ImportItemVO {

    /** unit id。 */
    private Long unitId;

    /** 标题。 */
    private String title;

    /** 文件类型。 */
    private String fileType;

    /** 流水线状态。 */
    private String ingestStatus;

    /** 被拒绝时的原因。 */
    private String rejectReason;
}
