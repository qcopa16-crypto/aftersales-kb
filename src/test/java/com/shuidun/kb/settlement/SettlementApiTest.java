package com.shuidun.kb.settlement;

import static org.hamcrest.Matchers.greaterThanOrEqualTo;
import static org.hamcrest.Matchers.hasItem;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.shuidun.kb.dashboard.LatencySample;
import com.shuidun.kb.dashboard.service.QaAccessLogService;
import com.shuidun.kb.faq.FaqCacheService;
import com.shuidun.kb.faq.entity.Faq;
import com.shuidun.kb.faq.mapper.FaqMapper;
import com.shuidun.kb.gap.GapService;
import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.ObjectMapper;

/**
 * P6 FAQ / 知识缺口 / 看板真实链路测试。
 */
@SpringBootTest
@AutoConfigureMockMvc
class SettlementApiTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private FaqMapper faqMapper;

    @Autowired
    private GapService gapService;

    @Autowired
    private QaAccessLogService qaAccessLogService;

    @Test
    void faqReviewApproveThenOffline() throws Exception {
        String token = login("admin", "123456");
        Faq faq = new Faq();
        faq.setQuestion("测试问题-" + System.nanoTime());
        faq.setAnswer("测试答案");
        faq.setCategory("product");
        faq.setSourceType("auto_mined");
        faq.setStatus("pending_review");
        // F-25：question_norm 为 NOT NULL（缓存回源按规范化键查询），生产由 FaqService 统一填充
        faq.setQuestionNorm(FaqCacheService.normalize(faq.getQuestion()));
        faqMapper.insert(faq);

        mockMvc.perform(get("/api/settlement/faqs/recommendations?page=1&size=50")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.records[*].id", hasItem(faq.getId().intValue())));

        // 审核通过 → 发布
        mockMvc.perform(post("/api/settlement/faqs/" + faq.getId() + "/review")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"action\":\"approve\",\"editedAnswer\":\"审核后的答案\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        mockMvc.perform(get("/api/settlement/faqs?status=published&page=1&size=50")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.records[*].id", hasItem(faq.getId().intValue())))
                .andExpect(jsonPath("$.data.records[?(@.id==" + faq.getId() + ")].answer")
                        .value(hasItem("审核后的答案")));

        // 重复审核应被拒绝（非 pending）
        mockMvc.perform(post("/api/settlement/faqs/" + faq.getId() + "/review")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"action\":\"approve\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 编辑已发布 FAQ
        mockMvc.perform(put("/api/settlement/faqs/" + faq.getId())
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"answer\":\"编辑后的答案\"}"))
                .andExpect(status().isOk());

        // 下线
        mockMvc.perform(post("/api/settlement/faqs/" + faq.getId() + "/offline")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk());

        mockMvc.perform(get("/api/settlement/faqs?status=rejected&page=1&size=50")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.records[*].id", hasItem(faq.getId().intValue())));
    }

    @Test
    void invalidFaqReviewActionRejected() throws Exception {
        String token = login("admin", "123456");
        Faq faq = new Faq();
        faq.setQuestion("非法动作-" + System.nanoTime());
        faq.setAnswer("x");
        faq.setSourceType("manual");
        faq.setStatus("pending_review");
        // F-25：question_norm 为 NOT NULL，与生产 FaqService 行为对齐
        faq.setQuestionNorm(FaqCacheService.normalize(faq.getQuestion()));
        faqMapper.insert(faq);

        mockMvc.perform(post("/api/settlement/faqs/" + faq.getId() + "/review")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"action\":\"delete\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void nonReviewerForbiddenOnFaqReview() throws Exception {
        String token = login("zhangsan", "123456");
        mockMvc.perform(get("/api/settlement/faqs/recommendations").header("Authorization", "Bearer " + token))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value(403));
    }

    @Test
    void gapAggregatesAndStatusUpdates() throws Exception {
        String token = login("admin", "123456");
        String question = "缺口聚合问题-" + System.nanoTime();
        gapService.recordUnanswered(question);
        gapService.recordUnanswered(question);
        gapService.recordUnanswered(question);

        MvcResult result = mockMvc.perform(get("/api/settlement/knowledge-gaps?page=1&size=50")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andReturn();
        long gapId = findGapId(result, question);
        org.junit.jupiter.api.Assertions.assertTrue(gapId > 0, "应聚合出缺口记录");

        mockMvc.perform(patch("/api/settlement/knowledge-gaps/" + gapId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"ignored\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        mockMvc.perform(patch("/api/settlement/knowledge-gaps/" + gapId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"bogus\"}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void dashboardEndpointsReturnData() throws Exception {
        String token = login("admin", "123456");
        qaAccessLogService.record("s_dash", 1L, "看板问题", "答案", null,
                "kb", List.of(1L), List.of(1L), List.of(), 100, 20, new LatencySample(1200, 300, 800, 40, 5), false);

        mockMvc.perform(get("/api/dashboard/metrics").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.qaCount").value(greaterThanOrEqualTo(1)))
                .andExpect(jsonPath("$.data.unitCount").value(greaterThanOrEqualTo(5)));

        mockMvc.perform(get("/api/dashboard/rankings/questions?limit=5")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray());

        mockMvc.perform(get("/api/dashboard/rankings/units?limit=5")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data[*].label", hasItem("整机保修政策")));

        mockMvc.perform(get("/api/dashboard/stats/tokens?days=7")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data[*].qaCount").exists());

        // 趋势现在按桶返回（含空桶），断言"存在非空桶"而非"第一个桶非空"
        MvcResult trend = mockMvc.perform(get("/api/dashboard/stats/tokens?granularity=day&buckets=7")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        var buckets = objectMapper.readTree(trend.getResponse().getContentAsString()).path("data");
        long totalQa = 0;
        for (var b : buckets) {
            totalQa += b.path("qaCount").asLong();
        }
        org.junit.jupiter.api.Assertions.assertTrue(totalQa >= 1, "趋势桶内应至少统计到 1 次问答");

        // 回归：selectSince 必须带出 faq_hit，否则「FAQ 直出平均耗时」会恒为 0
        mockMvc.perform(get("/api/dashboard/latency?days=7")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.avgMs").exists())
                .andExpect(jsonPath("$.data.p95Ms").exists())
                .andExpect(jsonPath("$.data.breakdown.retrieveMs").exists())
                .andExpect(jsonPath("$.data.breakdown.generateMs").exists())
                .andExpect(jsonPath("$.data.breakdown.dbMs").exists())
                .andExpect(jsonPath("$.data.breakdown.cacheMs").exists())
                .andExpect(jsonPath("$.data.breakdown.otherMs").exists())
                .andExpect(jsonPath("$.data.breakdown.faqMs").value(org.hamcrest.Matchers.greaterThan(0)))
                .andExpect(jsonPath("$.data.slowest").isArray())
                .andExpect(jsonPath("$.data.slowest[0].dbMs").exists())
                .andExpect(jsonPath("$.data.slowest[0].cacheMs").exists());
    }

    @Test
    void dashboardRequiresPermission() throws Exception {
        String token = login("lisi", "123456");
        mockMvc.perform(get("/api/dashboard/metrics").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk());
    }

    private long findGapId(MvcResult result, String question) throws Exception {
        var records = objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("records");
        for (var record : records) {
            if (question.equals(record.path("questionPattern").asText())) {
                return record.path("id").asLong();
            }
        }
        return -1;
    }

    @Test
    void unitRankingSkipsMissingUnitsAndAggregatesByTitle() throws Exception {
        String token = login("admin", "123456");
        String name = "rank-test-" + System.nanoTime() + ".md";
        long unitA = importOne(token, name);
        long unitB = importOne(token, name); // 同名文件 → 同 title（模拟重复导入/多版本）
        long ghost = 9_999_999L;             // 已不存在的 unit（模拟被删除的知识）

        qaAccessLogService.record("s_rank", 1L, "q", "a", null, "kb", List.of(unitA), List.of(unitA),
                List.of(), 1, 1, new LatencySample(10, 1, 1, 1, 1), false);
        qaAccessLogService.record("s_rank", 1L, "q", "a", null, "kb", List.of(unitB), List.of(unitB),
                List.of(), 1, 1, new LatencySample(10, 1, 1, 1, 1), false);
        qaAccessLogService.record("s_rank", 1L, "q", "a", null, "kb", List.of(ghost), List.of(ghost),
                List.of(), 1, 1, new LatencySample(10, 1, 1, 1, 1), false);

        MvcResult result = mockMvc.perform(get("/api/dashboard/rankings/units?limit=50")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        var rows = objectMapper.readTree(result.getResponse().getContentAsString()).path("data");

        String aggregateLabel = null;
        long aggregateCount = 0;
        for (var row : rows) {
            String label = row.path("label").asText();
            org.junit.jupiter.api.Assertions.assertFalse(label.startsWith("# "),
                    "不得出现已删除单元的兜底标签：" + label);
            if (name.equals(label)) {
                aggregateLabel = label;
                aggregateCount = row.path("count").asLong();
            }
        }
        org.junit.jupiter.api.Assertions.assertNotNull(aggregateLabel, "同名知识应出现在榜单中");
        org.junit.jupiter.api.Assertions.assertEquals(2, aggregateCount, "同名知识应按标题聚合成一条");
    }

    private long importOne(String token, String filename) throws Exception {
        MockMultipartFile file = new MockMultipartFile("files", filename, "text/markdown",
                ("# " + filename + "\n\n正文内容").getBytes(StandardCharsets.UTF_8));
        MvcResult result = mockMvc.perform(multipart("/api/knowledge/import").file(file)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("items").get(0).path("unitId").asLong();
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
