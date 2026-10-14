package com.shuidun.kb.knowledge.service;

import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.common.PageResult;
import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.job.entity.IngestJob;
import com.shuidun.kb.knowledge.KnowledgeEnums;
import com.shuidun.kb.knowledge.dto.DeleteItemResult;
import com.shuidun.kb.knowledge.dto.DeleteTaskVO;
import com.shuidun.kb.knowledge.dto.KnowledgeUnitUpdateRequest;
import com.shuidun.kb.knowledge.dto.KnowledgeUnitVO;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.org.entity.User;
import com.shuidun.kb.org.mapper.UserMapper;
import com.shuidun.kb.permission.DataPermissionEngine;
import com.shuidun.kb.permission.PermissionDecision;
import com.shuidun.kb.permission.UnitPermissionService;
import com.shuidun.kb.permission.dto.CheckPermissionResponse;
import com.shuidun.kb.permission.dto.UnitPermissionRequest;
import java.util.ArrayList;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 知识单元服务（docs/api/api.md §3）。
 *
 * <p>删除严格遵循 DESIGN.md §6.6 与 F-03/F-04：先 CAS 标记 deleting → 取消排队中的入库作业 →
 * 等待在途 embed 租约结束 → 幂等重放 AI DELETE → 再删主数据；失败持久化到删除补偿任务
 * （{@link KnowledgeDeleteService}）按退避重试，保持 deleting 并逐项返回失败原因，不假成功。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class KnowledgeUnitService {

    private static final int MAX_PAGE_SIZE = 100;

    private final KnowledgeUnitMapper unitMapper;
    private final UnitPermissionService unitPermissionService;
    private final DataPermissionEngine dataPermissionEngine;
    private final IngestJobService ingestJobService;
    private final UserMapper userMapper;
    private final KnowledgeDeleteService knowledgeDeleteService;

    /** F-06 候选 unit id 数量上限：防止越权枚举探测。 */
    private static final int MAX_CHECK_UNIT_IDS = 100;

    /**
     * 分页查询知识单元（默认不展示 deleting）。
     *
     * <p>四维数据权限：拥有 {@code knowledge:perm} 的知识管理员可见全部；其余用户只能看到
     * {@link DataPermissionEngine} 判定为可访问的单元（防止通过列表接口越权读取）。
     *
     * @param currentUser 当前用户
     * @param page        页码
     * @param size        每页条数
     * @param keyword     关键字
     * @param category    分类
     * @param status      业务状态
     * @return 分页结果
     */
    public PageResult<KnowledgeUnitVO> list(CurrentUser currentUser, int page, int size,
                                            String keyword, String category, String status) {
        int normalizedPage = Math.max(page, 1);
        int normalizedSize = Math.min(Math.max(size, 1), MAX_PAGE_SIZE);
        String kw = (keyword == null || keyword.isBlank()) ? null : keyword.trim();
        String cat = (category == null || category.isBlank()) ? null : category.trim();
        String st = (status == null || status.isBlank()) ? null : status.trim();
        if (st != null && !KnowledgeEnums.STATUSES.contains(st)) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "非法 status: " + st);
        }
        List<Long> allowedUnitIds = resolveScope(currentUser);
        if (allowedUnitIds != null && allowedUnitIds.isEmpty()) {
            // 无任何授权单元：fail-closed，直接返回空页
            return PageResult.of(0, normalizedPage, normalizedSize, List.of());
        }
        long offset = (long) (normalizedPage - 1) * normalizedSize;
        if (offset > Integer.MAX_VALUE) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "page 超出范围");
        }
        long total = unitMapper.countPage(kw, cat, st, allowedUnitIds);
        List<KnowledgeUnit> units = unitMapper.selectPage(kw, cat, st, allowedUnitIds,
                (int) offset, normalizedSize);
        List<KnowledgeUnitVO> records = new ArrayList<>(units.size());
        for (KnowledgeUnit unit : units) {
            records.add(toVO(unit, false));
        }
        return PageResult.of(total, normalizedPage, normalizedSize, records);
    }

    /**
     * 查询知识单元详情（含数据权限摘要）。
     *
     * @param currentUser 当前用户
     * @param id          主键
     * @return 详情视图
     */
    public KnowledgeUnitVO detail(CurrentUser currentUser, Long id) {
        KnowledgeUnit unit = requireUnit(id);
        List<Long> allowedUnitIds = resolveScope(currentUser);
        if (allowedUnitIds != null && !allowedUnitIds.contains(id)) {
            // 数据权限不足：HTTP 固定 200，靠 body code=40001 区分（代码设计规范 §3.3）
            throw new BusinessException(ErrorCode.DATA_PERMISSION_DENIED, "无权访问该知识");
        }
        KnowledgeUnitVO vo = toVO(unit, true);
        vo.setPermissions(unitPermissionService.summaries(id));
        return vo;
    }

    /**
     * 计算数据权限范围。
     *
     * @param currentUser 当前用户
     * @return null 表示不限制（知识管理员）；否则为可访问 unit id 列表
     */
    private List<Long> resolveScope(CurrentUser currentUser) {
        if (currentUser == null || currentUser.hasPermission("knowledge:perm")) {
            return null;
        }
        return dataPermissionEngine.accessibleUnitIds(currentUser);
    }

    /**
     * 更新知识单元；正文变更触发 re-ingest（期间 ingest_status=queued，不可被检索）。
     *
     * @param id      主键
     * @param request 更新请求
     */
    @Transactional
    public void update(Long id, KnowledgeUnitUpdateRequest request) {
        KnowledgeUnit unit = requireUnit(id);
        // F-03：删除中/已下线的单元禁止修改，防止删除被更新"复活"
        KnowledgeDeleteService.requireModifiable(unit);
        if (request.getCategory() != null && !KnowledgeEnums.CATEGORIES.contains(request.getCategory())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "非法 category: " + request.getCategory());
        }
        if (request.getStatus() != null
                && !("active".equals(request.getStatus()) || "disabled".equals(request.getStatus()))) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "更新仅支持 active / disabled");
        }
        boolean contentChanged = request.getContent() != null && !request.getContent().equals(unit.getContent());
        // R-02：可选乐观锁——客户端若携带读取时的版本，先校验一致性
        if (request.getLifecycleVersion() != null
                && !request.getLifecycleVersion().equals(unit.getLifecycleVersion())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "数据已被其他操作修改，请刷新后重试");
        }
        if (request.getTitle() != null) {
            unit.setTitle(request.getTitle());
        }
        if (request.getContent() != null) {
            unit.setContent(request.getContent());
        }
        if (request.getSummary() != null) {
            unit.setSummary(request.getSummary());
        }
        if (request.getCategory() != null) {
            unit.setCategory(request.getCategory());
        }
        if (request.getStatus() != null) {
            unit.setStatus(request.getStatus());
        }
        if (contentChanged) {
            // 正文变更触发 re-ingest；期间该 unit 不可被检索（代码设计规范 §5.1）
            unit.setIngestStatus("queued");
        }
        // R-02：lifecycle_version CAS——读取后被并发删除（deleting）或修改时影响行数为 0，
        // 必须报并发冲突并停止写入，绝不用旧快照把 deleting 覆盖回 active。
        int updated = unitMapper.update(unit);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID,
                    "知识单元已被并发修改或删除，请刷新后重试");
        }
        if (contentChanged) {
            ingestJobService.enqueue(unit.getBatchId(), unit.getId(), unit.getCreatorId());
            log.info("content changed, re-ingest queued unitId={}", id);
        }
        log.info("update knowledge unit unitId={}", id);
    }

    /**
     * 批量删除，逐项返回结果（禁止部分失败整体成功）。
     *
     * @param ids unit id 列表
     * @return 逐项结果
     */
    public List<DeleteItemResult> deleteBatch(List<Long> ids) {
        if (ids == null || ids.isEmpty()) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "ids 不能为空");
        }
        List<DeleteItemResult> results = new ArrayList<>(ids.size());
        for (Long id : ids) {
            results.add(deleteOne(id));
        }
        return results;
    }

    /**
     * 配置知识单元数据权限。
     *
     * @param id      主键
     * @param request 权限请求
     */
    public void configurePermissions(Long id, UnitPermissionRequest request) {
        requireUnit(id);
        unitPermissionService.configure(id, request);
    }

    /**
     * 校验指定用户对给定 unit 的数据权限。
     *
     * <p>F-06：普通用户只能校验自身；校验他人必须持有 {@code knowledge:perm}。
     * 候选 id 数量受限，未知用户统一返回无权限响应（不泄漏用户存在性）。
     *
     * @param currentUser 当前调用者
     * @param userId      待校验用户
     * @param unitIds     待校验 unit（数量受限）
     * @return 授权 / 未授权集合
     */
    public CheckPermissionResponse checkPermissions(CurrentUser currentUser, Long userId, List<Long> unitIds) {
        if (unitIds == null || unitIds.isEmpty()) {
            // 空候选集是合法输入：无待校验对象即无授权/未授权项（保持既有契约，不用 400 惩罚空查询）
            return new CheckPermissionResponse(List.of(), List.of());
        }
        if (unitIds.size() > MAX_CHECK_UNIT_IDS) {
            throw new BusinessException(ErrorCode.PARAM_INVALID,
                    "单次校验的 unit 数量不能超过 " + MAX_CHECK_UNIT_IDS);
        }
        boolean selfCheck = currentUser != null && currentUser.getId().equals(userId);
        if (!selfCheck && (currentUser == null || !currentUser.hasPermission("knowledge:perm"))) {
            throw new BusinessException(ErrorCode.FORBIDDEN, "无权查询其他用户的数据权限");
        }
        User user = userMapper.selectById(userId);
        if (user == null) {
            // 未知对象统一响应：与既有契约一致返回 400，话术不泄漏用户存在性
            throw new BusinessException(ErrorCode.PARAM_INVALID, "目标用户不存在或不可校验");
        }
        CurrentUser target = new CurrentUser(user.getId(), user.getUsername(), user.getDisplayName(),
                user.getDepartmentId(), null, null);
        PermissionDecision decision = dataPermissionEngine.filter(target, unitIds);
        return new CheckPermissionResponse(decision.authorized(), decision.unauthorized());
    }

    /**
     * 检索可见的 unit 业务编码（status=active AND ingest_status=active）。
     *
     * @return unit_code 列表
     */
    public List<String> retrievableUnitCodes() {
        return unitMapper.selectRetrievableUnitCodes();
    }

    /**
     * 未完成的删除任务（F-04 管理端展示失败态）。
     *
     * @param limit 条数上限
     * @return 删除任务视图
     */
    public List<DeleteTaskVO> unfinishedDeletes(int limit) {
        return knowledgeDeleteService.unfinishedDeletes(limit);
    }

    /**
     * R-03 管理端重试失败删除任务（需 knowledge:delete 权限；operatorId 记入审计）。
     *
     * @param unitId     单元 id
     * @param operatorId 操作人 id
     * @return 逐项删除结果
     */
    public DeleteItemResult retryDeleteTask(Long unitId, Long operatorId) {
        return knowledgeDeleteService.retryDeleteTask(unitId, operatorId);
    }

    /**
     * 按主键取实体，不存在抛 400。
     *
     * @param id 主键
     * @return 实体
     */
    public KnowledgeUnit requireUnit(Long id) {
        KnowledgeUnit unit = unitMapper.selectById(id);
        if (unit == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "知识单元不存在");
        }
        return unit;
    }

    private DeleteItemResult deleteOne(Long id) {
        // F-03/F-04：删除编排（CAS 进 deleting → 取消排队作业 → 等 embed 租约 → 删向量 →
        // 清主数据 → 失败退避补偿）统一收敛在 KnowledgeDeleteService
        return knowledgeDeleteService.requestDelete(id);
    }

    private KnowledgeUnitVO toVO(KnowledgeUnit unit, boolean withContent) {
        KnowledgeUnitVO vo = new KnowledgeUnitVO();
        vo.setId(unit.getId());
        vo.setUnitCode(unit.getUnitCode());
        vo.setTitle(unit.getTitle());
        vo.setSummary(unit.getSummary());
        vo.setCategory(unit.getCategory());
        vo.setSourceFileName(unit.getSourceFileName());
        vo.setFileType(unit.getFileType());
        vo.setFileSize(unit.getFileSize());
        vo.setStatus(unit.getStatus());
        vo.setIngestStatus(unit.getIngestStatus());
        vo.setBatchId(unit.getBatchId());
        vo.setCreatorId(unit.getCreatorId());
        vo.setCreatedAt(unit.getCreatedAt());
        vo.setUpdatedAt(unit.getUpdatedAt());
        if (withContent) {
            vo.setContent(unit.getContent());
        }
        return vo;
    }

    /**
     * 记录一次作业入队并返回（供导入服务复用）。
     *
     * @param unit 知识单元
     * @return 作业
     */
    public IngestJob enqueueJob(KnowledgeUnit unit) {
        return ingestJobService.enqueue(unit.getBatchId(), unit.getId(), unit.getCreatorId());
    }
}
