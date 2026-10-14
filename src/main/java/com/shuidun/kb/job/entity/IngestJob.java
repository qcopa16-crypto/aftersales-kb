package com.shuidun.kb.job.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 入库作业，映射 ingest_jobs 表（docs/services/database.md §2.6.1）。
 */
@Data
public class IngestJob {

    /** 主键。 */
    private Long id;

    /** 导入批次 id。 */
    private String batchId;

    /** 知识单元 id。 */
    private Long unitId;

    /** 阶段：queued / parse / chunk_qc / classify / quality_gate / embed / finalize / done / failed。 */
    private String stage;

    /** 进度 0–100。 */
    private Integer progress;

    /** 错误码，如 PARSE_FAILED / MINERU_TIMEOUT。 */
    private String errorCode;

    /** 可读错误原因 + 建议动作。 */
    private String errorMessage;

    /** 重试次数（≤3）。 */
    private Integer attempt;

    /** 创建人。 */
    private Long createdBy;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;

    /** F-02 当前持有租约的 worker 标识（进程崩溃后据此识别 stale 作业）。 */
    private String claimedBy;

    /** F-02 领取前的可领取阶段（queued / embed）：租约过期回收时回到该阶段。 */
    private String claimedFrom;

    /** F-02 租约到期时间：过期即可被任意实例回收，避免 worker 崩溃后作业永久卡住。 */
    private LocalDateTime leaseUntil;

    /** F-02 最近一次心跳 / 阶段推进时间（观测用）。 */
    private LocalDateTime heartbeatAt;

    /** F-02 退避窗口结束前不可被领取（持久化，多实例共享，替代进程本地 notBefore）。 */
    private LocalDateTime notBefore;
}
