package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE clarify 事件载荷（docs/api/api.md §4）。
 *
 * <p>两种语义，由 {@link #kind} 区分：
 * <ul>
 *   <li>{@code ask}：**问题信息不足，需要追问**。例如「路由器有问题」没说具体现象。
 *       此时给的是补充提示选项，点选后会拼进原问题重新检索——而不是让用户去挑知识文档。</li>
 *   <li>{@code reference}：**问题已经问清楚，只是知识库没命中**。诚实说明没找到，
 *       并把可能相关的几份列为参考；用户想点就点，不点也不影响。</li>
 * </ul>
 */
@Data
@AllArgsConstructor
public class ClarifyEvent {

    /** ask（信息不足需追问）/ reference（无命中给参考）。 */
    private String kind;

    /** kind=ask 时的追问文案；kind=reference 时为空。 */
    private String prompt;

    /** 选项：kind=ask 为补充提示，kind=reference 为知识单元。 */
    private List<ClarifyOptionVO> options;
}
