package com.shuidun.kb.knowledge.controller;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.knowledge.dto.DeleteItemResult;
import com.shuidun.kb.knowledge.dto.DeleteRequest;
import com.shuidun.kb.knowledge.dto.DeleteTaskVO;
import com.shuidun.kb.knowledge.dto.KnowledgeUnitUpdateRequest;
import com.shuidun.kb.knowledge.dto.KnowledgeUnitVO;
import com.shuidun.kb.knowledge.service.KnowledgeUnitService;
import com.shuidun.kb.permission.dto.CheckPermissionRequest;
import com.shuidun.kb.permission.dto.CheckPermissionResponse;
import com.shuidun.kb.permission.dto.UnitPermissionRequest;
import com.shuidun.kb.permission.RequirePermission;
import jakarta.validation.Valid;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 知识单元接口（docs/api/api.md §3）。
 */
@RestController
@RequestMapping("/api/knowledge")
@RequiredArgsConstructor
public class KnowledgeUnitController {

    private final KnowledgeUnitService knowledgeUnitService;

    /**
     * 知识单元分页列表。
     *
     * @param page     页码
     * @param size     每页条数
     * @param keyword  关键字
     * @param category 分类
     * @param status   业务状态
     * @return 分页结果
     */
    @GetMapping("/units")
    @RequirePermission("knowledge:list")
    public Result<PageResult<KnowledgeUnitVO>> list(@RequestParam(defaultValue = "1") int page,
                                                    @RequestParam(defaultValue = "20") int size,
                                                    @RequestParam(required = false) String keyword,
                                                    @RequestParam(required = false) String category,
                                                    @RequestParam(required = false) String status) {
        return Result.ok(knowledgeUnitService.list(SecurityUtils.requireCurrentUser(),
                page, size, keyword, category, status));
    }

    /**
     * 知识单元详情（含权限摘要）。
     *
     * @param id 主键
     * @return 详情
     */
    @GetMapping("/units/{id}")
    @RequirePermission("knowledge:list")
    public Result<KnowledgeUnitVO> detail(@PathVariable Long id) {
        return Result.ok(knowledgeUnitService.detail(SecurityUtils.requireCurrentUser(), id));
    }

    /**
     * 更新知识单元；正文变更触发 re-ingest。
     *
     * @param id      主键
     * @param request 更新请求
     * @return 空结果
     */
    @PutMapping("/units/{id}")
    @RequirePermission("knowledge:edit")
    public Result<Void> update(@PathVariable Long id, @RequestBody KnowledgeUnitUpdateRequest request) {
        knowledgeUnitService.update(id, request);
        return Result.ok();
    }

    /**
     * 批量删除（逐项结果；先 deleting → 删向量 → 删主数据）。
     *
     * @param request 删除请求
     * @return 逐项结果
     */
    @DeleteMapping("/units")
    @RequirePermission("knowledge:delete")
    public Result<List<DeleteItemResult>> delete(@RequestBody DeleteRequest request) {
        return Result.ok(knowledgeUnitService.deleteBatch(request.getIds()));
    }

    /**
     * 配置知识单元数据权限。
     *
     * @param id      主键
     * @param request 权限请求
     * @return 空结果
     */
    @PostMapping("/units/{id}/permissions")
    @RequirePermission("knowledge:perm")
    public Result<Void> configurePermissions(@PathVariable Long id,
                                             @Valid @RequestBody UnitPermissionRequest request) {
        knowledgeUnitService.configurePermissions(id, request);
        return Result.ok();
    }

    /**
     * 校验指定用户对给定 unit 的数据权限。
     *
     * <p>F-06：普通用户只能校验自身（userId = 当前登录用户）；校验他人需持有
     * {@code knowledge:perm}，否则 403。候选 id 数量受限，未知用户统一响应。
     *
     * @param request 校验请求
     * @return 授权 / 未授权集合
     */
    @PostMapping("/check-permissions")
    @RequirePermission("knowledge:list")
    public Result<CheckPermissionResponse> checkPermissions(@Valid @RequestBody CheckPermissionRequest request) {
        return Result.ok(knowledgeUnitService.checkPermissions(SecurityUtils.requireCurrentUser(),
                request.getUserId(), request.getUnitIds()));
    }

    /**
     * 未完成的删除任务（删除失败态 + 最近错误 + 下次重试，F-04 管理端展示）。
     *
     * @return 删除任务列表
     */
    @GetMapping("/units/delete-tasks")
    @RequirePermission("knowledge:delete")
    public Result<List<DeleteTaskVO>> deleteTasks(@RequestParam(defaultValue = "50") int limit) {
        return Result.ok(knowledgeUnitService.unfinishedDeletes(Math.min(Math.max(limit, 1), 200)));
    }

    /**
     * R-03 重试失败的知识删除任务（带授权与审计：需 {@code knowledge:delete}）。
     *
     * @param unitId 单元 id
     * @return 逐项删除结果
     */
    @PostMapping("/units/delete-tasks/{unitId}/retry")
    @RequirePermission("knowledge:delete")
    public Result<DeleteItemResult> retryDeleteTask(@PathVariable Long unitId) {
        CurrentUser operator = SecurityUtils.requireCurrentUser();
        return Result.ok(knowledgeUnitService.retryDeleteTask(unitId, operator.getId()));
    }
}
