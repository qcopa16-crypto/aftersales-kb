package com.shuidun.kb.job;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.config.QualityReviewProperties;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * 质检判定策略纯函数真值表测试（无 Spring 依赖）。
 */
class QualityReviewPolicyTest {

    private final QualityReviewProperties props = new QualityReviewProperties();

    private ReviewResult resp(String verdict, Double confidence, String rules, String llm, String vlm) {
        ReviewResult result = new ReviewResult();
        result.setVerdict(verdict);
        result.setConfidence(confidence);
        ReviewResult.Tiers tiers = new ReviewResult.Tiers();
        tiers.setRules(rules);
        tiers.setLlm(llm);
        tiers.setVlm(vlm);
        result.setTiers(tiers);
        return result;
    }

    private QualityReviewService.Decision decision(ReviewResult resp, boolean imageHeavy) {
        return QualityReviewPolicy.decide(resp, imageHeavy, props).decision();
    }

    private String reason(ReviewResult resp, boolean imageHeavy) {
        return QualityReviewPolicy.decide(resp, imageHeavy, props).reason();
    }

    @Test
    void autoCategoryByRisk() {
        assertTrue(QualityReviewPolicy.isAutoCategory("product", props));
        assertTrue(QualityReviewPolicy.isAutoCategory("sop", props));
        assertFalse(QualityReviewPolicy.isAutoCategory("finance", props));
        assertFalse(QualityReviewPolicy.isAutoCategory("sales", props));
        assertFalse(QualityReviewPolicy.isAutoCategory("policy", props));
        assertFalse(QualityReviewPolicy.isAutoCategory("unknown", props));
        assertFalse(QualityReviewPolicy.isAutoCategory(null, props));
    }

    @Test
    void aiTierTriggerGate() {
        assertFalse(QualityReviewPolicy.needsAiTier(List.of()));
        assertFalse(QualityReviewPolicy.needsAiTier(List.of("product_manual_heuristic")));
        assertFalse(QualityReviewPolicy.needsAiTier(null));
        assertTrue(QualityReviewPolicy.needsAiTier(List.of("empty_chunks")));
        assertTrue(QualityReviewPolicy.needsAiTier(List.of("low_heading_coverage")));
        assertTrue(QualityReviewPolicy.needsAiTier(List.of("image_count_high")));
    }

    @Test
    void imageHeavyDetection() {
        assertTrue(QualityReviewPolicy.isImageHeavy(List.of("image_count_high")));
        assertFalse(QualityReviewPolicy.isImageHeavy(List.of("empty_chunks")));
        assertFalse(QualityReviewPolicy.isImageHeavy(null));
    }

    @Test
    void nullResponseFailsClosed() {
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(null, false));
        assertEquals("ai_error", reason(null, false));
    }

    @Test
    void allPassAutoApproves() {
        ReviewResult r = resp("pass", 0.95, "pass", "pass", "pass");
        assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, false));
        assertEquals("ai_pass", reason(r, false));
    }

    @Test
    void llmErrorFailsClosed() {
        ReviewResult r = resp("pass", 0.95, "review", "error", "skipped");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false));
        assertEquals("llm_review", reason(r, false));
    }

    @Test
    void llmSkippedStillAutoApproves() {
        ReviewResult r = resp("pass", 1.0, "pass", "skipped", "skipped");
        assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, false));
    }

    @Test
    void lowConfidenceFailsClosed() {
        ReviewResult r = resp("pass", 0.5, "pass", "pass", "skipped");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false));
        assertEquals("low_confidence", reason(r, false));
    }

    @Test
    void missingConfidenceFailsClosed() {
        ReviewResult r = resp("pass", null, "pass", "pass", "skipped");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false),
                "LLM 档 pass 但置信度缺失必须转人工（不得自动通过）");
        assertEquals("confidence_missing", reason(r, false));
    }

    @Test
    void nanConfidenceFailsClosed() {
        ReviewResult r = resp("pass", Double.NaN, "pass", "pass", "skipped");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false),
                "NaN 置信度不得通过阈值比较而被放行");
        assertEquals("confidence_invalid", reason(r, false));
    }

    @Test
    void outOfRangeConfidenceFailsClosed() {
        for (Double bad : new Double[]{-0.1, 1.1, 2.0}) {
            ReviewResult r = resp("pass", bad, "pass", "pass", "skipped");
            assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false),
                    "越界置信度必须转人工: " + bad);
            assertEquals("confidence_invalid", reason(r, false));
        }
    }

    @Test
    void boundaryConfidenceAtThresholdAutoApproves() {
        ReviewResult r = resp("pass", 0.8, "pass", "pass", "skipped");
        assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, false),
                "等于阈值时应放行");
    }

    @Test
    void skippedLlmTierWithoutConfidenceStillAutoApproves() {
        ReviewResult r = resp("pass", null, "pass", "skipped", "skipped");
        assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, false),
                "LLM 档未真实跑通（skipped）时不校验置信度，保持历史行为");
    }

    @Test
    void imageHeavyVlmUnavailableFailsClosed() {
        ReviewResult r = resp("review", 1.0, "pass", "skipped", "unavailable");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, true));
        assertEquals("vlm_unavailable", reason(r, true));
    }

    @Test
    void imageHeavyVlmErrorFailsClosed() {
        ReviewResult r = resp("review", 1.0, "pass", "skipped", "error");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, true));
        assertEquals("vlm_unusable", reason(r, true));
    }

    @Test
    void imageHeavyVlmPassAutoApproves() {
        ReviewResult r = resp("pass", 1.0, "pass", "skipped", "pass");
        assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, true));
    }

    @Test
    void verdictReviewFailsClosed() {
        ReviewResult r = resp("review", 1.0, "pass", "pass", "pass");
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, decision(r, false));
        assertEquals("verdict_review", reason(r, false));
    }

    @Test
    void vlmRequiredOffDoesNotBlockOnVlm() {
        props.setVlmRequired(false);
        try {
            ReviewResult r = resp("pass", 1.0, "pass", "skipped", "unavailable");
            assertEquals(QualityReviewService.Decision.AUTO_PASS, decision(r, true));
        } finally {
            props.setVlmRequired(true);
        }
    }
}
