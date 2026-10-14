<script setup>
import { onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
// ECharts 按需引入：只注册用到的图表与组件，避免整包进入 dashboard 分包。
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  GraphicComponent,
  MarkLineComponent,
  AxisPointerComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  GraphicComponent,
  MarkLineComponent,
  AxisPointerComponent,
  CanvasRenderer,
])
import {
  fetchMetrics,
  fetchQuestionRankings,
  fetchTokenStats,
  fetchUnitRankings,
} from '@/api/dashboard'
import PageHeader from '@/components/PageHeader.vue'

const router = useRouter()
const loading = ref(false)
const metrics = ref(null)
const questions = ref([])
const units = ref([])
const trend = ref([])
const granularity = ref('day')

/** 粒度选项（含各自默认桶数） */
const GRANULARITY_OPTIONS = [
  { label: '小时', value: 'hour', buckets: 24 },
  { label: '天', value: 'day', buckets: 7 },
  { label: '周', value: 'week', buckets: 8 },
  { label: '月', value: 'month', buckets: 6 },
]

function currentBuckets() {
  return GRANULARITY_OPTIONS.find((o) => o.value === granularity.value)?.buckets ?? 7
}

const chartRef = ref(null)
const chart = shallowRef(null)

const cards = [
  {
    key: 'qaCount',
    label: '问答次数',
    suffix: '',
    icon: 'ChatLineSquare',
    desc: '统计区间内的问答总次数',
  },
  { key: 'uv', label: '活跃用户', suffix: '', icon: 'User', desc: '有提问行为的独立用户数' },
  {
    key: 'unitCount',
    label: '知识单元',
    suffix: '',
    icon: 'Files',
    desc: '当前业务可用的知识单元总数',
  },
  {
    key: 'totalTokens',
    label: '累计 Token',
    suffix: '',
    icon: 'Coin',
    desc: '输入 + 输出 token 累计消耗',
  },
  {
    key: 'avgResponseMs',
    label: '平均耗时',
    suffix: 'ms',
    icon: 'Timer',
    desc: '端到端平均耗时，点击查看阶段拆分',
  },
]

function cardValue(key) {
  if (!metrics.value) return '—'
  const value = metrics.value[key]
  return typeof value === 'number' ? value.toLocaleString() : (value ?? '—')
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart.value) chart.value = echarts.init(chartRef.value)

  const dates = trend.value.map((t) => t.date)
  const showSymbol = dates.length <= 24

  chart.value.setOption({
    // top 留足空间：图例独占首行，两个 Y 轴名称再下移一行，三者互不遮挡
    grid: { left: 64, right: 68, top: 62, bottom: 32 },
    tooltip: {
      trigger: 'axis',
      borderWidth: 0,
      backgroundColor: 'rgba(31,35,41,0.92)',
      textStyle: { color: '#fff', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: '#d8dade', type: 'dashed' } },
      formatter: (params) => {
        const lines = params.map(
          (p) =>
            `${p.marker}${p.seriesName}：<b>${Number(p.value ?? 0).toLocaleString()}</b>${
              p.seriesName === '平均耗时' ? ' ms' : ''
            }`,
        )
        return [params[0]?.axisValueLabel ?? '', ...lines].join('<br/>')
      },
    },
    legend: {
      data: ['Token 消耗', '平均耗时'],
      // 居中置顶：避免与右上角的「耗时 (ms)」轴名重叠
      top: 4,
      left: 'center',
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 20,
      icon: 'roundRect',
      textStyle: { color: '#5b6470', fontSize: 12 },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLine: { lineStyle: { color: '#e8eaed' } },
      axisLabel: { color: '#9aa1ac', fontSize: 12 },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        name: 'Token',
        nameGap: 16,
        nameTextStyle: { color: '#9aa1ac', fontSize: 12, align: 'left' },
        splitLine: { lineStyle: { color: '#eeeff1' } },
        axisLabel: { color: '#9aa1ac', fontSize: 12 },
      },
      {
        type: 'value',
        name: '耗时 (ms)',
        nameGap: 16,
        nameTextStyle: { color: '#9aa1ac', fontSize: 12, align: 'right' },
        splitLine: { show: false },
        axisLabel: { color: '#9aa1ac', fontSize: 12 },
      },
    ],
    series: [
      {
        name: 'Token 消耗',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        showSymbol,
        data: trend.value.map((t) => t.totalTokens),
        itemStyle: { color: '#3e63dd' },
        lineStyle: { color: '#3e63dd', width: 2 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(62, 99, 221, 0.20)' },
            { offset: 1, color: 'rgba(62, 99, 221, 0.01)' },
          ]),
        },
      },
      {
        name: '平均耗时',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        showSymbol,
        data: trend.value.map((t) => t.avgResponseMs),
        itemStyle: { color: '#10b981' },
        lineStyle: { color: '#10b981', width: 2 },
      },
    ],
  })
}

function onResize() {
  chart.value?.resize()
}

async function load() {
  loading.value = true
  try {
    const [m, q, u, t] = await Promise.all([
      fetchMetrics(),
      fetchQuestionRankings(),
      fetchUnitRankings(),
      fetchTokenStats(granularity.value, currentBuckets()),
    ])
    metrics.value = m
    questions.value = q
    units.value = u
    trend.value = t
    renderChart()
  } finally {
    loading.value = false
  }
}

/** 切换粒度只重取趋势，不必整页刷新 */
async function loadTrend() {
  trend.value = await fetchTokenStats(granularity.value, currentBuckets())
  renderChart()
}

watch(granularity, () => {
  void loadTrend()
})

/** 平均耗时单独成页：耗时阶段拆分与慢问题排查 */
function goLatency() {
  router.push('/latency')
}

onMounted(() => {
  load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  chart.value?.dispose()
})
</script>

<template>
  <div v-loading="loading">
    <PageHeader title="数据看板" description="问答量、知识热度与 Token 消耗趋势" />

    <div class="cards">
      <el-tooltip v-for="c in cards" :key="c.key" :content="c.desc" placement="top">
        <div
          class="kb-card metric-card"
          :class="{ 'metric-card--link': c.key === 'avgResponseMs' }"
          @click="c.key === 'avgResponseMs' ? goLatency() : undefined"
        >
          <div class="metric-card__head">
            <span class="metric-card__icon">
              <el-icon :size="14"><component :is="c.icon" /></el-icon>
            </span>
            <span class="metric-card__label">
              {{ c.label }}
              <span v-if="c.key === 'avgResponseMs'" class="metric-card__more">耗时分析 →</span>
            </span>
          </div>
          <div class="metric-card__value">
            {{ cardValue(c.key) }}
            <span v-if="c.suffix" class="metric-card__suffix">{{ c.suffix }}</span>
          </div>
        </div>
      </el-tooltip>
    </div>

    <div class="kb-card mt-16">
      <div class="panel-head">
        <div class="panel-title">Token 与耗时趋势</div>
        <el-radio-group v-model="granularity" size="small">
          <el-radio-button v-for="o in GRANULARITY_OPTIONS" :key="o.value" :value="o.value">
            {{ o.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div ref="chartRef" class="chart" />
      <el-empty v-if="!trend.length && !loading" description="暂无趋势数据" :image-size="60" />
    </div>

    <div class="rankings mt-16">
      <div class="kb-card">
        <div class="panel-title">高频问题 TOP</div>
        <el-empty v-if="!questions.length" description="暂无数据" :image-size="50" />
        <ul class="rank-list">
          <li v-for="(q, i) in questions" :key="i" class="rank-item">
            <span class="rank-item__no" :class="{ 'is-top': i < 3 }">{{ i + 1 }}</span>
            <span class="rank-item__text">{{ q.label }}</span>
            <span class="rank-item__count text-mono text-tertiary">{{ q.count }}</span>
          </li>
        </ul>
      </div>

      <div class="kb-card">
        <div class="panel-title">热门知识 TOP</div>
        <el-empty v-if="!units.length" description="暂无数据" :image-size="50" />
        <ul class="rank-list">
          <li v-for="(u, i) in units" :key="u.id" class="rank-item">
            <span class="rank-item__no" :class="{ 'is-top': i < 3 }">{{ i + 1 }}</span>
            <span class="rank-item__text">{{ u.label }}</span>
            <span class="rank-item__count text-mono text-tertiary">{{ u.count }}</span>
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}
.metric-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.metric-card__icon {
  width: 20px;
  height: 20px;
  border-radius: 4px;
  background: var(--el-color-primary-light-9, #eef1fd);
  color: var(--brand-500, #3e63dd);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.metric-card__label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  flex: 1;
  font-size: 12px;
}
.metric-card__more {
  font-size: 11px;
  font-weight: 600;
  color: var(--brand-500, #3e63dd);
  opacity: 0;
  transition: opacity 0.15s ease;
}
.metric-card--link {
  cursor: pointer;
}
.metric-card--link:hover {
  border-color: var(--brand-500, #3e63dd);
}
.metric-card--link:hover .metric-card__more {
  opacity: 1;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.panel-head .panel-title {
  margin-bottom: 0;
}
.metric-card__value {
  margin-top: 6px;
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
}
.metric-card__suffix {
  font-size: 13px;
  font-weight: 400;
  color: var(--color-text-tertiary);
  margin-left: 2px;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
}
.chart {
  height: 280px;
}
.rankings {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.rank-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.rank-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--color-border);
  font-size: 13px;
}
.rank-item:last-child {
  border-bottom: none;
}
.rank-item__no {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: var(--color-surface);
  color: var(--color-text-tertiary);
  font-size: 12px;
}
.rank-item__no.is-top {
  background: var(--el-color-primary-light-9);
  color: var(--brand-500);
  font-weight: 600;
}
.rank-item__text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 1100px) {
  .cards {
    grid-template-columns: repeat(2, 1fr);
  }
  .rankings {
    grid-template-columns: 1fr;
  }
}
</style>
