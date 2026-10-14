package com.shuidun.kb.ai;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.shuidun.kb.ai.dto.ParseQuality;
import com.shuidun.kb.ai.dto.ReviewRequest;
import com.shuidun.kb.ai.dto.ReviewResult;
import com.shuidun.kb.config.AiClientConfig;
import java.util.List;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

/**
 * 质检 DTO 与 AI 冻结契约的序列化/反序列化测试（snake_case 字段名）。
 *
 * <p>守护 {@code POST /internal/review} 的字段命名，避免真实 E2E 因契约字段错位而 422。
 */
class ReviewDtoSerializationTest {

    private final ObjectMapper mapper = new AiClientConfig().aiObjectMapper();

    @Test
    void reviewRequestUsesSnakeCaseFields() {
        ReviewRequest request = new ReviewRequest();
        request.setUnitId("U-1001");
        request.setCategory("product");
        request.setMarkdown("# 标题");
        request.setContentHash("hash");
        request.setIngestId("ig-1");
        request.setEnableLlm(true);
        request.setEnableVlm(false);
        ReviewRequest.Chunk chunk = new ReviewRequest.Chunk();
        chunk.setChunkIndex(0);
        chunk.setContent("正文");
        chunk.setHeadingPath(List.of("安装"));
        request.setChunks(List.of(chunk));
        ReviewRequest.Image image = new ReviewRequest.Image();
        image.setRelativePath("images/x.jpg");
        request.setImages(List.of(image));
        ParseQuality quality = new ParseQuality();
        quality.setReviewReasons(List.of("image_count_high"));
        request.setQuality(quality);

        String json = mapper.writeValueAsString(request);

        assertTrue(json.contains("\"unit_id\":\"U-1001\""), json);
        assertTrue(json.contains("\"content_hash\":\"hash\""), json);
        assertTrue(json.contains("\"ingest_id\":\"ig-1\""), json);
        assertTrue(json.contains("\"enable_llm\":true"), json);
        assertTrue(json.contains("\"enable_vlm\":false"), json);
        assertTrue(json.contains("\"chunk_index\":0"), json);
        assertTrue(json.contains("\"heading_path\""), json);
        assertTrue(json.contains("\"relative_path\":\"images/x.jpg\""), json);
        assertTrue(json.contains("\"review_reasons\""), json);
    }

    @Test
    void reviewResultParsesSnakeCaseFields() {
        String json = """
                {"verdict":"pass","confidence":0.97,"reasons":["rules:clean","vlm:pass"],
                 "tiers":{"rules":"pass","llm":"skipped","vlm":"pass"},
                 "coverage":{"char_coverage":1.0},
                 "vlm_findings":[{"relative_path":"images/a.jpg","usable":true,"reason":"清晰"}],
                 "model":"glm-4.6v","stage_timings":{"llm_ms":0.0,"vlm_ms":22750.0},
                 "ingest_id":"ig-1"}
                """;

        ReviewResult result = mapper.readValue(json, ReviewResult.class);

        assertEquals("pass", result.getVerdict());
        assertEquals(0.97, result.getConfidence(), 0.0001);
        assertEquals("pass", result.getTiers().getVlm());
        assertEquals("skipped", result.getTiers().getLlm());
        assertEquals(1, result.getVlmFindings().size());
        assertEquals("images/a.jpg", result.getVlmFindings().get(0).getRelativePath());
        assertTrue(result.getVlmFindings().get(0).getUsable());
        assertEquals(22750.0, result.getStageTimings().getVlmMs(), 0.0001);
        assertEquals("ig-1", result.getIngestId());
    }
}
