package com.shuidun.kb.job;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.ParseQuality;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.config.QualityReviewProperties;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.support.FakeAiClient;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;

/**
 * 质检判定编排测试（H2 + 真实 Spring/MyBatis，FakeAiClient 替代真实 AI）。
 *
 * <p>覆盖 fail-closed 关键路径：熔断打开、AI 异常、VLM 不可用、总开关关闭的历史行为。
 */
@SpringBootTest(properties = {"app.ingest.worker.enabled=false"})
class QualityReviewServiceTest {

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Autowired
    private QualityReviewService service;

    @Autowired
    private FakeAiClient fakeAiClient;

    @Autowired
    private ReviewCircuitBreaker circuitBreaker;

    @Autowired
    private QualityReviewProperties props;

    @Autowired
    private IngestReviewMapper ingestReviewMapper;

    @BeforeEach
    void setUp() {
        props.setEnabled(true);
        props.setPreReviewManualCategories(true);
        props.setLlmEnabled(true);
        props.setVlmEnabled(true);
        props.setVlmRequired(true);
        circuitBreaker.reset();
        fakeAiClient.failReview = false;
        fakeAiClient.reviewVerdict = "pass";
        fakeAiClient.reviewConfidence = 0.95;
        fakeAiClient.rulesTier = "pass";
        fakeAiClient.llmTier = "pass";
        fakeAiClient.vlmTier = "pass";
    }

    @AfterEach
    void tearDown() {
        circuitBreaker.reset();
        props.setEnabled(true);
        props.setPreReviewManualCategories(true);
        props.setVlmRequired(true);
    }

    @Test
    void manualCategorySkipsAiWhenPreReviewDisabled() {
        props.setPreReviewManualCategories(false);
        int before = fakeAiClient.reviewCalls.get();
        QualityReviewService.Outcome outcome = service.review(unit("finance"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision());
        assertEquals("risk_category", outcome.reason());
        assertEquals(before, fakeAiClient.reviewCalls.get(), "关闭预审时风险类别不得调用 AI");

        IngestReview review = ingestReviewMapper.selectLatestByUnitId(1L);
        assertEquals("human_required", review.getDecision());
        assertEquals("skipped", review.getRulesVerdict(), "未评估的规则档应显式置 skipped");
        assertTrue(review.getDetailJson().contains("risk_category"), "detail_json 应含 reason");
    }

    @Test
    void manualPreReviewCallsAiAndRecordsVerdict() {
        int before = fakeAiClient.reviewCalls.get();
        QualityReviewService.Outcome outcome = service.review(unit("finance"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision(), "manual 预审后决策仍为人工");
        assertEquals("risk_category", outcome.reason(), "reason 仍为 risk_category");
        assertEquals(before + 1, fakeAiClient.reviewCalls.get(), "manual 预审应调用 AI");

        IngestReview review = ingestReviewMapper.selectLatestByUnitId(1L);
        assertEquals("human_required", review.getDecision(), "决策恒为人工");
        assertEquals("ai", review.getDecider());
        assertEquals("pass", JsonUtils.fieldString(review.getDetailJson(), "verdict"),
                "detail_json 顶层应含 AI verdict");
        assertEquals("true", JsonUtils.fieldString(review.getDetailJson(), "aiPreReview"));
        assertEquals("fake-model", review.getAiModel(), "AI 可用时应回填 ai_model");
        assertEquals(0.95, review.getLlmConfidence(), 0.0001, "AI 可用时应回填置信度");
    }

    @Test
    void manualPreReviewAiExceptionKeepsDecisionAndRecordsError() {
        fakeAiClient.failReview = true;
        QualityReviewService.Outcome outcome = service.review(unit("finance"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision(),
                "AI 异常不得改变 manual 决策（人工本就是决策者）");
        assertEquals("risk_category", outcome.reason());

        IngestReview review = ingestReviewMapper.selectLatestByUnitId(1L);
        assertEquals("human_required", review.getDecision());
        String aiError = JsonUtils.fieldString(review.getDetailJson(), "aiError");
        assertNotNull(aiError, "AI 不可用应记录 aiError");
        assertFalse(aiError.isBlank());
        assertNull(JsonUtils.fieldString(review.getDetailJson(), "verdict"), "AI 不可用时不应有 verdict");
    }

    @Test
    void manualPreReviewCircuitOpenKeepsDecisionAndRecordsUnavailable() {
        circuitBreaker.recordFailure();
        circuitBreaker.recordFailure();
        circuitBreaker.recordFailure();
        assertTrue(circuitBreaker.isOpen(), "连续失败应打开熔断");

        int before = fakeAiClient.reviewCalls.get();
        QualityReviewService.Outcome outcome = service.review(unit("sales"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision());
        assertEquals("risk_category", outcome.reason());
        assertEquals(before, fakeAiClient.reviewCalls.get(), "熔断打开不得调用 AI");

        IngestReview review = ingestReviewMapper.selectLatestByUnitId(1L);
        assertEquals("circuit_open", JsonUtils.fieldString(review.getDetailJson(), "aiError"));
    }

    @Test
    void rulesCleanAutoPassesWithoutAi() {
        int before = fakeAiClient.reviewCalls.get();
        QualityReviewService.Outcome outcome = service.review(unit("sop"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.AUTO_PASS, outcome.decision());
        assertEquals("rules_clean", outcome.reason());
        assertEquals(before, fakeAiClient.reviewCalls.get(), "规则档干净不得调用 AI");

        IngestReview review = ingestReviewMapper.selectLatestByUnitId(1L);
        assertEquals("auto_pass", review.getDecision());
        assertEquals("pass", review.getRulesVerdict(), "规则档干净应显式置 pass");
        assertEquals(1.0, review.getLlmConfidence(), 0.0001, "规则档确定性命中=满置信 1.0");
        assertTrue(review.getDetailJson().contains("rules_clean"), "detail_json 应含 reason");
    }

    @Test
    void circuitOpenGoesToHumanWithoutAi() {
        // 打开熔断（默认阈值 3）
        circuitBreaker.recordFailure();
        circuitBreaker.recordFailure();
        circuitBreaker.recordFailure();
        assertTrue(circuitBreaker.isOpen(), "连续失败应打开熔断");

        int before = fakeAiClient.reviewCalls.get();
        QualityReviewService.Outcome outcome = service.review(unit("product"),
                result(List.of("empty_chunks")), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision());
        assertEquals("circuit_open", outcome.reason());
        assertEquals(before, fakeAiClient.reviewCalls.get(), "熔断打开不得调用 AI");
    }

    @Test
    void aiExceptionFailsClosedToHuman() {
        fakeAiClient.failReview = true;
        QualityReviewService.Outcome outcome = service.review(unit("product"),
                result(List.of("empty_chunks")), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision());
        assertTrue(outcome.reason().startsWith("ai_error:"), "AI 异常应 fail-closed: " + outcome.reason());
    }

    @Test
    void imageHeavyVlmUnavailableFailsClosed() {
        fakeAiClient.vlmTier = "unavailable";
        fakeAiClient.reviewVerdict = "review";
        QualityReviewService.Outcome outcome = service.review(unit("product"),
                result(List.of("image_count_high")), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, outcome.decision());
        assertEquals("vlm_unavailable", outcome.reason());
    }

    @Test
    void disabledReturnsLegacyBehavior() {
        props.setEnabled(false);
        int before = fakeAiClient.reviewCalls.get();

        QualityReviewService.Outcome product = service.review(unit("product"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.HUMAN_REQUIRED, product.decision());
        assertEquals("legacy_product_gate", product.reason());

        QualityReviewService.Outcome sop = service.review(unit("sop"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.AUTO_PASS, sop.decision());
        assertEquals("legacy_auto", sop.reason());

        QualityReviewService.Outcome sales = service.review(unit("sales"), result(List.of()), job());
        assertEquals(QualityReviewService.Decision.AUTO_PASS, sales.decision(),
                "关闭时 sales 应回到历史的自动放行");

        assertEquals(before, fakeAiClient.reviewCalls.get(), "关闭总开关不得调用 AI");
    }

    private KnowledgeUnit unit(String category) {
        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setId(1L);
        unit.setUnitCode("U-TEST");
        unit.setCategory(category);
        unit.setBatchId("B-TEST");
        unit.setContentHash("content-hash-test");
        return unit;
    }

    private ParseResult result(List<String> reviewReasons) {
        ParseResult result = new ParseResult();
        result.setMarkdown("# 测试文档");
        result.setChunks(new ArrayList<>());
        ParseQuality quality = new ParseQuality();
        quality.setReviewReasons(reviewReasons);
        result.setQuality(quality);
        return result;
    }

    private IngestJob job() {
        IngestJob job = new IngestJob();
        job.setId(10L);
        job.setUnitId(1L);
        return job;
    }
}
