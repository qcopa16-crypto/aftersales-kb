package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 澄清选项（docs/api/api.md §4 SSE clarify 事件）。
 *
 * <p>两种形态，由 {@link #type} 区分：
 * <ul>
 *   <li>{@code hint}：**补充提示**。用户点选后，该文案会被拼到原问题后面重新检索
 *       （例如原问题「路由器有问题」+ 提示「指示灯异常」）；
 *   <li>{@code unit}：**知识单元**。用户点选后直接基于该单元正文作答（不走检索）。
 * </ul>
 *
 * <p>无论哪种，选项都只来自**当前用户可访问**的范围，不会泄露无权限知识的存在。
 */
@Data
@AllArgsConstructor
public class ClarifyOptionVO {

    /** 选项类型：hint（补充提示）/ unit（知识单元）。 */
    private String type;

    /** type=unit：知识单元 id（点选后作为 chosenUnitId 回传）。 */
    private Long unitId;

    /** type=unit：知识单元编码。 */
    private String unitCode;

    /** 展示文案；type=hint 时同时是追加到原问题后面的补充文本。 */
    private String title;

    /** type=unit：分类。 */
    private String category;

    /** type=unit：命中位置附近的正文片段。 */
    private String snippet;

    /** type=unit：匹配得分（仅用于排序）。 */
    private int score;

    /** 构造一个"补充提示"选项。 */
    public static ClarifyOptionVO hint(String text) {
        return new ClarifyOptionVO("hint", null, null, text, null, null, 0);
    }

    /** 构造一个"知识单元"选项。 */
    public static ClarifyOptionVO unit(Long unitId, String unitCode, String title,
                                       String category, String snippet, int score) {
        return new ClarifyOptionVO("unit", unitId, unitCode, title, category, snippet, score);
    }
}
