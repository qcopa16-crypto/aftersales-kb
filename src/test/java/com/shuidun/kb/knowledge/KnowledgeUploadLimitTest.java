package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.support.FakeAiClient;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.test.context.TestPropertySource;
import tools.jackson.databind.ObjectMapper;

/**
 * 上传超限必须返回**可读的 413**，而不是被兜底成 500「系统错误」。
 *
 * <p>回归背景：Spring Boot 默认单文件上限 1MB，真实产品手册 PDF 超限后抛
 * {@code MaxUploadSizeExceededException}，前端只看到"系统错误"。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestPropertySource(properties = "spring.servlet.multipart.max-file-size=1KB")
class KnowledgeUploadLimitTest {

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

    private final HttpClient httpClient = HttpClient.newHttpClient();

    private final ObjectMapper json = tools.jackson.databind.json.JsonMapper.builder().build();

    @Test
    void oversizedUploadReturnsReadable413() throws Exception {
        String token = login();
        String boundary = "----kb" + UUID.randomUUID().toString().replace("-", "");
        byte[] big = new byte[4096];
        Arrays.fill(big, (byte) 'a');

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.UTF_8));
        out.write(("Content-Disposition: form-data; name=\"files\"; filename=\"big.md\"\r\n")
                .getBytes(StandardCharsets.UTF_8));
        out.write("Content-Type: text/markdown\r\n\r\n".getBytes(StandardCharsets.UTF_8));
        out.write(big);
        out.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/knowledge/import"))
                .header("Content-Type", "multipart/form-data; boundary=" + boundary)
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofByteArray(out.toByteArray()))
                .build();
        HttpResponse<String> resp = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        assertEquals(413, resp.statusCode(), resp.body());
        assertEquals(40002, json.readTree(resp.body()).path("code").asInt(), resp.body());
        assertTrue(resp.body().contains("上传文件过大"), resp.body());
    }

    private String login() throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + port + "/api/auth/login"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        "{\"username\":\"admin\",\"password\":\"123456\"}", StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
        return json.readTree(response.body()).path("data").path("accessToken").asText();
    }
}
