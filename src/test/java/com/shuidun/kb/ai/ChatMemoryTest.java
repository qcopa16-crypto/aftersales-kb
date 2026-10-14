package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.SessionTurn;
import com.shuidun.kb.faq.FaqCacheService;
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
import java.util.Optional;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import tools.jackson.databind.ObjectMapper;

/**
 * 多轮会话上下文记忆（Task B P1）接线测试。
 *
 * <p>覆盖：FAQ / 澄清 ask / 澄清 reference 各追加一轮；retrieve/generate 收到 acl+session；
 * ownerId 不透明且稳定；append 抛异常不破坏 SSE。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class ChatMemoryTest {

    /** 可开关的 FAQ 缓存替身（命中时直接直出）。 */
    static class StubFaqCacheService extends FaqCacheService {
        volatile String answer;

        StubFaqCacheService() {
            super(null, null);
        }

        @Override
        public Optional<String> findAnswer(String question) {
            return Optional.ofNullable(answer);
        }
    }

    @TestConfiguration
    static class StubConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }

        @Bean
        @Primary
        StubFaqCacheService stubFaqCacheService() {
            return new StubFaqCacheService();
        }
    }

    @Value("${local.server.port}")
    private int port;

    @Autowired
    private FakeAiClient fakeAiClient;

    @Autowired
    private StubFaqCacheService faqCache;

    private final HttpClient httpClient = HttpClient.newHttpClient();

    private final ObjectMapper json = tools.jackson.databind.json.JsonMapper.builder().build();

    @BeforeEach
    void reset() {
        faqCache.answer = null;
        fakeAiClient.vectorStore.clear();
        fakeAiClient.forcedRetrieveUnitCodes = List.of();
        fakeAiClient.lastGenerateContexts.clear();
        fakeAiClient.appendCalls.clear();
        fakeAiClient.failAppend = false;
        fakeAiClient.lastSessionId = null;
        fakeAiClient.lastOwnerId = null;
        fakeAiClient.lastTurnId = null;
        fakeAiClient.lastWriteHistory = false;
        // U-1001 售后部可见；U-1005 全局可见
        fakeAiClient.vectorStore.put("U-1001", List.of(chunk(0, "整机保修期为一年，人为损坏不在免费保修范围内。")));
        fakeAiClient.vectorStore.put("U-1005", List.of(chunk(0, "设备上电后 PWR 指示灯常亮绿色表示正常。")));
    }

    @Test
    void normalQaPassesSessionAndAclToRetrieveAndGenerate() throws Exception {
        String token = login();
        postSse(token, "{\"sessionId\":\"s_mem\",\"question\":\"保修多久\"}");

        assertEquals("s_mem", fakeAiClient.lastSessionId);
        assertNotNull(fakeAiClient.lastOwnerId, "应下发 ownerId");
        assertEquals(16, fakeAiClient.lastOwnerId.length(), "ownerId 应为 16 位不透明十六进制");
        assertNotNull(fakeAiClient.lastTurnId, "应下发 turnId");
        assertFalse(fakeAiClient.lastAclUnitCodes.isEmpty(), "generate 应收到 acl 单元编码");
        assertTrue(fakeAiClient.lastWriteHistory, "正常问答轮允许 AI 收尾写会话记忆");
        // 正常问答轮由 AI 自己在 generate 收尾写入，Java 不重复 append
        assertTrue(fakeAiClient.appendCalls.isEmpty(), "generate 轮不应由 Java 再 append");
    }

    @Test
    void ownerIdIsOpaqueAndDeterministic() throws Exception {
        String token = login();
        postSse(token, "{\"sessionId\":\"s_o1\",\"question\":\"保修多久\"}");
        String first = fakeAiClient.lastOwnerId;
        postSse(token, "{\"sessionId\":\"s_o2\",\"question\":\"保修多久\"}");
        assertEquals(first, fakeAiClient.lastOwnerId, "同一用户的 ownerId 必须稳定");
        assertTrue(first.matches("[0-9a-f]{16}"), "ownerId 应是不透明十六进制，不含 userId 明文");
    }

    @Test
    void faqHitAppendsFaqTurn() throws Exception {
        faqCache.answer = "FAQ 直出答案：整机保修一年。";
        String token = login();
        postSse(token, "{\"sessionId\":\"s_faq_mem\",\"question\":\"保修多久\"}");

        assertEquals(1, fakeAiClient.appendCalls.size(), "FAQ 轮应追加一条会话记忆");
        SessionTurn turn = fakeAiClient.appendCalls.get(0);
        assertEquals("faq", turn.getTurnType());
        assertEquals("s_faq_mem", turn.getSessionId());
        assertTrue(turn.isFaqHit());
        assertTrue(turn.getAuthorizedUnitIds().isEmpty());
        assertNotNull(turn.getTurnId());
        assertFalse(turn.getOwnerId().isBlank());
    }

    @Test
    void clarifyAskAppendsClarifyTurn() throws Exception {
        fakeAiClient.vectorStore.clear();
        String token = login();
        postSse(token, "{\"sessionId\":\"s_ask_mem\",\"question\":\"路由器有问题\"}");

        assertEquals(1, fakeAiClient.appendCalls.size());
        assertEquals("clarify_ask", fakeAiClient.appendCalls.get(0).getTurnType());
    }

    @Test
    void referenceTurnRecordsReferencedUnitCodes() throws Exception {
        fakeAiClient.vectorStore.clear();
        String token = login();
        postSse(token, "{\"sessionId\":\"s_ref_mem\",\"question\":\"H3C 路由器指示灯不亮\"}");

        assertEquals(1, fakeAiClient.appendCalls.size(), "参考轮应追加一条会话记忆");
        SessionTurn turn = fakeAiClient.appendCalls.get(0);
        assertEquals("clarify_reference", turn.getTurnType());
        assertFalse(turn.getReferencedUnitIds().isEmpty(), "参考候选编码应记入 referenced（参与 ACL 过滤）");
    }

    @Test
    void appendFailureDoesNotBreakSse() throws Exception {
        faqCache.answer = "FAQ 直出答案：整机保修一年。";
        fakeAiClient.failAppend = true; // 模拟 AI 侧 append 抛异常
        String token = login();
        SseResponse response = postSse(token, "{\"sessionId\":\"s_fail\",\"question\":\"保修多久\"}");

        assertEquals(200, response.statusCode());
        assertTrue(response.body().contains("FAQ 直出答案"), "append 失败不得破坏 SSE 主链路");
        assertTrue(response.body().contains("\"faqHit\":true"));
    }

    private String login() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/auth/login"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"username\":\"zhangsan\",\"password\":\"123456\"}", StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        return json.readTree(response.body()).path("data").path("accessToken").asText();
    }

    private SseResponse postSse(String token, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/ai/chat/stream"))
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

    private record SseResponse(int statusCode, String body) {
    }

    private AiChunk chunk(int index, String content) {
        AiChunk chunk = new AiChunk();
        chunk.setChunkIndex(index);
        chunk.setContent(content);
        return chunk;
    }
}
