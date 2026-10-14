package com.shuidun.kb.gap;

import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.gap.dto.GapUpdateRequest;
import com.shuidun.kb.gap.dto.KnowledgeGapVO;
import com.shuidun.kb.permission.RequirePermission;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 知识缺口接口（docs/api/api.md §6）。
 */
@RestController
@RequestMapping("/api/settlement/knowledge-gaps")
@RequiredArgsConstructor
public class GapController {

    private final GapService gapService;

    /**
     * 缺口列表。
     *
     * @param page   页码
     * @param size   每页条数
     * @param status 状态过滤
     * @return 分页结果
     */
    @GetMapping
    @RequirePermission("gap:view")
    public Result<PageResult<KnowledgeGapVO>> list(@RequestParam(defaultValue = "1") int page,
                                                   @RequestParam(defaultValue = "20") int size,
                                                   @RequestParam(required = false) String status) {
        return Result.ok(gapService.list(page, size, status));
    }

    /**
     * 更新缺口状态。
     *
     * @param id      主键
     * @param request 更新请求
     * @return 空结果
     */
    @PatchMapping("/{id}")
    @RequirePermission("gap:view")
    public Result<Void> updateStatus(@PathVariable Long id, @RequestBody GapUpdateRequest request) {
        gapService.updateStatus(com.shuidun.kb.auth.SecurityUtils.requireCurrentUser(), id, request);
        return Result.ok();
    }
}
