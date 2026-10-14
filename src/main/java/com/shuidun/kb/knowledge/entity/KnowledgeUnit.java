package com.shuidun.kb.knowledge.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 知识单元，映射 knowledge_units 表（docs/services/database.md §2.6）。
 *
 * <p>status（业务可用性）与 ingestStatus（流水线）必须分离；检索可见性只认二者皆为 active。
 */
@Data
public class KnowledgeUnit {

    /** 主键。 */
    private Long id;

    /** 业务编码（全链路对 AI 的 unit_id 形态）。 */
    private String unitCode;

    /** 标题。 */
    private String title;

    /** 正文。 */
    private String content;

    /** 摘要。 */
    private String summary;

    /** 分类。 */
    private String category;

    /** 源文件名。 */
    private String sourceFileName;

    /** 文件类型。 */
    private String fileType;

    /** 文件大小。 */
    private Long fileSize;

    /** 本地存储相对路径。 */
    private String storagePath;

    /** parse 返回的原文哈希（用于 embed 幂等与审计）。 */
    private String contentHash;

    /** parse 返回的切片哈希（embed 一致性校验）。 */
    private String chunksHash;

    /** 是否在导入时让 AI 对 PDF 图片做 VLM 图注：1 是，0 否。 */
    private Integer useVlm;

    /** 业务可用性：active / disabled / deleting / superseded。 */
    private String status;

    /** 流水线：draft / queued / parsing / parsed / embedding / active / failed。 */
    private String ingestStatus;

    /** 导入批次 id。 */
    private String batchId;

    /** 创建人。 */
    private Long creatorId;

    /** 创建时间。 */
    private LocalDateTime createdAt;

    /** 更新时间。 */
    private LocalDateTime updatedAt;

    /**
     * F-03 生命周期版本：内容/状态每次变更 +1。
     *
     * <p>用于 CAS 校验：删除抢占（active/disabled → deleting）、入库最终发布
     * （不得覆盖 disabled/deleting）都依赖它检测"读取后被并发修改"。
     */
    private Long lifecycleVersion;
}
