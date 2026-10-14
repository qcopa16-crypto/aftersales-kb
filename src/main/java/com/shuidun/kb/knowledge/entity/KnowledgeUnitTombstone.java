package com.shuidun.kb.knowledge.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 已删除知识单元墓碑（F-03），映射 knowledge_unit_tombstones 表。
 *
 * <p>主数据删除后保留单元编码：迟到 embed（worker 崩溃前发出的向量写入）可能把孤儿向量
 * 写回向量库，墓碑使补偿扫描可以反复重放 AI DELETE 清理，直到确认无残留。
 */
@Data
public class KnowledgeUnitTombstone {

    /** 主键。 */
    private Long id;

    /** 被删除的单元 id。 */
    private Long unitId;

    /** 被删除单元的业务编码。 */
    private String unitCode;

    /** 删除完成时间。 */
    private LocalDateTime deletedAt;

    /** 最近一次墓碑重放扫描时间。 */
    private LocalDateTime lastSweptAt;
}
