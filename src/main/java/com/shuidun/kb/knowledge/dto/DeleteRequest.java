package com.shuidun.kb.knowledge.dto;

import java.util.List;
import lombok.Data;

/**
 * 批量删除请求（docs/api/api.md §3 DELETE /api/knowledge/units）。
 */
@Data
public class DeleteRequest {

    /** 待删除 unit id 列表。 */
    private List<Long> ids;
}
