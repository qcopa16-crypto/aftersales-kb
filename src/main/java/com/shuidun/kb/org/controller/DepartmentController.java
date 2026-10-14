package com.shuidun.kb.org.controller;

import com.shuidun.kb.common.Result;
import com.shuidun.kb.org.dto.DepartmentRequest;
import com.shuidun.kb.org.dto.DepartmentVO;
import com.shuidun.kb.org.service.DepartmentService;
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
 * 组织-部门接口（docs/api/api.md §2）。
 */
@RestController
@RequestMapping("/api/org/departments")
@RequiredArgsConstructor
public class DepartmentController {

    private final DepartmentService departmentService;

    /**
     * 部门树。
     *
     * @return 根节点列表
     */
    @GetMapping
    @RequirePermission("org:dept:list")
    public Result<List<DepartmentVO>> tree() {
        return Result.ok(departmentService.tree());
    }

    /**
     * 新增部门。
     *
     * @param request 部门请求
     * @return 新部门 id
     */
    @PostMapping
    @RequirePermission("org:dept:edit")
    public Result<Long> create(@Valid @RequestBody DepartmentRequest request) {
        return Result.ok(departmentService.create(request));
    }

    /**
     * 更新部门。
     *
     * @param id      部门 id
     * @param request 部门请求
     * @return 空结果
     */
    @PutMapping("/{id}")
    @RequirePermission("org:dept:edit")
    public Result<Void> update(@PathVariable Long id, @Valid @RequestBody DepartmentRequest request) {
        departmentService.update(id, request);
        return Result.ok();
    }
}
