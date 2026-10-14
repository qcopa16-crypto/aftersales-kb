package com.shuidun.kb.knowledge.mapper;

import com.shuidun.kb.knowledge.entity.KnowledgeDeleteTask;
import java.time.LocalDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 知识删除补偿任务 Mapper（F-04 outbox）。
 */
@Mapper
public interface KnowledgeDeleteTaskMapper {

    /**
     * 新增删除任务（unit_id 唯一；并发重复插入由唯一约束兜底）。
     *
     * @param task 任务实体
     * @return 影响行数
     */
    @Insert("INSERT INTO knowledge_delete_tasks(unit_id, unit_code, status, retry_count, next_retry_at, "
            + "last_error, created_at, updated_at) VALUES(#{unitId}, #{unitCode}, #{status}, #{retryCount}, "
            + "#{nextRetryAt}, #{lastError}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(KnowledgeDeleteTask task);

    /**
     * 按单元查询删除任务。
     *
     * @param unitId 单元 id
     * @return 任务；不存在返回 null
     */
    @Select("SELECT * FROM knowledge_delete_tasks WHERE unit_id = #{unitId}")
    KnowledgeDeleteTask selectByUnitId(@Param("unitId") Long unitId);

    /**
     * 拉取到期待处理任务（pending / waiting 且退避已过期、无有效处理租约）。
     *
     * @param now   当前时间
     * @param limit 条数上限
     * @return 任务列表
     */
    @Select("SELECT * FROM knowledge_delete_tasks WHERE status IN ('pending', 'waiting') "
            + "AND (next_retry_at IS NULL OR next_retry_at <= #{now}) "
            + "AND (lease_until IS NULL OR lease_until <= #{now}) ORDER BY id LIMIT #{limit}")
    List<KnowledgeDeleteTask> selectDue(@Param("now") LocalDateTime now, @Param("limit") int limit);

    /**
     * R-03 原子抢占到期任务：登记 worker_id + claim_token + lease_until。
     *
     * <p>仅当任务仍为 pending/waiting、退避已过期且无有效租约（未被其他消费者持有）时才抢占成功。
     * 影响行数为 0 表示已被其他实例领走或尚未到期，调用方必须放弃处理。
     *
     * @param id         任务 id
     * @param workerId   消费者标识
     * @param claimToken 本次抢占令牌
     * @param leaseUntil 处理租约到期时间
     * @param now        当前时间
     * @return 影响行数：1 抢占成功，0 未抢到
     */
    @Update("UPDATE knowledge_delete_tasks SET claimed_by = #{workerId}, claim_token = #{claimToken}, "
            + "lease_until = #{leaseUntil}, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND status IN ('pending', 'waiting') "
            + "AND (next_retry_at IS NULL OR next_retry_at <= #{now}) "
            + "AND (lease_until IS NULL OR lease_until <= #{now})")
    int claim(@Param("id") Long id, @Param("workerId") String workerId,
              @Param("claimToken") String claimToken, @Param("leaseUntil") LocalDateTime leaseUntil,
              @Param("now") LocalDateTime now);

    /**
     * R-03 终态 CAS：仅当仍由本次抢占者（worker_id + claim_token）持有时标记成功并清租约。
     *
     * @param id         任务 id
     * @param workerId   消费者标识
     * @param claimToken 本次抢占令牌
     * @return 影响行数：1 成功，0 租约已丢失/已被接管
     */
    @Update("UPDATE knowledge_delete_tasks SET status = 'succeeded', next_retry_at = NULL, last_error = NULL, "
            + "claimed_by = '', claim_token = '', lease_until = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND claimed_by = #{workerId} AND claim_token = #{claimToken} "
            + "AND status <> 'succeeded'")
    int completeOwned(@Param("id") Long id, @Param("workerId") String workerId,
                      @Param("claimToken") String claimToken);

    /**
     * R-03 重排/失败 CAS：仅当仍由本次抢占者持有时更新状态、重试计数与下次可见时间并清租约。
     *
     * @param id           任务 id
     * @param workerId     消费者标识
     * @param claimToken   本次抢占令牌
     * @param status       新状态（pending / waiting / failed）
     * @param retryCount   新的重试计数
     * @param nextRetryAt  下次可见时间；null 表示立即可见
     * @param lastError    最近错误/等待原因
     * @return 影响行数：1 成功，0 租约已丢失/已被接管
     */
    @Update("UPDATE knowledge_delete_tasks SET status = #{status}, retry_count = #{retryCount}, "
            + "next_retry_at = #{nextRetryAt}, last_error = #{lastError}, "
            + "claimed_by = '', claim_token = '', lease_until = NULL, updated_at = CURRENT_TIMESTAMP "
            + "WHERE id = #{id} AND claimed_by = #{workerId} AND claim_token = #{claimToken}")
    int rescheduleOwned(@Param("id") Long id, @Param("workerId") String workerId,
                        @Param("claimToken") String claimToken, @Param("status") String status,
                        @Param("retryCount") int retryCount, @Param("nextRetryAt") LocalDateTime nextRetryAt,
                        @Param("lastError") String lastError);

    /**
     * R-03 管理端重试入口：仅失败态可重排回 pending，并重置退避计数与租约。
     *
     * @param unitId 单元 id
     * @return 影响行数：1 成功，0 当前非失败态
     */
    @Update("UPDATE knowledge_delete_tasks SET status = 'pending', retry_count = 0, next_retry_at = NULL, "
            + "last_error = NULL, claimed_by = '', claim_token = '', lease_until = NULL, "
            + "updated_at = CURRENT_TIMESTAMP WHERE unit_id = #{unitId} AND status = 'failed'")
    int resetFailedForRetry(@Param("unitId") Long unitId);

    /**
     * 未完成的任务（管理端展示失败态与最近错误，F-04）。
     *
     * @param limit 条数上限
     * @return 任务列表
     */
    @Select("SELECT * FROM knowledge_delete_tasks WHERE status IN ('pending', 'waiting', 'failed') "
            + "ORDER BY updated_at DESC, id DESC LIMIT #{limit}")
    List<KnowledgeDeleteTask> selectUnfinished(@Param("limit") int limit);
}
