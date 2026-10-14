package com.shuidun.kb.knowledge.dto;

import java.util.List;
import lombok.Data;

/**
 * 批次详情（docs/api/api.md §3 GET /api/knowledge/import/batches/{batchId}）。
 */
@Data
public class BatchDetailVO {

    /** 批次 id。 */
    private String batchId;

    /** 逐文件明细。 */
    private List<BatchFileVO> files;
}
