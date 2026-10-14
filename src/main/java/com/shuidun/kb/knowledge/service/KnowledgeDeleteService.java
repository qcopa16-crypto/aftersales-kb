package com.shuidun.kb.knowledge.service;

import com.shuidun.kb.ai.AiClient;
import com.shuidun.kb.ai.AiException;
import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.job.IngestJobService;
import com.shuidun.kb.knowledge.dto.DeleteItemResult;
import com.shuidun.kb.knowledge.dto.DeleteTaskVO;
import com.shuidun.kb.knowledge.entity.KnowledgeDeleteTask;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.entity.KnowledgeUnitTombstone;
import com.shuidun.kb.knowledge.mapper.KnowledgeChunkMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeDeleteTaskMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitTombstoneMapper;
import com.shuidun.kb.permission.UnitPermissionService;
import jakarta.annotation.PostConstruct;
import java.net.InetAddress;
import java.time.LocalDateTime;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 知识删除服务（DESIGN.md §6.6 + F-03/F-04/R-03）。
 *
 * <p>删除是跨 MySQL / 向量库的多步流程，任何单点失败都必须可恢复：
 * <ol>
 *   <li>R-03：在同一数据库事务内 CAS 标记 {@code deleting} 并写入唯一 outbox task，
 *       消除「标记后进程退出、无任务」的永久 deleting 窗口；</li>
 *   <li>取消排队中的入库作业（queued/embed 可领取阶段），避免删除后又被领取执行；</li>
 *   <li>R-03：消费者通过原子 UPDATE 抢占 pending/waiting 任务（worker_id + claim_token +
 *       lease_until，过期可回收），多实例/定时扫描并发时只有一个 owner；</li>
 *   <li>等待在途 embed 租约结束——没有有效 lease 后才删向量，杜绝迟到 embed 写回孤儿向量；</li>
 *   <li>幂等重放 AI DELETE；成功后清权限/切片/主数据，并写可重复的 tombstone（忽略唯一冲突）；</li>
 *   <li>终态/重排使用 owner + token CAS；失败按退避重试，超限落 {@code failed}，
 *       并提供带授权与审计的人工重试入口。</li>
 * </ol>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class KnowledgeDeleteService {

    /** 删除任务退避基数（毫秒）：第 n 次重试 = base &lt;&lt; min(n, 6)。 */
    @Value("${app.knowledge.delete.retry-backoff-ms:30000}")
    private long retryBackoffMs;

    /** 最大补偿重试次数；超限任务落 failed（需人工介入/告警）。 */
    @Value("${app.knowledge.delete.max-retries:20}")
    private int maxRetries;

    /** 单轮扫描处理的到期任务数。 */
    @Value("${app.knowledge.delete.batch-size:10}")
    private int batchSize;

    /** tombstone 重放间隔（毫秒）：周期性重放 AI DELETE 清理孤儿向量。 */
    @Value("${app.knowledge.delete.sweep-interval-ms:3600000}")
    private long sweepIntervalMs;

    /** R-03 消费者处理租约时长（毫秒）：处理期间持有，过期后其他实例可回收重试。 */
    @Value("${app.knowledge.delete.claim-lease-ms:300000}")
    private long claimLeaseMs;

    /** R-03 可选稳定消费者标识；缺省用 主机名+随机后缀。 */
    @Value("${app.knowledge.delete.worker-id:}")
    private String configuredWorkerId;

    /** 本消费者标识：抢占/日志统一使用。 */
    private String workerId;

    private final AiClient aiClient;
    private final KnowledgeUnitMapper unitMapper;
    private final KnowledgeChunkMapper chunkMapper;
    private final KnowledgeDeleteTaskMapper deleteTaskMapper;
    private final KnowledgeUnitTombstoneMapper tombstoneMapper;
    private final IngestJobService ingestJobService;
    private final UnitPermissionService unitPermissionService;
    private final PlatformTransactionManager transactionManager;

    /** R-03 编程式事务模板：用于把 markDeleting 与 outbox task 创建收敛到同一事务。 */
    private TransactionTemplate transactionTemplate;

    @PostConstruct
    void init() {
        this.transactionTemplate = new TransactionTemplate(transactionManager);
        this.workerId = (configuredWorkerId == null || configuredWorkerId.isBlank())
                ? defaultWorkerId() : configuredWorkerId.trim();
        log.info("delete consumer started workerId={} claimLeaseMs={}", this.workerId, this.claimLeaseMs);
    }

    private static String defaultWorkerId() {
        String host;
        try {
            host = InetAddress.getLocalHost().getHostName();
        } catch (java.net.UnknownHostException ex) {
            host = "unknown-host";
        }
        return host + "-" + UUID.randomUUID().toString().substring(0, 8);
    }

    /**
     * 请求删除单个单元（同步入口，逐项返回、禁止假成功）。
     *
     * <p>R-03：标记 deleting 与写入 outbox task 在同一事务内完成；随后在事务外抢占并处理任务
     * （跨服务 AI 调用不应占用数据库事务）。
     *
     * @param id 单元 id
     * @return 逐项删除结果
     */
    public DeleteItemResult requestDelete(Long id) {
        PendingDelete pending = transactionTemplate.execute(status -> prepareDelete(id));
        if (pending == null) {
            return new DeleteItemResult(id, false, "DELETE_PENDING", "删除排队中，请稍后刷新查看结果");
        }
        if (pending.earlyResult() != null) {
            return pending.earlyResult();
        }
        return claimAndProcess(pending.task());
    }

    /**
     * 事务内：CAS 标记 deleting → 取消排队作业 → 创建 outbox task。
     *
     * @param id 单元 id
     * @return 待处理任务或提前返回结果
     */
    private PendingDelete prepareDelete(Long id) {
        KnowledgeUnit unit = unitMapper.selectById(id);
        if (unit == null) {
            return new PendingDelete(null, new DeleteItemResult(id, false, "UNIT_NOT_FOUND", "知识单元不存在"));
        }
        int marked = unitMapper.markDeleting(id);
        if (marked == 0) {
            // 并发删除已在进行（或单元已不在可删除状态）：返回任务当前进度，绝不重复触发
            KnowledgeDeleteTask existing = deleteTaskMapper.selectByUnitId(id);
            if (existing != null) {
                return new PendingDelete(null, resultFromTask(existing));
            }
            return new PendingDelete(null,
                    new DeleteItemResult(id, false, "DELETE_IN_PROGRESS", "该单元正在删除中，请稍后刷新查看结果"));
        }
        // 取消排队中的重新入库意图；在途（有租约）作业不取消，其发布 CAS 会失败，
        // 删除流程在下方等待其租约结束后再删向量
        ingestJobService.cancelClaimableJobsForUnit(id);
        KnowledgeDeleteTask task = ensureTask(id, unit.getUnitCode());
        return new PendingDelete(task, null);
    }

    /**
     * 补偿消费者：扫描到期删除任务并重试（F-04 后台补偿）。
     */
    @Scheduled(fixedDelayString = "${app.knowledge.delete.scan-interval-ms:10000}",
            initialDelayString = "${app.knowledge.delete.scan-initial-delay-ms:0}")
    public void scanDueTasks() {
        LocalDateTime now = LocalDateTime.now();
        for (KnowledgeDeleteTask task : deleteTaskMapper.selectDue(now, batchSize)) {
            try {
                claimAndProcess(task);
            } catch (RuntimeException ex) {
                log.warn("process delete task failed unitId={}: {}", task.getUnitId(), ex.getMessage());
            }
        }
    }

    /**
     * R-03 reconcile 扫描：修复「单元处于 deleting 但无 outbox task」的崩溃窗口。
     */
    @Scheduled(fixedDelayString = "${app.knowledge.delete.reconcile-interval-ms:60000}",
            initialDelayString = "${app.knowledge.delete.reconcile-initial-delay-ms:0}")
    public void reconcileDeletingWithoutTask() {
        for (KnowledgeUnit unit : unitMapper.selectDeletingWithoutTask(batchSize)) {
            try {
                KnowledgeDeleteTask task = ensureTask(unit.getId(), unit.getUnitCode());
                log.warn("reconciled deleting unit without delete task unitId={} taskId={}",
                        unit.getId(), task == null ? null : task.getId());
            } catch (RuntimeException ex) {
                log.warn("reconcile deleting unit failed unitId={}: {}", unit.getId(), ex.getMessage());
            }
        }
    }

    /**
     * 墓碑重放扫描：对迟到 embed 可能写回的孤儿向量周期性重放 AI DELETE（F-03）。
     *
     * <p>重放失败**不标记** {@code last_swept_at}：该墓碑在下一轮扫描中会再次命中并重试，
     * 无需新增退避列（失败到下一轮的时间由 {@code sweep-interval-ms} 决定）。
     */
    @Scheduled(fixedDelayString = "${app.knowledge.delete.sweep-interval-ms:3600000}",
            initialDelayString = "${app.knowledge.delete.sweep-initial-delay-ms:0}")
    public void sweepTombstones() {
        LocalDateTime cutoff = LocalDateTime.now().minus(sweepIntervalMs, ChronoUnit.MILLIS);
        for (KnowledgeUnitTombstone tombstone : tombstoneMapper.selectSweepCandidates(cutoff, batchSize)) {
            boolean replayed = true;
            try {
                aiClient.deleteVectors(tombstone.getUnitCode());
            } catch (AiException ex) {
                replayed = false;
                log.warn("tombstone sweep replay failed unitCode={} code={}",
                        tombstone.getUnitCode(), ex.getAiErrorCode());
            } catch (RuntimeException ex) {
                replayed = false;
                log.warn("tombstone sweep replay failed unitCode={}: {}", tombstone.getUnitCode(), ex.getMessage());
            }
            if (replayed) {
                tombstoneMapper.markSwept(tombstone.getId());
            }
        }
    }

    /**
     * R-03 管理端重试入口：仅失败任务可重排回 pending（重置退避计数），随后立即尝试处理。
     *
     * <p>调用方须持有 {@code knowledge:delete} 权限；operatorId 记入审计日志。
     *
     * @param unitId     单元 id
     * @param operatorId 操作人 id（审计）
     * @return 逐项删除结果
     */
    public DeleteItemResult retryDeleteTask(Long unitId, Long operatorId) {
        KnowledgeDeleteTask task = deleteTaskMapper.selectByUnitId(unitId);
        if (task == null) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "该单元无删除任务");
        }
        if (!KnowledgeDeleteTask.STATUS_FAILED.equals(task.getStatus())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "仅失败任务可重试，当前状态: " + task.getStatus());
        }
        if (deleteTaskMapper.resetFailedForRetry(unitId) == 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "任务状态已变更，请刷新后重试");
        }
        log.warn("delete task manually retried unitId={} operatorId={}", unitId, operatorId);
        task.setStatus(KnowledgeDeleteTask.STATUS_PENDING);
        task.setRetryCount(0);
        task.setNextRetryAt(null);
        task.setLastError(null);
        return claimAndProcess(task);
    }

    /** 管理端：未完成的删除任务（失败态 + 最近错误 + 下次重试）。 */
    public List<DeleteTaskVO> unfinishedDeletes(int limit) {
        List<DeleteTaskVO> result = new ArrayList<>();
        for (KnowledgeDeleteTask task : deleteTaskMapper.selectUnfinished(limit)) {
            DeleteTaskVO vo = new DeleteTaskVO();
            vo.setUnitId(task.getUnitId());
            vo.setUnitCode(task.getUnitCode());
            vo.setStatus(task.getStatus());
            vo.setRetryCount(task.getRetryCount());
            vo.setLastError(task.getLastError());
            vo.setNextRetryAt(task.getNextRetryAt());
            vo.setUpdatedAt(task.getUpdatedAt());
            result.add(vo);
        }
        return result;
    }

    /**
     * R-03 原子抢占任务后处理；未抢到则返回当前进度。
     *
     * @param task 候选任务
     * @return 逐项删除结果
     */
    private DeleteItemResult claimAndProcess(KnowledgeDeleteTask task) {
        if (task == null) {
            return new DeleteItemResult(null, false, "DELETE_PENDING", "删除任务不存在");
        }
        if (KnowledgeDeleteTask.STATUS_SUCCEEDED.equals(task.getStatus())) {
            return new DeleteItemResult(task.getUnitId(), true, null, null);
        }
        LocalDateTime now = LocalDateTime.now();
        String token = UUID.randomUUID().toString();
        LocalDateTime lease = now.plus(claimLeaseMs, ChronoUnit.MILLIS);
        int claimed = deleteTaskMapper.claim(task.getId(), workerId, token, lease, now);
        if (claimed == 0) {
            KnowledgeDeleteTask latest = deleteTaskMapper.selectByUnitId(task.getUnitId());
            return latest != null ? resultFromTask(latest)
                    : new DeleteItemResult(task.getUnitId(), false, "DELETE_PENDING", "删除排队中，后台将自动完成");
        }
        task.setClaimedBy(workerId);
        task.setClaimToken(token);
        task.setLeaseUntil(lease);
        return processTask(task);
    }

    /**
     * 处理单个已抢占任务：等待在途 lease → 删向量 → 幂等清理主数据 → 终态 CAS。
     *
     * @param task 任务（须已持有 worker_id + claim_token）
     * @return 逐项删除结果
     */
    DeleteItemResult processTask(KnowledgeDeleteTask task) {
        if (KnowledgeDeleteTask.STATUS_SUCCEEDED.equals(task.getStatus())) {
            return new DeleteItemResult(task.getUnitId(), true, null, null);
        }
        // F-03：存在有效 embed/ingest lease 时不得删向量，等待其结束（迟到写入先于删除完成）
        int inFlight = ingestJobService.countInFlightJobsForUnit(task.getUnitId());
        if (inFlight > 0) {
            String note = "等待进行中的入库任务结束（" + inFlight + " 个在途作业）";
            reschedule(task, KnowledgeDeleteTask.STATUS_WAITING,
                    task.getRetryCount() == null ? 0 : task.getRetryCount(),
                    LocalDateTime.now().plus(retryBackoffMs, ChronoUnit.MILLIS), note);
            return new DeleteItemResult(task.getUnitId(), false, "DELETE_PENDING",
                    "删除排队中：等待进行中的入库任务结束后自动完成");
        }
        try {
            int deleted = aiClient.deleteVectors(task.getUnitCode());
            log.info("deleted vectors unitId={} unitCode={} count={}", task.getUnitId(), task.getUnitCode(), deleted);
        } catch (AiException ex) {
            return markRetry(task, ex.getAiErrorCode() == null ? "DELETE_FAILED" : ex.getAiErrorCode(),
                    ex.getMessage());
        }
        // 向量已确认删除：幂等清理主数据（权限/切片/单元 + tombstone）。任一步异常留在 pending
        // 由下一轮重试——所有步骤均可重复执行，tombstone 冲突忽略。
        try {
            unitPermissionService.replace(task.getUnitId(), List.of());
            chunkMapper.deleteByUnitId(task.getUnitId());
            unitMapper.deleteById(task.getUnitId());
            insertTombstoneIdempotent(task);
        } catch (RuntimeException ex) {
            return markRetry(task, "CLEANUP_FAILED", ex.getMessage());
        }
        if (deleteTaskMapper.completeOwned(task.getId(), task.getClaimedBy(), task.getClaimToken()) == 0) {
            // 租约已被回收/接管：清理已幂等完成，交由新 owner 收尾，不谎报成功
            log.warn("delete task completion CAS lost unitId={} workerId={}", task.getUnitId(), task.getClaimedBy());
            return new DeleteItemResult(task.getUnitId(), false, "DELETE_PENDING", "删除进行中，请稍后刷新");
        }
        task.setStatus(KnowledgeDeleteTask.STATUS_SUCCEEDED);
        task.setNextRetryAt(null);
        task.setLastError(null);
        log.info("knowledge unit deleted unitId={} unitCode={}", task.getUnitId(), task.getUnitCode());
        return new DeleteItemResult(task.getUnitId(), true, null, null);
    }

    /** R-03 重排/终态：owner + token CAS 更新任务状态并清租约。 */
    private void reschedule(KnowledgeDeleteTask task, String status, int retryCount,
                            LocalDateTime nextRetryAt, String lastError) {
        int rows = deleteTaskMapper.rescheduleOwned(task.getId(), task.getClaimedBy(), task.getClaimToken(),
                status, retryCount, nextRetryAt, lastError);
        if (rows == 0) {
            log.warn("delete task reschedule CAS lost unitId={} workerId={} status={}",
                    task.getUnitId(), task.getClaimedBy(), status);
        }
        task.setStatus(status);
        task.setRetryCount(retryCount);
        task.setNextRetryAt(nextRetryAt);
        task.setLastError(lastError);
    }

    /** AI DELETE / 清理失败：退避重试；超限落 failed（管理端告警）。 */
    private DeleteItemResult markRetry(KnowledgeDeleteTask task, String code, String detail) {
        int retryCount = (task.getRetryCount() == null ? 0 : task.getRetryCount()) + 1;
        String lastError = code + ": " + detail;
        if (retryCount >= maxRetries) {
            reschedule(task, KnowledgeDeleteTask.STATUS_FAILED, retryCount, null, lastError);
            log.error("delete task exceeded max retries unitId={} unitCode={} retries={} lastError={}",
                    task.getUnitId(), task.getUnitCode(), retryCount, lastError);
        } else {
            long backoffMs = retryBackoffMs << Math.min(retryCount - 1, 6);
            reschedule(task, KnowledgeDeleteTask.STATUS_PENDING, retryCount,
                    LocalDateTime.now().plus(backoffMs, ChronoUnit.MILLIS), lastError);
        }
        log.error("delete vectors failed unitId={} unitCode={} code={} retry={}",
                task.getUnitId(), task.getUnitCode(), code, retryCount);
        return new DeleteItemResult(task.getUnitId(), false, code,
                "向量删除失败，单元保持 deleting 待补偿: " + detail);
    }

    /** 幂等写入 tombstone：唯一冲突（已写过）视为成功。 */
    private void insertTombstoneIdempotent(KnowledgeDeleteTask task) {
        try {
            tombstoneMapper.insert(task.getUnitId(), task.getUnitCode());
        } catch (DuplicateKeyException ex) {
            log.debug("tombstone already present unitId={}", task.getUnitId());
        }
    }

    /** 确保存在删除任务（并发重复插入由唯一约束兜底；已存在则原样返回，不静默重置失败态）。 */
    private KnowledgeDeleteTask ensureTask(Long unitId, String unitCode) {
        KnowledgeDeleteTask existing = deleteTaskMapper.selectByUnitId(unitId);
        if (existing != null) {
            return existing;
        }
        KnowledgeDeleteTask task = new KnowledgeDeleteTask();
        task.setUnitId(unitId);
        task.setUnitCode(unitCode);
        task.setStatus(KnowledgeDeleteTask.STATUS_PENDING);
        task.setRetryCount(0);
        try {
            deleteTaskMapper.insert(task);
        } catch (DuplicateKeyException ex) {
            // 依赖 MySQL/H2 的**语句级回滚**语义：唯一键冲突只回滚该 INSERT，事务本身仍可继续。
            // 注意：迁到 PostgreSQL 会因语句失败而 abort 整个事务（后续 SELECT 报 "current transaction is
            // aborted"），届时须改为 `INSERT ... ON CONFLICT DO NOTHING` 或先查后插。
            task = deleteTaskMapper.selectByUnitId(unitId);
        }
        return task;
    }

    /** 已完成 / 进行中任务的当前结果（重复删除请求的幂等响应）。 */
    private DeleteItemResult resultFromTask(KnowledgeDeleteTask task) {
        if (KnowledgeDeleteTask.STATUS_SUCCEEDED.equals(task.getStatus())) {
            return new DeleteItemResult(task.getUnitId(), true, null, null);
        }
        String code = KnowledgeDeleteTask.STATUS_FAILED.equals(task.getStatus()) ? "DELETE_FAILED" : "DELETE_PENDING";
        String message = KnowledgeDeleteTask.STATUS_FAILED.equals(task.getStatus())
                ? "删除失败超过重试上限，请人工介入: " + task.getLastError()
                : "删除排队中，后台将自动完成（" + task.getLastError() + "）";
        return new DeleteItemResult(task.getUnitId(), false, code, message);
    }

    /**
     * 删除前校验：处于 deleting / superseded 的单元不允许再修改（F-03 防复活）。
     *
     * @param unit 单元
     */
    public static void requireModifiable(KnowledgeUnit unit) {
        if ("deleting".equals(unit.getStatus()) || "superseded".equals(unit.getStatus())) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "单元正在删除或已下线，不能修改");
        }
    }

    /** 事务内准备结果：待处理任务或提前返回结果。 */
    private record PendingDelete(KnowledgeDeleteTask task, DeleteItemResult earlyResult) {
    }
}
