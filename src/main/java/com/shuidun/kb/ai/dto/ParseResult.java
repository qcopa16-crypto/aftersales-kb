package com.shuidun.kb.ai.dto;

import java.util.ArrayList;
import java.util.List;
import lombok.Data;

/**
 * parse 响应（aftersales-kb-ai/docs/ai-service-api.md）。
 */
@Data
public class ParseResult {

    /** 回显 unit_id。 */
    private String unitId;

    /** 抽取出的 Markdown 全文。 */
    private String markdown;

    /** 切片列表。 */
    private List<AiChunk> chunks;

    /** 图片列表（仅透传给 review 供 VLM 抽检定位，不落库）。 */
    private List<ParseImage> images = new ArrayList<>();

    /** 质量报告。 */
    private ParseQuality quality;

    /** 原文哈希。 */
    private String contentHash;

    /** 切片哈希，embed 幂等校验用。 */
    private String chunksHash;

    /** Java 生成的审计 id。 */
    private String ingestId;

    /** AI 建议分类（Java 决定是否落库）。 */
    private String suggestedCategory;

    /** AI 建议权限（Java 决定是否落库）。 */
    private String permissionHint;

    /** 页数。 */
    private Integer pageCount;

    /** 文件类型。 */
    private String fileType;
}
