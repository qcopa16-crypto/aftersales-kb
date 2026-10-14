package com.shuidun.kb.qa;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.shuidun.kb.support.FakeAiClient;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * QA 独立边界/对抗测试（gstack QA，2026-10-10）。
 *
 * <p>覆盖任务点名的五类高风险边界：分页 size 超上限、越权访问他人数据、批量删除混合存在/不存在 id、
 * 非法枚举值、超大 question。使用内存版 FakeAiClient，其余为真实 Spring/MyBatis/H2 链路。
 *
 * <p>注意：本类默认 Worker 关闭（application.properties），导入不会自动入库，便于确定性断言。
 */
@SpringBootTest
@AutoConfigureMockMvc
class QaBoundaryTest {

    @TestConfiguration
    static class FakeAiConfig {

        @Bean
        @Primary
        FakeAiClient fakeAiClient() {
            return new FakeAiClient();
        }
    }

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    /**
     * 分页 size 超上限必须被夹取到 100，小于 1 夹取到 1；page < 1 归一到 1。
     */
    @Test
    void paginationSizeIsClampedToBoundaries() throws Exception {
        String token = login("admin", "123456");

        mockMvc.perform(get("/api/knowledge/units?page=1&size=9999").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.size").value(100))
                .andExpect(jsonPath("$.data.page").value(1));

        mockMvc.perform(get("/api/knowledge/units?page=0&size=0").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.size").value(1))
                .andExpect(jsonPath("$.data.page").value(1));

        mockMvc.perform(get("/api/knowledge/units?page=-5&size=-3").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.size").value(1))
                .andExpect(jsonPath("$.data.page").value(1));
    }

    /**
     * 【QA-001 回归】极大 page 触发 offset 溢出必须被显式拒绝为 400，不得 500。
     *
     * <p>最小复现：GET /api/knowledge/units?page=2147483647。
     * 修复：offset 以 long 计算，{@code (long)(page-1)*size > Integer.MAX_VALUE} 时抛 PARAM_INVALID。
     * 独立回归锚点（security-officer 的 PaginationOverflowGuardTest 为补充覆盖）。
     */
    @Test
    void extremePageNumberRejectedWith400() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units?page=2147483647")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    /**
     * 批量删除混合「存在 / 不存在 / 重复」id：必须逐项返回结果，不得整体假成功。
     */
    @Test
    void batchDeleteMixedAndDuplicateIdsReturnsPerItemResults() throws Exception {
        String token = login("admin", "123456");
        long unitId = importOneFile(token, "混合删除" + System.nanoTime() + ".md", "sop");

        MvcResult result = mockMvc.perform(delete("/api/knowledge/units")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"ids\":[" + unitId + ",999999," + unitId + "]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.length()").value(3))
                .andReturn();

        JsonNode items = objectMapper.readTree(result.getResponse().getContentAsString()).path("data");
        assertTrue(items.get(0).path("success").asBoolean(), "存在的 id 应删除成功");
        assertEquals("UNIT_NOT_FOUND", items.get(1).path("errorCode").asText(), "不存在的 id 应逐项失败");
        assertTrue(!items.get(2).path("success").asBoolean(), "重复 id 第二次应因已删除而失败（逐项结果）");

        // 删除后详情不可再取
        mockMvc.perform(get("/api/knowledge/units/" + unitId).header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest());
    }

    /**
     * 空 ids 应被拒绝（参数非法），不得返回 200 假成功。
     */
    @Test
    void batchDeleteEmptyIdsRejected() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(delete("/api/knowledge/units")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"ids\":[]}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    /**
     * 非法枚举值必须在各入口被拒绝（更新 status / 更新 category / 导入 category / 列表 status）。
     */
    @Test
    void illegalEnumValuesRejectedAcrossEndpoints() throws Exception {
        String token = login("admin", "123456");

        // 更新 status=deleting（更新仅允许 active/disabled）
        mockMvc.perform(put("/api/knowledge/units/1")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"deleting\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 更新非法 category
        mockMvc.perform(put("/api/knowledge/units/1")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"category\":\"bogus\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 导入非法 category
        MockMultipartFile file = new MockMultipartFile("files", "x.md", "text/markdown",
                "# x".getBytes(StandardCharsets.UTF_8));
        mockMvc.perform(multipart("/api/knowledge/import").file(file)
                        .param("category", "bogus")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 列表非法 status
        mockMvc.perform(get("/api/knowledge/units?status=bogus").header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    /**
     * 对抗验证：售后账号（zhangsan，部门=售后部）读取财务私有单元详情必须被四维数据权限拦截。
     *
     * <p>U-1004「收费标准与费用制度」为 finance 私有（role=finance），售后账号无数据权限；
     * 期望 body code=40001 且 HTTP 200（不泄露正文）。安全审计 F-001 修复后的回归锚点。
     */
    @Test
    void afterSalesAccountCannotReadFinancePrivateUnitDetail() throws Exception {
        String token = login("zhangsan", "123456");
        MvcResult result = mockMvc.perform(get("/api/knowledge/units/4")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(40001))
                .andReturn();
        String body = result.getResponse().getContentAsString();
        assertTrue(!body.contains("上门服务费"), "【QA 断言】未授权详情不得返回 finance 私有正文");
    }

    /**
     * 对抗验证：售后账号的分页列表不得包含财务/销售私有单元（四维数据权限作用于列表）。
     */
    @Test
    void afterSalesAccountListExcludesPrivateUnits() throws Exception {
        String token = login("zhangsan", "123456");
        MvcResult result = mockMvc.perform(get("/api/knowledge/units?page=1&size=100")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andReturn();
        String body = result.getResponse().getContentAsString();
        JsonNode records = objectMapper.readTree(body).path("data").path("records");
        java.util.List<Long> ids = new java.util.ArrayList<>();
        for (JsonNode node : records) {
            ids.add(node.path("id").asLong());
        }
        assertTrue(ids.contains(1L) && ids.contains(5L), "应能看到售后部与全局单元");
        assertTrue(!ids.contains(3L), "不得看到销售私有单元 U-1003");
        assertTrue(!ids.contains(4L), "不得看到财务私有单元 U-1004");
    }

    /**
     * 【QA-005 回归】question 长度边界：空串与超上限（>2000）必须 400，正常长度放行。
     *
     * <p>修复（安全审计 F-013）：ChatRequest.question 增加 {@code @Size(max=2000)}。
     */
    @Test
    void oversizedQuestionRejectedWith400() throws Exception {
        String token = login("zhangsan", "123456");

        // 空问题必须 400（@NotBlank）
        mockMvc.perform(post("/api/ai/chat/stream")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"sessionId\":\"s_qa\",\"question\":\"\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 超上限（2001 字符）必须 400（@Size）
        String tooLong = "测".repeat(2001);
        mockMvc.perform(post("/api/ai/chat/stream")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"sessionId\":\"s_qa\",\"question\":\"" + tooLong + "\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));

        // 正常长度问题：SSE 正常建立（HTTP 200）
        mockMvc.perform(post("/api/ai/chat/stream")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"sessionId\":\"s_qa\",\"question\":\"保修多久\"}"))
                .andExpect(status().isOk());
    }

    /**
     * check-permissions 边界：空候选集合返回空结果；不存在的 userId 返回 400。
     */
    @Test
    void checkPermissionsBoundaries() throws Exception {
        String token = login("admin", "123456");

        mockMvc.perform(post("/api/knowledge/check-permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":2,\"unitIds\":[]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.authorizedUnitIds.length()").value(0))
                .andExpect(jsonPath("$.data.unauthorizedUnitIds.length()").value(0));

        mockMvc.perform(post("/api/knowledge/check-permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":999999,\"unitIds\":[1]}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    private long importOneFile(String token, String filename, String category) throws Exception {
        MockMultipartFile file = new MockMultipartFile("files", filename, "text/markdown",
                ("# " + filename + "\n\n正文内容").getBytes(StandardCharsets.UTF_8));
        MvcResult result = mockMvc.perform(multipart("/api/knowledge/import").file(file)
                        .param("category", category)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.acceptedCount").value(1))
                .andReturn();
        JsonNode data = objectMapper.readTree(result.getResponse().getContentAsString()).path("data");
        return data.path("items").get(0).path("unitId").asLong();
    }

    private String login(String username, String password) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("accessToken").asText();
    }
}
