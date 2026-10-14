package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.Data;

/**
 * parse 质量报告（aftersales-kb-ai/docs/ai-service-api.md POST /internal/parse）。
 */
@Data
public class ParseQuality {

    /** 切片数。 */
    private Integer chunkCount;

    /** 空切片比例。 */
    private Double emptyChunkRatio;

    /** 平均切片字符数。 */
    private Double avgChunkChars;

    /** 标题数量。 */
    private Integer headingCount;

    /** 带标题切片比例。 */
    private Double chunksWithHeadingRatio;

    /** 图片数量。 */
    private Integer imageCount;

    /** Markdown 字符数。 */
    private Integer markdownChars;

    /** 页数。 */
    private Integer pageCount;

    /** 是否建议人工复核。 */
    private Boolean suggestReview;

    /** 复核原因枚举：product_manual_heuristic / image_count_high / empty_chunks / low_heading_coverage。 */
    private List<String> reviewReasons;
}
