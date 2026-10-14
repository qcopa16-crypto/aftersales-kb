package com.shuidun.kb.knowledge.dto;

import java.util.List;
import lombok.Data;

/**
 * 批次内单个文件的阶段状态。
 */
@Data
public class BatchFileVO {

    /** unit id。 */
    private Long unitId;

    /** 业务编码。 */
    private String unitCode;

    /** 标题。 */
    private String title;

    /** 分类。 */
    private String category;

    /** 业务状态。 */
    private String status;

    /** 流水线状态。 */
    private String ingestStatus;

    /** 当前阶段。 */
    private String stage;

    /** 进度 0–100。 */
    private Integer progress;

    /** 错误码。 */
    private String errorCode;

    /** 可读错误原因与建议动作。 */
    private String errorMessage;

    /** 重试次数。 */
    private Integer attempt;

    /** 切片数（已解析时）。 */
    private Integer chunkCount;

    /** 随机抽样切片全文（质检抽屉用，最多 3 条）。 */
    private List<String> samples;

    /** 质检结论：auto_pass / human_required / human_confirmed / human_adjusted；无审计为 null。 */
    private String reviewVerdict;

    /** LLM 置信度。 */
    private Double reviewConfidence;

    /** 判定来源：ai / human；无审计为 null。 */
    private String reviewSource;

    /** 复核原因（parse 的 content/image 告警）。 */
    private List<String> reviewReasons;

    /** VLM 图片抽检结论（可读字符串）。 */
    private List<String> vlmFindings;

    /** AI 预审 verdict：pass / review；无 AI 预审为 null。 */
    private String aiVerdict;

    /** AI 预审 reasons[]。 */
    private List<String> aiReasons;

    /** AI 预审模型名（取 ai_model 列）。 */
    private String aiModel;
}
