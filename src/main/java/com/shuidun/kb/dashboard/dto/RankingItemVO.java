package com.shuidun.kb.dashboard.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 排行榜条目。
 */
@Data
@AllArgsConstructor
public class RankingItemVO {

    /** 展示标签（问题或知识标题）。 */
    private String label;

    /** 关联 id（知识条目时有值）。 */
    private Long id;

    /** 计数。 */
    private long count;
}
