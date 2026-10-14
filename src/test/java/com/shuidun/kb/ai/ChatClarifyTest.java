package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.support.FakeAiClient;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * 澄清式消歧测试（真实 Tomcat + H2 + 内存 AI）。
 *
 * <p>核心原则：<b>缺什么才问什么</b>。「检索为空」不等于「需要澄清」——
 * 检索为空最常见的原因是知识还没切片向量化，跟用户问题是否完整无关。
 *
 * <p>因此两条路径必须分开断言：
 * <ul>
 *   <li>问题<b>信息不足</b>（「路由器有问题」没说现象）→ {@code kind=ask} 追问 + 补充提示选项；</li>
 *   <li>问题<b>已经问清楚</b>（「H3C 路由器指示灯不亮」）→ {@code kind=reference} 给参考资料，
 *       <b>不得</b>再追问。</li>
 * </ul>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class ChatClarifyTest {

    /** 售后部可见的「整机保修政策」。 */
    private static final long UNIT_POLICY = 1L;

    /** 仅销售角色可见的「渠道价格政策」——售后账号不可访问，用于越权断言。 */
    private static final long UNIT_SALES_ONLY = 3L;

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Value("${local.server.port}")
    private int port;

    @Autowired
    private FakeAiClient fakeAiClient;

    private final HttpClient httpClient = HttpClient.newHttpClient();

    private final ObjectMapper json = JsonMapper.builder().build();

    @BeforeEach
    void clearVectors() {
        // 关键：向量库为空 → 检索必然无召回，强制走到澄清判断
        fakeAiClient.vectorStore.clear();
        fakeAiClient.forcedRetrieveUnitCodes = List.of();
        fakeAiClient.lastGenerateContexts.clear();
    }

    @Test
    void vagueQuestionAsksForMissingDetailInsteadOfListingDocuments() throws Exception {
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_ask\",\"question\":\"路由器有问题\"}");

        Map<String, List<String>> events = parseSse(response.body());
        assertTrue(events.containsKey("clarify"), "信息不足时应追问：" + response.body());

        JsonNode clarify = json.readTree(events.get("clarify").get(0));
        assertEquals("ask", clarify.path("kind").asText(), "应判定为追问而不是给参考资料：" + clarify);
        assertFalse(clarify.path("prompt").asText().isBlank(), "追问要有文案：" + clarify);
        assertTrue(clarify.path("prompt").asText().contains("路由器有问题"),
                "追问文案应回显原问题，让用户知道在问什么：" + clarify);

        JsonNode options = clarify.path("options");
        assertTrue(options.size() > 0, "追问要给出可点的补充选项：" + clarify);
        for (JsonNode option : options) {
            assertEquals("hint", option.path("type").asText(),
                    "追问的选项应是补充提示（点选后拼进问题重新检索），不是知识文档：" + option);
        }

        assertTrue(fakeAiClient.lastGenerateContexts.isEmpty(), "追问轮不应调用 generate");
    }

    @Test
    void wellFormedQuestionGetsReferenceNotAsk() throws Exception {
        String token = login("zhangsan", "123456");
        // 有产品（H3C 路由器）也有现象（指示灯不亮），信息是完整的
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_ref\",\"question\":\"H3C 路由器指示灯不亮\"}");

        JsonNode clarify = json.readTree(parseSse(response.body()).get("clarify").get(0));
        assertEquals("reference", clarify.path("kind").asText(),
                "信息已完整时不该再追问，只给参考：" + clarify);
        assertEquals("", clarify.path("prompt").asText(), "参考形态不该有追问文案");

        JsonNode options = clarify.path("options");
        assertTrue(options.size() > 0, "应给出可能相关的资料：" + clarify);
        for (JsonNode option : options) {
            assertEquals("unit", option.path("type").asText(), "参考选项应是知识单元：" + option);
        }
    }

    @Test
    void supplementingAfterAskMovesPastClarification() throws Exception {
        String token = login("zhangsan", "123456");
        // 第一次：笼统描述 → 追问
        SseResponse first = postSse(token,
                "{\"sessionId\":\"s_supp\",\"question\":\"路由器有问题\"}");
        assertEquals("ask", json.readTree(parseSse(first.body()).get("clarify").get(0))
                .path("kind").asText());

        // 用户点选/补充了现象后，前端会把原问题拼进来重发
        SseResponse second = postSse(token,
                "{\"sessionId\":\"s_supp\",\"question\":\"路由器有问题 指示灯异常\"}");
        JsonNode clarify = json.readTree(parseSse(second.body()).get("clarify").get(0));
        assertNotEquals("ask", clarify.path("kind").asText(),
                "补充了现象之后不应再追问同一件事：" + clarify);
        assertTrue(clarify.path("options").size() > 0, "应能给出相关资料：" + clarify);
    }

    @Test
    void choosingCandidateAnswersFromThatUnitWithoutRetrieval() throws Exception {
        String token = login("zhangsan", "123456");
        int retrieveBefore = fakeAiClient.retrieveCalls.get();

        SseResponse response = postSse(token, "{\"sessionId\":\"s_choose\","
                + "\"question\":\"保修期是多久？\",\"chosenUnitId\":" + UNIT_POLICY + "}");
        Map<String, List<String>> events = parseSse(response.body());

        assertFalse(events.containsKey("clarify"), "点选后不应再给候选：" + response.body());
        assertEquals(retrieveBefore, fakeAiClient.retrieveCalls.get(), "点选应跳过向量检索");

        List<String> contextCodes = fakeAiClient.lastGenerateContexts.stream()
                .map(GenerateContext::getUnitId).toList();
        assertEquals(List.of("U-1001"), contextCodes, "应只用选中的单元作为上下文");

        String sources = events.get("sources").get(0);
        assertTrue(sources.contains("整机保修政策"), "来源应指向所选知识：" + sources);

        String done = events.get("done").get(0);
        assertTrue(done.contains("\"tokens\":140"), "应带上真实 token 数（120+20）：" + done);
    }

    @Test
    void choosingInaccessibleUnitIsRejectedWithoutLeakingExistence() throws Exception {
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token, "{\"sessionId\":\"s_deny\","
                + "\"question\":\"渠道价格是多少？\",\"chosenUnitId\":" + UNIT_SALES_ONLY + "}");

        String body = response.body();
        assertTrue(body.contains("所选知识不存在或您暂无访问权限"),
                "不可访问时应给统一话术（不区分不存在/无权限）：" + body);
        assertFalse(body.contains("七折"), "不得泄露无权限知识正文");
        assertTrue(fakeAiClient.lastGenerateContexts.isEmpty(), "不可访问时不得调用 generate");
        assertFalse(parseSse(body).containsKey("clarify"), "点选轮不应回退到候选");
    }

    @Test
    void clarifyChoiceIsRecordedInHistory() throws Exception {
        String token = login("zhangsan", "123456");
        postSse(token, "{\"sessionId\":\"s_hist_choice\","
                + "\"question\":\"保修期是多久？\",\"chosenUnitId\":" + UNIT_POLICY + "}");

        JsonNode rounds = getJson(token, "/api/ai/chat/sessions/s_hist_choice")
                .path("data").path("rounds");
        assertEquals(1, rounds.size(), "应落库一轮问答");
        JsonNode round = rounds.get(0);
        assertEquals(UNIT_POLICY, round.path("chosenUnitId").asLong(), "应落库本轮的点选单元");
        assertEquals("整机保修政策", round.path("chosenTitle").asText(), "应反查出所选知识标题");
    }

    /* ---------------- 辅助 ---------------- */

    private SseResponse postSse(String token, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/ai/chat/stream"))
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> raw = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertEquals(200, raw.statusCode(), "SSE 应返回 200");
        return new SseResponse(raw.statusCode(), raw.body());
    }

    private JsonNode getJson(String token, String path) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .header("Accept", "application/json")
                .header("Authorization", "Bearer " + token)
                .GET()
                .build();
        HttpResponse<String> response = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertEquals(200, response.statusCode(), "GET " + path + " 应返回 200");
        return json.readTree(response.body());
    }

    private String login(String username, String password) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/auth/login"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}",
                        StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        return json.readTree(response.body()).path("data").path("accessToken").asText();
    }

    private String baseUrl() {
        return "http://127.0.0.1:" + port;
    }

    private Map<String, List<String>> parseSse(String body) {
        Map<String, List<String>> events = new LinkedHashMap<>();
        String currentEvent = null;
        for (String line : body.split("\n")) {
            if (line.startsWith("event:")) {
                currentEvent = line.substring("event:".length()).trim();
            } else if (line.startsWith("data:")) {
                String data = line.substring("data:".length()).trim();
                if (currentEvent != null) {
                    events.computeIfAbsent(currentEvent, key -> new ArrayList<>()).add(data);
                }
            }
        }
        return events;
    }

    /**
     * SSE 响应。
     *
     * @param statusCode HTTP 状态码
     * @param body       响应体
     */
    private record SseResponse(int statusCode, String body) {
    }
}
