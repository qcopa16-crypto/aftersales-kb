package com.shuidun.kb.security;

import static org.hamcrest.Matchers.hasItem;
import static org.hamcrest.Matchers.not;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Assertions;
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
 * 安全回归测试：知识管理读接口必须强制四维数据权限（安全审计 F-001 的修复验证）。
 *
 * <p>种子数据：
 * <ul>
 *   <li>unit1「整机保修政策」/ unit2「工单处理 SOP」→ 仅 department=2（售后部）</li>
 *   <li>unit3「渠道价格政策」→ 仅 role=4（sales）</li>
 *   <li>unit4「收费标准与费用制度」→ 仅 role=5（finance）</li>
 *   <li>unit5「H3C 路由器用户手册」→ global</li>
 * </ul>
 * lisi = 销售部(3)/sales(4)：仅可访问 unit3 与 unit5；zhangsan = 售后部(2)/after_sales(3)：仅可访问 unit1/unit2/unit5。
 */
@SpringBootTest
@AutoConfigureMockMvc
class DataPermissionScopeTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void salesUserCannotReadFinanceOnlyUnitDetail() throws Exception {
        String token = login("lisi", "123456");
        // 数据权限不足：HTTP 固定 200，靠 body code=40001 区分
        mockMvc.perform(get("/api/knowledge/units/4").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(40001));
    }

    @Test
    void salesUserListIsScopedByDataPermission() throws Exception {
        String token = login("lisi", "123456");
        List<Long> ids = listUnitIds(token);
        Assertions.assertFalse(ids.contains(4L), "不得泄露财务私有单元 unit4");
        Assertions.assertFalse(ids.contains(1L), "不得泄露售后部门单元 unit1");
        Assertions.assertFalse(ids.contains(2L), "不得泄露售后部门单元 unit2");
        Assertions.assertTrue(ids.contains(3L), "应能看到角色授权的 unit3");
        Assertions.assertTrue(ids.contains(5L), "应能看到全局公开的 unit5");
    }

    @Test
    void afterSalesUserListIsScopedByDataPermission() throws Exception {
        String token = login("zhangsan", "123456");
        List<Long> ids = listUnitIds(token);
        Assertions.assertTrue(ids.contains(1L));
        Assertions.assertTrue(ids.contains(2L));
        Assertions.assertTrue(ids.contains(5L));
        Assertions.assertFalse(ids.contains(3L), "不得泄露销售私有单元 unit3");
        Assertions.assertFalse(ids.contains(4L), "不得泄露财务私有单元 unit4");
    }

    @Test
    void afterSalesUserCannotReadSalesOnlyUnitDetail() throws Exception {
        String token = login("zhangsan", "123456");
        mockMvc.perform(get("/api/knowledge/units/3").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(40001));
    }

    @Test
    void knowledgeAdminSeesAllUnits() throws Exception {
        String token = login("admin", "123456");
        List<Long> ids = listUnitIds(token);
        Assertions.assertTrue(ids.containsAll(List.of(1L, 2L, 3L, 4L, 5L)),
                "拥有 knowledge:perm 的知识管理员应可见全部单元");
        mockMvc.perform(get("/api/knowledge/units/4").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.title").value("收费标准与费用制度"));
    }

    @Test
    void scopedDetailStillReturnsAuthorizedUnit() throws Exception {
        String token = login("lisi", "123456");
        mockMvc.perform(get("/api/knowledge/units/3").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.unitCode").value("U-1003"));
    }

    @Test
    void scopedListKeepsPagingContract() throws Exception {
        String token = login("lisi", "123456");
        mockMvc.perform(get("/api/knowledge/units?page=1&size=1").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.page").value(1))
                .andExpect(jsonPath("$.data.size").value(1))
                .andExpect(jsonPath("$.data.records.length()").value(1))
                .andExpect(jsonPath("$.data.records[*].id", not(hasItem(4))));
    }

    private List<Long> listUnitIds(String token) throws Exception {
        MvcResult result = mockMvc.perform(get("/api/knowledge/units?page=1&size=100")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andReturn();
        JsonNode records = objectMapper.readTree(result.getResponse().getContentAsString())
                .path("data").path("records");
        List<Long> ids = new ArrayList<>();
        for (JsonNode node : records) {
            ids.add(node.path("id").asLong());
        }
        return ids;
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
