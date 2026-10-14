package com.shuidun.kb.faq.dto;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * FAQ 视图。
 */
@Data
public class FaqVO {

    /** 主键。 */
    private Long id;

    /** 问题。 */
    private String question;

    /** 答案。 */
    private String answer;

    /** 分类。 */
    private String category;

    /** 关联知识单元 id。 */
    private Long relatedUnitId;

    /** 来源：manual / auto_mined。 */
    private String sourceType;

    /** 状态。 */
    private String status;

    /** 命中次数。 */
    private Integer hitCount;

    /** 审核时间。 */
    private LocalDateTime reviewedAt;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;
}
