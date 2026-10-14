package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.shuidun.kb.dashboard.LatencySample;
import com.shuidun.kb.dashboard.service.QaAccessLogService;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * F-29 回归：会话回看必须按<b>当前</b>数据权限重新过滤，失权引用不得泄露旧答案与来源标题。
 *
 * <p>路径：zhangsan 曾显式授权访问单元 A 并形成历史（含点选轮与 FAQ 轮）→ 撤销 A 的授权
 * → 再次回看：A 相关轮次答案脱敏、来源剔除 A、点选置空；无授权引用的 FAQ 轮不受影响。
 *
 * <p>R-11：无论授权是否撤销，未授权来源（unauthorized）一律不返回，普通历史响应不含失权 ID/标题。
 */
@SpringBootTest
@AutoConfigureMockMvc
class ChatHistoryRevocationTest {

    private static final String MASK = "（该轮回答引用的知识已被撤销访问权限，内容已隐藏）";

    /** zhangsan 的用户 id（种子数据）。 */
    private static final long ZHANGSAN = 2L;

    /** 李四（销售）私有的未授权单元（种子 U-1003）。 */
    private static final long FOREIGN_UNIT = 3L;

    private String sessionId;
    private Long revocableUnitId;

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private QaAccessLogService qaAccessLogService;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @BeforeEach
    void seedHistory() {
        sessionId = "s_revoke_" + System.nanoTime();

        KnowledgeUnit unit = new KnowledgeUnit();
        unit.setUnitCode("U-RVK-" + System.nanoTime());
        unit.setTitle("权限撤销测试单元");
        unit.setCategory("sop");
        unit.setUseVlm(0);
        unit.setStatus("active");
        unit.setIngestStatus("active");
        unit.setCreatorId(1L);
        unitMapper.insert(unit);
        revocableUnitId = unit.getId();

        // zhangsan 对该单元的显式用户授权（提问时可见）
        jdbcTemplate.update("INSERT INTO unit_permissions(unit_id, target_type, target_id) VALUES(?, 'user', ?)",
                revocableUnitId, ZHANGSAN);

        LatencySample latency = new LatencySample(1200, 300, 800, 40, 5);

        // 轮次 1：授权引用单元 A，同时存在未授权引用（销售私有单元）
        qaAccessLogService.record(sessionId, ZHANGSAN, "撤销前的问题", "整机保修期为一年，人为损坏不保。",
                null, "kb", List.of(revocableUnitId, FOREIGN_UNIT),
                List.of(revocableUnitId), List.of(FOREIGN_UNIT), 10, 20, latency, false);

        // 轮次 2：点选了单元 A
        qaAccessLogService.record(sessionId, ZHANGSAN, "点选轮的问题", "点选后的回答正文。",
                revocableUnitId, "clarify", List.of(revocableUnitId),
                List.of(revocableUnitId), List.of(), 10, 20, latency, false);

        // 轮次 3：FAQ 直出，无任何知识引用（撤销后不应受影响）
        qaAccessLogService.record(sessionId, ZHANGSAN, "FAQ 轮的问题", "FAQ 缓存的答案。",
                null, "faq", List.of(), List.of(), List.of(), 5, 5, latency, true);
    }

    @Test
    void beforeRevocationHistoryShowsAnswerAndSources() throws Exception {
        String token = login("zhangsan", "123456");
        JsonNode rounds = rounds(token);

        assertEquals("整机保修期为一年，人为损坏不保。", rounds.get(0).path("answer").asText());
        assertEquals(1, rounds.get(0).path("sources").path("authorized").size(),
                "撤销前应展示授权来源");
        assertEquals("权限撤销测试单元",
                rounds.get(0).path("sources").path("authorized").get(0).path("title").asText());
        // R-11：未授权来源（当时即不可见）不得返回任何 id/标题，列表恒为空
        assertEquals(0, rounds.get(0).path("sources").path("unauthorized").size(),
                "未授权来源不得返回（不泄露失权知识标识）");
    }

    @Test
    void afterRevocationAnswerMaskedAndSourcesStripped() throws Exception {
        String token = login("zhangsan", "123456");

        // 撤销 zhangsan 对单元 A 的显式授权
        jdbcTemplate.update("DELETE FROM unit_permissions WHERE unit_id = ? AND target_type = 'user'",
                revocableUnitId);

        JsonNode rounds = rounds(token);

        // 轮次 1：答案脱敏 + 授权来源剔除 A
        assertEquals(MASK, rounds.get(0).path("answer").asText(), "失权引用的答案必须脱敏");
        assertEquals(0, rounds.get(0).path("sources").path("authorized").size(),
                "失权 unit 必须从 sources 剔除");
        assertEquals(0, rounds.get(0).path("sources").path("unauthorized").size(),
                "未授权来源列表恒为空（R-11：普通历史不得含失权 ID/标题）");

        // 轮次 2：点选单元失权 → 置空且不返回标题
        // 注：全局 Jackson 配置 default-property-inclusion=non_null，置空表现为字段省略
        assertEquals(MASK, rounds.get(1).path("answer").asText());
        assertTrue(rounds.get(1).path("chosenUnitId").isMissingNode()
                        || rounds.get(1).path("chosenUnitId").isNull(),
                "失权点选单元必须置空（null 按 non_null 策略省略）");
        assertFalse(rounds.get(1).hasNonNull("chosenTitle"), "不得返回失权点选标题");
        assertEquals(0, rounds.get(1).path("sources").path("authorized").size());

        // 轮次 3：FAQ 直出无引用，答案不受影响
        assertEquals("FAQ 缓存的答案。", rounds.get(2).path("answer").asText(),
                "无授权引用的轮次不得被脱敏");
    }

    @Test
    void unauthorizedSourcesNeverLeakForeignTitleOrId() throws Exception {
        String token = login("zhangsan", "123456");
        MvcResult result = mockMvc.perform(get("/api/ai/chat/sessions/" + sessionId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        String body = result.getResponse().getContentAsString();
        // 未授权单元（销售私有 U-1003 渠道价格政策）的标题不得出现在普通历史响应任何位置
        assertFalse(body.contains("渠道价格政策"),
                "普通历史响应不得泄露未授权知识标题：" + body);
        JsonNode rounds = objectMapper.readTree(body).path("data").path("rounds");
        for (JsonNode round : rounds) {
            assertEquals(0, round.path("sources").path("unauthorized").size(),
                    "未授权来源列表必须恒为空（R-11）");
        }
    }

    @Test
    void otherUsersStillCannotReadForeignSession() throws Exception {
        String zhangsan = login("zhangsan", "123456");
        String lisi = login("lisi", "123456");
        assertEquals(0, rounds(lisi).size(), "他人会话不得读取");
        assertEquals(3, rounds(zhangsan).size(), "本人会话完整");
    }

    private JsonNode rounds(String token) throws Exception {
        MvcResult result = mockMvc.perform(get("/api/ai/chat/sessions/" + sessionId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("rounds");
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
