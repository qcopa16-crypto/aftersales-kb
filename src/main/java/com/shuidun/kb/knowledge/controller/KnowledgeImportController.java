package com.shuidun.kb.knowledge.controller;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.auth.SecurityUtils;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.common.Result;
import com.shuidun.kb.knowledge.dto.BatchDetailVO;
import com.shuidun.kb.knowledge.dto.BatchSummaryVO;
import com.shuidun.kb.knowledge.dto.ImportResponseVO;
import com.shuidun.kb.knowledge.dto.QualityGateRequest;
import com.shuidun.kb.knowledge.dto.QualityMetricsVO;
import com.shuidun.kb.knowledge.dto.RetryRequest;
import com.shuidun.kb.knowledge.service.KnowledgeImportService;
import com.shuidun.kb.knowledge.service.QualityMetricsService;
import com.shuidun.kb.permission.RequirePermission;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/**
 * 知识导入与入库作业接口（DESIGN.md §9 后端必做表面）。
 */
@RestController
@RequestMapping("/api/knowledge/import")
@RequiredArgsConstructor
public class KnowledgeImportController {

    private final KnowledgeImportService knowledgeImportService;
    private final QualityMetricsService qualityMetricsService;

    /**
     * 异步导入（multipart，多文件）。
     *
     * @param files    上传文件
     * @param category 可选分类
     * @return 批次摘要
     */
    @PostMapping
    @RequirePermission("knowledge:import")
    public Result<ImportResponseVO> importFiles(@RequestPart("files") List<MultipartFile> files,
                                                @RequestParam(required = false) String category,
                                                @RequestParam(required = false, defaultValue = "false") Boolean useVlm) {
        Long creatorId = SecurityUtils.requireCurrentUser().getId();
        return Result.ok(knowledgeImportService.importFiles(files, category, Boolean.TRUE.equals(useVlm), creatorId));
    }

    /**
     * 批次列表（按当前用户数据范围过滤：本人创建 / 授权单元 / 知识管理员全量）。
     *
     * @param page 页码
     * @param size 每页条数
     * @return 分页结果
     */
    @GetMapping("/batches")
    @RequirePermission("knowledge:list")
    public Result<PageResult<BatchSummaryVO>> batches(@RequestParam(defaultValue = "1") int page,
                                                      @RequestParam(defaultValue = "20") int size) {
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return Result.ok(knowledgeImportService.batches(currentUser, page, size));
    }

    /**
     * 批次详情（按当前用户数据范围过滤；越权批次返回空 files，与不存在不可区分）。
     *
     * @param batchId 批次 id
     * @return 批次详情
     */
    @GetMapping("/batches/{batchId}")
    @RequirePermission("knowledge:list")
    public Result<BatchDetailVO> batchDetail(@PathVariable String batchId) {
        CurrentUser currentUser = SecurityUtils.requireCurrentUser();
        return Result.ok(knowledgeImportService.batchDetail(currentUser, batchId));
    }

    /**
     * 入库质检度量（只读，回答「AI 自动判定省了多少人工」）。
     *
     * @param days 统计窗口天数（默认 30，夹取到 [1, 365]）
     * @return 质检度量
     */
    @GetMapping("/quality-metrics")
    @RequirePermission("knowledge:list")
    public Result<QualityMetricsVO> qualityMetrics(@RequestParam(defaultValue = "30") int days) {
        return Result.ok(qualityMetricsService.metrics(days));
    }

    /**
     * 质检确认（product 手册强制）。
     *
     * @param unitId  unit id
     * @param request 质检请求
     * @return 空结果
     */
    @PostMapping("/quality-gate/{unitId}")
    @RequirePermission("knowledge:import")
    public Result<Void> qualityGate(@PathVariable Long unitId, @RequestBody QualityGateRequest request) {
        knowledgeImportService.qualityGate(unitId, request);
        return Result.ok();
    }

    /**
     * 从失败阶段重试。
     *
     * @param unitId  unit id
     * @param request 重试请求（可为空）
     * @return 空结果
     */
    @PostMapping("/retry/{unitId}")
    @RequirePermission("knowledge:import")
    public Result<Void> retry(@PathVariable Long unitId, @RequestBody(required = false) RetryRequest request) {
        knowledgeImportService.retry(unitId, request);
        return Result.ok();
    }
}
