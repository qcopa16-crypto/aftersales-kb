package com.shuidun.kb.org;

import static org.hamcrest.Matchers.containsString;
import static org.hamcrest.Matchers.greaterThanOrEqualTo;
import static org.hamcrest.Matchers.hasItem;
import static org.hamcrest.Matchers.not;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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
 * P1 组织管理真实链路测试（H2 + 种子数据）。
 *
 * <p>覆盖：用户分页字段契约、部门树、角色权限、操作权限码 403、新增用户后可登录、重置密码。
 */
@SpringBootTest
@AutoConfigureMockMvc
class OrgControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void adminCanListUsersWithPagingContract() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/org/users?page=1&size=20").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data.total").value(greaterThanOrEqualTo(5)))
                .andExpect(jsonPath("$.data.page").value(1))
                .andExpect(jsonPath("$.data.size").value(20))
                .andExpect(jsonPath("$.data.records").isArray())
                .andExpect(jsonPath("$.data.records[?(@.username=='admin')]").exists());
    }

    @Test
    void userListNeverExposesPasswordFields() throws Exception {
        String token = login("admin", "123456");
        MvcResult result = mockMvc.perform(get("/api/org/users?page=1&size=20")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn();
        String body = result.getResponse().getContentAsString();
        org.junit.jupiter.api.Assertions.assertFalse(body.contains("passwordHash"), "响应不得包含 passwordHash");
        org.junit.jupiter.api.Assertions.assertFalse(body.contains("password_hash"), "响应不得包含 password_hash");
    }

    @Test
    void nonAdminGetsForbiddenOnOrgUsers() throws Exception {
        String token = login("zhangsan", "123456");
        mockMvc.perform(get("/api/org/users").header("Authorization", "Bearer " + token))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value(403));
    }

    @Test
    void adminCanReadDepartmentTree() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/org/departments").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data").isArray())
                .andExpect(jsonPath("$.data[*].name", hasItem("售后部")));
    }

    @Test
    void adminCanReadRolesWithPermissions() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/org/roles").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[*].roleCode", hasItem("admin")))
                .andExpect(jsonPath("$.data[?(@.roleCode=='admin')].permissions[*]", hasItem("knowledge:import")));
    }

    @Test
    void createdUserCanLogin() throws Exception {
        String adminToken = login("admin", "123456");
        String username = "tester" + System.nanoTime();
        mockMvc.perform(post("/api/org/users")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"pass1234\","
                                + "\"displayName\":\"测试员\",\"departmentId\":2,\"roleIds\":[3]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data").isNumber());

        String newToken = login(username, "pass1234");
        mockMvc.perform(get("/api/auth/me").header("Authorization", "Bearer " + newToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.username").value(username))
                .andExpect(jsonPath("$.data.displayName").value("测试员"))
                .andExpect(jsonPath("$.data.roles", hasItem("after_sales")));
    }

    @Test
    void duplicateUsernameRejected() throws Exception {
        String adminToken = login("admin", "123456");
        mockMvc.perform(post("/api/org/users")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"admin\",\"password\":\"pass1234\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void resetPasswordChangesLoginCredential() throws Exception {
        String adminToken = login("admin", "123456");
        String username = "reset" + System.nanoTime();
        MvcResult created = mockMvc.perform(post("/api/org/users")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"oldpass1\",\"roleIds\":[3]}"))
                .andExpect(status().isOk())
                .andReturn();
        long userId = objectMapper.readTree(created.getResponse().getContentAsString()).path("data").asLong();

        mockMvc.perform(post("/api/org/users/" + userId + "/reset-password")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"password\":\"newpass1\"}"))
                .andExpect(status().isOk())
                // F-12：响应体不得出现任何明文密码
                .andExpect(content().string(not(containsString("newpass1"))));

        // F-12：不提供密码（旧"服务端随机生成并回显"路径）必须被拒绝
        mockMvc.perform(post("/api/org/users/" + userId + "/reset-password")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{}"))
                .andExpect(status().isBadRequest());

        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"oldpass1\"}"))
                .andExpect(status().isUnauthorized());

        login(username, "newpass1");
    }

    @Test
    void adminCanUpdateRolePermissions() throws Exception {
        String adminToken = login("admin", "123456");
        mockMvc.perform(post("/api/org/roles/4/permissions")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"mode\":\"replace\",\"permissions\":[\"knowledge:list\",\"knowledge:ai\",\"dashboard:view\",\"gap:view\"]}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));

        mockMvc.perform(get("/api/org/roles/4/permissions").header("Authorization", "Bearer " + adminToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data", hasItem("gap:view")));
    }

    private String login(String username, String password) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + username + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn();
        JsonNode node = objectMapper.readTree(result.getResponse().getContentAsString());
        return node.path("data").path("accessToken").asText();
    }
}
