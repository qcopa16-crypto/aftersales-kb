package com.shuidun.kb.knowledge.dto;

import com.shuidun.kb.permission.dto.PermissionSummaryVO;
import java.time.LocalDateTime;
import java.util.List;
import lombok.Data;

/**
 * 知识单元视图（列表/详情）。详情额外带 permissions。
 */
@Data
public class KnowledgeUnitVO {

    /** 主键。 */
    private Long id;

    /** 业务编码。 */
    private String unitCode;

    /** 标题。 */
    private String title;

    /** 摘要。 */
    private String summary;

    /** 正文（仅详情返回）。 */
    private String content;

    /** 分类。 */
    private String category;

    /** 源文件名。 */
    private String sourceFileName;

    /** 文件类型。 */
    private String fileType;

    /** 文件大小。 */
    private Long fileSize;

    /** 业务状态。 */
    private String status;

    /** 流水线状态。 */
    private String ingestStatus;

    /** 批次 id。 */
    private String batchId;

    /** 创建人。 */
    private Long creatorId;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;

    /** 数据权限摘要（仅详情）。 */
    private List<PermissionSummaryVO> permissions;
}
