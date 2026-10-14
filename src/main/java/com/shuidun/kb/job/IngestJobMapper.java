package com.shuidun.kb.job;

import com.shuidun.kb.job.entity.IngestJob;
import java.time.LocalDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 入库作业 Mapper。
 */
@Mapper
public interface IngestJobMapper {

    /**
     * 新增作业。
     *
     * @param job 作业实体
     * @return 影响行数
     */
    @Insert("INSERT INTO ingest_jobs(batch_id, unit_id, stage, progress, attempt, created_by, created_at, updated_at) "
            + "VALUES(#{batchId}, #{unitId}, #{stage}, #{progress}, #{attempt}, #{createdBy}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(IngestJob job);

    /**
     * 按批次查询作业。
     *
     * @param batchId 批次 id
     * @return 作业列表
     */
    @Select("SELECT * FROM ingest_jobs WHERE batch_id = #{batchId} ORDER BY id")
    List<IngestJob> selectByBatchId(String batchId);

    /**
     * 查询 unit 最新一条作业。
     *
     * @param unitId unit id
     * @return 作业；不存在返回 null
     */
    @Select("SELECT * FROM ingest_jobs WHERE unit_id = #{unitId} ORDER BY id DESC LIMIT 1")
    IngestJob selectLatestByUnitId(Long unitId);

    /**
     * 查询某阶段的作业（Worker 拉取）。
     *
     * <p>退避持久化（F-02）：{@code not_before} 未到期的作业不可见，
     * 避免多实例各自维护本地 notBefore 导致重复领取/立即重试。
     *
     * @param stage 阶段
     * @param limit 条数上限
     * @return 作业列表
     */
    @Select("SELECT * FROM ingest_jobs WHERE stage = #{stage} "
            + "AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP) "
            + "ORDER BY id LIMIT #{limit}")
    List<IngestJob> selectByStage(@Param("stage") String stage, @Param("limit") int limit);

    /**
     * 原子领取作业（乐观锁 + 租约）：仅当作业仍处于期望阶段时才领取。
     *
     * <p>并发场景下两个 Worker 同时 SELECT 到同一作业，只有其中一个的 UPDATE 会命中
     * （{@code stage} 已被对方改写），另一个影响行数为 0 从而放弃处理，保证同一作业
     * 不会被重复领取执行。领取同时写入 {@code claimed_by / claimed_from / lease_until /
     * heartbeat_at}，为后续心跳与 stale 回收提供依据（F-02）。
     *
     * @param id            作业 id
     * @param expectedStage 期望的当前阶段（可领取阶段）
     * @param claimedStage  领取后写入的阶段标记
     * @param claimedBy     领取者标识（workerId）
     * @param leaseUntil    租约到期时间
     * @return 影响行数：1 表示领取成功，0 表示已被其他 Worker 领走
     */
    @Update("UPDATE ingest_jobs SET stage = #{claimedStage}, claimed_by = #{claimedBy}, "
            + "claimed_from = #{expectedStage}, lease_until = #{leaseUntil}, heartbeat_at = CURRENT_TIMESTAMP, "
            + "updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND stage = #{expectedStage}")
    int claim(@Param("id") Long id, @Param("expectedStage") String expectedStage,
              @Param("claimedStage") String claimedStage, @Param("claimedBy") String claimedBy,
              @Param("leaseUntil") LocalDateTime leaseUntil);

    /**
     * 心跳续租：仅当作业仍由本人持有时延长租约。
     *
     * <p>返回 0 表示租约已被回收/易主（worker 崩溃后被其他实例回收），
     * 当前处理方必须立即中止，避免与回收方的执行相互覆盖。
     *
     * @param id         作业 id
     * @param claimedBy  当前处理方标识
     * @param leaseUntil 新的租约到期时间
     * @return 影响行数：1 续租成功，0 租约丢失
     */
    @Update("UPDATE ingest_jobs SET lease_until = #{leaseUntil}, heartbeat_at = CURRENT_TIMESTAMP, "
            + "updated_at = CURRENT_TIMESTAMP WHERE id = #{id} AND claimed_by = #{claimedBy} "
            + "AND stage NOT IN ('done', 'failed')")
    int heartbeat(@Param("id") Long id, @Param("claimedBy") String claimedBy,
                  @Param("leaseUntil") LocalDateTime leaseUntil);

    /**
     * 拉取租约已过期的在途作业（F-02 stale 回收候选）。
     *
     * <p>只覆盖「处理中」阶段（claimed 及各真实处理阶段）；queued 未被领取无需回收，
     * quality_gate 在等待人工确认、done/failed 为终态，均不属于 stale。
     *
     * @param now   当前时间
     * @param limit 条数上限
     * @return 过期在途作业列表
     */
    @Select("SELECT * FROM ingest_jobs WHERE stage IN ('claimed', 'parse', 'chunk_qc', 'classify', "
            + "'auto_review', 'embed', 'finalize') AND lease_until IS NOT NULL AND lease_until < #{now} "
            + "ORDER BY id LIMIT #{limit}")
    List<IngestJob> selectStaleInFlight(@Param("now") LocalDateTime now, @Param("limit") int limit);

    /**
     * 原子回收过期租约作业：把作业重置回 {@code claimed_from} 阶段并清空租约字段。
     *
     * <p>CAS 条件 {@code lease_until < now} 保证同一作业不会被两个回收方同时重置。
     *
     * @param id  作业 id
     * @param now 当前时间
     * @return 影响行数：1 回收成功，0 已被回收或租约已续
     */
    @Update("UPDATE ingest_jobs SET stage = CASE WHEN claimed_from IS NULL OR claimed_from = '' "
            + "THEN 'queued' ELSE claimed_from END, claimed_by = '', claimed_from = '', lease_until = NULL, "
            + "heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND lease_until IS NOT NULL AND lease_until < #{now} "
            + "AND stage IN ('claimed', 'parse', 'chunk_qc', 'classify', 'auto_review', 'embed', 'finalize')")
    int reclaimExpiredLease(@Param("id") Long id, @Param("now") LocalDateTime now);

    /**
     * 取消某单元尚未被领取的入库作业（queued / embed 可领取阶段）。
     *
     * <p>F-03：删除单元时先把排队中的重新入库意图取消，避免删除后又被领取执行。
     * 已领取（有租约）的作业不在此取消——其最终发布 CAS 会因单元进入 deleting 而失败，
     * 由删除补偿任务等待其租约结束后再删向量，保证删除与向量写入的先后顺序。
     *
     * @param unitId      单元 id
     * @param errorCode   取消原因码
     * @param errorMessage 取消原因
     * @return 影响行数
     */
    @Update("UPDATE ingest_jobs SET stage = 'failed', error_code = #{errorCode}, error_message = #{errorMessage}, "
            + "claimed_by = '', claimed_from = '', lease_until = NULL, heartbeat_at = NULL, "
            + "updated_at = CURRENT_TIMESTAMP "
            + "WHERE unit_id = #{unitId} AND stage IN ('queued', 'embed')")
    int cancelClaimableForUnit(@Param("unitId") Long unitId, @Param("errorCode") String errorCode,
                               @Param("errorMessage") String errorMessage);

    /**
     * 统计某单元仍在途（有有效租约、非终态）的入库作业数。
     *
     * <p>F-03：删除向量前必须确认无有效 embed/ingest lease，否则迟到 embed 会把
     * 已删除知识的向量重新写回向量库（孤儿向量）。
     *
     * @param unitId 单元 id
     * @return 在途作业数
     */
    @Select("SELECT COUNT(*) FROM ingest_jobs WHERE unit_id = #{unitId} "
            + "AND stage IN ('claimed', 'parse', 'chunk_qc', 'classify', 'auto_review', 'embed', 'finalize') "
            + "AND lease_until IS NOT NULL AND lease_until >= CURRENT_TIMESTAMP")
    int countInFlightForUnit(@Param("unitId") Long unitId);

    /**
     * R-01 owner + 有效租约 CAS 的阶段推进（同时延长租约）。
     *
     * <p>把 stage/progress/error/lease 合并为一条原子 SQL：仅当作业仍由 {@code claimedBy} 持有
     * 且租约尚未过期时才写入，并顺带把租约延长到 {@code leaseUntil}。影响行数为 0 表示
     * 租约已丢失（被回收/易主/已终态），调用方必须立即中止处理，绝不能继续写入。
     *
     * @param job        作业实体（stage/progress/attempt 取自实体）
     * @param claimedBy  当前处理方标识（owner）
     * @param leaseUntil 新的租约到期时间
     * @return 影响行数：1 成功，0 租约丢失
     */
    @Update("UPDATE ingest_jobs SET stage = #{job.stage}, progress = #{job.progress}, "
            + "error_code = NULL, error_message = NULL, attempt = #{job.attempt}, not_before = NULL, "
            + "lease_until = #{leaseUntil}, heartbeat_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND claimed_by = #{claimedBy} "
            + "AND lease_until IS NOT NULL AND lease_until > CURRENT_TIMESTAMP "
            + "AND stage NOT IN ('done', 'failed')")
    int advanceOwned(@Param("job") IngestJob job, @Param("claimedBy") String claimedBy,
                     @Param("leaseUntil") LocalDateTime leaseUntil);

    /**
     * R-01 owner + 有效租约 CAS 的失败终态：以当前 owner + 非终态为条件，并在同一语句清空租约。
     *
     * @param job        作业实体（errorCode/errorMessage 取自实体）
     * @param claimedBy  当前处理方标识（owner）
     * @return 影响行数：1 成功，0 租约丢失
     */
    @Update("UPDATE ingest_jobs SET stage = 'failed', error_code = #{job.errorCode}, "
            + "error_message = #{job.errorMessage}, not_before = NULL, claimed_by = '', claimed_from = '', "
            + "lease_until = NULL, heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND claimed_by = #{claimedBy} "
            + "AND lease_until IS NOT NULL AND lease_until > CURRENT_TIMESTAMP "
            + "AND stage NOT IN ('done', 'failed')")
    int markFailedOwned(@Param("job") IngestJob job, @Param("claimedBy") String claimedBy);

    /**
     * R-01 owner + 有效租约 CAS 的完成终态：以当前 owner + 非终态为条件，并在同一语句清空租约。
     *
     * @param job       作业实体
     * @param claimedBy 当前处理方标识（owner）
     * @return 影响行数：1 成功，0 租约丢失
     */
    @Update("UPDATE ingest_jobs SET stage = 'done', progress = 100, error_code = NULL, error_message = NULL, "
            + "not_before = NULL, claimed_by = '', claimed_from = '', lease_until = NULL, heartbeat_at = NULL, "
            + "updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND claimed_by = #{claimedBy} "
            + "AND lease_until IS NOT NULL AND lease_until > CURRENT_TIMESTAMP "
            + "AND stage NOT IN ('done', 'failed')")
    int markDoneOwned(@Param("job") IngestJob job, @Param("claimedBy") String claimedBy);

    /**
     * R-01 owner + 有效租约 CAS 的延迟重排（可重试失败）：重置回可领取阶段并清租约。
     *
     * @param job       作业实体（stage/attempt/notBefore 取自实体）
     * @param claimedBy 当前处理方标识（owner）
     * @return 影响行数：1 成功，0 租约丢失
     */
    @Update("UPDATE ingest_jobs SET stage = #{job.stage}, progress = 0, error_code = NULL, "
            + "error_message = NULL, attempt = #{job.attempt}, not_before = #{job.notBefore}, "
            + "claimed_by = '', claimed_from = '', lease_until = NULL, heartbeat_at = NULL, "
            + "updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND claimed_by = #{claimedBy} "
            + "AND lease_until IS NOT NULL AND lease_until > CURRENT_TIMESTAMP "
            + "AND stage NOT IN ('done', 'failed')")
    int resetForRetryOwned(@Param("job") IngestJob job, @Param("claimedBy") String claimedBy);

    /**
     * 非租约方（人工质检门放行）推进阶段：以「当前阶段 == expectedStage」为 CAS，并在同一语句清租约。
     *
     * <p>人工门是独立的授权动作（非 Worker 持有租约期间），但仍必须防止旧 Worker 的过期租约残留、
     * 或作业已被回收/易主后被错误推进。
     *
     * @param id            作业 id
     * @param expectedStage 期望的当前阶段（如 quality_gate）
     * @param stage         新阶段
     * @param progress      进度
     * @return 影响行数：1 成功，0 阶段已变
     */
    @Update("UPDATE ingest_jobs SET stage = #{stage}, progress = #{progress}, error_code = NULL, "
            + "error_message = NULL, not_before = NULL, claimed_by = '', claimed_from = '', "
            + "lease_until = NULL, heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND stage = #{expectedStage} AND stage NOT IN ('done', 'failed')")
    int advanceFromStage(@Param("id") Long id, @Param("expectedStage") String expectedStage,
                         @Param("stage") String stage, @Param("progress") int progress);

    /**
     * 非租约方（管理端/测试）标记失败：以「非终态」为 CAS，并在同一语句清租约。
     *
     * <p>Worker 处理路径必须使用 {@link #markFailedOwned}，本方法仅用于无租约的行政/清理场景。
     *
     * @param job 作业实体
     * @return 影响行数
     */
    @Update("UPDATE ingest_jobs SET stage = 'failed', error_code = #{job.errorCode}, "
            + "error_message = #{job.errorMessage}, not_before = NULL, claimed_by = '', claimed_from = '', "
            + "lease_until = NULL, heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND stage NOT IN ('done', 'failed')")
    int markFailedUnowned(@Param("job") IngestJob job);

    /**
     * R-04 准入拒绝的**守卫写**：仅当作业仍处于 {@code queued}（尚未被任何 worker claim）时才落 failed。
     *
     * <p>用于 Worker 在 {@code claim} 之前的准入拒绝（file_size 缺失/不一致、实际大小无法确认等）：
     * 以 {@code stage = 'queued'} 为 CAS，避免「A 节点因瞬时 IO 失败拒绝」与「B 节点同刻 claim 并
     * 开始 parse」的竞态下，A 的写把 B 正在处理的作业错误置为 failed。若已被 claim（stage 已变），
     * 影响行数为 0，由 claim 方（owner）自行处理。
     *
     * @param job 作业实体（errorCode/errorMessage 取自实体）
     * @return 影响行数：1 表示仍为 queued 并已置失败；0 表示已被 claim/终态，不得覆盖
     */
    @Update("UPDATE ingest_jobs SET stage = 'failed', error_code = #{job.errorCode}, "
            + "error_message = #{job.errorMessage}, not_before = NULL, claimed_by = '', claimed_from = '', "
            + "lease_until = NULL, heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{job.id} AND stage = 'queued'")
    int markFailedIfQueued(@Param("job") IngestJob job);

    /**
     * 非租约方（管理端）把失败作业重排回可领取阶段重试：以「stage = failed」为 CAS。
     *
     * @param id        作业 id
     * @param stage     重试起始阶段
     * @param attempt   新的尝试次数
     * @param notBefore 下次可见时间；null 表示立即可领取
     * @return 影响行数：1 成功，0 当前非失败态
     */
    @Update("UPDATE ingest_jobs SET stage = #{stage}, progress = 0, error_code = NULL, error_message = NULL, "
            + "attempt = #{attempt}, not_before = #{notBefore}, claimed_by = '', claimed_from = '', "
            + "lease_until = NULL, heartbeat_at = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND stage = 'failed'")
    int resetFailedForRetry(@Param("id") Long id, @Param("stage") String stage,
                            @Param("attempt") int attempt, @Param("notBefore") LocalDateTime notBefore);
}
