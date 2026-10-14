package com.shuidun.kb.faq.dto;

import lombok.Data;

/**
 * FAQ 审核请求（docs/api/api.md §5）。
 */
@Data
public class FaqReviewRequest {

    /** 审核动作：approve / reject。 */
    private String action;

    /** 审核时修订的答案（可选）。 */
    private String editedAnswer;
}
