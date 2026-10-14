package com.shuidun.kb.ai.dto;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import lombok.Data;

/**
 * 入库质检 AI 判定响应（POST /internal/review，阶段 1 冻结契约）。
 *
 * <p>字段使用 snake_case 命名策略（由 {@code aiObjectMapper} 反序列化）：
 * {@code vlm_findings / stage_timings / llm_ms / vlm_ms / relative_path / ingest_id}。
 *
 * <p>AI 只给结论，Java 决策与落库；档位级失败走 HTTP 200 + {@link Tiers}（非 pass 即 fail-closed）。
 */
@Data
public class ReviewResult {

    /** 综合结论：pass / review。 */
    private String verdict;

    /** 置信度 0.0–1.0。 */
    private Double confidence;

    /** 结论理由（含档位前缀，如 rules:clean / llm:pass）。 */
    private List<String> reasons = new ArrayList<>();

    /** 三档状态。 */
    private Tiers tiers;

    /** 覆盖率明细（AI 侧结构，Java 原样透传/审计）。 */
    private Map<String, Object> coverage;

    /** VLM 图片抽检结论。 */
    private List<VlmFinding> vlmFindings = new ArrayList<>();

    /** 使用的模型名（如 glm-4.6v）。 */
    private String model;

    /** 各档耗时。 */
    private StageTimings stageTimings;

    /** 回显审计 id。 */
    private String ingestId;

    /** 三档状态：rules / llm / vlm。 */
    @Data
    public static class Tiers {

        /** 规则档：pass / review。 */
        private String rules;

        /** LLM 档：pass / review / skipped / error。 */
        private String llm;

        /** VLM 档：pass / review / skipped / unavailable / error。 */
        private String vlm;
    }

    /** VLM 图片抽检单条结论。 */
    @Data
    public static class VlmFinding {

        /** 图片相对路径。 */
        private String relativePath;

        /** 是否可用。 */
        private Boolean usable;

        /** 判定理由。 */
        private String reason;
    }

    /** 档位耗时（毫秒）。 */
    @Data
    public static class StageTimings {

        /** LLM 耗时。 */
        private Double llmMs;

        /** VLM 耗时。 */
        private Double vlmMs;
    }
}
