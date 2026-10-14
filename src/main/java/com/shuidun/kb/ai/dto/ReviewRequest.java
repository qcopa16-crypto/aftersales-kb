package com.shuidun.kb.ai.dto;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;

/**
 * 入库质检 AI 判定请求（POST /internal/review，阶段 1 冻结契约）。
 *
 * <p>字段使用 snake_case 命名策略（由 {@code aiObjectMapper} 序列化）：
 * {@code unit_id / enable_llm / enable_vlm / content_hash / ingest_id / chunk_index / heading_path / relative_path}。
 *
 * <p>数据全部来自内存中的 parse 结果，不额外查库。
 */
@Data
public class ReviewRequest {

    /** unit 业务编码（对 AI 的 unit_id）。 */
    private String unitId;

    /** 分类。 */
    private String category;

    /** 抽取出的 Markdown 全文。 */
    private String markdown;

    /** 切片列表。 */
    private List<Chunk> chunks = new ArrayList<>();

    /** parse 质量报告（全字段透传）。 */
    private ParseQuality quality;

    /** 图片列表（仅相对路径）。 */
    private List<Image> images = new ArrayList<>();

    /** 是否启用 LLM 档。 */
    private boolean enableLlm;

    /** 是否启用 VLM 档。 */
    private boolean enableVlm;

    /** 原文哈希（AI 侧据此反查图片缓存）。 */
    private String contentHash;

    /** 审计 id。 */
    private String ingestId;

    /** 切片（snake_case：chunk_index / heading_path）。 */
    @Data
    public static class Chunk {

        /** 切片序号。 */
        private Integer chunkIndex;

        /** 切片正文。 */
        private String content;

        /** 标题路径。 */
        private List<String> headingPath = new ArrayList<>();
    }

    /** 图片（snake_case：relative_path）。 */
    @Data
    public static class Image {

        /** 相对路径。 */
        private String relativePath;
    }
}
