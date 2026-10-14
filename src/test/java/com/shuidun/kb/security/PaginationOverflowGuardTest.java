package com.shuidun.kb.security;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.ObjectMapper;

/**
 * 安全回归测试：分页参数极大值不得因 int 溢出导致 500（安全审计 F-012 / QA 边界缺陷修复）。
 *
 * <p>根因：{@code (normalizedPage - 1) * normalizedSize} 以 int 计算，极大 page 溢出为负 OFFSET，
 * SQL 报错 → HTTP 500。修复后 offset 以 long 计算，超出 int 范围抛 {@code PARAM_INVALID(400)}。
 * 覆盖 knowledge / user / faq 三类共用同一算法的 list 端点。
 */
@SpringBootTest
@AutoConfigureMockMvc
class PaginationOverflowGuardTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void knowledgeListRejectsOverflowPageWith400() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units?page=2147483647").header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
        mockMvc.perform(get("/api/knowledge/units?page=2000000000&size=100")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void userListRejectsOverflowPageWith400() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/org/users?page=2147483647").header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void faqListRejectsOverflowPageWith400() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/settlement/faqs?page=2147483647").header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value(400));
    }

    @Test
    void normalPageStillOk() throws Exception {
        String token = login("admin", "123456");
        mockMvc.perform(get("/api/knowledge/units?page=1&size=20").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0));
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
