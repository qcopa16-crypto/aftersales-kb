package com.shuidun.kb.knowledge.mapper;

import com.shuidun.kb.knowledge.entity.KnowledgeChunk;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 知识切片 Mapper（冗余存储，便于排查）。
 */
@Mapper
public interface KnowledgeChunkMapper {

    /**
     * 批量写入切片。
     *
     * @param chunks 切片列表（非空）
     * @return 影响行数
     */
    @Insert("<script>INSERT INTO knowledge_chunks(unit_id, chunk_index, content, heading_path, created_at) VALUES "
            + "<foreach collection='chunks' item='c' separator=','>"
            + "(#{c.unitId}, #{c.chunkIndex}, #{c.content}, #{c.headingPathJson}, CURRENT_TIMESTAMP)</foreach>"
            + "</script>")
    int insertBatch(@Param("chunks") List<KnowledgeChunk> chunks);

    /**
     * 删除某 unit 的全部切片。
     *
     * @param unitId unit id
     * @return 影响行数
     */
    @Delete("DELETE FROM knowledge_chunks WHERE unit_id = #{unitId}")
    int deleteByUnitId(Long unitId);

    /**
     * 查询某 unit 的切片（按序号）。
     *
     * <p>注意：必须把 {@code heading_path} 显式别名为 {@code heading_path_json}，
     * 否则 map-underscore-to-camel-case 会尝试映射到不存在的 {@code headingPath} 属性，
     * 导致 {@code headingPathJson} 恒为 null，进而在 embed 时丢失 heading_path
     * 触发 409 CHUNKS_HASH_MISMATCH。
     *
     * @param unitId unit id
     * @return 切片列表
     */
    @Select("SELECT id, unit_id, chunk_index, content, heading_path AS heading_path_json, created_at "
            + "FROM knowledge_chunks WHERE unit_id = #{unitId} ORDER BY chunk_index")
    List<KnowledgeChunk> selectByUnitId(Long unitId);

    /**
     * 批量查询多个 unit 的切片（按 unit_id, chunk_index 排序，便于调用方分组复用）。
     *
     * <p>调用方须保证 {@code unitIds} 非空（空集合会生成非法 {@code IN ()}）。
     * 同一 unit 内的切片顺序与 {@link #selectByUnitId} 一致，保证抽样语义不变。
     *
     * @param unitIds unit id 列表（非空）
     * @return 切片列表
     */
    @Select("<script>SELECT id, unit_id, chunk_index, content, heading_path AS heading_path_json, created_at "
            + "FROM knowledge_chunks WHERE unit_id IN "
            + "<foreach collection='unitIds' item='uid' open='(' separator=',' close=')'>#{uid}</foreach> "
            + "ORDER BY unit_id, chunk_index</script>")
    List<KnowledgeChunk> selectByUnitIds(@Param("unitIds") List<Long> unitIds);
}
