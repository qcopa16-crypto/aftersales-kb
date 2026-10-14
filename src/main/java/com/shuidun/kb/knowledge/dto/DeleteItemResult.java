package com.shuidun.kb.knowledge.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 批量删除逐项结果（禁止部分失败整体成功）。
 */
@Data
@AllArgsConstructor
public class DeleteItemResult {

    /** unit id。 */
    private Long unitId;

    /** 是否成功。 */
    private boolean success;

    /** 失败时的错误码。 */
    private String errorCode;

    /** 失败时的可读原因。 */
    private String errorMessage;
}
