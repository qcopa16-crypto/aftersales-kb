package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.AiChunk;
import com.shuidun.kb.ai.dto.DeleteResult;
import com.shuidun.kb.ai.dto.EmbedResult;
import com.shuidun.kb.ai.dto.GenerateContext;
import com.shuidun.kb.ai.dto.ParseResult;
import com.shuidun.kb.ai.dto.RetrieveResult;
import com.shuidun.kb.ai.dto.ReviewRequest;
import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.ai.dto.SessionTurn;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.config.AiProperties;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestClientResponseException;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import tools.jackson.databind.ObjectMapper;

/**
 * 基于 HTTP 的 AI 客户端实现（代码设计规范 §3.8）。
 *
 * <p>JSON 编解码统一使用 snake_case 的 aiObjectMapper；generate 走 SSE 流式读取（java.net.http）。
 */
@Slf4j
@Component
public class HttpAiClient implements AiClient {

    private static final String TOKEN_HEADER = "X-Internal-Token";

    private final AiProperties properties;
    private final ObjectMapper aiObjectMapper;
    private final RestClient parseClient;
    private final RestClient embedClient;
    private final RestClient retrieveClient;
    private final RestClient deleteClient;
    private final RestClient vlmClient;
    private final RestClient sessionClient;
    private final RestClient reviewClient;
    /**
     * generate 流式读取专用 HttpClient（单例复用）。
     *
     * <p>HttpClient 是线程安全且设计为复用的：每次调用都 newBuilder().build() 会重复创建
     * 连接池与选择器线程，属资源浪费。此处构建一次并复用，<b>强制 HTTP/1.1</b> 与 connectTimeout 语义不变。
     */
    private final HttpClient generateHttpClient;

    /**
     * 构造客户端，按用途建立不同读超时的 RestClient。
     *
     * @param properties     AI 配置
     * @param aiObjectMapper snake_case 映射器
     */
    public HttpAiClient(AiProperties properties, @Qualifier("aiObjectMapper") ObjectMapper aiObjectMapper) {
        this.properties = properties;
        this.aiObjectMapper = aiObjectMapper;
        this.parseClient = buildClient(properties.getParseTimeoutMs());
        this.embedClient = buildClient(properties.getEmbedTimeoutMs());
        this.retrieveClient = buildClient(properties.getRetrieveTimeoutMs());
        this.deleteClient = buildClient(properties.getDeleteTimeoutMs());
        this.vlmClient = buildClient(properties.getVlmTimeoutMs());
        // 会话记忆短超时 client：best-effort，绝不拖慢问答主链路
        this.sessionClient = buildClient(properties.getSessionTimeoutMs());
        // 质检判定独立超时：必须 > AI 侧 LLM+VLM 最坏串行预算（20s + 60s = 80s）+ 余量（F-22）
        this.reviewClient = buildClient(properties.getReviewTimeoutMs());
        this.generateHttpClient = HttpClient.newBuilder()
                // 必须强制 HTTP/1.1：JDK HttpClient 默认 HTTP/2，对 http:// 会先发 h2c upgrade，
                // 而 uvicorn 不支持，会回 "Unsupported upgrade request / Invalid HTTP request"。
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofMillis(properties.getConnectTimeoutMs()))
                .build();
    }

    @Override
    public ParseResult parse(byte[] content, String filename, String fileType, String unitId,
                             int chunkSize, boolean useVlm, String ingestId) {
        MultiValueMap<String, Object> form = new LinkedMultiValueMap<>();
        form.add("file", new ByteArrayResource(content) {
            @Override
            public String getFilename() {
                return filename;
            }
        });
        form.add("file_type", fileType);
        form.add("unit_id", unitId);
        form.add("chunk_size", String.valueOf(chunkSize));
        form.add("use_vlm", String.valueOf(useVlm));
        if (ingestId != null) {
            form.add("ingest_id", ingestId);
        }
        String json = call("parse", () -> parseClient.post()
                .uri("/internal/parse")
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(form)
                .retrieve()
                .body(String.class));
        return readValue(json, ParseResult.class, "parse");
    }

    @Override
    public ReviewResult review(ReviewRequest request) {
        String payload = writeValue(request, "review");
        String json = call("review", () -> reviewClient.post()
                .uri("/internal/review")
                .contentType(MediaType.APPLICATION_JSON)
                .body(payload)
                .retrieve()
                .body(String.class));
        return readValue(json, ReviewResult.class, "review");
    }

    @Override
    public EmbedResult embed(String unitId, List<AiChunk> chunks, String contentHash,
                             String chunksHash, String ingestId) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("unit_id", unitId);
        body.put("chunks", chunks);
        body.put("content_hash", contentHash);
        body.put("chunks_hash", chunksHash);
        body.put("ingest_id", ingestId);
        String payload = writeValue(body, "embed");
        String json = call("embed", () -> embedClient.post()
                .uri("/internal/embed")
                .contentType(MediaType.APPLICATION_JSON)
                .body(payload)
                .retrieve()
                .body(String.class));
        return readValue(json, EmbedResult.class, "embed");
    }

    @Override
    public RetrieveResult retrieve(String question, int topK, List<String> aclUnitIds, List<String> denyUnitIds,
                                   String sessionId, String ownerId, boolean enableSessionRewrite) {
        Map<String, Object> aclFilter = new LinkedHashMap<>();
        aclFilter.put("unit_ids", aclUnitIds);
        aclFilter.put("deny_unit_ids", denyUnitIds == null ? List.of() : denyUnitIds);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("question", question);
        body.put("top_k", topK);
        body.put("acl_filter", aclFilter);
        // 会话记忆（检索期指代消解）；AI 侧默认关，关闭时忽略这些字段
        body.put("session_id", sessionId == null ? "" : sessionId);
        body.put("owner_id", ownerId == null ? "" : ownerId);
        body.put("enable_session_rewrite", enableSessionRewrite);
        String payload = writeValue(body, "retrieve");
        String json = call("retrieve", () -> retrieveClient.post()
                .uri("/internal/retrieve")
                .contentType(MediaType.APPLICATION_JSON)
                .body(payload)
                .retrieve()
                .body(String.class));
        return readValue(json, RetrieveResult.class, "retrieve");
    }

    @Override
    public void generate(String question, List<GenerateContext> contexts, String sessionId, AiGenerateMode mode,
                         String ownerId, String turnId, List<String> aclUnitIds, Long chosenUnitId,
                         boolean writeHistory, GenerateStreamHandler handler) {
        // F-23 终态契约：SSE error（或 HTTP>=400）经 handler.onError 回传后正常返回，
        // 调用方（编排器）据此只发 error、不记成功；仅收到 done 事件才视为成功。
        // done 与 error 互斥（parseSse 保证），EOF 截断时回调里没有任何终态。
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("question", question);
        body.put("contexts", contexts);
        body.put("session_id", sessionId);
        // KB → "kb"（严格依据资料）；GENERAL → "chat"（通用知识兜底，AI 侧权威闸门默认关）
        body.put("mode", mode == AiGenerateMode.GENERAL ? "chat" : "kb");
        // 会话记忆（读回灌 + 收尾写轮）；AI 侧默认关，关闭时忽略这些字段
        body.put("owner_id", ownerId == null ? "" : ownerId);
        body.put("turn_id", turnId == null ? "" : turnId);
        body.put("acl_unit_ids", aclUnitIds == null ? List.of() : aclUnitIds);
        body.put("chosen_unit_id", chosenUnitId);
        body.put("write_history", writeHistory);
        String payload = writeValue(body, "generate");

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(properties.getBaseUrl() + "/internal/generate"))
                .timeout(Duration.ofMillis(properties.getGenerateTimeoutMs()))
                .header("Content-Type", "application/json")
                .header(TOKEN_HEADER, properties.getInternalToken())
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
                .build();
        try {
            HttpResponse<InputStream> response =
                    generateHttpClient.send(request, HttpResponse.BodyHandlers.ofInputStream());
            if (response.statusCode() >= 400) {
                String errorBody = new String(response.body().readAllBytes(), StandardCharsets.UTF_8);
                handler.onError(extractErrorCode(errorBody), "生成失败: HTTP " + response.statusCode());
                return;
            }
            parseSse(response.body(), handler);
        } catch (IOException ex) {
            throw new AiException(ErrorCode.AI_UNAVAILABLE, null, "generate 调用失败: " + ex.getMessage());
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            throw new AiException(ErrorCode.AI_UNAVAILABLE, null, "generate 调用被中断");
        }
    }

    @Override
    public void appendSessionTurn(SessionTurn turn) {
        try {
            String payload = writeValue(turn, "appendSessionTurn");
            sessionClient.post()
                    .uri("/internal/session/append")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(payload)
                    .retrieve()
                    .toBodilessEntity();
        } catch (RuntimeException ex) {
            // 会话记忆是 best-effort：任何失败都吞掉并记日志，绝不破坏问答主链路
            log.warn("append session turn failed (ignored): {}", ex.getMessage());
        }
    }

    @Override
    public int deleteVectors(String unitId) {
        String json = call("deleteVectors", () -> deleteClient.delete()
                .uri("/internal/units/{unitId}", unitId)
                .retrieve()
                .body(String.class));
        DeleteResult result = readValue(json, DeleteResult.class, "deleteVectors");
        if (result.getDeleted() == null || !result.getDeleted()) {
            // 删除失败不得假成功（代码设计规范 §7.3）
            throw new AiException(ErrorCode.AI_PROCESS_FAILED, "DELETE_FAILED", "向量删除未确认成功");
        }
        return result.getDeletedCount() == null ? 0 : result.getDeletedCount();
    }

    @Override
    public String vlmDescribe(byte[] content, String filename, String contentType) {
        MultiValueMap<String, Object> form = new LinkedMultiValueMap<>();
        form.add("file", new ByteArrayResource(content) {
            @Override
            public String getFilename() {
                return filename;
            }
        });
        String json = call("vlmDescribe", () -> vlmClient.post()
                .uri("/internal/vlm/describe")
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(form)
                .retrieve()
                .body(String.class));
        Map<String, Object> map = readMap(json, "vlmDescribe");
        Object caption = map.get("caption");
        return caption == null ? "" : String.valueOf(caption);
    }

    private RestClient buildClient(int readTimeoutMs) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(properties.getConnectTimeoutMs());
        factory.setReadTimeout(readTimeoutMs);
        return RestClient.builder()
                .baseUrl(properties.getBaseUrl())
                .defaultHeader(TOKEN_HEADER, properties.getInternalToken())
                .requestFactory(factory)
                .build();
    }

    private String call(String operation, java.util.function.Supplier<String> action) {
        try {
            return action.get();
        } catch (RestClientResponseException ex) {
            String aiCode = extractErrorCode(ex.getResponseBodyAsString());
            log.warn("ai call failed op={} status={} aiCode={}", operation, ex.getStatusCode(), aiCode);
            throw new AiException(ErrorCode.AI_PROCESS_FAILED, aiCode,
                    operation + " 失败: HTTP " + ex.getStatusCode().value());
        } catch (RestClientException ex) {
            log.warn("ai unavailable op={} message={}", operation, ex.getMessage());
            throw new AiException(ErrorCode.AI_UNAVAILABLE, null, operation + " 调用失败: " + ex.getMessage());
        }
    }

    /**
     * 解析 SSE 流并派发事件（F-23：终态互斥）。
     *
     * <p>{@code done} / {@code error} 是互斥终态：一旦派发其一，后续事件一律忽略
     * （包括畸形流里的第二个终态），保证编排器与客户端看到的终态唯一。
     * 流在无终态事件时结束（EOF 截断）同样可被编排器识别——回调里既无 done 也无 error。
     *
     * @param inputStream 响应体
     * @param handler     事件回调
     * @throws IOException 读取失败
     */
    private void parseSse(InputStream inputStream, GenerateStreamHandler handler) throws IOException {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(inputStream, StandardCharsets.UTF_8))) {
            String event = null;
            StringBuilder data = new StringBuilder();
            boolean terminal = false;
            String line;
            while (!terminal && (line = reader.readLine()) != null) {
                if (line.isEmpty()) {
                    terminal = dispatch(event, data.toString(), handler);
                    event = null;
                    data.setLength(0);
                    continue;
                }
                if (line.startsWith("event:")) {
                    event = line.substring("event:".length()).trim();
                } else if (line.startsWith("data:")) {
                    if (data.length() > 0) {
                        data.append('\n');
                    }
                    data.append(line.substring("data:".length()).trim());
                }
            }
            if (!terminal && (event != null || data.length() > 0)) {
                dispatch(event, data.toString(), handler);
            }
        }
    }

    /**
     * 派发单个 SSE 事件。
     *
     * @param event   事件名
     * @param data    JSON 数据
     * @param handler 事件回调
     * @return true 表示已派发终态事件（done / error），调用方应停止读取
     */
    private boolean dispatch(String event, String data, GenerateStreamHandler handler) {
        if (event == null || data.isEmpty()) {
            return false;
        }
        Map<String, Object> payload = readMap(data, "sse:" + event);
        switch (event) {
            case "delta" -> handler.onDelta(stringValue(payload.get("text")));
            case "done" -> {
                handler.onDone(intValue(payload.get("prompt_tokens")),
                        intValue(payload.get("completion_tokens")),
                        longValue(payload.get("elapsed_ms")),
                        booleanValue(payload.get("faqHit") != null ? payload.get("faqHit") : payload.get("faq_hit")));
                return true;
            }
            case "error" -> {
                handler.onError(stringValue(payload.get("code")), stringValue(payload.get("message")));
                return true;
            }
            default -> log.debug("ignore sse event={}", event);
        }
        return false;
    }

    private String writeValue(Object value, String operation) {
        try {
            return aiObjectMapper.writeValueAsString(value);
        } catch (RuntimeException ex) {
            throw new AiException(ErrorCode.AI_PROCESS_FAILED, null, operation + " 请求序列化失败");
        }
    }

    private <T> T readValue(String json, Class<T> type, String operation) {
        try {
            return aiObjectMapper.readValue(json, type);
        } catch (RuntimeException ex) {
            log.warn("ai response parse failed op={}", operation);
            throw new AiException(ErrorCode.AI_PROCESS_FAILED, null, operation + " 响应解析失败");
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readMap(String json, String operation) {
        try {
            return aiObjectMapper.readValue(json, Map.class);
        } catch (RuntimeException ex) {
            log.warn("ai response parse failed op={}", operation);
            throw new AiException(ErrorCode.AI_PROCESS_FAILED, null, operation + " 响应解析失败");
        }
    }

    private String extractErrorCode(String body) {
        if (body == null || body.isBlank()) {
            return null;
        }
        try {
            Map<String, Object> map = aiObjectMapper.readValue(body, Map.class);
            Object code = map.get("error_code");
            if (code == null && map.get("detail") instanceof Map<?, ?> detail) {
                code = detail.get("error_code");
            }
            return code == null ? null : String.valueOf(code);
        } catch (RuntimeException ex) {
            return null;
        }
    }

    private String stringValue(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    private int intValue(Object value) {
        return value instanceof Number number ? number.intValue() : 0;
    }

    private long longValue(Object value) {
        return value instanceof Number number ? number.longValue() : 0L;
    }

    private boolean booleanValue(Object value) {
        if (value instanceof Boolean bool) {
            return bool;
        }
        return value != null && Boolean.parseBoolean(String.valueOf(value));
    }
}
