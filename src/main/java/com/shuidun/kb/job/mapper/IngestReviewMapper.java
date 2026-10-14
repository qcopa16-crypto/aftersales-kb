package com.shuidun.kb.job.mapper;

import com.shuidun.kb.job.entity.IngestReview;
import java.time.LocalDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 入库质检审计 Mapper（ingest_reviews）。
 */
@Mapper
public interface IngestReviewMapper {

    /**
     * 新增一条审计记录。
     *
     * @param review 审计实体
     * @return 影响行数
     */
    @Insert("INSERT INTO ingest_reviews(unit_id, job_id, batch_id, category, decision, decider, decider_user_id, "
            + "rules_verdict, llm_verdict, llm_confidence, vlm_verdict, review_reasons, detail_json, ai_model, "
            + "llm_ms, vlm_ms, created_at) "
            + "VALUES(#{unitId}, #{jobId}, #{batchId}, #{category}, #{decision}, #{decider}, #{deciderUserId}, "
            + "#{rulesVerdict}, #{llmVerdict}, #{llmConfidence}, #{vlmVerdict}, #{reviewReasons}, #{detailJson}, "
            + "#{aiModel}, #{llmMs}, #{vlmMs}, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(IngestReview review);

    /**
     * 查询 unit 最新一条审计记录。
     *
     * @param unitId unit id
     * @return 审计记录；不存在返回 null
     */
    @Select("SELECT * FROM ingest_reviews WHERE unit_id = #{unitId} ORDER BY id DESC LIMIT 1")
    IngestReview selectLatestByUnitId(Long unitId);

    /**
     * 批量查询每个 unit 的最新一条审计记录（批次详情用）。
     *
     * @param unitIds unit id 列表（非空）
     * @return 每个 unit 的最新审计记录
     */
    @Select({"<script>",
            "SELECT r.* FROM ingest_reviews r JOIN (",
            "  SELECT unit_id, MAX(id) AS max_id FROM ingest_reviews WHERE unit_id IN ",
            "  <foreach collection='unitIds' item='id' open='(' separator=',' close=')'>#{id}</foreach>",
            "  GROUP BY unit_id",
            ") t ON r.id = t.max_id",
            "</script>"})
    List<IngestReview> selectLatestByUnitIds(@Param("unitIds") List<Long> unitIds);

    /**
     * 取窗口内审计行（阶段 4 度量）。
     *
     * <p><b>跨库兼容</b>：SQL 只做「时间过滤 + 倒序 + 截断」三件事，聚合一律在 Java 侧完成，
     * 不使用任何 MySQL 专有函数（{@code JSON_EXTRACT} / {@code CAST(... AS UNSIGNED)} / {@code DATE_FORMAT} 等），
     * 保证 H2 与 MySQL 双库行为一致（与 {@code DashboardService.trend} 同口径）。
     *
     * @param since 窗口起点（含）
     * @param limit 最多返回行数（按 id 倒序，取最近的行）
     * @return 审计行（仅含度量所需列；未选列在实体中为 null）
     */
    @Select("SELECT decision, decider, decider_user_id, category, ai_model, review_reasons, detail_json, "
            + "llm_ms, vlm_ms, created_at FROM ingest_reviews "
            + "WHERE created_at >= #{since} ORDER BY id DESC LIMIT #{limit}")
    List<IngestReview> selectSince(@Param("since") LocalDateTime since, @Param("limit") int limit);
}
