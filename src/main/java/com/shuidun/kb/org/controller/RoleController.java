package com.shuidun.kb.org.controller;

import com.shuidun.kb.common.Result;
import com.shuidun.kb.org.dto.RolePermissionRequest;
import com.shuidun.kb.org.dto.RoleRequest;
import com.shuidun.kb.org.dto.RoleVO;
import com.shuidun.kb.org.service.RoleService;
import com.shuidun.kb.permission.RequirePermission;
import jakarta.validation.Valid;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 组织-角色接口（docs/api/api.md §2）。
 */
@RestController
@RequestMapping("/api/org/roles")
@RequiredArgsConstructor
public class RoleController {

    private final RoleService roleService;

    /**
     * 角色列表（含权限码）。
     *
     * @return 角色视图列表
     */
    @GetMapping
    @RequirePermission("org:role:list")
    public Result<List<RoleVO>> list() {
        return Result.ok(roleService.list());
    }

    /**
     * 新增角色。
     *
     * @param request 角色请求
     * @return 新角色 id
     */
    @PostMapping
    @RequirePermission("org:role:edit")
    public Result<Long> create(@Valid @RequestBody RoleRequest request) {
        return Result.ok(roleService.create(request));
    }

    /**
     * 更新角色。
     *
     * @param id      角色 id
     * @param request 角色请求
     * @return 空结果
     */
    @PutMapping("/{id}")
    @RequirePermission("org:role:edit")
    public Result<Void> update(@PathVariable Long id, @Valid @RequestBody RoleRequest request) {
        roleService.update(id, request);
        return Result.ok();
    }

    /**
     * 查询角色操作权限。
     *
     * @param id 角色 id
     * @return 权限码列表
     */
    @GetMapping("/{id}/permissions")
    @RequirePermission("org:role:list")
    public Result<List<String>> permissions(@PathVariable Long id) {
        return Result.ok(roleService.permissions(id));
    }

    /**
     * 分配角色操作权限。
     *
     * @param id      角色 id
     * @param request 分配请求
     * @return 空结果
     */
    @PostMapping("/{id}/permissions")
    @RequirePermission("org:role:edit")
    public Result<Void> assignPermissions(@PathVariable Long id, @RequestBody RolePermissionRequest request) {
        roleService.assignPermissions(id, request);
        return Result.ok();
    }
}
