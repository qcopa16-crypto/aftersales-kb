package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 来源卡片（docs/api/api.md §4 SSE sources 事件）。
 */
@Data
@AllArgsConstructor
public class SourceItemVO {

    /** 知识单元 id。 */
    private Long id;

    /** 标题。 */
    private String title;
}
