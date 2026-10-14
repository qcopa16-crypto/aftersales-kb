package com.shuidun.kb.ai;

import com.shuidun.kb.ai.dto.ChatRoundVO;
import com.shuidun.kb.ai.dto.ChatSessionDetailVO;
import com.shuidun.kb.ai.dto.ChatSessionVO;
import com.shuidun.kb.ai.dto.SourceItemVO;
import com.shuidun.kb.ai.dto.SourcesEvent;
import com.shuidun.kb.auth.CurrentUser;
import com.shuidun.kb.common.JdbcValues;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.dashboard.mapper.QaAccessLogMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import com.shuidun.kb.permission.DataPermissionEngine;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 历史会话服务（docs/api/api.md §4）。
 *
 * <p>会话不单独建表：{@code qa_access_logs} 已经按 {@code session_id} 记录了每一轮问答，
 * 历史会话直接从这张表聚合重建，好处是「不产生第二份真相」，也不会出现
 * 会话表与日志表不一致的问题。
 *
 * <p>四条硬约束：
 * <ul>
 *   <li><b>只能读自己的会话</b>：所有查询都带 {@code user_id = 当前用户}，
 *       sessionId 被猜中也读不到别人的问答；</li>
 *   <li><b>聚合在 Java 侧</b>：避免 H2 与 MySQL 在窗口函数/子查询上的差异，
 *       与 {@code DashboardService.topUnits()} 同一思路；</li>
 *   <li><b>回看按当前数据权限重算</b>（F-29）：落库的授权快照不是放行依据，
 *      提问后失权的 unit 从 sources 剔除、答案整轮脱敏、点选单元置空；</li>
 *   <li><b>未授权来源不返回</b>（R-11）：普通历史响应只含当前授权来源，
 *       未授权候选的 id/标题一律不出现在响应里（审计视图另走独立管理接口）。</li>
 * </ul>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatHistoryService {

    /** 会话列表默认条数。 */
    private static final int DEFAULT_SESSIONS = 20;

    /** 会话列表条数上限。 */
    private static final int MAX_SESSIONS = 50;

    /** 单次扫描的日志条数上限（够覆盖最近若干个会话的轮次）。 */
    private static final int RECENT_SCAN = 500;

    /** 标题截断长度，与前端「用首问当标题」的口径一致。 */
    private static final int TITLE_MAX_CHARS = 20;

    /** 来源标题反查的 id 数上限，防止极端会话把 IN 子句撑爆。 */
    private static final int MAX_TITLE_LOOKUP_IDS = 500;

    /** 引用的知识被撤销访问权限时，对答案正文的统一脱敏文案（fail-closed，F-29）。 */
    private static final String REVOKED_ANSWER_MASK = "（该轮回答引用的知识已被撤销访问权限，内容已隐藏）";

    private static final DateTimeFormatter FULL_FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

    private final QaAccessLogMapper qaAccessLogMapper;
    private final KnowledgeUnitMapper unitMapper;
    private final DataPermissionEngine dataPermissionEngine;

    /**
     * 列出当前用户的历史会话，按最近提问时间倒序。
     *
     * @param userId 用户 id
     * @param limit  条数（null 或 &lt;=0 用默认值）
     * @return 会话列表
     */
    public List<ChatSessionVO> listSessions(Long userId, Integer limit) {
        int size = limit == null || limit <= 0 ? DEFAULT_SESSIONS : Math.min(limit, MAX_SESSIONS);

        // 已按 id 倒序：同一 sessionId 首次出现 = 最近一次提问，最后一次出现 = 首问
        List<Map<String, Object>> rows = qaAccessLogMapper.selectRecentByUser(userId, RECENT_SCAN);
        Map<String, SessionAggregate> bySession = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String sessionId = JdbcValues.stringValue(row.get("session_id"));
            if (sessionId.isBlank()) {
                continue;
            }
            SessionAggregate agg = bySession.computeIfAbsent(sessionId, SessionAggregate::new);
            agg.roundCount++;
            if (agg.lastAskedAt == null) {
                agg.lastAskedAt = JdbcValues.toLocalDateTime(row.get("created_at"));
            }
            // 持续覆盖，循环结束时留下的就是该会话最早的那条问题
            agg.firstQuestion = JdbcValues.stringValue(row.get("question"));
        }

        List<ChatSessionVO> result = new ArrayList<>();
        for (SessionAggregate agg : bySession.values()) {
            if (result.size() >= size) {
                break;
            }
            result.add(new ChatSessionVO(
                    agg.sessionId,
                    titleOf(agg.firstQuestion),
                    agg.roundCount,
                    agg.lastAskedAt == null ? "" : agg.lastAskedAt.format(FULL_FMT)));
        }
        return result;
    }

    /**
     * 回看某个会话的完整问答轮次（含来源卡片），并按<b>当前</b>数据权限重新过滤（F-29）。
     *
     * <p>权限变更后的 fail-closed 语义：提问时有授权、当前已撤销（部门/项目/类别/显式授权变化）
     * 的 unit 一律视为失权引用——
     * <ul>
     *   <li>该轮答案正文整轮脱敏（不保留可能引自失权知识的原文）；</li>
     *   <li>失权 unit 从 sources.authorized 剔除，其标题不再返回；</li>
     *   <li>点选单元（chosenUnitId）失效时置空，不返回其标题；</li>
     *   <li>R-11：unauthorized 来源恒为空列表，未授权候选的 id/标题不返回。</li>
     * </ul>
     * 实时审计视图需要「当时可见」的全量数据时，应使用独立接口与权限策略，不走本用户接口。
     *
     * @param user      当前用户
     * @param sessionId 会话 id
     * @return 会话详情；不属于该用户或不存在时返回空 rounds（不区分「无权限」与「不存在」，避免探测）
     */
    public ChatSessionDetailVO openSession(CurrentUser user, String sessionId) {
        List<Map<String, Object>> rows = qaAccessLogMapper.selectSessionRounds(user.getId(), sessionId);
        if (rows.isEmpty()) {
            return new ChatSessionDetailVO(sessionId, List.of());
        }

        // 先把所有轮次的来源 id 收齐，一次性批量反查标题（点选单元也一起查）
        //
        // R-11：只对「授权来源 + 点选单元」反查标题与重算权限。unauthorized_unit_ids_json 记录的
        // 是当时即无权的候选，其 id/标题属于失权知识的可识别标识，绝不进入普通历史返回路径
        // （既不加入批量反查，也不作为来源卡片返回）；实时审计视图应走独立的管理接口。
        List<List<Long>> authorizedPerRound = new ArrayList<>(rows.size());
        List<Long> chosenPerRound = new ArrayList<>(rows.size());
        Set<Long> allIds = new LinkedHashSet<>();
        for (Map<String, Object> row : rows) {
            List<Long> authorized = JsonUtils.toLongList(
                    JdbcValues.stringValue(row.get("authorized_unit_ids_json")));
            Object chosenRaw = row.get("chosen_unit_id");
            Long chosen = chosenRaw instanceof Number number ? number.longValue() : null;
            authorizedPerRound.add(authorized);
            chosenPerRound.add(chosen);
            allIds.addAll(authorized);
            if (chosen != null) {
                allIds.add(chosen);
            }
        }
        Map<Long, String> titles = loadTitles(allIds);
        // 按当前数据权限重算可见 unit（audit 路径的快照 id 不作为放行依据）
        Set<Long> currentlyAuthorized = new java.util.HashSet<>(
                dataPermissionEngine.filter(user, new ArrayList<>(allIds)).authorized());

        List<ChatRoundVO> rounds = new ArrayList<>(rows.size());
        for (int i = 0; i < rows.size(); i++) {
            Map<String, Object> row = rows.get(i);
            LocalDateTime ts = JdbcValues.toLocalDateTime(row.get("created_at"));
            Long chosen = chosenPerRound.get(i);
            boolean chosenRevoked = chosen != null && !currentlyAuthorized.contains(chosen);
            if (chosenRevoked) {
                chosen = null;
            }
            List<Long> authorized = authorizedPerRound.get(i);
            boolean revoked = authorized.stream().anyMatch(id -> !currentlyAuthorized.contains(id));
            List<Long> stillAuthorized = authorized.stream()
                    .filter(currentlyAuthorized::contains)
                    .toList();
            rounds.add(new ChatRoundVO(
                    JdbcValues.stringValue(row.get("question")),
                    revoked ? REVOKED_ANSWER_MASK : JdbcValues.stringValue(row.get("answer")),
                    JdbcValues.longValue(row.get("total_tokens")),
                    JdbcValues.longValue(row.get("response_time_ms")),
                    JdbcValues.longValue(row.get("faq_hit")) == 1,
                    JdbcValues.stringValue(row.get("answer_source")),
                    ts == null ? "" : ts.format(FULL_FMT),
                    chosen,
                    chosen == null ? null : titles.get(chosen),
                    // R-11：unauthorized 来源不返回任何 id/标题（普通历史响应不得含失权标识），
                    // 因此恒为空列表；「未授权命中」审计如需展示，另走脱敏的管理接口。
                    new SourcesEvent(
                            toSourceItems(stillAuthorized, titles),
                            List.of())));
        }
        return new ChatSessionDetailVO(sessionId, rounds);
    }

    private String titleOf(String question) {
        String text = question == null ? "" : question.strip();
        if (text.isEmpty()) {
            return "未命名会话";
        }
        return text.length() <= TITLE_MAX_CHARS ? text : text.substring(0, TITLE_MAX_CHARS);
    }

    /** 批量反查知识单元标题；已删除的单元不在结果里，调用方需跳过。 */
    private Map<Long, String> loadTitles(Collection<Long> ids) {
        if (ids.isEmpty()) {
            return Map.of();
        }
        if (ids.size() > MAX_TITLE_LOOKUP_IDS) {
            log.warn("chat history title lookup skipped: ids={} exceeds {}", ids.size(), MAX_TITLE_LOOKUP_IDS);
            return Map.of();
        }
        Map<Long, String> titles = new LinkedHashMap<>();
        for (KnowledgeUnit unit : unitMapper.selectByIds(new ArrayList<>(ids))) {
            titles.put(unit.getId(), unit.getTitle() == null ? "" : unit.getTitle());
        }
        return titles;
    }

    /**
     * 把 id 列表还原成来源卡片。
     *
     * <p>两点处理：
     * <ul>
     *   <li><b>去重</b>：落库的 {@code authorized_unit_ids_json} 是按「召回切片」逐个追加的，
     *       同一知识单元命中多个切片就会出现多次；实时 SSE 的 sources 用 Map 去重过，
     *       回看时必须对齐，否则来源卡片会重复。</li>
     *   <li><b>跳过已删除单元</b>：查不到标题直接丢弃，而不是回退成 "#id"。</li>
     * </ul>
     */
    private List<SourceItemVO> toSourceItems(List<Long> ids, Map<Long, String> titles) {
        List<SourceItemVO> items = new ArrayList<>();
        for (Long id : new LinkedHashSet<>(ids)) {
            String title = titles.get(id);
            if (title != null) {
                items.add(new SourceItemVO(id, title));
            }
        }
        return items;
    }

    /** 会话聚合中间态。 */
    private static final class SessionAggregate {

        private final String sessionId;
        private long roundCount;
        private LocalDateTime lastAskedAt;
        private String firstQuestion = "";

        private SessionAggregate(String sessionId) {
            this.sessionId = sessionId;
        }
    }
}
