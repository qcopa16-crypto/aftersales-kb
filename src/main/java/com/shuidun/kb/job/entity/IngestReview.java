package com.shuidun.kb.job.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 入库质检判定审计，映射 ingest_reviews 表（一次入库一行）。
 *
 * <p>记录 AI 与人工两条路径的判定结果，供合规追溯与「省了多少人工」度量（阶段 4）。
 */
@Data
public class IngestReview {

    /** 主键。 */
    private Long id;

    /** 知识单元 id。 */
    private Long unitId;

    /** 入库作业 id。 */
    private Long jobId;

    /** 导入批次 id。 */
    private String batchId;

    /** 分类。 */
    private String category;

    /** 判定结果：auto_pass / human_required / human_confirmed / human_adjusted。 */
    private String decision;

    /** 判定者：ai / human。 */
    private String decider;

    /** 人工判定者 user_id（AI 判定为 null）。 */
    private Long deciderUserId;

    /** 规则档结论。 */
    private String rulesVerdict;

    /** LLM 档结论。 */
    private String llmVerdict;

    /** LLM 置信度。 */
    private Double llmConfidence;

    /** VLM 档结论。 */
    private String vlmVerdict;

    /** 复核原因（逗号或 JSON 文本）。 */
    private String reviewReasons;

    /** 明细 JSON（AI 原始结论/决策 reason 等）。 */
    private String detailJson;

    /** AI 模型名。 */
    private String aiModel;

    /** LLM 档耗时（毫秒）。 */
    private Integer llmMs = 0;

    /** VLM 档耗时（毫秒）。 */
    private Integer vlmMs = 0;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
