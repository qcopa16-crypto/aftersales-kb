package com.shuidun.kb.knowledge.service;

import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.dto.QualityMetricsVO;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

/**
 * 入库质检度量服务（阶段 4）。
 *
 * <p>回答「AI 自动判定省了多少人工」：自动通过率、转人工率、判定/理由/分类/模型分布、
 * LLM/VLM 耗时分位、人工确认操作人分布与工作量节省。
 *
 * <p><b>跨库兼容</b>：{@link IngestReviewMapper#selectSince} 只做时间过滤 + 倒序截断，
 * 所有聚合（含 {@code detail_json} 的解析）在 Java 侧完成，不使用 MySQL 专有函数，
 * 保证 H2 与 MySQL 结果一致（与 {@code DashboardService.trend} 同做法）。
 *
 * <p><b>扫描上限</b>：为避免审计表无限增长拖慢查询，最多取最近 {@link #MAX_SCAN} 行；
 * 命中上限时在返回结果中以 {@code truncated=true} 如实暴露，调用方可知统计已被截断。
 */
@Service
@RequiredArgsConstructor
public class QualityMetricsService {

    /** 单次最多扫描的审计行数（按 id 倒序取最近的行）。 */
    static final int MAX_SCAN = 5000;

    /** 窗口起点的展示格式（示例：{@code 2026-09-12T00:00}）。 */
    private static final DateTimeFormatter WINDOW_FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm");

    /** 决策固定展示顺序（closed enum，保证前端图表稳定）。 */
    private static final List<String> DECISION_ORDER =
            List.of("auto_pass", "human_required", "human_confirmed", "human_adjusted");

    private static final String WORKLOAD_NOTE =
            "autoPassedWithoutHuman 即本次窗口内 AI 自动通过、未产生人工确认动作的单元数";

    private final IngestReviewMapper ingestReviewMapper;

    /**
     * 计算质检度量。
     *
     * @param days 统计窗口天数（夹取到 [1, 365]）
     * @return 度量结果
     */
    public QualityMetricsVO metrics(int days) {
        return metrics(days, MAX_SCAN);
    }

    /**
     * 计算质检度量（可指定扫描上限，便于单测覆盖截断行为）。
     *
     * @param days    统计窗口天数（夹取到 [1, 365]）
     * @param maxScan 扫描上限（至少 1 行）
     * @return 度量结果
     */
    QualityMetricsVO metrics(int days, int maxScan) {
        int normalizedDays = Math.min(Math.max(days, 1), 365);
        int scanLimit = Math.max(maxScan, 1);
        // 与 DashboardService.latency 同口径：窗口 = 最近 normalizedDays 天（含今天）
        LocalDateTime windowStart = LocalDate.now().minusDays(normalizedDays - 1L).atStartOfDay();

        // 多取 1 行用于判定是否截断
        List<IngestReview> fetched = ingestReviewMapper.selectSince(windowStart, scanLimit + 1);
        boolean truncated = fetched.size() > scanLimit;
        List<IngestReview> rows = truncated ? fetched.subList(0, scanLimit) : fetched;

        long autoPass = 0;
        long humanRequired = 0;
        long humanConfirmed = 0;
        long humanAdjusted = 0;
        Map<String, Long> decisionCounts = new LinkedHashMap<>();
        Map<String, Long> reasonCounts = new HashMap<>();
        Map<String, Long> aiReasonCounts = new HashMap<>();
        Map<String, Long> humanReasonCounts = new HashMap<>();
        Map<String, Long> categoryCounts = new HashMap<>();
        Map<String, Long> modelCounts = new HashMap<>();
        Map<Long, Long> confirmedByUser = new HashMap<>();
        List<Long> llmSamples = new ArrayList<>();
        List<Long> vlmSamples = new ArrayList<>();

        for (IngestReview row : rows) {
            String decision = row.getDecision();
            if (decision != null && !decision.isBlank()) {
                decisionCounts.merge(decision, 1L, Long::sum);
                switch (decision) {
                    case "auto_pass" -> autoPass++;
                    case "human_required" -> humanRequired++;
                    case "human_confirmed" -> humanConfirmed++;
                    case "human_adjusted" -> humanAdjusted++;
                    default -> {
                        // 未知决策不参与已知计数，但仍出现在 decisionDistribution 中
                    }
                }
            }
            String reason = JsonUtils.fieldString(row.getDetailJson(), "reason");
            if (reason != null && !reason.isBlank()) {
                reasonCounts.merge(reason, 1L, Long::sum);
                // 按判定者拆分：AI 判定理由 vs 人工动作理由
                if ("ai".equals(row.getDecider())) {
                    aiReasonCounts.merge(reason, 1L, Long::sum);
                } else if ("human".equals(row.getDecider())) {
                    humanReasonCounts.merge(reason, 1L, Long::sum);
                }
            }
            if (row.getCategory() != null && !row.getCategory().isBlank()) {
                categoryCounts.merge(row.getCategory(), 1L, Long::sum);
            }
            if (row.getAiModel() != null && !row.getAiModel().isBlank()) {
                modelCounts.merge(row.getAiModel(), 1L, Long::sum);
            }
            // 未调用 AI 的行 llm_ms/vlm_ms=0，不计入耗时样本
            if (row.getLlmMs() != null && row.getLlmMs() > 0) {
                llmSamples.add((long) row.getLlmMs());
            }
            if (row.getVlmMs() != null && row.getVlmMs() > 0) {
                vlmSamples.add((long) row.getVlmMs());
            }
            if ("human_confirmed".equals(decision) && row.getDeciderUserId() != null) {
                confirmedByUser.merge(row.getDeciderUserId(), 1L, Long::sum);
            }
        }

        long totalReviews = autoPass + humanRequired;

        QualityMetricsVO vo = new QualityMetricsVO();
        vo.setDays(normalizedDays);
        vo.setWindowStart(windowStart.format(WINDOW_FMT));
        vo.setTotalReviews(totalReviews);
        vo.setTotalAuditRows(rows.size());
        vo.setDecisionDistribution(toDecisionDistribution(decisionCounts));
        vo.setAutoPassCount(autoPass);
        vo.setHumanRequiredCount(humanRequired);
        vo.setHumanConfirmedCount(humanConfirmed);
        vo.setHumanAdjustedCount(humanAdjusted);
        vo.setAutoPassRate(ratio(autoPass, totalReviews));
        vo.setHumanRequiredRate(ratio(humanRequired, totalReviews));
        vo.setReasonDistribution(toReasonDistribution(reasonCounts));
        vo.setAiReasonDistribution(toReasonDistribution(aiReasonCounts));
        vo.setHumanActionDistribution(toReasonDistribution(humanReasonCounts));
        vo.setCategoryDistribution(toCategoryDistribution(categoryCounts));
        vo.setAiModelDistribution(toModelDistribution(modelCounts));
        vo.setLlmLatency(latencyStats(llmSamples));
        vo.setVlmLatency(latencyStats(vlmSamples));
        vo.setHumanConfirmedByUser(toUserDistribution(confirmedByUser));
        vo.setWorkloadSaved(new QualityMetricsVO.WorkloadSaved(humanConfirmed, autoPass, WORKLOAD_NOTE));
        vo.setScannedRows(rows.size());
        vo.setTruncated(truncated);
        return vo;
    }

    /** 比例：分母 <=0 时返回 0.0（避免 NaN），否则保留 4 位小数（与示例 0.5833 一致）。 */
    private double ratio(long numerator, long denominator) {
        if (denominator <= 0) {
            return 0.0;
        }
        return Math.round((double) numerator / denominator * 10000.0) / 10000.0;
    }

    private List<QualityMetricsVO.DecisionCount> toDecisionDistribution(Map<String, Long> counts) {
        List<QualityMetricsVO.DecisionCount> out = new ArrayList<>();
        for (String decision : DECISION_ORDER) {
            Long count = counts.get(decision);
            if (count != null && count > 0) {
                out.add(new QualityMetricsVO.DecisionCount(decision, count));
            }
        }
        return out;
    }

    private List<QualityMetricsVO.ReasonCount> toReasonDistribution(Map<String, Long> counts) {
        Comparator<Map.Entry<String, Long>> cmp = Comparator
                .comparingLong((Map.Entry<String, Long> entry) -> entry.getValue()).reversed()
                .thenComparing(Map.Entry::getKey);
        return counts.entrySet().stream()
                .sorted(cmp)
                .map(entry -> new QualityMetricsVO.ReasonCount(entry.getKey(), entry.getValue()))
                .toList();
    }

    private List<QualityMetricsVO.CategoryCount> toCategoryDistribution(Map<String, Long> counts) {
        Comparator<Map.Entry<String, Long>> cmp = Comparator
                .comparingLong((Map.Entry<String, Long> entry) -> entry.getValue()).reversed()
                .thenComparing(Map.Entry::getKey);
        return counts.entrySet().stream()
                .sorted(cmp)
                .map(entry -> new QualityMetricsVO.CategoryCount(entry.getKey(), entry.getValue()))
                .toList();
    }

    private List<QualityMetricsVO.ModelCount> toModelDistribution(Map<String, Long> counts) {
        Comparator<Map.Entry<String, Long>> cmp = Comparator
                .comparingLong((Map.Entry<String, Long> entry) -> entry.getValue()).reversed()
                .thenComparing(Map.Entry::getKey);
        return counts.entrySet().stream()
                .sorted(cmp)
                .map(entry -> new QualityMetricsVO.ModelCount(entry.getKey(), entry.getValue()))
                .toList();
    }

    private List<QualityMetricsVO.UserCount> toUserDistribution(Map<Long, Long> counts) {
        Comparator<Map.Entry<Long, Long>> cmp = Comparator
                .comparingLong((Map.Entry<Long, Long> entry) -> entry.getValue()).reversed()
                .thenComparing(Map.Entry::getKey);
        return counts.entrySet().stream()
                .sorted(cmp)
                .map(entry -> new QualityMetricsVO.UserCount(entry.getKey(), entry.getValue()))
                .toList();
    }

    private QualityMetricsVO.LatencyStats latencyStats(List<Long> samples) {
        if (samples.isEmpty()) {
            return new QualityMetricsVO.LatencyStats(0, 0, 0, 0);
        }
        long[] sorted = samples.stream().mapToLong(Long::longValue).sorted().toArray();
        return new QualityMetricsVO.LatencyStats(sorted.length,
                percentile(sorted, 0.50), percentile(sorted, 0.95), sorted[sorted.length - 1]);
    }

    /** 与 {@code DashboardService.percentile} 同口径：{@code ceil(p*n)-1}，夹取到 [0, n-1]。 */
    private long percentile(long[] sorted, double p) {
        if (sorted.length == 0) {
            return 0;
        }
        int idx = (int) Math.ceil(p * sorted.length) - 1;
        return sorted[Math.min(Math.max(idx, 0), sorted.length - 1)];
    }
}
