package com.shuidun.kb.knowledge.dto;

import java.util.List;
import lombok.AllArgsConstructor;
import lombok.Data;

/**
 * 入库质检度量（阶段 4，回答「省了多少人工」）。
 *
 * <p>数据源为审计表 {@code ingest_reviews}，全部字段在 Java 侧聚合（SQL 只做时间过滤 + 倒序截断），
 * 保证 H2 与 MySQL 双库口径一致。
 */
@Data
public class QualityMetricsVO {

    /** 统计窗口天数（已夹取到 [1, 365]）。 */
    private int days;

    /** 窗口起点，格式 {@code yyyy-MM-dd'T'HH:mm}。 */
    private String windowStart;

    /**
     * AI 判定总数 = {@code autoPassCount + humanRequiredCount}（{@code decider="ai"} 的行）。
     *
     * <p>人工动作行（{@code human_confirmed} / {@code human_adjusted}）不计入，故它同时是自动通过率与转人工率的分母。
     * 注意：它**小于**窗口内全部审计行数，后者见 {@link #totalAuditRows}。
     */
    private long totalReviews;

    /**
     * 窗口内全部审计行数（{@code decider="ai"} + {@code decider="human"}）。
     *
     * <p>等于 {@link #scannedRows}（受扫描上限约束后的实际聚合行数）；语义上比 {@code scannedRows} 更面向调用方，
     * 用于回答「这段时间一共产生过多少条质检/人工动作记录」。
     */
    private long totalAuditRows;

    /** 判定分布（仅含窗口内出现过的决策，按固定顺序 auto_pass→human_required→human_confirmed→human_adjusted）。 */
    private List<DecisionCount> decisionDistribution;

    /** AI 自动通过行数。 */
    private long autoPassCount;

    /** 转人工行数（AI 判定后停质检门）。 */
    private long humanRequiredCount;

    /** 人工确认行数。 */
    private long humanConfirmedCount;

    /** 人工调整行数。 */
    private long humanAdjustedCount;

    /** 自动通过率 = autoPassCount / (autoPassCount + humanRequiredCount)；分母为 0 时返回 0.0（非 NaN）。 */
    private double autoPassRate;

    /** 转人工率 = humanRequiredCount / (autoPassCount + humanRequiredCount)；分母为 0 时返回 0.0（非 NaN）。 */
    private double humanRequiredRate;

    /**
     * 全量判定理由分布（从 {@code detail_json.reason} 解析，按次数倒序）。
     *
     * <p><b>含人工动作行</b>：即 {@link #aiReasonDistribution} 与 {@link #humanActionDistribution} 的并集，
     * 因此会出现 {@code human_confirmed} / {@code human_adjusted} 这类人工动作理由。需要区分 AI/人工时请用下面两个拆分字段。
     */
    private List<ReasonCount> reasonDistribution;

    /** AI 判定理由分布：仅 {@code decider="ai"} 的行（{@code rules_clean} / {@code risk_category} / {@code vlm_unusable} / {@code ai_error:*} 等），按次数倒序。 */
    private List<ReasonCount> aiReasonDistribution;

    /** 人工动作理由分布：仅 {@code decider="human"} 的行（{@code human_confirmed} / {@code human_adjusted}），按次数倒序。 */
    private List<ReasonCount> humanActionDistribution;

    /** 分类分布（按次数倒序）。 */
    private List<CategoryCount> categoryDistribution;

    /** AI 模型分布（按次数倒序）。 */
    private List<ModelCount> aiModelDistribution;

    /** LLM 档耗时（仅统计 {@code llm_ms > 0} 的样本）。 */
    private LatencyStats llmLatency;

    /** VLM 档耗时（仅统计 {@code vlm_ms > 0} 的样本）。 */
    private LatencyStats vlmLatency;

    /** 人工确认按操作人分布（{@code decision=human_confirmed}，按次数倒序）。 */
    private List<UserCount> humanConfirmedByUser;

    /** 省下的人工量。 */
    private WorkloadSaved workloadSaved;

    /** 本次实际参与聚合的审计行数（已受扫描上限约束）；与 {@link #totalAuditRows} 同值。 */
    private long scannedRows;

    /** 是否因命中扫描上限被截断（true 时只保留了最近的 {@code MAX_SCAN} 行，更早的行未纳入统计）。 */
    private boolean truncated;

    /** 决策计数项。 */
    @Data
    @AllArgsConstructor
    public static class DecisionCount {

        /** 决策：auto_pass / human_required / human_confirmed / human_adjusted。 */
        private String decision;

        /** 次数。 */
        private long count;
    }

    /** 理由计数项。 */
    @Data
    @AllArgsConstructor
    public static class ReasonCount {

        /** 理由（detail_json.reason）。 */
        private String reason;

        /** 次数。 */
        private long count;
    }

    /** 分类计数项。 */
    @Data
    @AllArgsConstructor
    public static class CategoryCount {

        /** 分类。 */
        private String category;

        /** 次数。 */
        private long count;
    }

    /** AI 模型计数项。 */
    @Data
    @AllArgsConstructor
    public static class ModelCount {

        /** AI 模型名。 */
        private String model;

        /** 次数。 */
        private long count;
    }

    /** 操作人计数项。 */
    @Data
    @AllArgsConstructor
    public static class UserCount {

        /** 操作人 user_id。 */
        private Long userId;

        /** 次数。 */
        private long count;
    }

    /** 耗时统计（分位口径与 {@code DashboardService.percentile} 一致）。 */
    @Data
    @AllArgsConstructor
    public static class LatencyStats {

        /** 样本数（仅 >0 的样本）。 */
        private long count;

        /** P50 耗时（毫秒）。 */
        private long p50Ms;

        /** P95 耗时（毫秒）。 */
        private long p95Ms;

        /** 最大耗时（毫秒）。 */
        private long maxMs;
    }

    /** 省下的人工量。 */
    @Data
    @AllArgsConstructor
    public static class WorkloadSaved {

        /** 窗口内人工确认次数。 */
        private long humanConfirmations;

        /** 窗口内 AI 自动通过、未产生人工确认动作的单元数。 */
        private long autoPassedWithoutHuman;

        /** 字段口径说明。 */
        private String note;
    }
}
