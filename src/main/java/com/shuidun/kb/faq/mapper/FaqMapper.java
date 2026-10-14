package com.shuidun.kb.faq.mapper;

import com.shuidun.kb.faq.entity.Faq;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * FAQ Mapper。
 */
@Mapper
public interface FaqMapper {

    /**
     * 分页查询 FAQ。
     *
     * @param status 状态过滤，可空
     * @param offset 偏移
     * @param size   条数
     * @return FAQ 列表
     */
    @Select("<script>SELECT * FROM faqs WHERE 1=1 "
            + "<if test='status != null'>AND status = #{status} </if>"
            + "ORDER BY updated_at DESC, id DESC LIMIT #{size} OFFSET #{offset}</script>")
    List<Faq> selectPage(@Param("status") String status, @Param("offset") int offset, @Param("size") int size);

    /**
     * 统计 FAQ 数。
     *
     * @param status 状态过滤，可空
     * @return 记录数
     */
    @Select("<script>SELECT COUNT(*) FROM faqs WHERE 1=1 "
            + "<if test='status != null'>AND status = #{status} </if></script>")
    long countPage(@Param("status") String status);

    /**
     * 按主键查询。
     *
     * @param id 主键
     * @return FAQ；不存在返回 null
     */
    @Select("SELECT * FROM faqs WHERE id = #{id}")
    Faq selectById(Long id);

    /**
     * 按规范化问题键查询已发布 FAQ（F-25：TTL 过期后的 MySQL 回源）。
     *
     * @param questionNorm 规范化问题键
     * @return published FAQ；不存在返回 null
     */
    @Select("SELECT * FROM faqs WHERE question_norm = #{questionNorm} AND status = 'published' LIMIT 1")
    Faq selectPublishedByNorm(@Param("questionNorm") String questionNorm);

    /**
     * 按原始问题精确匹配已发布 FAQ（R-04 纵深防御）。
     *
     * <p>当 Java 规范化键与 SQL 回填的 {@code question_norm} 因 Unicode 特例/规则差异而不一致时，
     * {@link #selectPublishedByNorm} 会静默 miss；本方法按原始问题精确匹配作为兜底回源。
     *
     * @param question 原始问题
     * @return published FAQ；不存在返回 null
     */
    @Select("SELECT * FROM faqs WHERE question = #{question} AND status = 'published' ORDER BY id DESC LIMIT 1")
    Faq selectPublishedByQuestion(@Param("question") String question);

    /**
     * 新增 FAQ。
     *
     * @param faq 实体
     * @return 影响行数
     */
    @Insert("INSERT INTO faqs(question, question_norm, answer, category, related_unit_id, source_type, status, hit_count, "
            + "created_at, updated_at) VALUES(#{question}, #{questionNorm}, #{answer}, #{category}, #{relatedUnitId}, "
            + "#{sourceType}, #{status}, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Faq faq);

    /**
     * 更新审核结果。
     *
     * @param faq 实体
     * @return 影响行数
     */
    @Update("UPDATE faqs SET question = #{question}, question_norm = #{questionNorm}, answer = #{answer}, "
            + "status = #{status}, reviewer_id = #{reviewerId}, "
            + "reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int updateReview(Faq faq);

    /**
     * 更新已发布 FAQ 内容。
     *
     * @param faq 实体
     * @return 影响行数
     */
    @Update("UPDATE faqs SET question = #{question}, question_norm = #{questionNorm}, answer = #{answer}, "
            + "category = #{category}, status = #{status}, updated_at = CURRENT_TIMESTAMP WHERE id = #{id}")
    int update(Faq faq);
}
