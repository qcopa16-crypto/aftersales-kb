package com.shuidun.kb.job;

import com.shuidun.kb.common.BusinessException;
import com.shuidun.kb.common.ErrorCode;
import com.shuidun.kb.job.entity.IngestJob;
import java.time.LocalDateTime;
import java.util.List;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 入库作业服务（DESIGN.md §6.2）：作业入队、阶段推进、失败与重试。
 *
 * <p>F-02 租约模型：Worker 领取作业时写入 {@code claimed_by / lease_until}，阶段推进即心跳续租；
 * 进程崩溃或租约过期后，作业可被任意实例回收（{@link #reclaimExpiredLeases()}）回到
 * {@code claimed_from} 阶段重新执行，不再永久卡住。可重试失败的退避时间持久化到
 * {@code not_before}，多实例共享，替代进程本地延迟表。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class IngestJobService {

    /** 流水线起始阶段。 */
    public static final String STAGE_QUEUED = "queued";

    /** 解析阶段。 */
    public static final String STAGE_PARSE = "parse";

    /** 切片质检阶段。 */
    public static final String STAGE_CHUNK_QC = "chunk_qc";

    /** 分类建议阶段。 */
    public static final String STAGE_CLASSIFY = "classify";

    /** AI 自动质检阶段（瞬时阶段，不进 Worker 领取集）。 */
    public static final String STAGE_AUTO_REVIEW = "auto_review";

    /** 人工质检门阶段。 */
    public static final String STAGE_QUALITY_GATE = "quality_gate";

    /** 向量化阶段。 */
    public static final String STAGE_EMBED = "embed";

    /** 收尾阶段。 */
    public static final String STAGE_FINALIZE = "finalize";

    /** 完成。 */
    public static final String STAGE_DONE = "done";

    /** 失败。 */
    public static final String STAGE_FAILED = "failed";

    /**
     * 领取中间态：Worker 通过乐观锁把作业从可领取阶段（queued/embed）推进到该标记，
     * 使其他 Worker 无法重复领取；随即被 {@code advance()} 覆盖为真实处理阶段。
     */
    public static final String STAGE_CLAIMED = "claimed";

    /** 最大重试次数。 */
    public static final int MAX_ATTEMPT = 3;

    /** stale 回收单轮上限：避免一次轮询回收过多作业。 */
    private static final int RECLAIM_LIMIT = 50;

    private final IngestJobMapper ingestJobMapper;

    /**
     * 创建作业并入队。
     *
     * @param batchId   批次 id
     * @param unitId    unit id
     * @param createdBy 创建人
     * @return 已创建的作业
     */
    public IngestJob enqueue(String batchId, Long unitId, Long createdBy) {
        IngestJob job = new IngestJob();
        job.setBatchId(batchId);
        job.setUnitId(unitId);
        job.setStage(STAGE_QUEUED);
        job.setProgress(0);
        job.setAttempt(0);
        job.setCreatedBy(createdBy);
        ingestJobMapper.insert(job);
        log.info("enqueue ingest job jobId={} unitId={} batchId={}", job.getId(), unitId, batchId);
        return job;
    }

    /**
     * 查询 unit 最新作业。
     *
     * @param unitId unit id
     * @return 作业；不存在返回 null
     */
    public IngestJob latestByUnit(Long unitId) {
        return ingestJobMapper.selectLatestByUnitId(unitId);
    }

    /**
     * 查询批次内全部作业。
     *
     * @param batchId 批次 id
     * @return 作业列表
     */
    public List<IngestJob> byBatch(String batchId) {
        return ingestJobMapper.selectByBatchId(batchId);
    }

    /**
     * 拉取指定阶段的作业（{@code not_before} 未到期的退避作业自动不可见）。
     *
     * @param stage 阶段
     * @param limit 条数上限
     * @return 作业列表
     */
    public List<IngestJob> pick(String stage, int limit) {
        return ingestJobMapper.selectByStage(stage, limit);
    }

    /**
     * 原子领取作业（乐观锁 + 租约）。
     *
     * <p>仅当作业当前仍处于 {@code expectedStage} 时，才把它推进到 {@link #STAGE_CLAIMED}
     * 并登记领取者与租约。并发下只有一个 Worker 能领取成功，其余返回 {@code false} 并应放弃处理。
     *
     * @param job           作业（来自 {@link #pick}）
     * @param expectedStage 期望的当前阶段（queued / embed）
     * @param claimedBy     领取者标识（workerId）
     * @param leaseUntil    租约到期时间
     * @return true 表示领取成功
     */
    public boolean claim(IngestJob job, String expectedStage, String claimedBy, LocalDateTime leaseUntil) {
        boolean claimed = ingestJobMapper.claim(job.getId(), expectedStage, STAGE_CLAIMED, claimedBy, leaseUntil) == 1;
        if (claimed) {
            job.setStage(STAGE_CLAIMED);
            job.setClaimedBy(claimedBy);
            job.setClaimedFrom(expectedStage);
            job.setLeaseUntil(leaseUntil);
        }
        return claimed;
    }

    /**
     * 心跳续租：仅当作业仍由本人持有时延长租约。
     *
     * @param job        作业
     * @param claimedBy  当前处理方标识
     * @param leaseUntil 新的租约到期时间
     * @return true 续租成功；false 表示租约已丢失（被回收/易主），调用方必须中止处理
     */
    public boolean heartbeat(IngestJob job, String claimedBy, LocalDateTime leaseUntil) {
        boolean ok = ingestJobMapper.heartbeat(job.getId(), claimedBy, leaseUntil) == 1;
        if (ok) {
            job.setLeaseUntil(leaseUntil);
        }
        return ok;
    }

    /**
     * 推进作业阶段与进度（无租约方调用，如人工质检门放行）。
     *
     * <p>R-01：以「当前阶段 == 作业读到的阶段」为 CAS 并在同一语句清租约，防止作业已被
     * 回收/易主后被错误推进。
     *
     * @param job      作业
     * @param stage    新阶段
     * @param progress 进度
     */
    public void advance(IngestJob job, String stage, int progress) {
        String expectedStage = job.getStage();
        int updated = ingestJobMapper.advanceFromStage(job.getId(), expectedStage, stage, progress);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID,
                    "作业阶段已变更，无法推进（当前期望阶段: " + expectedStage + "）");
        }
        job.setStage(stage);
        job.setProgress(progress);
        job.setErrorCode(null);
        job.setErrorMessage(null);
        job.setNotBefore(null);
        job.setClaimedBy("");
        job.setLeaseUntil(null);
    }

    /**
     * 推进作业阶段与进度，并以阶段推进作为心跳续租（R-01 owner + 有效租约 CAS）。
     *
     * <p>把 stage/progress/error/lease 合并为一条 SQL：仅当作业仍由 {@code claimedBy} 持有
     * 且租约未过期时才写入，同时延长租约。影响行数为 0（租约被回收/易主/已终态）时抛出
     * {@link LeaseLostException}，中止当前处理，绝不在 CAS 失败后继续写入。
     *
     * @param job        作业
     * @param stage      新阶段
     * @param progress   进度
     * @param claimedBy  当前处理方标识
     * @param leaseUntil 新的租约到期时间
     * @throws LeaseLostException 租约丢失
     */
    public void advance(IngestJob job, String stage, int progress, String claimedBy, LocalDateTime leaseUntil)
            throws LeaseLostException {
        job.setStage(stage);
        job.setProgress(progress);
        job.setErrorCode(null);
        job.setErrorMessage(null);
        job.setNotBefore(null);
        if (ingestJobMapper.advanceOwned(job, claimedBy, leaseUntil) != 1) {
            throw new LeaseLostException(job.getId());
        }
        job.setLeaseUntil(leaseUntil);
    }

    /**
     * 标记作业失败（R-01 owner + 有效租约 CAS，同一语句清空租约）。
     *
     * <p>影响行数为 0（租约丢失/已终态）时抛出 {@link LeaseLostException}：旧 worker 不得把
     * 新 owner 的作业标记为失败。
     *
     * @param job          作业
     * @param errorCode    错误码
     * @param errorMessage 可读原因与建议动作
     * @param claimedBy    当前处理方标识
     * @throws LeaseLostException 租约丢失
     */
    public void markFailed(IngestJob job, String errorCode, String errorMessage, String claimedBy)
            throws LeaseLostException {
        job.setStage(STAGE_FAILED);
        job.setErrorCode(errorCode);
        job.setErrorMessage(errorMessage);
        job.setNotBefore(null);
        if (ingestJobMapper.markFailedOwned(job, claimedBy) != 1) {
            throw new LeaseLostException(job.getId());
        }
        job.setClaimedBy("");
        job.setLeaseUntil(null);
        log.warn("ingest job failed jobId={} unitId={} code={} workerId={}",
                job.getId(), job.getUnitId(), errorCode, claimedBy);
    }

    /**
     * 标记作业失败（无租约方：管理端/清理）。
     *
     * <p>R-01：以「非终态」为 CAS 并在同一语句清租约；Worker 处理路径必须使用
     * {@link #markFailed(IngestJob, String, String, String)}。
     *
     * @param job          作业
     * @param errorCode    错误码
     * @param errorMessage 可读原因与建议动作
     */
    public void markFailed(IngestJob job, String errorCode, String errorMessage) {
        job.setStage(STAGE_FAILED);
        job.setErrorCode(errorCode);
        job.setErrorMessage(errorMessage);
        job.setNotBefore(null);
        ingestJobMapper.markFailedUnowned(job);
        log.warn("ingest job failed (unowned) jobId={} unitId={} code={}", job.getId(), job.getUnitId(), errorCode);
    }

    /**
     * R-04 准入拒绝的守卫写：仅当作业仍为 {@code queued}（尚未被 claim）时才落失败态。
     *
     * <p>Worker 在 {@code claim} 之前的准入拒绝必须用本方法，而非无条件的
     * {@link #markFailed(IngestJob, String, String)}：以 {@code stage='queued'} 为 CAS，避免
     * 「A 节点准入拒绝」与「B 节点同刻 claim 并开始处理」竞态下跨 owner 覆盖 B 的作业。
     * 若已被 claim，影响行数为 0，交由 claim 方（owner）处理。
     *
     * @param job          作业
     * @param errorCode    错误码
     * @param errorMessage 可读原因与建议动作
     * @return true=仍为 queued 并已置失败（调用方可同步置 unit 失败）；false=已被 claim/终态，未改动
     */
    public boolean markFailedIfQueued(IngestJob job, String errorCode, String errorMessage) {
        job.setStage(STAGE_FAILED);
        job.setErrorCode(errorCode);
        job.setErrorMessage(errorMessage);
        job.setNotBefore(null);
        int updated = ingestJobMapper.markFailedIfQueued(job);
        if (updated == 1) {
            log.warn("ingest job failed (queued-guard) jobId={} unitId={} code={}",
                    job.getId(), job.getUnitId(), errorCode);
        } else {
            log.warn("skip admission reject: job no longer queued (already claimed/terminal) jobId={} unitId={} code={}",
                    job.getId(), job.getUnitId(), errorCode);
        }
        return updated == 1;
    }

    /**
     * 标记作业完成（R-01 owner + 有效租约 CAS，同一语句清空租约）。
     *
     * @param job       作业
     * @param claimedBy 当前处理方标识
     * @throws LeaseLostException 租约丢失
     */
    public void markDone(IngestJob job, String claimedBy) throws LeaseLostException {
        job.setStage(STAGE_DONE);
        job.setProgress(100);
        job.setErrorCode(null);
        job.setErrorMessage(null);
        job.setNotBefore(null);
        if (ingestJobMapper.markDoneOwned(job, claimedBy) != 1) {
            throw new LeaseLostException(job.getId());
        }
        job.setClaimedBy("");
        job.setLeaseUntil(null);
    }

    /**
     * 管理端把失败作业重置以便重试（无持久化退避）。
     *
     * <p>R-01：以「stage = failed」为 CAS；仅失败态可被重排，避免管理端重排正在处理中的作业。
     *
     * @param job   作业
     * @param stage 重试起始阶段
     */
    public void resetForRetry(IngestJob job, String stage) {
        int attempt = (job.getAttempt() == null ? 0 : job.getAttempt()) + 1;
        int updated = ingestJobMapper.resetFailedForRetry(job.getId(), stage, attempt, null);
        if (updated == 0) {
            throw new BusinessException(ErrorCode.PARAM_INVALID, "仅失败作业可重试，当前阶段: " + job.getStage());
        }
        job.setStage(stage);
        job.setProgress(0);
        job.setErrorCode(null);
        job.setErrorMessage(null);
        job.setNotBefore(null);
        job.setAttempt(attempt);
        job.setClaimedBy("");
        job.setLeaseUntil(null);
        log.info("reset failed ingest job for retry jobId={} stage={} attempt={}", job.getId(), stage, attempt);
    }

    /**
     * 重置作业以便从指定阶段重试，并持久化下次可见时间（R-01 owner + 有效租约 CAS）。
     *
     * <p>退避落在数据库 {@code not_before} 列而不是进程内存：多实例部署下各 Worker
     * 都能遵守同一可见时间，避免本地 notBefore 导致的重复领取或立即重试。
     *
     * @param job       作业
     * @param stage     重试起始阶段
     * @param notBefore 下次可见时间；null 表示立即可领取
     * @param claimedBy 当前处理方标识
     * @throws LeaseLostException 租约丢失
     */
    public void resetForRetry(IngestJob job, String stage, LocalDateTime notBefore, String claimedBy)
            throws LeaseLostException {
        job.setStage(stage);
        job.setProgress(0);
        job.setErrorCode(null);
        job.setErrorMessage(null);
        job.setNotBefore(notBefore);
        job.setAttempt((job.getAttempt() == null ? 0 : job.getAttempt()) + 1);
        if (ingestJobMapper.resetForRetryOwned(job, claimedBy) != 1) {
            throw new LeaseLostException(job.getId());
        }
        job.setClaimedBy("");
        job.setLeaseUntil(null);
        log.info("reset ingest job for retry jobId={} stage={} attempt={} notBefore={} workerId={}",
                job.getId(), stage, job.getAttempt(), notBefore, claimedBy);
    }

    /**
     * 回收租约过期的在途作业（F-02 stale recovery）。
     *
     * <p>worker 崩溃/被杀后，其持有的在途作业（claimed/parse/.../finalize）租约会到期；
     * 任意实例都能把这些作业原子重置回 {@code claimed_from} 阶段重新执行。
     *
     * @return 本轮回收的作业数
     */
    public int reclaimExpiredLeases() {
        LocalDateTime now = LocalDateTime.now();
        int reclaimed = 0;
        for (IngestJob stale : ingestJobMapper.selectStaleInFlight(now, RECLAIM_LIMIT)) {
            if (ingestJobMapper.reclaimExpiredLease(stale.getId(), now) == 1) {
                reclaimed++;
                log.warn("reclaimed stale ingest job jobId={} unitId={} previousOwner={} stage={}",
                        stale.getId(), stale.getUnitId(), stale.getClaimedBy(), stale.getStage());
            }
        }
        return reclaimed;
    }

    /**
     * 取消某单元尚未被领取的入库作业（F-03：删除时取消排队中的重新入库意图）。
     *
     * @param unitId 单元 id
     */
    public void cancelClaimableJobsForUnit(Long unitId) {
        ingestJobMapper.cancelClaimableForUnit(unitId, "UNIT_DELETING", "单元正在删除，排队中的入库作业已取消");
    }

    /**
     * 统计某单元仍在途（有有效租约）的入库作业数（F-03：删向量前必须为 0）。
     *
     * @param unitId 单元 id
     * @return 在途作业数
     */
    public int countInFlightJobsForUnit(Long unitId) {
        return ingestJobMapper.countInFlightForUnit(unitId);
    }

    /**
     * 内部控制流信号：处理中租约丢失（作业被回收或已终态），必须立即中止当前处理。
     *
     * <p>非错误（关闭栈追踪）：此时作业已归其他执行方，本地任何写入都可能与之冲突。
     */
    public static class LeaseLostException extends RuntimeException {

        private static final long serialVersionUID = 1L;

        public LeaseLostException(Long jobId) {
            super("ingest job lease lost jobId=" + jobId, null, false, false);
        }
    }
}
