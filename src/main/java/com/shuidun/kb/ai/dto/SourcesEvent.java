package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * SSE sources 事件载荷：授权与未授权来源分列，未授权不得进入 Prompt。
 */
@Data
@AllArgsConstructor
public class SourcesEvent {

    /** 已授权来源。 */
    private List<SourceItemVO> authorized;

    /** 无权限来源（仅用于提示，不含内容）。 */
    private List<SourceItemVO> unauthorized;
}
