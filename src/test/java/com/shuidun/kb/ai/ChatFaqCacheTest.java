package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.faq.FaqCacheService;
import com.shuidun.kb.support.FakeAiClient;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * FAQ 缓存命中链路测试：命中后必须 SSE 直出且不触发检索/生成（kb-design-spec.md §5.2）。
 *
 * <p>用内存版 FaqCacheService 替换 Redis 实现，验证编排分支而非 Redis 本身。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class ChatFaqCacheTest {

    @TestConfiguration
    static class StubConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }

        @Bean
        @Primary
        FaqCacheService stubFaqCacheService() {
            return new FaqCacheService(null, null) {
                @Override
                public Optional<String> findAnswer(String question) {
                    return Optional.of("FAQ 直出答案：整机保修一年。");
                }
            };
        }
    }

    @Value("${local.server.port}")
    private int port;

    @Autowired
    private FakeAiClient fakeAiClient;

    private final HttpClient httpClient = HttpClient.newHttpClient();

    private final ObjectMapper json = JsonMapper.builder().build();

    @Test
    void faqHitStreamsDirectlyAndSkipsRetrieval() throws Exception {
        fakeAiClient.vectorStore.clear();
        fakeAiClient.lastGenerateContexts.clear();

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/ai/chat/stream"))
                .version(HttpClient.Version.HTTP_1_1)
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + login())
                .POST(HttpRequest.BodyPublishers.ofString("{\"sessionId\":\"s_faq\",\"question\":\"保修多久\"}"))
                .build();
        // 严格读取：chunked 流未正常收尾会抛 IOException（防回归）
        HttpResponse<String> raw = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        String body = raw.body();

        assertEquals(200, raw.statusCode());
        assertTrue(body.contains("FAQ 直出答案"), "FAQ 命中应直接输出缓存答案");
        assertTrue(body.contains("\"faqHit\":true"), "done 事件应标记 faqHit=true");
        assertTrue(fakeAiClient.lastGenerateContexts.isEmpty(), "FAQ 命中不得调用 generate");
    }

    private String login() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/auth/login"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{\"username\":\"zhangsan\",\"password\":\"123456\"}"))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        return json.readTree(response.body()).path("data").path("accessToken").asText();
    }
}
