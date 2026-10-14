package com.shuidun.kb.knowledge.mapper;

import com.shuidun.kb.knowledge.entity.KnowledgeUnitTombstone;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 已删除知识单元墓碑 Mapper（F-03）。
 */
@Mapper
public interface KnowledgeUnitTombstoneMapper {

    /**
     * 写入墓碑（unit_id 唯一，重复删除忽略唯一冲突）。
     *
     * @param unitId   单元 id
     * @param unitCode 业务编码
     * @return 影响行数
     */
    @Insert("INSERT INTO knowledge_unit_tombstones(unit_id, unit_code, deleted_at) "
            + "VALUES(#{unitId}, #{unitCode}, CURRENT_TIMESTAMP)")
    int insert(@Param("unitId") Long unitId, @Param("unitCode") String unitCode);

    /**
     * 拉取到期待重放的墓碑（从未扫描或上次扫描早于截止时间）。
     *
     * @param cutoff 截止时间（now - 重放间隔）
     * @param limit  条数上限
     * @return 墓碑列表
     */
    @Select("SELECT * FROM knowledge_unit_tombstones "
            + "WHERE last_swept_at IS NULL OR last_swept_at < #{cutoff} "
            + "ORDER BY id LIMIT #{limit}")
    List<KnowledgeUnitTombstone> selectSweepCandidates(@Param("cutoff") java.time.LocalDateTime cutoff,
                                                       @Param("limit") int limit);

    /**
     * 标记墓碑已重放。
     *
     * @param id 墓碑 id
     * @return 影响行数
     */
    @Update("UPDATE knowledge_unit_tombstones SET last_swept_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int markSwept(@Param("id") Long id);
}
