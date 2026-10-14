package com.shuidun.kb.knowledge.entity;

import java.time.LocalDateTime;
import lombok.Data;

/**
 * 知识切片冗余，映射 knowledge_chunks 表（权威向量在向量库）。
 */
@Data
public class KnowledgeChunk {

    /** 主键。 */
    private Long id;

    /** 所属 unit id。 */
    private Long unitId;

    /** 切片序号。 */
    private Integer chunkIndex;

    /** 切片正文。 */
    private String content;

    /**
     * 标题路径（JSON 数组字符串）。
     *
     * <p>必须持久化：质检门确认后需从库中重建切片再调 embed，
     * 而 AI 侧的 chunks_hash 由 chunk_index+content+heading_path 计算，
     * 缺 heading_path 会导致 409 CHUNKS_HASH_MISMATCH。
     */
    private String headingPathJson;

    /** 创建时间。 */
    private LocalDateTime createdAt;
}
