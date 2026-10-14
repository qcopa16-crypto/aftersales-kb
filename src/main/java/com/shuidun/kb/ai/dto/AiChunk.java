package com.shuidun.kb.ai.dto;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;

/**
 * 解析切片（AI 内部契约，snake_case）。
 */
@Data
public class AiChunk {

    /** 切片序号。 */
    private Integer chunkIndex;

    /** 切片正文。 */
    private String content;

    /**
     * 标题路径。
     *
     * <p>必须初始化为空列表：AI 侧 Pydantic 定义为 {@code list[str]}，
     * 显式传 {@code null} 会触发 422（默认值只在字段缺失时生效）。
     */
    private List<String> headingPath = new ArrayList<>();
}
