package com.shuidun.kb.dashboard;

import com.shuidun.kb.common.JdbcValues;
import com.shuidun.kb.common.JsonUtils;
import com.shuidun.kb.dashboard.dto.DailyStatVO;
import com.shuidun.kb.dashboard.dto.LatencyBreakdownVO;
import com.shuidun.kb.dashboard.dto.LatencyVO;
import com.shuidun.kb.dashboard.dto.MetricsVO;
import com.shuidun.kb.dashboard.dto.RankingItemVO;
import com.shuidun.kb.dashboard.dto.SlowQuestionVO;
import com.shuidun.kb.dashboard.mapper.QaAccessLogMapper;
import com.shuidun.kb.knowledge.entity.KnowledgeUnit;
import com.shuidun.kb.knowledge.mapper.KnowledgeUnitMapper;
import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.time.temporal.ChronoUnit;
import java.time.temporal.TemporalAdjusters;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

/**
 * 看板服务（docs/api/api.md §7 / features.md §3.8）。
 */
@Service
@RequiredArgsConstructor
public class DashboardService {

    private static final int MAX_LIMIT = 50;
    private static final int RECENT_LOG_SCAN = 500;
    private static final int MAX_BUCKETS = 120;
    private static final int SLOWEST_LIMIT = 10;

    /** 支持的趋势粒度。 */
    private static final List<String> GRANULARITIES = List.of("hour", "day", "week", "month");

    private static final DateTimeFormatter HOUR_FMT = DateTimeFormatter.ofPattern("MM-dd HH:00");
    private static final DateTimeFormatter DAY_FMT = DateTimeFormatter.ofPattern("MM-dd");
    private static final DateTimeFormatter MONTH_FMT = DateTimeFormatter.ofPattern("yyyy-MM");
    private static final DateTimeFormatter FULL_FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm");

    private final QaAccessLogMapper qaAccessLogMapper;
    private final KnowledgeUnitMapper unitMapper;

    /**
     * 总览指标。
     *
     * @return 指标
     */
    public MetricsVO metrics() {
        Map<String, Object> row = qaAccessLogMapper.selectMetrics();
        return new MetricsVO(
                JdbcValues.longValue(row.get("qa_count")),
                JdbcValues.longValue(row.get("uv")),
                unitMapper.countActive(),
                JdbcValues.longValue(row.get("total_tokens")),
                JdbcValues.longValue(row.get("avg_response_ms")));
    }

    /**
     * 高频问题 TOP。
     *
     * @param limit 条数
     * @return 排行
     */
    public List<RankingItemVO> topQuestions(int limit) {
        List<Map<String, Object>> rows = qaAccessLogMapper.selectTopQuestions(normalizeLimit(limit));
        List<RankingItemVO> result = new ArrayList<>(rows.size());
        for (Map<String, Object> row : rows) {
            result.add(new RankingItemVO(JdbcValues.stringValue(row.get("question")), null, JdbcValues.longValue(row.get("ask_count"))));
        }
        return result;
    }

    /**
     * 热门知识 TOP。
     *
     * <p>两点关键修正：① **跳过已被删除的知识单元**（否则会退化成 "# 13" 这种无意义标签）；
     * ② **按标题聚合**（同一份知识可能有多个版本/重复导入，分散展示会让榜单失真）。
     *
     * @param limit 条数
     * @return 排行
     */
    public List<RankingItemVO> topUnits(int limit) {
        int normalizedLimit = normalizeLimit(limit);
        List<String> jsonList = qaAccessLogMapper.selectRecentAuthorizedJson(RECENT_LOG_SCAN);
        Map<Long, Long> byUnit = new LinkedHashMap<>();
        for (String json : jsonList) {
            for (Long unitId : JsonUtils.toLongList(json)) {
                byUnit.merge(unitId, 1L, Long::sum);
            }
        }
        if (byUnit.isEmpty()) {
            return List.of();
        }
        // 一次性批量加载：避免对每个 unit 单查（原 N 次 selectById 退化为 1 次 selectByIds）。
        List<KnowledgeUnit> units = unitMapper.selectByIds(new ArrayList<>(byUnit.keySet()));
        Map<Long, KnowledgeUnit> unitById = new HashMap<>(units.size());
        for (KnowledgeUnit unit : units) {
            unitById.put(unit.getId(), unit);
        }
        // title -> [count, 代表 unitId]；遍历顺序仍取 byUnit（LinkedHashMap）以保证代表 id 与历史一致
        Map<String, long[]> byTitle = new LinkedHashMap<>();
        for (Map.Entry<Long, Long> entry : byUnit.entrySet()) {
            KnowledgeUnit unit = unitById.get(entry.getKey());
            if (unit == null || unit.getTitle() == null) {
                continue;
            }
            long[] slot = byTitle.computeIfAbsent(unit.getTitle(), key -> new long[]{0L, unit.getId()});
            slot[0] += entry.getValue();
        }
        return byTitle.entrySet().stream()
                .sorted((a, b) -> Long.compare(b.getValue()[0], a.getValue()[0]))
                .limit(normalizedLimit)
                .map(entry -> new RankingItemVO(entry.getKey(), entry.getValue()[1], entry.getValue()[0]))
                .toList();
    }

    /**
     * 按粒度统计趋势（小时 / 天 / 周 / 月）。
     *
     * <p>分桶在 Java 侧完成，避免 H2 与 MySQL 的日期函数差异。
     *
     * @param granularity hour / day / week / month
     * @param buckets     桶数量
     * @return 趋势点（含空桶，便于图表连续）
     */
    public List<DailyStatVO> trend(String granularity, int buckets) {
        String gran = normalizeGran(granularity);
        int count = Math.min(Math.max(buckets, 1), MAX_BUCKETS);
        // 只查 1 次：取数下界 = 趋势窗口起点
        return trendFromRows(gran, count, qaAccessLogMapper.selectSince(trendSince(gran, count)));
    }

    /**
     * 基于已取出的日志行计算趋势分桶（纯计算，不触发查询）。
     *
     * <p>抽出以便 {@link #latency(int, String)} 复用同一份 {@code selectSince} 结果，
     * 避免同一请求内重复查询。
     *
     * @param gran  已归一化的粒度（hour/day/week/month）
     * @param count 桶数量（已裁剪到 [1, MAX_BUCKETS]）
     * @param rows  日志行（含 created_at / total_tokens / response_time_ms）
     * @return 趋势点（含空桶）
     */
    private List<DailyStatVO> trendFromRows(String gran, int count, List<Map<String, Object>> rows) {
        LocalDateTime now = LocalDateTime.now();

        // 预生成桶键，保证空桶也在曲线上
        List<LocalDateTime> keys = new ArrayList<>(count);
        LocalDateTime cursor = bucketStart(now, gran).minus(count - 1L, unitOf(gran));
        for (int i = 0; i < count; i++) {
            keys.add(cursor);
            cursor = cursor.plus(1, unitOf(gran));
        }

        Map<LocalDateTime, long[]> agg = new LinkedHashMap<>();
        for (LocalDateTime key : keys) {
            agg.put(key, new long[]{0L, 0L, 0L}); // count, tokens, sumResponseMs
        }
        for (Map<String, Object> row : rows) {
            LocalDateTime ts = JdbcValues.toLocalDateTime(row.get("created_at"));
            if (ts == null) {
                continue;
            }
            long[] slot = agg.get(bucketStart(ts, gran));
            if (slot == null) {
                continue;
            }
            slot[0] += 1;
            slot[1] += JdbcValues.longValue(row.get("total_tokens"));
            slot[2] += JdbcValues.longValue(row.get("response_time_ms"));
        }

        List<DailyStatVO> result = new ArrayList<>(count);
        for (Map.Entry<LocalDateTime, long[]> entry : agg.entrySet()) {
            long[] slot = entry.getValue();
            long avg = slot[0] == 0 ? 0 : Math.round((double) slot[2] / slot[0]);
            result.add(new DailyStatVO(formatKey(entry.getKey(), gran), slot[0], slot[1], avg));
        }
        return result;
    }

    /** 归一化趋势粒度：非法值回落到 day（与历史行为一致）。 */
    private String normalizeGran(String granularity) {
        return (granularity == null || !GRANULARITIES.contains(granularity)) ? "day" : granularity;
    }

    /** 趋势窗口起点（最早桶的起始时刻）。 */
    private LocalDateTime trendSince(String gran, int count) {
        return bucketStart(LocalDateTime.now(), gran).minus(count - 1L, unitOf(gran));
    }

    /**
     * 耗时分析：分位、阶段拆分、最慢问题。
     *
     * @param days  统计天数
     * @param gran  趋势粒度
     * @return 耗时分析
     */
    public LatencyVO latency(int days, String gran) {
        int normalizedDays = Math.min(Math.max(days, 1), 90);
        LocalDateTime latencySince = LocalDate.now().minusDays(normalizedDays - 1L).atStartOfDay();

        // 单次取数：耗时统计窗口与趋势窗口取更早的下界，一次 selectSince 同时服务两者，
        // 避免 /api/dashboard/latency 每请求 2 次 selectSince（P3）。
        String normalizedGran = normalizeGran(gran);
        int trendBucketCount = trendBuckets(normalizedDays, gran);
        LocalDateTime trendSince = trendSince(normalizedGran, trendBucketCount);
        LocalDateTime querySince = trendSince.isBefore(latencySince) ? trendSince : latencySince;
        List<Map<String, Object>> rows = qaAccessLogMapper.selectSince(querySince);

        List<Long> responseTimes = new ArrayList<>(rows.size());
        long sumRetrieve = 0;
        long sumGenerate = 0;
        long sumDb = 0;
        long sumCache = 0;
        long sumOther = 0;
        long sumFaq = 0;
        long faqCount = 0;
        for (Map<String, Object> row : rows) {
            // 查询下界可能早于耗时窗口（趋势窗口更长时），这里按耗时窗口自身过滤以保持语义不变
            LocalDateTime ts = JdbcValues.toLocalDateTime(row.get("created_at"));
            if (ts == null || ts.isBefore(latencySince)) {
                continue;
            }
            long resp = JdbcValues.longValue(row.get("response_time_ms"));
            long ret = JdbcValues.longValue(row.get("retrieve_ms"));
            long gen = JdbcValues.longValue(row.get("generate_ms"));
            long db = JdbcValues.longValue(row.get("db_ms"));
            long cache = JdbcValues.longValue(row.get("cache_ms"));
            responseTimes.add(resp);
            sumRetrieve += ret;
            sumGenerate += gen;
            sumDb += db;
            sumCache += cache;
            sumOther += Math.max(0, resp - ret - gen - db - cache);
            if (JdbcValues.longValue(row.get("faq_hit")) == 1) {
                sumFaq += resp;
                faqCount++;
            }
        }

        long[] sorted = responseTimes.stream().mapToLong(Long::longValue).sorted().toArray();
        long samples = sorted.length;
        long avg = samples == 0 ? 0 : Math.round(Arrays.stream(sorted).average().orElse(0));
        LatencyBreakdownVO breakdown = new LatencyBreakdownVO(
                samples == 0 ? 0 : sumRetrieve / samples,
                samples == 0 ? 0 : sumGenerate / samples,
                samples == 0 ? 0 : sumDb / samples,
                samples == 0 ? 0 : sumCache / samples,
                samples == 0 ? 0 : sumOther / samples,
                faqCount == 0 ? 0 : sumFaq / faqCount);

        List<SlowQuestionVO> slowest = new ArrayList<>();
        for (Map<String, Object> row : qaAccessLogMapper.selectSlowest(SLOWEST_LIMIT)) {
            LocalDateTime ts = JdbcValues.toLocalDateTime(row.get("created_at"));
            slowest.add(new SlowQuestionVO(
                    JdbcValues.stringValue(row.get("question")),
                    JdbcValues.longValue(row.get("response_time_ms")),
                    JdbcValues.longValue(row.get("retrieve_ms")),
                    JdbcValues.longValue(row.get("generate_ms")),
                    JdbcValues.longValue(row.get("db_ms")),
                    JdbcValues.longValue(row.get("cache_ms")),
                    JdbcValues.longValue(row.get("faq_hit")) == 1,
                    JdbcValues.stringValue(row.get("answer_source")),
                    ts == null ? "" : ts.format(FULL_FMT)));
        }

        return new LatencyVO(normalizedDays, samples, avg,
                percentile(sorted, 0.50), percentile(sorted, 0.90), percentile(sorted, 0.95),
                samples == 0 ? 0 : sorted[sorted.length - 1],
                breakdown, trendFromRows(normalizedGran, trendBucketCount, rows), slowest);
    }

    private int trendBuckets(int days, String gran) {
        return switch (gran == null ? "day" : gran) {
            case "hour" -> Math.min(days * 24, MAX_BUCKETS);
            case "week" -> Math.min(Math.max(days / 7, 4), 52);
            case "month" -> Math.min(Math.max(days / 30, 3), 24);
            default -> Math.min(days, MAX_BUCKETS);
        };
    }

    private LocalDateTime bucketStart(LocalDateTime ts, String gran) {
        return switch (gran) {
            case "hour" -> ts.truncatedTo(ChronoUnit.HOURS);
            case "week" -> ts.toLocalDate().with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY)).atStartOfDay();
            case "month" -> ts.toLocalDate().withDayOfMonth(1).atStartOfDay();
            default -> ts.toLocalDate().atStartOfDay();
        };
    }

    private ChronoUnit unitOf(String gran) {
        return switch (gran) {
            case "hour" -> ChronoUnit.HOURS;
            case "week" -> ChronoUnit.WEEKS;
            case "month" -> ChronoUnit.MONTHS;
            default -> ChronoUnit.DAYS;
        };
    }

    private String formatKey(LocalDateTime key, String gran) {
        return switch (gran) {
            case "hour" -> key.format(HOUR_FMT);
            case "month" -> key.format(MONTH_FMT);
            case "week" -> key.format(DAY_FMT) + " 周";
            default -> key.format(DAY_FMT);
        };
    }

    private long percentile(long[] sorted, double p) {
        if (sorted.length == 0) {
            return 0;
        }
        int idx = (int) Math.ceil(p * sorted.length) - 1;
        return sorted[Math.min(Math.max(idx, 0), sorted.length - 1)];
    }

    private int normalizeLimit(int limit) {
        return Math.min(Math.max(limit, 1), MAX_LIMIT);
    }
}
