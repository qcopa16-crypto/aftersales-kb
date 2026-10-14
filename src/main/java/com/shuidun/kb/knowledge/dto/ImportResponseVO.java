package com.shuidun.kb.knowledge.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 异步导入响应（代码设计规范 §5.2，覆盖 api.md 旧同步示例）。
 */
@Data
@AllArgsConstructor
public class ImportResponseVO {

    /** 批次 id。 */
    private String batchId;

    /** 接收文件数。 */
    private int acceptedCount;

    /** 拒绝文件数。 */
    private int rejectedCount;

    /** 逐文件结果。 */
    private List<ImportItemVO> items;
}
