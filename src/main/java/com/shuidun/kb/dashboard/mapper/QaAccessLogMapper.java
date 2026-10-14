package com.shuidun.kb.dashboard.mapper;

import com.shuidun.kb.dashboard.entity.QaAccessLog;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * 问答访问日志与看板统计 Mapper。
 */
@Mapper
public interface QaAccessLogMapper {

    /**
     * 写入访问日志。
     *
     * @param log 日志实体
     * @return 影响行数
     */
    @Insert("INSERT INTO qa_access_logs(session_id, user_id, question, answer, recalled_unit_ids_json, "
            + "authorized_unit_ids_json, unauthorized_unit_ids_json, prompt_tokens, completion_tokens, "
            + "total_tokens, response_time_ms, retrieve_ms, generate_ms, db_ms, cache_ms, chosen_unit_id, "
            + "answer_source, faq_hit, created_at) "
            + "VALUES(#{sessionId}, #{userId}, #{question}, #{answer}, #{recalledUnitIdsJson}, "
            + "#{authorizedUnitIdsJson}, #{unauthorizedUnitIdsJson}, #{promptTokens}, #{completionTokens}, "
            + "#{totalTokens}, #{responseTimeMs}, #{retrieveMs}, #{generateMs}, #{dbMs}, #{cacheMs}, "
            + "#{chosenUnitId}, #{answerSource}, #{faqHit}, CURRENT_TIMESTAMP)")
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(QaAccessLog log);

    /**
     * 看板总览指标（列名保持 snake_case，跨 H2/MySQL 一致）。
     *
     * @return 单行指标
     */
    @Select("SELECT COUNT(*) AS qa_count, COUNT(DISTINCT user_id) AS uv, "
            + "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
            + "COALESCE(ROUND(AVG(response_time_ms)), 0) AS avg_response_ms FROM qa_access_logs")
    Map<String, Object> selectMetrics();

    /**
     * 高频问题 TOP。
     *
     * @param limit 条数
     * @return 问题与次数
     */
    @Select("SELECT question, COUNT(*) AS ask_count FROM qa_access_logs "
            + "WHERE answer_source <> 'llm_general' "
            + "GROUP BY question ORDER BY COUNT(*) DESC, question LIMIT #{limit}")
    List<Map<String, Object>> selectTopQuestions(@Param("limit") int limit);

    /**
     * 取最近若干条授权 unit 列表 JSON，用于在 Java 侧统计热门知识（跨库可移植）。
     *
     * @param limit 条数
     * @return JSON 字符串列表
     */
    @Select("SELECT authorized_unit_ids_json FROM qa_access_logs "
            + "WHERE authorized_unit_ids_json IS NOT NULL ORDER BY id DESC LIMIT #{limit}")
    List<String> selectRecentAuthorizedJson(@Param("limit") int limit);

    /**
     * 取指定时间之后的耗时样本，用于在 Java 侧按 小时/天/周/月 分桶（跨库可移植）。
     *
     * <p>必须带上 {@code faq_hit}：耗时分析要用它单独统计「FAQ 直出」的平均端到端耗时。
     *
     * @param since 起始时间
     * @return 每行含 created_at / total_tokens / response_time_ms / retrieve_ms / generate_ms / db_ms / cache_ms / faq_hit
     */
    @Select("SELECT created_at, total_tokens, response_time_ms, retrieve_ms, generate_ms, db_ms, cache_ms, faq_hit "
            + "FROM qa_access_logs WHERE created_at >= #{since} ORDER BY created_at")
    List<Map<String, Object>> selectSince(@Param("since") LocalDateTime since);

    /**
     * 最慢的问题 TOP（耗时分析用）。
     *
     * @param limit 条数
     * @return 每行含 question / response_time_ms / retrieve_ms / generate_ms / created_at
     */
    @Select("SELECT question, response_time_ms, retrieve_ms, generate_ms, db_ms, cache_ms, created_at, "
            + "faq_hit, answer_source "
            + "FROM qa_access_logs ORDER BY response_time_ms DESC, id DESC LIMIT #{limit}")
    List<Map<String, Object>> selectSlowest(@Param("limit") int limit);

    /**
     * 取某用户最近的问答日志，用于在 Java 侧按 session_id 聚合出「历史会话」列表。
     *
     * <p>按 id 倒序返回，因此同一会话首次出现即最近一次提问、最后一次出现即首问。
     * 聚合放在 Java 侧是为了避免 H2 与 MySQL 在窗口函数/子查询上的差异
     * （与 {@link #selectRecentAuthorizedJson} 同一思路）。
     *
     * @param userId 用户 id（只取本人日志，天然隔离他人会话）
     * @param limit  扫描条数上限
     * @return 每行含 session_id / question / created_at
     */
    @Select("SELECT session_id, question, created_at FROM qa_access_logs "
            + "WHERE user_id = #{userId} AND session_id IS NOT NULL ORDER BY id DESC LIMIT #{limit}")
    List<Map<String, Object>> selectRecentByUser(@Param("userId") Long userId, @Param("limit") int limit);

    /**
     * 取某用户某个会话的全部问答轮次（按时间正序），用于历史会话回看。
     *
     * <p>条件里同时带 userId：即使 sessionId 被猜中，也只能读到自己的会话。
     *
     * @param userId    用户 id
     * @param sessionId 会话 id
     * @return 每行含 question / answer / total_tokens / response_time_ms / faq_hit /
     *         authorized_unit_ids_json / unauthorized_unit_ids_json / created_at
     */
    @Select("SELECT question, answer, total_tokens, response_time_ms, faq_hit, chosen_unit_id, "
            + "answer_source, authorized_unit_ids_json, unauthorized_unit_ids_json, created_at "
            + "FROM qa_access_logs WHERE user_id = #{userId} AND session_id = #{sessionId} ORDER BY id")
    List<Map<String, Object>> selectSessionRounds(@Param("userId") Long userId,
                                                  @Param("sessionId") String sessionId);
}
