package com.shuidun.kb.knowledge;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.knowledge.entity.KnowledgeChunk;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeChunkMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * F-01 回归：批次列表/详情必须按当前用户数据范围过滤。
 *
 * <p>场景：admin（知识管理员）创建一个含切片正文与失败错误正文的批次；zhangsan
 * 仅持有 {@code knowledge:list} 与问答权限，对该批次既不可见详情也不可读正文；
 * 由 zhangsan 自己创建的批次可见，但不返回切片正文/错误正文/质检细节。
 */
@SpringBootTest
@AutoConfigureMockMvc
class KnowledgeImportDataPermissionTest {

    /** 他人（admin）创建的批次。 */
    private String foreignBatchId;

    /** 本人（zhangsan）创建的批次。 */
    private String ownBatchId;

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private KnowledgeUnitMapper unitMapper;

    @Autowired
    private KnowledgeChunkMapper chunkMapper;

    @Autowired
    private IngestJobService ingestJobService;

    @BeforeEach
    void seedBatches() {
        foreignBatchId = "B-FOREIGN-" + System.nanoTime();
        ownBatchId = "B-OWN-" + System.nanoTime();

        // admin 创建的批次：带切片正文 + 失败错误正文 + 质检审计字段应仅管理员可见
        KnowledgeUnit foreign = new KnowledgeUnit();
        foreign.setUnitCode("U-FGN-" + System.nanoTime());
        foreign.setTitle("他人批次-保修政策");
        foreign.setCategory("sop");
        foreign.setUseVlm(0);
        foreign.setStatus("active");
        foreign.setIngestStatus("failed");
        foreign.setBatchId(foreignBatchId);
        foreign.setCreatorId(1L);
        unitMapper.insert(foreign);
        chunkMapper.insertBatch(List.of(chunk(foreign.getId(), "机密切片正文-不得越权读取")));
        IngestJob job = ingestJobService.enqueue(foreignBatchId, foreign.getId(), 1L);
        ingestJobService.markFailed(job, "PARSE_FAILED", "解析失败：详细内部原因");

        // zhangsan 自己创建的批次：可见，但正文与错误细节需脱敏
        KnowledgeUnit own = new KnowledgeUnit();
        own.setUnitCode("U-OWN-" + System.nanoTime());
        own.setTitle("本人批次-操作手册");
        own.setCategory("sop");
        own.setUseVlm(0);
        own.setStatus("active");
        own.setIngestStatus("failed");
        own.setBatchId(ownBatchId);
        own.setCreatorId(2L);
        unitMapper.insert(own);
        chunkMapper.insertBatch(List.of(chunk(own.getId(), "本人切片正文")));
        IngestJob ownJob = ingestJobService.enqueue(ownBatchId, own.getId(), 2L);
        ingestJobService.markFailed(ownJob, "PARSE_FAILED", "解析失败：详细内部原因");
    }

    @Test
    void foreignBatchInvisibleToNonAdmin() throws Exception {
        String token = login("zhangsan", "123456");

        // 详情：越权批次返回空 files（与不存在不可区分）
        mockMvc.perform(get("/api/knowledge/import/batches/" + foreignBatchId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.files").isEmpty());

        // 列表：不得出现他人批次
        JsonNode page = listBatches(token);
        assertFalse(page.toString().contains(foreignBatchId),
                "普通用户批次列表不得包含他人批次: " + page);
    }

    @Test
    void ownBatchVisibleButMaskedForNonAdmin() throws Exception {
        String token = login("zhangsan", "123456");

        JsonNode files = batchFiles(token, ownBatchId);
        assertFalse(files.isEmpty(), "本人创建的批次应可见");
        JsonNode file = files.get(0);
        assertTrue(file.path("title").asText().contains("本人批次"), "本人批次标题可见");
        // 非知识管理员：不返回切片正文、错误正文与质检审查细节
        assertTrue(file.path("samples").isEmpty(), "非知识管理员不得读取切片正文: " + file);
        assertFalse(file.hasNonNull("errorMessage"), "非知识管理员不得读取错误正文: " + file);
        assertFalse(file.hasNonNull("reviewVerdict"), "非知识管理员不得读取质检结论: " + file);
        assertFalse(file.hasNonNull("aiVerdict"), "非知识管理员不得读取 AI 预审结论: " + file);
        assertFalse(file.hasNonNull("aiModel"), "非知识管理员不得读取 AI 模型: " + file);

        // 列表：本人批次出现
        JsonNode page = listBatches(token);
        assertTrue(page.toString().contains(ownBatchId), "本人批次应出现在列表: " + page);
    }

    @Test
    void knowledgeAdminSeesForeignBatchWithDetails() throws Exception {
        String token = login("admin", "123456");

        JsonNode files = batchFiles(token, foreignBatchId);
        assertFalse(files.isEmpty(), "知识管理员应能看到全部批次");
        JsonNode file = files.get(0);
        assertTrue(file.path("title").asText().contains("他人批次"), "管理员可见标题");
        assertTrue(file.path("samples").toString().contains("机密切片正文"),
                "管理员应能读取切片正文用于质检: " + file);
        assertTrue(file.path("errorMessage").asText().contains("解析失败"),
                "管理员应能读取错误正文: " + file);

        // 列表：两个批次都能看到
        JsonNode page = listBatches(token);
        assertTrue(page.toString().contains(foreignBatchId), "管理员应看到他人批次: " + page);
        assertTrue(page.toString().contains(ownBatchId), "管理员应看到全部批次: " + page);
    }

    @Test
    void batchesRequireAuthentication() throws Exception {
        mockMvc.perform(get("/api/knowledge/import/batches"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(get("/api/knowledge/import/batches/" + foreignBatchId))
                .andExpect(status().isUnauthorized());
    }

    /* ---------------- 辅助 ---------------- */

    private JsonNode listBatches(String token) throws Exception {
        MvcResult result = mockMvc.perform(get("/api/knowledge/import/batches?page=1&size=100")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("records");
    }

    private JsonNode batchFiles(String token, String batchId) throws Exception {
        MvcResult result = mockMvc.perform(get("/api/knowledge/import/batches/" + batchId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("files");
    }

    private KnowledgeChunk chunk(Long unitId, String content) {
        KnowledgeChunk chunk = new KnowledgeChunk();
        chunk.setUnitId(unitId);
        chunk.setChunkIndex(0);
        chunk.setContent(content);
        return chunk;
    }

    private String login(String username, String password) throws Exception {
        MvcResult result = mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("accessToken").asText();
    }
}
