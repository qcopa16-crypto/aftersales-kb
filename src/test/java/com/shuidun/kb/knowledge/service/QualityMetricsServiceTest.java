package com.shuidun.kb.knowledge.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.dto.QualityMetricsVO;
import java.time.LocalDateTime;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * 质检度量聚合测试（H2 + 真实 Spring/MyBatis）。
 *
 * <p>每个用例前清空 {@code ingest_reviews}，保证断言精确（H2 内存库在同 JVM 内跨测试类共享）。
 * 覆盖：分布计数、自动通过率（含分母为 0 边界）、P50/P95 只算 &gt;0 样本、扫描上限截断、窗口过滤。
 */
@SpringBootTest
class QualityMetricsServiceTest {

    @Autowired
    private QualityMetricsService service;

    @Autowired
    private IngestReviewMapper ingestReviewMapper;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @BeforeEach
    void cleanTable() {
        jdbcTemplate.update("DELETE FROM ingest_reviews");
    }

    @Test
    void aggregatesDecisionReasonAndUserDistributions() {
        insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);
        insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);
        insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);
        insert("human_required", "ai", null, "finance", "{\"reason\":\"risk_category\"}", null, 0, 0);
        insert("human_required", "ai", null, "product", "{\"reason\":\"vlm_unusable\"}", "glm-4.6v", 0, 6984);
        insert("human_confirmed", "human", 1L, "finance",
                "{\"action\":\"confirm\",\"reason\":\"human_confirmed\"}", null, 0, 0);
        insert("human_confirmed", "human", 1L, "finance",
                "{\"action\":\"confirm\",\"reason\":\"human_confirmed\"}", null, 0, 0);
        insert("human_adjusted", "human", 2L, "finance",
                "{\"action\":\"adjust\",\"reason\":\"human_adjusted\"}", null, 0, 0);

        QualityMetricsVO vo = service.metrics(30);

        assertEquals(30, vo.getDays());
        // totalReviews = AI 判定总数 = auto_pass + human_required（人工动作行不计入）
        assertEquals(5, vo.getTotalReviews());
        assertEquals(3, vo.getAutoPassCount());
        assertEquals(2, vo.getHumanRequiredCount());
        assertEquals(2, vo.getHumanConfirmedCount());
        assertEquals(1, vo.getHumanAdjustedCount());
        assertEquals(0.6, vo.getAutoPassRate(), 1e-9);
        assertEquals(0.4, vo.getHumanRequiredRate(), 1e-9);
        assertEquals(8, vo.getScannedRows());
        assertEquals(8, vo.getTotalAuditRows(), "totalAuditRows 应为窗口内全部审计行数，且等于 scannedRows");
        assertFalse(vo.isTruncated());

        // 判定分布：固定顺序，仅含出现过的决策
        assertEquals(List.of("auto_pass", "human_required", "human_confirmed", "human_adjusted"),
                vo.getDecisionDistribution().stream().map(QualityMetricsVO.DecisionCount::getDecision).toList());
        assertEquals(List.of(3L, 2L, 2L, 1L),
                vo.getDecisionDistribution().stream().map(QualityMetricsVO.DecisionCount::getCount).toList());

        // 全量理由分布按次数倒序：rules_clean 3 > risk_category/vlm_unusable/human_confirmed/human_adjusted 各 1
        assertEquals("rules_clean", vo.getReasonDistribution().get(0).getReason());
        assertEquals(3, vo.getReasonDistribution().get(0).getCount());

        // AI 判定理由分布：仅 decider=ai 的行，不得出现人工动作理由
        assertEquals(List.of("risk_category", "rules_clean", "vlm_unusable"),
                vo.getAiReasonDistribution().stream().map(QualityMetricsVO.ReasonCount::getReason).sorted().toList());
        assertTrue(vo.getAiReasonDistribution().stream()
                        .noneMatch(r -> "human_confirmed".equals(r.getReason()) || "human_adjusted".equals(r.getReason())),
                "aiReasonDistribution 不得含人工动作理由");

        // 人工动作理由分布：仅 decider=human 的行
        assertEquals(List.of("human_adjusted", "human_confirmed"),
                vo.getHumanActionDistribution().stream().map(QualityMetricsVO.ReasonCount::getReason).sorted().toList());
        assertEquals(2, vo.getHumanActionDistribution().stream()
                .filter(r -> "human_confirmed".equals(r.getReason())).findFirst().orElseThrow().getCount());

        // 分类分布：product 4（3 auto + 1 human_required）、finance 4
        assertEquals(4, vo.getCategoryDistribution().stream()
                .filter(c -> "product".equals(c.getCategory())).findFirst().orElseThrow().getCount());

        // 人工确认操作人分布：仅 human_confirmed，userId=1 共 2 次
        assertEquals(1, vo.getHumanConfirmedByUser().size());
        assertEquals(1L, vo.getHumanConfirmedByUser().get(0).getUserId());
        assertEquals(2, vo.getHumanConfirmedByUser().get(0).getCount());

        // 省下的人工
        assertEquals(2, vo.getWorkloadSaved().getHumanConfirmations());
        assertEquals(3, vo.getWorkloadSaved().getAutoPassedWithoutHuman());
    }

    @Test
    void latencyIgnoresNonPositiveSamples() {
        insert("human_required", "ai", null, "product", "{\"reason\":\"vlm_unusable\"}", "glm-4.6v", 1200, 9000);
        insert("human_required", "ai", null, "product", "{\"reason\":\"vlm_unusable\"}", "glm-4.6v", 0, 13797);
        insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);

        QualityMetricsVO vo = service.metrics(30);

        // llm_ms=0 不计入 → 仅 1200 一个样本
        assertEquals(1, vo.getLlmLatency().getCount());
        assertEquals(1200, vo.getLlmLatency().getP50Ms());
        assertEquals(1200, vo.getLlmLatency().getP95Ms());
        assertEquals(1200, vo.getLlmLatency().getMaxMs());

        // vlm 样本 [9000, 13797]：P50=ceil(0.5*2)-1=0 → 9000；P95=ceil(1.9)-1=1 → 13797
        assertEquals(2, vo.getVlmLatency().getCount());
        assertEquals(9000, vo.getVlmLatency().getP50Ms());
        assertEquals(13797, vo.getVlmLatency().getP95Ms());
        assertEquals(13797, vo.getVlmLatency().getMaxMs());

        // AI 模型分布
        assertEquals(1, vo.getAiModelDistribution().size());
        assertEquals("glm-4.6v", vo.getAiModelDistribution().get(0).getModel());
        assertEquals(2, vo.getAiModelDistribution().get(0).getCount());
    }

    @Test
    void emptyWindowReturnsZeroRatesWithoutNan() {
        QualityMetricsVO vo = service.metrics(30);

        assertEquals(0, vo.getTotalReviews());
        assertEquals(0.0, vo.getAutoPassRate(), 1e-9);
        assertFalse(Double.isNaN(vo.getAutoPassRate()), "分母为 0 不得返回 NaN");
        assertEquals(0.0, vo.getHumanRequiredRate(), 1e-9);
        assertEquals(0, vo.getScannedRows());
        assertEquals(0, vo.getTotalAuditRows());
        assertFalse(vo.isTruncated());
        assertTrue(vo.getDecisionDistribution().isEmpty());
        assertTrue(vo.getReasonDistribution().isEmpty());
        assertTrue(vo.getAiReasonDistribution().isEmpty());
        assertTrue(vo.getHumanActionDistribution().isEmpty());
        assertEquals(0, vo.getLlmLatency().getCount());
        assertEquals(0, vo.getLlmLatency().getP50Ms());
        assertEquals(0, vo.getVlmLatency().getCount());
    }

    @Test
    void scanLimitMarksTruncatedAndKeepsNewest() {
        for (int i = 0; i < 5; i++) {
            insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);
        }

        QualityMetricsVO vo = service.metrics(30, 3);

        assertEquals(3, vo.getScannedRows());
        assertEquals(3, vo.getTotalAuditRows(), "截断后 totalAuditRows 仍等于实际聚合行数");
        assertTrue(vo.isTruncated());
        assertEquals(3, vo.getAutoPassCount());
        assertEquals(3, vo.getTotalReviews());
        // 全为 decider=ai 的行 → aiReasonDistribution 有值、humanActionDistribution 为空
        assertEquals("rules_clean", vo.getAiReasonDistribution().get(0).getReason());
        assertTrue(vo.getHumanActionDistribution().isEmpty());
    }

    @Test
    void excludesRowsOutsideWindow() {
        // 40 天前的一行（超出 30 天窗口）
        jdbcTemplate.update("INSERT INTO ingest_reviews(unit_id, decision, decider, category, detail_json, "
                        + "llm_ms, vlm_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
                999L, "auto_pass", "ai", "product", "{\"reason\":\"rules_clean\"}", 0, 0,
                LocalDateTime.now().minusDays(40));
        insert("auto_pass", "ai", null, "product", "{\"reason\":\"rules_clean\"}", null, 0, 0);

        QualityMetricsVO vo = service.metrics(30);

        assertEquals(1, vo.getScannedRows());
        assertEquals(1, vo.getAutoPassCount());
    }

    @Test
    void daysClampedAndWindowStartFormatted() {
        assertEquals(1, service.metrics(0).getDays());
        assertEquals(365, service.metrics(9999).getDays());
        assertTrue(service.metrics(30).getWindowStart().matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}"),
                "windowStart 应为 yyyy-MM-dd'T'HH:mm");
    }

    private void insert(String decision, String decider, Long userId, String category, String detailJson,
                        String aiModel, int llmMs, int vlmMs) {
        IngestReview review = new IngestReview();
        review.setUnitId(1L);
        review.setDecision(decision);
        review.setDecider(decider);
        review.setDeciderUserId(userId);
        review.setCategory(category);
        review.setDetailJson(detailJson);
        review.setAiModel(aiModel);
        review.setLlmMs(llmMs);
        review.setVlmMs(vlmMs);
        ingestReviewMapper.insert(review);
    }
}
