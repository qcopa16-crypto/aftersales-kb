package com.shuidun.kb.dashboard;

import com.shuidun.kb.common.Result;
import com.shuidun.kb.dashboard.dto.DailyStatVO;
import com.shuidun.kb.dashboard.dto.LatencyVO;
import com.shuidun.kb.dashboard.dto.MetricsVO;
import com.shuidun.kb.dashboard.dto.RankingItemVO;
import com.shuidun.kb.permission.RequirePermission;
import java.util.List;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 看板接口（docs/api/api.md §7）。
 */
@RestController
@RequestMapping("/api/dashboard")
@RequiredArgsConstructor
public class DashboardController {

    private final DashboardService dashboardService;

    /**
     * 总览指标。
     *
     * @return 指标
     */
    @GetMapping("/metrics")
    @RequirePermission("dashboard:view")
    public Result<MetricsVO> metrics() {
        return Result.ok(dashboardService.metrics());
    }

    /**
     * 高频问题 TOP。
     *
     * @param limit 条数
     * @return 排行
     */
    @GetMapping("/rankings/questions")
    @RequirePermission("dashboard:view")
    public Result<List<RankingItemVO>> topQuestions(@RequestParam(defaultValue = "10") int limit) {
        return Result.ok(dashboardService.topQuestions(limit));
    }

    /**
     * 热门知识 TOP。
     *
     * @param limit 条数
     * @return 排行
     */
    @GetMapping("/rankings/units")
    @RequirePermission("dashboard:view")
    public Result<List<RankingItemVO>> topUnits(@RequestParam(defaultValue = "10") int limit) {
        return Result.ok(dashboardService.topUnits(limit));
    }

    /**
     * Token 与耗时趋势（支持小时/天/周/月粒度）。
     *
     * @param granularity hour / day / week / month
     * @param buckets     桶数量；不传则按 days 推导
     * @param days        兼容旧参数：按天粒度时的天数
     * @return 趋势点
     */
    @GetMapping("/stats/tokens")
    @RequirePermission("dashboard:view")
    public Result<List<DailyStatVO>> trend(@RequestParam(defaultValue = "day") String granularity,
                                           @RequestParam(required = false) Integer buckets,
                                           @RequestParam(defaultValue = "7") int days) {
        int size = buckets != null ? buckets : days;
        return Result.ok(dashboardService.trend(granularity, size));
    }

    /**
     * 耗时分析：分位、阶段拆分、最慢问题。
     *
     * @param days        统计天数
     * @param granularity 趋势粒度
     * @return 耗时分析
     */
    @GetMapping("/latency")
    @RequirePermission("dashboard:view")
    public Result<LatencyVO> latency(@RequestParam(defaultValue = "7") int days,
                                     @RequestParam(defaultValue = "day") String granularity) {
        return Result.ok(dashboardService.latency(days, granularity));
    }
}
