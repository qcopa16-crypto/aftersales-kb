package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.AiChunk;
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
import org.springframework.test.context.TestPropertySource;
import tools.jackson.databind.ObjectMapper;

/**
 * 闲聊/通用问答兜底（{@code ai.chat-fallback-enabled=true}）的编排测试。
 *
 * <p>三条关键断言：
 * <ol>
 *   <li>非业务问题 + 无召回 → 发 notice 横幅 + 走 GENERAL 模式生成；</li>
 *   <li>业务问题（保修）→ 仍拒答，绝不闲聊；</li>
 *   <li>权限拦截（unauthorizedSources 非空）→ 仍权限拒答，绝不闲聊。</li>
 * </ol>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestPropertySource(properties = "ai.chat-fallback-enabled=true")
class ChatGeneralFallbackTest {

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

    private final ObjectMapper json = tools.jackson.databind.json.JsonMapper.builder().build();

    @BeforeEach
    void resetFake() {
        fakeAiClient.vectorStore.clear();
        fakeAiClient.forcedRetrieveUnitCodes = List.of();
        fakeAiClient.lastGenerateContexts.clear();
        fakeAiClient.lastGenerateMode = null;
    }

    @Test
    void generalQuestionFallsBackToLlmGeneral() throws Exception {
        // 向量库为空 → 无召回；问题非业务性 → 应走闲聊兜底
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_chat\",\"question\":\"量子计算机的散热方案是什么\"}");
        assertEquals(200, response.statusCode());
        Map<String, List<String>> events = parseSse(response.body());

        assertTrue(events.containsKey("notice"), "闲聊兜底应先发 notice 横幅");
        assertTrue(events.get("notice").get(0).contains("llm_general"), "notice.kind 应为 llm_general");
        assertTrue(response.body().contains(FakeAiClient.GENERAL_ANSWER), "答案应来自 GENERAL 生成");
        assertEquals(AiGenerateMode.GENERAL, fakeAiClient.lastGenerateMode, "应走 GENERAL 模式");
        assertTrue(fakeAiClient.lastGenerateContexts.isEmpty(), "闲聊兜底不得携带 contexts");
    }

    @Test
    void businessQuestionStillRefusesAndNeverChats() throws Exception {
        // 「保修期是多久」命中业务词表 → 即使无召回也必须拒答，不得闲聊
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_biz\",\"question\":\"保修期是多久\"}");
        Map<String, List<String>> events = parseSse(response.body());

        assertFalse(events.containsKey("notice"), "业务问题不得走闲聊兜底");
        assertFalse(response.body().contains(FakeAiClient.GENERAL_ANSWER), "业务问题不得返回通用知识答案");
        assertNull(fakeAiClient.lastGenerateMode, "业务问题无召回时不得调用 generate");
    }

    @Test
    void unauthorizedRecallNeverFallsBackToChat() throws Exception {
        // 只有销售私有的 U-1003 有向量；zhangsan（售后）无权限 → unauthorizedSources 非空
        fakeAiClient.vectorStore.put("U-1003", List.of(chunk(0, "渠道商拿货价为建议零售价的七折。")));
        fakeAiClient.forcedRetrieveUnitCodes = List.of("U-1003");

        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_perm\",\"question\":\"量子计算机的散热方案是什么\"}");

        assertFalse(response.body().contains("llm_general"), "权限拦截绝不闲聊兜底");
        assertTrue(response.body().contains("暂无访问权限"), "应给权限拒答话术");
        assertNull(fakeAiClient.lastGenerateMode, "权限拦截不得调用 generate");
    }

    @Test
    void shortGeneralQuestionGoesToChatNotDeviceClarify() throws Exception {
        // 「1+1」这类短通用问题：开关开启时不得被追问"是哪个设备"，应走闲聊兜底
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token, "{\"sessionId\":\"s_math\",\"question\":\"1+1\"}");
        Map<String, List<String>> events = parseSse(response.body());

        assertTrue(events.containsKey("notice"), "1+1 应走闲聊兜底");
        assertFalse(response.body().contains("哪个设备"), "1+1 不该出现设备追问");
        assertEquals(AiGenerateMode.GENERAL, fakeAiClient.lastGenerateMode);
    }

    @Test
    void vagueFaultStillAsksPhenomenonNotChat() throws Exception {
        // 「路由器有问题」含笼统故障词 → 规则 1 仍应追问现象（不被闲聊兜底抢走）
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token, "{\"sessionId\":\"s_vague\",\"question\":\"路由器有问题\"}");
        Map<String, List<String>> events = parseSse(response.body());

        assertTrue(events.containsKey("clarify"), "笼统故障问题仍应澄清追问现象");
        assertFalse(events.containsKey("notice"), "澄清追问不得走闲聊兜底");
        assertNull(fakeAiClient.lastGenerateMode, "澄清轮不得调用 generate");
    }

    @Test
    void kbQuestionWithEntityHitDoesNotChat() throws Exception {
        // 知识库有内容时，命中实体的问题必须走知识库，不得闲聊兜底
        fakeAiClient.vectorStore.put("U-1005", List.of(chunk(0, "设备上电后 PWR 指示灯常亮绿色表示正常。")));
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token, "{\"sessionId\":\"s_entity\",\"question\":\"指示灯绿色代表什么\"}");

        assertFalse(response.body().contains("llm_general"), "命中知识库实体的问题不得走闲聊兜底");
        assertEquals(AiGenerateMode.KB, fakeAiClient.lastGenerateMode, "应走知识库生成");
    }

    @Test
    void shortGeneralQuestionChatsEvenWhenRetrievalReturnsChunks() throws Exception {
        // 知识库有内容（检索会返回不相关 chunk）时，「1+1」仍应走闲聊兜底
        fakeAiClient.vectorStore.put("U-1005", List.of(chunk(0, "设备上电后 PWR 指示灯常亮绿色表示正常。")));
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token, "{\"sessionId\":\"s_math2\",\"question\":\"1+1\"}");
        Map<String, List<String>> events = parseSse(response.body());

        assertTrue(events.containsKey("notice"), "知识库有内容时 1+1 仍应走闲聊兜底");
        assertEquals(AiGenerateMode.GENERAL, fakeAiClient.lastGenerateMode);
    }

    private String baseUrl() {
        return "http://127.0.0.1:" + port;
    }

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
        return new SseResponse(raw.statusCode(), raw.body());
    }

    /**
     * SSE 响应（状态码 + 原始文本）。
     *
     * @param statusCode HTTP 状态码
     * @param body       响应体
     */
    private record SseResponse(int statusCode, String body) {
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

    private AiChunk chunk(int index, String content) {
        AiChunk chunk = new AiChunk();
        chunk.setChunkIndex(index);
        chunk.setContent(content);
        return chunk;
    }
}
