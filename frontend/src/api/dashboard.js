import { get } from './http'

/** 总览指标 */
export function fetchMetrics() {
  return get('/dashboard/metrics')
}

/** 高频问题 TOP */
export function fetchQuestionRankings(limit = 10) {
  return get('/dashboard/rankings/questions', { limit })
}

/** 热门知识 TOP */
export function fetchUnitRankings(limit = 10) {
  return get('/dashboard/rankings/units', { limit })
}

/** Token 与耗时趋势（granularity: hour / day / week / month） */
export function fetchTokenStats(granularity = 'day', buckets = 7) {
  return get('/dashboard/stats/tokens', { granularity, buckets })
}

/** 耗时分析：分位 / 阶段拆分 / 最慢问题 */
export function fetchLatency(days = 7, granularity = 'day') {
  return get('/dashboard/latency', { days, granularity })
}
