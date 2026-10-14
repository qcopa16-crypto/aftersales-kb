package com.shuidun.kb.gap.dto;

import java.time.LocalDateTime;
import java.util.List;
import lombok.Data;

/**
 * 知识缺口视图。
 */
@Data
public class KnowledgeGapVO {

    /** 主键。 */
    private Long id;

    /** 归一化问题模式。 */
    private String questionPattern;

    /** 样例问题。 */
    private List<String> sampleQuestions;

    /** 提问次数。 */
    private Integer askCount;

    /** 最近提问时间。 */
    private LocalDateTime lastAskedAt;

    /** 状态。 */
    private String status;

    /** 关联解决的知识单元。 */
    private Long resolvedUnitId;
}
