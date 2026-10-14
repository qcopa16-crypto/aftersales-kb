package com.shuidun.kb.knowledge.dto;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 入库批次摘要（docs/api/api.md §3 GET /api/knowledge/import/batches）。
 */
@Data
public class BatchSummaryVO {

    /** 批次 id。 */
    private String batchId;

    /** 文件数。 */
    private Long fileCount;

    /** 已就绪数。 */
    private Long activeCount;

    /** 失败数。 */
    private Long failedCount;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 创建人。 */
    private Long creatorId;
}
