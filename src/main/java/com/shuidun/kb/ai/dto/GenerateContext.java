package com.shuidun.kb.ai.dto;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * generate 的上下文（必须是 Java 权限过滤后的 authorized 集合）。
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class GenerateContext {

    /** unit 业务编码。 */
    private String unitId;

    /** 标题。 */
    private String title;

    /** 正文。 */
    private String content;
}
