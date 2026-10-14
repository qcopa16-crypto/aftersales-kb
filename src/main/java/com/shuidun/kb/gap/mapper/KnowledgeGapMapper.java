package com.shuidun.kb.gap.mapper;

import com.shuidun.kb.gap.entity.KnowledgeGap;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * 知识缺口 Mapper。
 */
@Mapper
public interface KnowledgeGapMapper {

    /**
     * 分页查询缺口。
     *
     * @param status 状态过滤，可空
     * @param offset 偏移
     * @param size   条数
     * @return 缺口列表
     */
    @Select("<script>SELECT * FROM knowledge_gaps WHERE 1=1 "
            + "<if test='status != null'>AND status = #{status} </if>"
            + "ORDER BY ask_count DESC, last_asked_at DESC LIMIT #{size} OFFSET #{offset}</script>")
    List<KnowledgeGap> selectPage(@Param("status") String status, @Param("offset") int offset, @Param("size") int size);

    /**
     * 统计缺口数。
     *
     * @param status 状态过滤，可空
     * @return 记录数
     */
    @Select("<script>SELECT COUNT(*) FROM knowledge_gaps WHERE 1=1 "
            + "<if test='status != null'>AND status = #{status} </if></script>")
    long countPage(@Param("status") String status);

    /**
     * 按模式查询缺口。
     *
     * @param questionPattern 归一化模式
     * @return 缺口；不存在返回 null
     */
    @Select("SELECT * FROM knowledge_gaps WHERE question_pattern = #{questionPattern} ORDER BY id DESC LIMIT 1")
    KnowledgeGap selectByPattern(String questionPattern);

    /**
     * 按主键查询。
     *
     * @param id 主键
     * @return 缺口；不存在返回 null
     */
    @Select("SELECT * FROM knowledge_gaps WHERE id = #{id}")
    KnowledgeGap selectById(Long id);

    /**
     * 新增缺口。
     *
     * @param gap 实体
     * @return 影响行数
     */
    @Insert("INSERT INTO knowledge_gaps(question_pattern, sample_questions_json, ask_count, last_asked_at, "
            + "status, created_at, updated_at) VALUES(#{questionPattern}, #{sampleQuestionsJson}, #{askCount}, "
            + "CURRENT_TIMESTAMP, #{status}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(KnowledgeGap gap);

    /**
     * 原子累加提问次数并刷新时间戳（F-09 / R-16）。
     *
     * <p>计数在数据库侧自增（{@code ask_count = ask_count + 1}），不再写入应用读到的绝对值——
     * 并发记录同一问题时不会互相覆盖计数。样例问题已规范化到 {@code knowledge_gap_samples}
     * 独立表（R-16），此处不再改写 {@code sample_questions_json}（保留列仅为兼容历史数据）。
     *
     * @param id 主键
     * @return 影响行数
     */
    @Update("UPDATE knowledge_gaps SET ask_count = ask_count + 1, "
            + "last_asked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int incrementCount(@Param("id") Long id);

    /**
     * 记录一条样例问题（R-16）。
     *
     * <p>{@code knowledge_gap_samples} 上 (gap_id, question) 唯一；并发重复插入由唯一约束兜底，
     * 调用方捕获 {@link org.springframework.dao.DuplicateKeyException} 忽略即可。
     *
     * @param gapId    缺口主键
     * @param question 样例问题（已截断到列长度）
     * @return 影响行数
     */
    @Insert("INSERT INTO knowledge_gap_samples(gap_id, question, created_at) "
            + "VALUES(#{gapId}, #{question}, CURRENT_TIMESTAMP)")
    int insertSample(@Param("gapId") Long gapId, @Param("question") String question);

    /**
     * 统计某缺口的样例数（用于容量上限判定）。
     *
     * @param gapId 缺口主键
     * @return 样例条数
     */
    @Select("SELECT COUNT(*) FROM knowledge_gap_samples WHERE gap_id = #{gapId}")
    long countSamples(@Param("gapId") Long gapId);

    /**
     * 按记录顺序读取样例问题（R-16）。
     *
     * @param gapId 缺口主键
     * @param limit 上限
     * @return 样例问题列表
     */
    @Select("SELECT question FROM knowledge_gap_samples WHERE gap_id = #{gapId} ORDER BY id LIMIT #{limit}")
    List<String> selectSamples(@Param("gapId") Long gapId, @Param("limit") int limit);

    /**
     * 更新状态（F-08：resolved 时同时持久化关联的解决单元）。
     *
     * @param id             主键
     * @param status         状态
     * @param resolvedUnitId 关联解决单元（可为 null，表示清空）
     * @return 影响行数
     */
    @Update("UPDATE knowledge_gaps SET status = #{status}, resolved_unit_id = #{resolvedUnitId}, "
            + "updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateStatusAndResolved(@Param("id") Long id, @Param("status") String status,
                               @Param("resolvedUnitId") Long resolvedUnitId);

    /**
     * 更新状态。
     *
     * @param id     主键
     * @param status 状态
     * @return 影响行数
     */
    @Update("UPDATE knowledge_gaps SET status = #{status}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateStatus(@Param("id") Long id, @Param("status") String status);
}
