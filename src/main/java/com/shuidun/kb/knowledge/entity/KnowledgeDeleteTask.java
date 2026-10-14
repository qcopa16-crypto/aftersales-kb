package com.shuidun.kb.knowledge.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 知识删除补偿任务（F-04 outbox），映射 knowledge_delete_tasks 表。
 *
 * <p>删除流程：单元 CAS 进入 deleting → 取消排队中的入库作业 → 等待在途 embed 租约结束 →
 * 幂等重放 AI DELETE → 清理 MySQL 主数据。任一步骤失败都持久化到本表，由后台消费者
 * 按退避重试，杜绝"停在 deleting 无人负责"。
 */
@Data
public class KnowledgeDeleteTask {

    /** 待处理（可立即处理或退避等待中）。 */
    public static final String STATUS_PENDING = "pending";

    /** 等待在途入库作业（有效 embed lease）结束。 */
    public static final String STATUS_WAITING = "waiting";

    /** 删除完成（向量与主数据均已清理）。 */
    public static final String STATUS_SUCCEEDED = "succeeded";

    /** 重试超过上限，需人工介入（告警）。 */
    public static final String STATUS_FAILED = "failed";

    /** 主键。 */
    private Long id;

    /** 被删除的单元 id。 */
    private Long unitId;

    /** 删除时的业务编码（AI DELETE 的 unit_id）。 */
    private String unitCode;

    /** 状态：pending / waiting / succeeded / failed。 */
    private String status;

    /** 补偿重试次数。 */
    private Integer retryCount;

    /** 下次重试时间（退避）。 */
    private LocalDateTime nextRetryAt;

    /** 最近一次失败/等待原因（管理端展示）。 */
    private String lastError;

    /** R-03 当前持有处理租约的消费者标识。 */
    private String claimedBy;

    /** R-03 本次抢占令牌：终态/重排 CAS 必须匹配，防止过期抢占者覆盖新 owner。 */
    private String claimToken;

    /** R-03 处理租约到期时间；过期后任务可被任意实例回收。 */
    private LocalDateTime leaseUntil;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;
}
