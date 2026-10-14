package com.shuidun.kb.faq;

import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.faq.dto.FaqReviewRequest;
import com.shuidun.kb.faq.dto.FaqUpdateRequest;
import com.shuidun.kb.faq.dto.FaqVO;
import com.shuidun.kb.permission.RequirePermission;
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
 * FAQ 接口（docs/api/api.md §5）。
 */
@RestController
@RequestMapping("/api/settlement/faqs")
@RequiredArgsConstructor
public class FaqController {

    private final FaqService faqService;

    /**
     * 待审核推荐列表。
     *
     * @param page 页码
     * @param size 每页条数
     * @return 分页结果
     */
    @GetMapping("/recommendations")
    @RequirePermission("faq:review")
    public Result<PageResult<FaqVO>> recommendations(@RequestParam(defaultValue = "1") int page,
                                                     @RequestParam(defaultValue = "20") int size) {
        return Result.ok(faqService.recommendations(page, size));
    }

    /**
     * 已发布/全部 FAQ 列表。
     *
     * @param page   页码
     * @param size   每页条数
     * @param status 状态过滤
     * @return 分页结果
     */
    @GetMapping
    @RequirePermission("knowledge:list")
    public Result<PageResult<FaqVO>> list(@RequestParam(defaultValue = "1") int page,
                                          @RequestParam(defaultValue = "20") int size,
                                          @RequestParam(required = false) String status) {
        return Result.ok(faqService.list(page, size, status));
    }

    /**
     * 审核 FAQ。
     *
     * @param id      主键
     * @param request 审核请求
     * @return 空结果
     */
    @PostMapping("/{id}/review")
    @RequirePermission("faq:review")
    public Result<Void> review(@PathVariable Long id, @RequestBody FaqReviewRequest request) {
        faqService.review(id, request, SecurityUtils.requireCurrentUser().getId());
        return Result.ok();
    }

    /**
     * 编辑已发布 FAQ。
     *
     * @param id      主键
     * @param request 编辑请求
     * @return 空结果
     */
    @PutMapping("/{id}")
    @RequirePermission("faq:review")
    public Result<Void> update(@PathVariable Long id, @RequestBody FaqUpdateRequest request) {
        faqService.update(id, request);
        return Result.ok();
    }

    /**
     * 下线 FAQ。
     *
     * @param id 主键
     * @return 空结果
     */
    @PostMapping("/{id}/offline")
    @RequirePermission("faq:review")
    public Result<Void> offline(@PathVariable Long id) {
        faqService.offline(id);
        return Result.ok();
    }
}
