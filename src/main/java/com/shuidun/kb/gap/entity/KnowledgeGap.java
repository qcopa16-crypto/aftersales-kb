package com.shuidun.kb.gap.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 知识缺口，映射 knowledge_gaps 表（docs/services/database.md §2.11）。
 */
@Data
public class KnowledgeGap {

    /** 主键。 */
    private Long id;

    /** 归一化问题模式。 */
    private String questionPattern;

    /** 样例问题（JSON 数组）。 */
    private String sampleQuestionsJson;

    /** 提问次数。 */
    private Integer askCount;

    /** 最近提问时间。 */
    private LocalDateTime lastAskedAt;

    /** 状态：unresolved / resolved / ignored。 */
    private String status;

    /** 关联解决的知识单元。 */
    private Long resolvedUnitId;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;
}
