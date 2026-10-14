package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.ChatRequest;
import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.dashboard.mapper.QaAccessLogMapper;
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

/**
 * P4 问答编排真实端到端测试（真实 Tomcat + 真实 SSE + H2 + 内存 AI）。
 *
 * <p>核心断言：无权限召回内容绝不进入 generate 的 contexts（代码设计规范 §9.2 第 8 条）。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class ChatOrchestrationTest {

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

    @Autowired
    private QaAccessLogMapper qaAccessLogMapper;

    private final HttpClient httpClient = HttpClient.newHttpClient();

    private final ObjectMapper json = tools.jackson.databind.json.JsonMapper.builder().build();

    @BeforeEach
    void seedVectors() {
        fakeAiClient.vectorStore.clear();
        fakeAiClient.forcedRetrieveUnitCodes = List.of();
        fakeAiClient.lastGenerateContexts.clear();
        // U-1001 售后部可见；U-1005 全局；U-1003 仅销售角色可见
        fakeAiClient.vectorStore.put("U-1001", List.of(chunk(0, "整机保修期为一年，人为损坏不在免费保修范围内。")));
        fakeAiClient.vectorStore.put("U-1005", List.of(chunk(0, "设备上电后 PWR 指示灯常亮绿色表示正常。")));
        fakeAiClient.vectorStore.put("U-1003", List.of(chunk(0, "渠道商拿货价为建议零售价的七折，禁止跨区窜货。")));
    }

    @Test
    void streamsSourcesDeltaDoneForAuthorizedUser() throws Exception {
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_auth\",\"question\":\"保修多久\"}");
        assertEquals(200, response.statusCode());
        Map<String, List<String>> events = parseSse(response.body());

        assertTrue(events.containsKey("sources"), "应先收到 sources 事件");
        assertTrue(events.containsKey("delta"), "应收到 delta 事件");
        assertTrue(events.containsKey("done"), "应收到 done 事件");

        String sources = events.get("sources").get(0);
        assertTrue(sources.contains("整机保修政策"), "授权来源应包含售后政策");
        assertTrue(sources.contains("H3C 路由器用户手册"), "授权来源应包含全局产品手册");

        String done = events.get("done").get(0);
        assertTrue(done.contains("\"faqHit\":false"));
        assertTrue(done.contains("\"sessionId\":\"s_auth\""));

        // generate 只收到可访问的 contexts
        List<String> contextCodes = fakeAiClient.lastGenerateContexts.stream()
                .map(GenerateContext::getUnitId).toList();
        assertTrue(contextCodes.contains("U-1001"));
        assertTrue(contextCodes.contains("U-1005"));
        assertFalse(contextCodes.contains("U-1003"), "销售私有单元不得进入售后用户 Prompt");
    }

    @Test
    void unauthorizedRecallNeverReachesPrompt() throws Exception {
        // 模拟 AI 越权召回：强制返回销售私有单元 U-1003
        fakeAiClient.forcedRetrieveUnitCodes = List.of("U-1003");
        String token = login("zhangsan", "123456");
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_leak\",\"question\":\"渠道价格是多少\"}");

        List<String> contextCodes = fakeAiClient.lastGenerateContexts.stream()
                .map(GenerateContext::getUnitId).toList();
        assertFalse(contextCodes.contains("U-1003"), "越权召回内容必须被 Java 侧过滤，不得进入 Prompt");
        assertTrue(contextCodes.contains("U-1001"), "授权内容仍应正常进入 Prompt");

        String sources = parseSse(response.body()).get("sources").get(0);
        JsonNode sourcesNode = json.readTree(sources);
        assertTrue(sourcesNode.path("unauthorized").toString().contains("渠道价格政策"),
                "未授权来源应单独提示，但不含正文");
        assertFalse(sources.contains("七折"), "未授权来源卡片不得携带正文内容");
    }

    @Test
    void emptyContextsRefusesWithoutCallingGenerate() throws Exception {
        fakeAiClient.vectorStore.clear();
        fakeAiClient.lastGenerateContexts.clear();
        String token = login("zhangsan", "123456");
        // 刻意选一个「问题问得清楚、但完全超出知识库范围」的问题：
        // 若用「随便问问」这类短问题，会先被澄清追问拦下（见 ChatClarifyTest），
        // 就走不到这里的"真·无召回"兜底分支了。
        SseResponse response = postSse(token,
                "{\"sessionId\":\"s_none\",\"question\":\"量子计算机的散热方案是什么\"}");

        String body = response.body();
        assertTrue(body.contains("暂未检索到可依据的资料"), "空召回应拒答而非编造");
        assertTrue(fakeAiClient.lastGenerateContexts.isEmpty(), "contexts 为空时禁止调用 generate");
    }

    @Test
    void chatWritesAccessLog() throws Exception {
        long before = qaCount();
        String token = login("zhangsan", "123456");
        postSse(token, "{\"sessionId\":\"s_log\",\"question\":\"保修多久\"}");
        assertTrue(qaCount() > before, "应写入 qa_access_logs");
    }

    @Test
    void blankQuestionRejected() throws Exception {
        String token = login("zhangsan", "123456");
        JsonResponse response = postJson(token, "/api/ai/chat/stream",
                "{\"sessionId\":\"s_bad\",\"question\":\"\"}");
        assertEquals(400, response.statusCode());
    }

    @Test
    void oversizedQuestionRejected() throws Exception {
        String token = login("zhangsan", "123456");
        String oversized = "问".repeat(ChatRequest.MAX_QUESTION_CHARS + 1);
        JsonResponse response = postJson(token, "/api/ai/chat/stream",
                "{\"sessionId\":\"s_long\",\"question\":\"" + oversized + "\"}");
        assertEquals(400, response.statusCode(), "超过 2000 字符的问题应在边界被拒绝");

        String boundary = "问".repeat(ChatRequest.MAX_QUESTION_CHARS);
        // 恰好 2000 字符应被放行并进入 SSE 流（用宽松读取，避免 chunked 结束异常）
        SseResponse boundaryResponse = postSse(token,
                "{\"sessionId\":\"s_edge\",\"question\":\"" + boundary + "\"}");
        assertTrue(boundaryResponse.statusCode() < 500, "恰好 2000 字符不应触发 5xx");
    }

    @Test
    void chatRequiresAuthentication() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/ai/chat/stream"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{\"question\":\"x\"}"))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        assertEquals(401, response.statusCode());
    }

    private long qaCount() {
        Map<String, Object> metrics = qaAccessLogMapper.selectMetrics();
        Object value = metrics.get("qa_count");
        return value instanceof Number number ? number.longValue() : 0L;
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
        // 严格读取：若服务端 chunked 流未正常收尾（缺少 0\r\n\r\n），这里会抛 IOException 让测试失败。
        // 该断言用于防回归——曾因 Spring Security 对 ASYNC 派发鉴权导致连接被硬关闭。
        HttpResponse<String> raw = httpClient.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
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

    /**
     * JSON 响应（状态码 + 宽松读取的响应体）。
     *
     * @param statusCode HTTP 状态码
     * @param body       响应体（连接提前关闭时为已读部分）
     */
    private record JsonResponse(int statusCode, String body) {
    }

    private JsonResponse postJson(String token, String path, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        // 严格读取：参数校验失败的 400 响应是普通 JSON，不应出现分帧异常
        HttpResponse<String> raw = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        return new JsonResponse(raw.statusCode(), raw.body());
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
