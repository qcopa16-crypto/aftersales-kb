package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.ChatRequest;
import com.shuidun.kb.ai.dto.ChatSessionDetailVO;
import com.shuidun.kb.ai.dto.ChatSessionVO;
import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.permission.RequirePermission;
import jakarta.validation.Valid;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * AI 问答接口（docs/api/api.md §4）。前端只访问本接口，禁止直连 FastAPI。
 */
@RestController
@RequestMapping("/api/ai")
@RequiredArgsConstructor
public class ChatController {

    private final ChatOrchestrator chatOrchestrator;
    private final ChatHistoryService chatHistoryService;

    /**
     * 流式问答（SSE）。
     *
     * @param request 问答请求
     * @return SSE 流
     */
    @PostMapping(value = "/chat/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    @RequirePermission("knowledge:ai")
    public SseEmitter stream(@Valid @RequestBody ChatRequest request) {
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return chatOrchestrator.stream(request, currentUser);
    }

    /**
     * 历史会话列表（仅本人），按最近提问时间倒序。
     *
     * @param limit 条数，默认 20，上限 50
     * @return 会话列表
     */
    @GetMapping("/chat/sessions")
    @RequirePermission("knowledge:ai")
    public Result<List<ChatSessionVO>> sessions(@RequestParam(required = false) Integer limit) {
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return Result.ok(chatHistoryService.listSessions(currentUser.getId(), limit));
    }

    /**
     * 历史会话详情（仅本人），按时间正序返回问答轮次与来源卡片。
     *
     * <p>不属于当前用户的会话一律返回空 rounds，不区分「无权限」与「不存在」，避免会话 id 被探测。
     * 回看时按<b>当前</b>数据权限重新过滤：提问后失权的知识单元会从来源剔除、答案脱敏（F-29）。
     *
     * @param sessionId 会话 id
     * @return 会话详情
     */
    @GetMapping("/chat/sessions/{sessionId}")
    @RequirePermission("knowledge:ai")
    public Result<ChatSessionDetailVO> sessionDetail(@PathVariable String sessionId) {
        if (sessionId.length() > ChatRequest.MAX_SESSION_ID_CHARS) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "会话 id 长度不能超过 64");
        }
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return Result.ok(chatHistoryService.openSession(currentUser, sessionId));
    }
}
