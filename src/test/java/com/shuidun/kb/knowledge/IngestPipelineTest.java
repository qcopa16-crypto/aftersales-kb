package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.shuidun.kb.job.IngestWorker;
import com.shuidun.kb.job.entity.IngestReview;
import com.shuidun.kb.job.mapper.IngestReviewMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.entity.UnitPermission;
import com.shuidun.kb.permission.mapper.UnitPermissionMapper;
import com.shuidun.kb.support.FakeAiClient;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * P3 入库链路真实端到端测试：上传 → Worker 串行处理 → 质检门 → embed → finalize → 权限写入。
 *
 * <p>使用内存版 FakeAiClient 替代真实 FastAPI，其余全部为真实 Spring/MyBatis/H2 链路。
 * Worker 通过手动调用 poll() 驱动，保证确定性。
 *
 * <p><b>独立 H2 库隔离</b>：本类开启了后台 {@code @Scheduled} Worker（{@code worker.enabled=true}），
 * 若与其它测试类共享 {@code aftersales_kb} 库，其它用例遗留的 queued 作业会被后台 poll 抢占，
 * 并污染本类对 {@code FakeAiClient.parseCalls} 的全局计数断言，导致与执行顺序相关的偶发失败。
 * 故本类改用独立库 {@code aftersales_kb_pipeline_isolated}。
 *
 * <p>数据源 URL 可覆盖：{@code -Dtest.datasource.url=...}（或环境变量 {@code TEST_DATASOURCE_URL}）
 * 未设置时保持原 {@code jdbc:h2:mem:aftersales_kb_pipeline_isolated} 默认值，离线自包含可跑；
 * mysql profile 下可用其指向真实库（driver/凭据由 application-mysql.yaml 提供）。
 */
@SpringBootTest(properties = {
        "app.ingest.worker.enabled=true",
        "app.ingest.worker.interval-ms=3600000",
        "spring.datasource.url=${test.datasource.url:${TEST_DATASOURCE_URL:jdbc:h2:mem:aftersales_kb_pipeline_isolated;MODE=MySQL;"
                + "DATABASE_TO_LOWER=TRUE;CASE_INSENSITIVE_IDENTIFIERS=TRUE;DB_CLOSE_DELAY=-1}}"
})
@AutoConfigureMockMvc
class IngestPipelineTest {

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private FakeAiClient fakeAiClient;

    @Autowired
    private IngestWorker ingestWorker;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private UnitPermissionMapper unitPermissionMapper;

    @Autowired
    private IngestReviewMapper ingestReviewMapper;

    @BeforeEach
    void resetFake() {
        fakeAiClient.failParse = false;
        fakeAiClient.parseFailTimes.set(0);
        fakeAiClient.suggestedCategory = "sop";
        fakeAiClient.reviewReasonsOverride = null;
        fakeAiClient.reviewVerdict = "pass";
        fakeAiClient.reviewConfidence = 0.95;
        fakeAiClient.rulesTier = "pass";
        fakeAiClient.llmTier = "pass";
        fakeAiClient.vlmTier = "pass";
        fakeAiClient.failReview = false;
    }

    @Test
    void productManualAutoPassesWhenAiVerdictPass() throws Exception {
        fakeAiClient.suggestedCategory = "product";
        // 触发 AI 档（product_manual_heuristic 本身不再触发）
        fakeAiClient.reviewReasonsOverride = List.of("empty_chunks");
        fakeAiClient.reviewVerdict = "pass";
        fakeAiClient.reviewConfidence = 0.95;
        fakeAiClient.llmTier = "pass";
        fakeAiClient.vlmTier = "skipped";
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "产品手册自动通过" + System.nanoTime() + ".md", null);
        long unitId = imported.path("items").get(0).path("unitId").asLong();

        drainWorker();

        KnowledgeUnit unit = unitMapper.selectById(unitId);
        assertEquals("active", unit.getIngestStatus(), "AI 判定通过应自动 active");
        assertEquals("active", unit.getStatus());
        assertTrue(fakeAiClient.vectorStore.containsKey(unit.getUnitCode()), "自动通过应写入向量");
        IngestReview review = ingestReviewMapper.selectLatestByUnitId(unitId);
        assertNotNull(review, "自动通过应写审计");
        assertEquals("auto_pass", review.getDecision());
        assertEquals("ai", review.getDecider());
    }

    @Test
    void productManualStaysAtGateWhenAiVerdictReview() throws Exception {
        fakeAiClient.suggestedCategory = "product";
        fakeAiClient.reviewReasonsOverride = List.of("empty_chunks");
        fakeAiClient.reviewVerdict = "review";
        fakeAiClient.reviewConfidence = 0.4;
        fakeAiClient.llmTier = "pass";
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "产品手册转人工" + System.nanoTime() + ".md", null);
        String batchId = imported.path("batchId").asText();
        long unitId = imported.path("items").get(0).path("unitId").asLong();

        drainWorker();

        JsonNode file = batchFile(token, batchId, unitId);
        assertEquals("quality_gate", file.path("stage").asText(), "AI 判不准应停在质检门");
        assertEquals("parsed", file.path("ingestStatus").asText());
        assertFalse(fakeAiClient.vectorStore.containsKey(unitCode(unitId)), "未确认前不得写入向量");
        assertEquals("human_required", file.path("reviewVerdict").asText(), "批次详情应暴露 AI 结论");
        assertEquals("ai", file.path("reviewSource").asText());

        mockMvc.perform(post("/api/knowledge/import/quality-gate/" + unitId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"action\":\"confirm\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        drainWorker();

        KnowledgeUnit unit = unitMapper.selectById(unitId);
        assertEquals("active", unit.getIngestStatus());
        assertTrue(fakeAiClient.vectorStore.containsKey(unit.getUnitCode()), "确认后应写入向量");
        IngestReview review = ingestReviewMapper.selectLatestByUnitId(unitId);
        assertNotNull(review);
        assertEquals("human_confirmed", review.getDecision(), "人工确认应写审计");
        assertEquals("human", review.getDecider());
    }

    @Test
    void salesDocStaysAtGateThenActivatesWithRolePermissionAfterConfirm() throws Exception {
        fakeAiClient.suggestedCategory = "sales";
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "销售政策" + System.nanoTime() + ".md", null);
        String batchId = imported.path("batchId").asText();
        long unitId = imported.path("items").get(0).path("unitId").asLong();

        drainWorker();

        JsonNode file = batchFile(token, batchId, unitId);
        assertEquals("quality_gate", file.path("stage").asText(), "sales 属 manual-categories，永远人工");
        assertEquals("parsed", file.path("ingestStatus").asText());
        assertEquals("human_required", file.path("reviewVerdict").asText());
        // manual 类别默认「AI 预审 + 人工确认」：批次详情应暴露 AI 预审结论，但决策仍停在质检门
        assertEquals("pass", file.path("aiVerdict").asText(), "批次详情应暴露 AI 预审 verdict");
        assertEquals("fake-model", file.path("aiModel").asText(), "批次详情应暴露 AI 模型");
        assertTrue(file.path("aiReasons").isArray() && file.path("aiReasons").size() > 0,
                "批次详情应暴露 AI 预审 reasons");

        mockMvc.perform(post("/api/knowledge/import/quality-gate/" + unitId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"action\":\"confirm\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        drainWorker();

        KnowledgeUnit unit = unitMapper.selectById(unitId);
        assertEquals("active", unit.getIngestStatus(), "确认后应自动通过");
        assertEquals("sales", unit.getCategory(), "应采纳 AI 建议分类");
        List<UnitPermission> permissions = unitPermissionMapper.selectByUnitId(unitId);
        assertTrue(permissions.stream().anyMatch(p -> "role".equals(p.getTargetType())),
                "sales 默认权限应为角色维度");
    }

    @Test
    void nonRetryableParseFailureMarksFailedWithActionableMessage() throws Exception {
        fakeAiClient.failParse = true;
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "加密手册" + System.nanoTime() + ".md", "sop");
        String batchId = imported.path("batchId").asText();
        long unitId = imported.path("items").get(0).path("unitId").asLong();

        drainWorker();

        JsonNode file = batchFile(token, batchId, unitId);
        assertEquals("failed", file.path("stage").asText());
        assertEquals("PARSE_FAILED", file.path("errorCode").asText());
        assertTrue(file.path("errorMessage").asText().contains("加密"), "错误信息应给出可行动原因");
        assertEquals("failed", unitMapper.selectById(unitId).getIngestStatus());
    }

    @Test
    void retryableFailureRetriesThenSucceeds() throws Exception {
        fakeAiClient.parseFailTimes.set(2);
        int baseline = fakeAiClient.parseCalls.get();
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "重试手册" + System.nanoTime() + ".md", "sop");
        long unitId = imported.path("items").get(0).path("unitId").asLong();

        drainWorker();

        KnowledgeUnit unit = unitMapper.selectById(unitId);
        assertEquals("active", unit.getIngestStatus(), "可重试错误应在指数退避后成功");
        assertEquals(3, fakeAiClient.parseCalls.get() - baseline, "应尝试 3 次（2 次失败 + 1 次成功）");
    }

    @Test
    void failedJobCanBeRetriedFromApi() throws Exception {
        fakeAiClient.failParse = true;
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "重试接口" + System.nanoTime() + ".md", "sop");
        long unitId = imported.path("items").get(0).path("unitId").asLong();
        drainWorker();
        assertEquals("failed", unitMapper.selectById(unitId).getIngestStatus());

        fakeAiClient.failParse = false;
        mockMvc.perform(post("/api/knowledge/import/retry/" + unitId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fromStage\":\"parse\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        drainWorker();
        assertEquals("active", unitMapper.selectById(unitId).getIngestStatus(), "重试后应可检索");
    }

    @Test
    void deleteRemovesVectorsAndMainDataWhenAiAvailable() throws Exception {
        String token = login("admin", "123456");
        JsonNode imported = importOneFile(token, "删除成功" + System.nanoTime() + ".md", "sop");
        long unitId = imported.path("items").get(0).path("unitId").asLong();
        drainWorker();
        String unitCode = unitCode(unitId);
        assertTrue(fakeAiClient.vectorStore.containsKey(unitCode));

        mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .delete("/api/knowledge/units")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"ids\":[" + unitId + "]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[0].success").value(true));

        assertFalse(fakeAiClient.vectorStore.containsKey(unitCode), "向量应已删除");
        assertEquals(0, unitPermissionMapper.selectByUnitId(unitId).size(), "权限记录应级联删除");
        mockMvc.perform(get("/api/knowledge/units/" + unitId).header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest());
    }

    private void drainWorker() {
        for (int i = 0; i < 6; i++) {
            ingestWorker.poll();
        }
    }

    private String unitCode(long unitId) {
        return unitMapper.selectById(unitId).getUnitCode();
    }

    private JsonNode batchFile(String token, String batchId, long unitId) throws Exception {
        MvcResult result = mockMvc.perform(get("/api/knowledge/import/batches/" + batchId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        JsonNode files = objectMapper.readTree(result.getResponse().getContentAsString()).path("data").path("files");
        for (JsonNode file : files) {
            if (file.path("unitId").asLong() == unitId) {
                return file;
            }
        }
        throw new IllegalStateException("批次中未找到 unitId=" + unitId);
    }

    private JsonNode importOneFile(String token, String filename, String category) throws Exception {
        MockMultipartFile file = new MockMultipartFile("files", filename, "text/markdown",
                ("# " + filename + "\n\n正文内容").getBytes(StandardCharsets.UTF_8));
        var request = multipart("/api/knowledge/import").file(file).header("Authorization", "Bearer " + token);
        if (category != null) {
            request = request.param("category", category);
        }
        MvcResult result = mockMvc.perform(request)
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.acceptedCount").value(1))
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString()).path("data");
    }

    private String login(String username, String password) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("accessToken").asText();
    }
}
