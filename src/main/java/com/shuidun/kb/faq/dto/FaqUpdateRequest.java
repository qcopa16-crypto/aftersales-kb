package com.shuidun.kb.faq.dto;

import lombok.Data;

/**
 * 已发布 FAQ 编辑请求。
 */
@Data
public class FaqUpdateRequest {

    /** 问题。 */
    private String question;

    /** 答案。 */
    private String answer;

    /** 分类。 */
    private String category;
}
