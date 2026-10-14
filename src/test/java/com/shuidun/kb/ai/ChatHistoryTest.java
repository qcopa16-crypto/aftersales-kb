package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.support.FakeAiClient;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
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
 * 历史会话接口测试（真实 Tomcat + H2 + 内存 AI）。
 *
 * <p>历史会话不单独建表，而是从 {@code qa_access_logs} 按 {@code session_id} 聚合重建，
 * 因此这里必须走「真实提问 → 落库 → 查历史」的完整路径，而不是直接往库里塞数据。
 *
 * <p>核心断言：<b>只能读到自己的会话</b>——即使 sessionId 被猜中，也不能读到别人的问答。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class ChatHistoryTest {

    /** 本次用例独占的会话 id，避免与其他用例/种子数据互相干扰。 */
    private static final String SESSION_ID = "s_hist_main";

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
    void seedVectors() {
        fakeAiClient.vectorStore.clear();
        fakeAiClient.forcedRetrieveUnitCodes = List.of();
        // U-1001 给两个切片：落库的 authorized_unit_ids_json 会按切片重复记录同一个单元，
        // 用于验证回看时来源卡片会去重（与实时 SSE 的 sources 口径一致）。
        fakeAiClient.vectorStore.put("U-1001", List.of(
                chunk(0, "整机保修期为一年，人为损坏不在免费保修范围内。"),
                chunk(1, "人为损坏不在免费保修范围内，建议走付费维修流程。")));
        fakeAiClient.vectorStore.put("U-1005",
                List.of(chunk(0, "设备上电后 PWR 指示灯常亮绿色表示正常。")));
    }

    @Test
    void listsOwnSessionsAndReplaysRoundsWithSources() throws Exception {
        String token = login("zhangsan", "123456");
        ask(token, SESSION_ID, "保修期是多久？");
        ask(token, SESSION_ID, "人为损坏能保修吗？");

        // ---- 列表：能找到该会话，标题取首问、轮次为 2 ----
        JsonNode sessions = getJson(token, "/api/ai/chat/sessions").path("data");
        JsonNode target = null;
        for (JsonNode node : sessions) {
            if (SESSION_ID.equals(node.path("sessionId").asText())) {
                target = node;
            }
        }
        assertNotNull(target, "历史会话列表应包含刚提问的会话");
        assertEquals("保修期是多久？", target.path("title").asText(), "标题应取首问");
        assertEquals(2, target.path("messageCount").asLong(), "应有 2 轮问答");
        assertFalse(target.path("lastAskedAt").asText().isBlank(), "应有最近提问时间");

        // ---- 详情：按时间正序还原两轮，含来源卡片与 token ----
        JsonNode rounds = getJson(token, "/api/ai/chat/sessions/" + SESSION_ID).path("data").path("rounds");
        assertEquals(2, rounds.size(), "详情应还原 2 轮问答");
        JsonNode first = rounds.get(0);
        assertEquals("保修期是多久？", first.path("question").asText(), "轮次应按时间正序");
        assertTrue(first.path("tokens").asLong() > 0, "应带上 token 数");
        assertTrue(first.path("elapsedMs").asLong() >= 0, "应带上端到端耗时");
        assertFalse(first.path("faqHit").asBoolean(), "未命中 FAQ");

        String authorized = first.path("sources").path("authorized").toString();
        assertTrue(authorized.contains("整机保修政策"), "来源卡片应反查出知识标题：" + authorized);
        assertTrue(first.path("answer").asText().length() > 0, "应带回落库的答案");

        // 来源按知识单元去重：同一单元命中多个切片时只应出现一次（对齐实时 SSE 的 sources）
        JsonNode authorizedItems = first.path("sources").path("authorized");
        java.util.Set<String> uniqueIds = new java.util.HashSet<>();
        for (JsonNode item : authorizedItems) {
            uniqueIds.add(item.path("id").asText());
        }
        assertEquals(authorizedItems.size(), uniqueIds.size(),
                "来源卡片应按知识单元去重：" + authorizedItems);
    }

    @Test
    void otherUsersCannotReadForeignSession() throws Exception {
        String zhangsan = login("zhangsan", "123456");
        ask(zhangsan, SESSION_ID, "保修期是多久？");

        String lisi = login("lisi", "123456");

        // 猜中 sessionId 也读不到别人的会话：返回空 rounds 而非 403，避免会话 id 被探测
        JsonNode rounds = getJson(lisi, "/api/ai/chat/sessions/" + SESSION_ID).path("data").path("rounds");
        assertEquals(0, rounds.size(), "不得读到他人会话内容");

        // 列表里也不应出现他人会话
        JsonNode sessions = getJson(lisi, "/api/ai/chat/sessions").path("data");
        for (JsonNode node : sessions) {
            assertFalse(SESSION_ID.equals(node.path("sessionId").asText()),
                    "列表不得泄露他人会话");
        }
    }

    @Test
    void historyRequiresAuthentication() throws Exception {
        assertEquals(401, get("/api/ai/chat/sessions", null).statusCode());
        assertEquals(401, get("/api/ai/chat/sessions/" + SESSION_ID, null).statusCode());
    }

    @Test
    void oversizedSessionIdRejected() throws Exception {
        String token = login("zhangsan", "123456");
        String tooLong = "s".repeat(65);
        assertEquals(400, get("/api/ai/chat/sessions/" + tooLong, token).statusCode(),
                "超过 64 字符的会话 id 应被拒绝");
    }

    /* ---------------- 辅助 ---------------- */

    private void ask(String token, String sessionId, String question) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/ai/chat/stream"))
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"sessionId\":\"" + sessionId + "\",\"question\":\"" + question + "\"}",
                        StandardCharsets.UTF_8))
                .build();
        // 严格读取：SSE 流必须正常收尾，且日志在流结束前已落库
        HttpResponse<String> response = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertEquals(200, response.statusCode());
        assertTrue(response.body().contains("event:done"), "提问应正常结束：" + response.body());
    }

    private JsonNode getJson(String token, String path) throws Exception {
        HttpResponse<String> response = get(path, token);
        assertEquals(200, response.statusCode(), "GET " + path + " 应返回 200");
        return json.readTree(response.body());
    }

    private HttpResponse<String> get(String path, String token) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .header("Accept", "application/json")
                .GET();
        if (token != null) {
            builder.header("Authorization", "Bearer " + token);
        }
        return httpClient.send(builder.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
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

    private AiChunk chunk(int index, String content) {
        AiChunk chunk = new AiChunk();
        chunk.setChunkIndex(index);
        chunk.setContent(content);
        return chunk;
    }
}
