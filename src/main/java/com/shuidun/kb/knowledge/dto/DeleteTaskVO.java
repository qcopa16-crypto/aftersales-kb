package com.shuidun.kb.knowledge.dto;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 删除补偿任务视图（管理端展示删除失败态与最近错误，F-04）。
 */
@Data
public class DeleteTaskVO {

    /** 单元 id。 */
    private Long unitId;

    /** 单元业务编码。 */
    private String unitCode;

    /** 任务状态：pending / waiting / failed。 */
    private String status;

    /** 已重试次数。 */
    private Integer retryCount;

    /** 最近一次失败/等待原因。 */
    private String lastError;

    /** 下次重试时间。 */
    private LocalDateTime nextRetryAt;

    /** 最近更新时间。 */
    private LocalDateTime updatedAt;
}
