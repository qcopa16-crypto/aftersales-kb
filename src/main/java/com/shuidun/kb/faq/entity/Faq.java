package com.shuidun.kb.faq.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * FAQ 实体，映射 faqs 表（docs/services/database.md §2.10）。
 */
@Data
public class Faq {

    /** 主键。 */
    private Long id;

    /** 问题。 */
    private String question;

    /** F-25 规范化问题键（与 Redis 缓存键一致）：TTL 过期后按该键 MySQL 回源并回填。 */
    private String questionNorm;

    /** 答案。 */
    private String answer;

    /** 分类。 */
    private String category;

    /** 关联知识单元 id。 */
    private Long relatedUnitId;

    /** 来源：manual / auto_mined。 */
    private String sourceType;

    /** 状态：pending_review / published / rejected。 */
    private String status;

    /** 命中次数。 */
    private Integer hitCount;

    /** 审核人。 */
    private Long reviewerId;

    /** 审核时间。 */
    private LocalDateTime reviewedAt;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;
}
