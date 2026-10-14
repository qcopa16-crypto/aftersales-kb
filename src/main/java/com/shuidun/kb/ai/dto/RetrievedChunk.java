package com.shuidun.kb.ai.dto;

import java.util.List;
import lombok.Data;

/**
 * 检索命中切片（aftersales-kb-ai/docs/ai-service-api.md POST /internal/retrieve）。
 */
@Data
public class RetrievedChunk {

    /** 所属 unit（字符串业务编码）。 */
    private String unitId;

    /** 切片 id。 */
    private String chunkId;

    /** 融合得分。 */
    private Double score;

    /** 切片正文。 */
    private String content;

    /** 版本 id。 */
    private String versionId;

    /** 标题路径。 */
    private List<String> headingPath;

    /** dense 得分。 */
    private Double scoreDense;

    /** lexical 得分。 */
    private Double scoreLexical;

    /** 融合得分明细。 */
    private Double scoreFused;

    /** 命中路径：dense / lexical。 */
    private List<String> retrievalPaths;
}
