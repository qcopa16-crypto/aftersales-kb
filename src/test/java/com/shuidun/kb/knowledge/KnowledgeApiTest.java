package com.shuidun.kb.knowledge;

import static org.hamcrest.Matchers.greaterThanOrEqualTo;
import static org.hamcrest.Matchers.hasItem;
import static org.hamcrest.Matchers.not;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.nio.charset.StandardCharsets;
import java.util.List;
import org.junit.jupiter.api.Assertions;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * P2 知识与四维数据权限真实链路测试（H2 + 种子数据；Worker 关闭，AI 服务不可达以验证 fail-closed）。
 */
@SpringBootTest
@AutoConfigureMockMvc
class KnowledgeApiTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void listUnitsUsesPagingContract() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units?page=1&size=20").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.total").value(greaterThanOrEqualTo(5)))
                .andExpect(jsonPath("$.data.page").value(1))
                .andExpect(jsonPath("$.data.size").value(20))
                .andExpect(jsonPath("$.data.records").isArray());
    }

    @Test
    void listUnitsRejectsInvalidStatus() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units?status=bogus").header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void detailReturnsPermissionSummary() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units/1").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.unitCode").value("U-1001"))
                .andExpect(jsonPath("$.data.permissions").isArray())
                .andExpect(jsonPath("$.data.permissions[0].targetType").value("department"))
                .andExpect(jsonPath("$.data.permissions[0].label").value("部门:售后部"));
    }

    @Test
    void checkPermissionsRespectsFourDimensions() throws Exception {
        String token = login("admin", "123456");
        // zhangsan: 部门=售后部(2)、角色=after_sales(3) → 命中 unit1/unit2(部门) 与 unit5(global)
        MvcResult zhangsan = mockMvc.perform(post("/api/knowledge/check-permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":2,\"unitIds\":[1,2,3,4,5]}"))
                .andExpect(status().isOk())
                .andReturn();
        JsonNode zsData = objectMapper.readTree(zhangsan.getResponse().getContentAsString()).path("data");
        List<Long> zsAuthorized = toLongList(zsData.path("authorizedUnitIds"));
        List<Long> zsUnauthorized = toLongList(zsData.path("unauthorizedUnitIds"));
        Assertions.assertEquals(List.of(1L, 2L, 5L), zsAuthorized, "售后用户应命中部门与全局");
        Assertions.assertEquals(List.of(3L, 4L), zsUnauthorized, "销售/财务私有单元不得授权");

        // lisi: 部门=销售部(3)、角色=sales(4) → 命中 unit3(角色) 与 unit5(global)
        MvcResult lisi = mockMvc.perform(post("/api/knowledge/check-permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":3,\"unitIds\":[1,2,3,4,5]}"))
                .andExpect(status().isOk())
                .andReturn();
        JsonNode lsData = objectMapper.readTree(lisi.getResponse().getContentAsString()).path("data");
        Assertions.assertEquals(List.of(3L, 5L), toLongList(lsData.path("authorizedUnitIds")));
    }

    @Test
    void nonPrivilegedUserForbiddenOnImport() throws Exception {
        String token = login("zhangsan", "123456");
        MockMultipartFile file = new MockMultipartFile("files", "x.md", "text/markdown",
                "# x".getBytes(StandardCharsets.UTF_8));
        mockMvc.perform(multipart("/api/knowledge/import").file(file)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value(403));
    }

    @Test
    void configurePermissionsThenCheckTakesEffect() throws Exception {
        String token = login("admin", "123456");
        long unitId = importOneFile(token, "权限测试" + System.nanoTime() + ".md", "policy");

        // 权限在 embed 成功后写入；此处显式配置为「部门=售后部」
        mockMvc.perform(post("/api/knowledge/units/" + unitId + "/permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"mode\":\"replace\",\"items\":[{\"targetType\":\"department\",\"targetId\":2}]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        Assertions.assertTrue(checkAuthorized(token, 2L, unitId).contains(unitId), "售后用户应可访问");
        Assertions.assertFalse(checkAuthorized(token, 3L, unitId).contains(unitId), "销售用户不应可访问");

        // 改为全局公开
        mockMvc.perform(post("/api/knowledge/units/" + unitId + "/permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"mode\":\"replace\",\"items\":[{\"targetType\":\"global\",\"targetId\":0}]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        Assertions.assertTrue(checkAuthorized(token, 3L, unitId).contains(unitId), "改全局后销售用户应可访问");

        // 非法维度应被拒绝
        mockMvc.perform(post("/api/knowledge/units/" + unitId + "/permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"mode\":\"replace\",\"items\":[{\"targetType\":\"team\",\"targetId\":1}]}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void deleteKeepsDeletingWhenAiUnavailableAndDoesNotFakeSuccess() throws Exception {
        String token = login("admin", "123456");
        long unitId = importOneFile(token, "删除测试" + System.nanoTime() + ".md", "sop");

        // AI 服务不可达：不得假成功
        mockMvc.perform(delete("/api/knowledge/units")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"ids\":[" + unitId + "]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].success").value(false))
                .andExpect(jsonPath("$.data[0].errorCode").isNotEmpty());

        // 单元保持 deleting，且默认列表不再展示
        mockMvc.perform(get("/api/knowledge/units/" + unitId).header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value("deleting"));

        mockMvc.perform(get("/api/knowledge/units?keyword=删除测试").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.records[*].id", not(hasItem((int) unitId))));
    }

    @Test
    void qualityMetricsRequiresAuthentication() throws Exception {
        mockMvc.perform(get("/api/knowledge/import/quality-metrics"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value(401));
    }

    @Test
    void qualityMetricsReturnsAggregatesForAuthorized() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/import/quality-metrics?days=7").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.days").value(7))
                .andExpect(jsonPath("$.data.windowStart").isString())
                .andExpect(jsonPath("$.data.totalReviews").isNumber())
                .andExpect(jsonPath("$.data.totalAuditRows").isNumber())
                .andExpect(jsonPath("$.data.autoPassRate").isNumber())
                .andExpect(jsonPath("$.data.humanRequiredRate").isNumber())
                .andExpect(jsonPath("$.data.decisionDistribution").isArray())
                .andExpect(jsonPath("$.data.reasonDistribution").isArray())
                .andExpect(jsonPath("$.data.aiReasonDistribution").isArray())
                .andExpect(jsonPath("$.data.humanActionDistribution").isArray())
                .andExpect(jsonPath("$.data.categoryDistribution").isArray())
                .andExpect(jsonPath("$.data.aiModelDistribution").isArray())
                .andExpect(jsonPath("$.data.llmLatency.count").isNumber())
                .andExpect(jsonPath("$.data.vlmLatency.count").isNumber())
                .andExpect(jsonPath("$.data.humanConfirmedByUser").isArray())
                .andExpect(jsonPath("$.data.workloadSaved.note").isString())
                .andExpect(jsonPath("$.data.scannedRows").isNumber())
                .andExpect(jsonPath("$.data.truncated").isBoolean());
    }

    @Test
    void qualityMetricsClampsDays() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/import/quality-metrics?days=9999")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.days").value(365));
    }

    @Test
    void nonExistentUnitReturnsBadRequest() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units/999999").header("Authorization", "Bearer " + token))
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

    private List<Long> checkAuthorized(String token, Long userId, long unitId) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/knowledge/check-permissions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":" + userId + ",\"unitIds\":[" + unitId + "]}"))
                .andExpect(status().isOk())
                .andReturn();
        JsonNode data = objectMapper.readTree(result.getResponse().getContentAsString()).path("data");
        return toLongList(data.path("authorizedUnitIds"));
    }

    private List<Long> toLongList(JsonNode array) {
        return java.util.stream.StreamSupport.stream(array.spliterator(), false)
                .map(JsonNode::asLong)
                .toList();
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
