package com.shuidun.kb.org.controller;

import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.org.dto.ResetPasswordRequest;
import com.shuidun.kb.org.dto.UserCreateRequest;
import com.shuidun.kb.org.dto.UserUpdateRequest;
import com.shuidun.kb.org.dto.UserVO;
import com.shuidun.kb.org.service.UserService;
import com.shuidun.kb.permission.RequirePermission;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 组织-用户接口（docs/api/api.md §2）。
 */
@RestController
@RequestMapping("/api/org/users")
@RequiredArgsConstructor
public class UserController {

    private final UserService userService;

    /**
     * 用户分页列表。
     *
     * @param page    页码
     * @param size    每页条数
     * @param keyword 关键字
     * @return 分页结果
     */
    @GetMapping
    @RequirePermission("org:user:list")
    public Result<PageResult<UserVO>> list(@RequestParam(defaultValue = "1") int page,
                                           @RequestParam(defaultValue = "20") int size,
                                           @RequestParam(required = false) String keyword) {
        return Result.ok(userService.list(page, size, keyword));
    }

    /**
     * 新增用户。
     *
     * @param request 新增请求
     * @return 新用户 id
     */
    @PostMapping
    @RequirePermission("org:user:edit")
    public Result<Long> create(@Valid @RequestBody UserCreateRequest request) {
        return Result.ok(userService.create(request));
    }

    /**
     * 更新用户。
     *
     * @param id      用户 id
     * @param request 更新请求
     * @return 空结果
     */
    @PutMapping("/{id}")
    @RequirePermission("org:user:edit")
    public Result<Void> update(@PathVariable Long id, @RequestBody UserUpdateRequest request) {
        userService.update(id, request);
        return Result.ok();
    }

    /**
     * 重置密码（F-12）。
     *
     * <p>安全修复：不再返回新密码明文（明文会进入浏览器网络面板/代理/客户端日志，
     * 且让管理员长期持有可直接登录的口令）。管理员须显式提供新密码并经带外渠道
     * 告知用户，用户首次登录后自行修改；响应只返回操作结果。
     *
     * @param id      用户 id
     * @param request 重置请求（必须显式提供新密码）
     * @return 操作结果（不含任何凭据）
     */
    @PostMapping("/{id}/reset-password")
    @RequirePermission("org:user:edit")
    public Result<Void> resetPassword(@PathVariable Long id,
                                      @RequestBody(required = false) ResetPasswordRequest request) {
        userService.resetPassword(id, request == null ? null : request.getPassword());
        return Result.ok();
    }
}
