<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
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
import { fetchLatency } from '@/api/dashboard'
import PageHeader from '@/components/PageHeader.vue'

const router = useRouter()
const loading = ref(false)
const data = ref(null)
const days = ref(7)
const granularity = ref('day')

const pieRef = ref(null)
const lineRef = ref(null)
const pie = shallowRef(null)
const line = shallowRef(null)

const GRANULARITY_OPTIONS = [
  { label: '小时', value: 'hour' },
  { label: '天', value: 'day' },
  { label: '周', value: 'week' },
  { label: '月', value: 'month' },
]

/**
 * 阶段元数据：名称 / 取值 / 颜色 / 所属层 / 说明。
 *
 * 顺序即堆叠与图例顺序：业务侧"贵"的两段（检索、生成）在前，
 * 基础设施侧两段（数据库、缓存）居中，兜底残差放最后。
 */
const STAGE_META = [
  {
    name: '检索',
    key: 'retrieveMs',
    color: '#3e63dd',
    layer: 'AI 服务',
    desc: 'Java 调 AI /internal/retrieve：查询向量化 + 向量库召回 + 关键词检索 + RRF 融合重排',
  },
  {
    name: '生成',
    key: 'generateMs',
    color: '#10b981',
    layer: 'AI 服务',
    desc: 'AI 流式 /internal/generate：上下文拼装 + 云 LLM 逐 token 生成，通常是耗时大头',
  },
  {
    name: '数据库',
    key: 'dbMs',
    color: '#f59e0b',
    layer: 'MySQL',
    desc: '四维数据权限过滤（global/department/role/user 取并集）+ 知识单元批量加载，均为 MySQL 查询',
  },
  {
    name: '缓存',
    key: 'cacheMs',
    color: '#8b5cf6',
    layer: 'Redis',
    desc: 'FAQ 命中查询：Redis 按问题归一化键读取缓存答案，命中则直接返回、跳过检索与生成',
  },
  {
    name: '其他开销',
    key: 'otherMs',
    color: '#94a3b8',
    layer: '框架',
    desc: '端到端减去以上四段：JWT 校验、权限注解拦截、SSE 透传与序列化、网络往返',
  },
]

/** 数值格式化：>=1s 用秒（保留 2 位），否则用毫秒 —— 避免满屏 4 位数 */
function fmt(ms) {
  if (!ms) return '0 ms'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`
}

function pctOf(part, whole) {
  if (!whole) return '0%'
  return ((part / whole) * 100).toFixed(1) + '%'
}

/** 每行"其他开销" = 端到端 − 检索 − 生成 − 数据库 − 缓存（不为负） */
function otherOf(row) {
  return Math.max(0, row.responseMs - row.retrieveMs - row.generateMs - row.dbMs - row.cacheMs)
}

/**
 * 判定该次问答实际走到了哪条链路。
 *
 * 不能只看 faqHit：无召回 / 无权限时会「检索完直接拒答」，generate 从未执行
 * （generateMs=0），此时若仍标成"检索+生成"就是错的。
 */
function sourceTag(row) {
  // 闲聊兜底：知识库没命中但用了 AI 通用知识回答，来源需与"检索+生成"区分
  if (row.answerSource === 'llm_general') {
    return {
      text: 'AI 通用知识',
      type: 'warning',
      tip: '知识库未命中，使用 AI 通用知识回答（非公司规定，仅供参考）',
    }
  }
  if (row.faqHit) {
    return { text: 'FAQ 缓存', type: 'success', tip: '命中 Redis 缓存，未调用检索与生成' }
  }
  if (row.generateMs > 0) {
    return { text: '检索+生成', type: 'info', tip: '检索到授权知识并调用云 LLM 生成答案' }
  }
  if (row.retrieveMs > 0) {
    return {
      text: '检索后拒答',
      type: 'warning',
      tip: '检索无可用召回（或召回均无权限），直接返回兜底话术，未调用生成',
    }
  }
  return { text: '权限拦截', type: 'warning', tip: '当前账号无任何可访问知识，检索前即拒答' }
}

const summaryCards = computed(() => {
  const d = data.value
  return [
    {
      label: '样本数',
      value: d?.samples ?? 0,
      suffix: '次',
      icon: 'Histogram',
      desc: '统计区间内的问答次数',
    },
    {
      label: '平均耗时',
      value: d?.avgMs ?? 0,
      suffix: 'ms',
      icon: 'Timer',
      desc: '端到端平均：从收到请求到 SSE done',
    },
    {
      label: 'P50',
      value: d?.p50Ms ?? 0,
      suffix: 'ms',
      icon: 'Odometer',
      desc: '一半请求快于此值（中位数）',
    },
    {
      label: 'P90',
      value: d?.p90Ms ?? 0,
      suffix: 'ms',
      icon: 'TrendCharts',
      desc: '90% 的请求快于此值',
    },
    {
      label: 'P95',
      value: d?.p95Ms ?? 0,
      suffix: 'ms',
      icon: 'AlarmClock',
      desc: '95% 的请求快于此值，常用于 SLA',
    },
    {
      label: '最慢',
      value: d?.maxMs ?? 0,
      suffix: 'ms',
      icon: 'Warning',
      desc: '区间内单次最慢请求',
    },
  ]
})

const stageRows = computed(() => {
  const d = data.value
  if (!d) return []
  const total = STAGE_META.reduce((sum, m) => sum + (d.breakdown[m.key] ?? 0), 0)
  return STAGE_META.map((m) => ({
    name: m.name,
    color: m.color,
    layer: m.layer,
    avgMs: d.breakdown[m.key] ?? 0,
    share: pctOf(d.breakdown[m.key] ?? 0, total),
    desc: m.desc,
  }))
})

const stageTotal = computed(() => stageRows.value.reduce((sum, r) => sum + r.avgMs, 0))

/** 数据层合计（数据库 + 缓存），用于概览提示 */
const dataLayerTotal = computed(() => {
  const d = data.value
  if (!d) return 0
  return (d.breakdown.dbMs ?? 0) + (d.breakdown.cacheMs ?? 0)
})

function renderCharts() {
  const d = data.value
  if (!d) return

  if (pieRef.value) {
    if (!pie.value) pie.value = echarts.init(pieRef.value)
    pie.value.setOption({
      tooltip: {
        trigger: 'item',
        borderWidth: 0,
        backgroundColor: 'rgba(31,35,41,0.92)',
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (p) => {
          const meta = STAGE_META[p.dataIndex]
          return `<b>${p.name}</b> · ${meta?.layer ?? ''}<br/>平均 ${fmt(p.value)}（${p.percent}%）`
        },
      },
      legend: {
        bottom: 0,
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        icon: 'roundRect',
        textStyle: { color: '#5b6470', fontSize: 12 },
      },
      graphic: [
        {
          type: 'text',
          left: 'center',
          top: '32%',
          silent: true,
          style: {
            text: fmt(stageTotal.value),
            fontSize: 18,
            fontWeight: 600,
            fill: '#1f2329',
            textAlign: 'center',
          },
        },
        {
          type: 'text',
          left: 'center',
          top: '44%',
          silent: true,
          style: { text: '阶段合计', fontSize: 11, fill: '#9aa1ac', textAlign: 'center' },
        },
      ],
      series: [
        {
          type: 'pie',
          radius: ['52%', '74%'],
          center: ['50%', '40%'],
          avoidLabelOverlap: true,
          padAngle: 1,
          itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 3 },
          label: { show: false },
          emphasis: { scale: true, scaleSize: 4 },
          data: STAGE_META.map((m) => ({
            name: m.name,
            value: d.breakdown[m.key] ?? 0,
            itemStyle: { color: m.color },
          })),
        },
      ],
    })
  }

  if (lineRef.value) {
    if (!line.value) line.value = echarts.init(lineRef.value)
    line.value.setOption({
      grid: { left: 62, right: 30, top: 46, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        borderWidth: 0,
        backgroundColor: 'rgba(31,35,41,0.92)',
        textStyle: { color: '#fff', fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: '#d8dade', type: 'dashed' } },
        valueFormatter: (v) => fmt(v),
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: d.trend.map((t) => t.date),
        axisLine: { lineStyle: { color: '#e8eaed' } },
        axisLabel: { color: '#9aa1ac', fontSize: 12 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: '平均耗时',
        nameGap: 16,
        nameTextStyle: { color: '#9aa1ac', fontSize: 12, align: 'left' },
        splitLine: { lineStyle: { color: '#eeeff1' } },
        axisLabel: { color: '#9aa1ac', fontSize: 12, formatter: (v) => `${v}` },
      },
      series: [
        {
          name: '平均耗时',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          showSymbol: d.trend.length <= 24,
          data: d.trend.map((t) => t.avgResponseMs),
          itemStyle: { color: '#3e63dd' },
          lineStyle: { color: '#3e63dd', width: 2 },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(62, 99, 221, 0.20)' },
              { offset: 1, color: 'rgba(62, 99, 221, 0.01)' },
            ]),
          },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: '#c7cbd1', type: 'dashed' },
            label: {
              formatter: '区间均值 {c} ms',
              color: '#9aa1ac',
              fontSize: 11,
              position: 'insideEndTop',
            },
            data: [{ type: 'average' }],
          },
        },
      ],
    })
  }
}

async function load() {
  loading.value = true
  try {
    data.value = await fetchLatency(days.value, granularity.value)
    renderCharts()
  } finally {
    loading.value = false
  }
}

function onResize() {
  pie.value?.resize()
  line.value?.resize()
}

onMounted(() => {
  void load()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  pie.value?.dispose()
  line.value?.dispose()
})
</script>

<template>
  <div v-loading="loading">
    <PageHeader title="耗时分析" description="端到端耗时分布、五阶段拆分与最慢问题排查">
      <template #actions>
        <el-button link type="primary" @click="router.push('/dashboard')">← 返回数据看板</el-button>
      </template>
    </PageHeader>

    <div class="kb-card toolbar">
      <div class="toolbar__group">
        <span class="toolbar__label">统计区间</span>
        <el-radio-group v-model="days" size="small" @change="load">
          <el-radio-button :value="1">今日</el-radio-button>
          <el-radio-button :value="7">近 7 天</el-radio-button>
          <el-radio-button :value="30">近 30 天</el-radio-button>
        </el-radio-group>
      </div>
      <div class="toolbar__group">
        <span class="toolbar__label">趋势粒度</span>
        <el-radio-group v-model="granularity" size="small" @change="load">
          <el-radio-button v-for="o in GRANULARITY_OPTIONS" :key="o.value" :value="o.value">
            {{ o.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <!-- 概览指标：每张卡带图标 + 悬停说明 -->
    <div class="cards">
      <el-tooltip v-for="c in summaryCards" :key="c.label" :content="c.desc" placement="top">
        <div class="kb-card metric-card">
          <div class="metric-card__head">
            <span class="metric-card__icon">
              <el-icon :size="14"><component :is="c.icon" /></el-icon>
            </span>
            <span class="metric-card__label">{{ c.label }}</span>
          </div>
          <div class="metric-card__value">
            {{ c.value.toLocaleString() }}
            <span class="metric-card__suffix">{{ c.suffix }}</span>
          </div>
        </div>
      </el-tooltip>
    </div>

    <div class="grid-2 mt-16">
      <div class="kb-card">
        <div class="panel-title">耗时构成（各阶段平均）</div>
        <div ref="pieRef" class="chart chart--pie" />
        <p class="hint text-tertiary text-sm">
          五段合计
          <strong class="text-mono">{{ fmt(stageTotal) }}</strong>
          ；其中数据层（数据库 + 缓存）
          <strong class="text-mono">{{ fmt(dataLayerTotal) }}</strong>
          。命中 FAQ 缓存时直出，平均
          <strong class="text-mono">{{ fmt(data?.breakdown.faqMs ?? 0) }}</strong>
          ，不经过检索与生成。
        </p>
      </div>

      <div class="kb-card">
        <div class="panel-title">平均耗时趋势</div>
        <div ref="lineRef" class="chart" />
        <el-empty v-if="data && !data.trend.length" description="暂无数据" :image-size="60" />
      </div>
    </div>

    <!-- 阶段明细：给出所属层、单位、占比与每段到底在做什么 -->
    <div class="kb-card mt-16">
      <div class="panel-title">阶段耗时明细</div>
      <el-table :data="stageRows" size="small">
        <el-table-column label="阶段" width="130">
          <template #default="{ row }">
            <span class="stage-name">
              <span class="stage-name__dot" :style="{ background: row.color }" />
              {{ row.name }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="所属层" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" type="info" class="layer-tag">
              {{ row.layer }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="平均耗时" width="120" align="right">
          <template #header>
            <el-tooltip content="该阶段在统计区间内的平均单次耗时" placement="top">
              <span class="th-help">平均耗时</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num num--strong">{{ fmt(row.avgMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="占比" width="120" align="right">
          <template #header>
            <el-tooltip content="该阶段平均耗时 / 五段合计" placement="top">
              <span class="th-help">占比</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="share">
              <span class="text-mono num">{{ row.share }}</span>
              <span class="share__bar">
                <span class="share__fill" :style="{ width: row.share, background: row.color }" />
              </span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="320">
          <template #default="{ row }">
            <span class="text-secondary">{{ row.desc }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 最慢问题：给出各阶段耗时、其他开销与构成条 -->
    <div class="kb-card mt-16">
      <div class="panel-title">最慢问题 TOP 10</div>
      <el-table :data="data?.slowest ?? []" size="small" stripe>
        <el-table-column type="index" width="48" align="center" />
        <el-table-column prop="question" label="问题" min-width="170" show-overflow-tooltip />
        <el-table-column label="端到端" width="100" align="right" sortable :sort-by="'responseMs'">
          <template #header>
            <el-tooltip content="从收到请求到 SSE done 的总耗时" placement="top">
              <span class="th-help">端到端</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num num--strong">{{ fmt(row.responseMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="检索" width="86" align="right">
          <template #header>
            <el-tooltip
              content="AI /internal/retrieve：向量化 + 召回 + 关键词检索 + RRF 融合"
              placement="top"
            >
              <span class="th-help">检索</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num">{{ fmt(row.retrieveMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="生成" width="86" align="right">
          <template #header>
            <el-tooltip
              content="AI /internal/generate：Prompt 组装 + 云 LLM 流式生成"
              placement="top"
            >
              <span class="th-help">生成</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num">{{ fmt(row.generateMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="数据库" width="86" align="right">
          <template #header>
            <el-tooltip content="MySQL：四维数据权限过滤 + 知识单元加载" placement="top">
              <span class="th-help">数据库</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num">{{ fmt(row.dbMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="缓存" width="86" align="right">
          <template #header>
            <el-tooltip content="Redis：FAQ 命中查询" placement="top">
              <span class="th-help">缓存</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num">{{ fmt(row.cacheMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="其他" width="86" align="right">
          <template #header>
            <el-tooltip
              content="端到端 − 检索 − 生成 − 数据库 − 缓存：JWT 校验、SSE 透传等"
              placement="top"
            >
              <span class="th-help">其他</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="text-mono num text-tertiary">{{ fmt(otherOf(row)) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="构成" width="120">
          <template #header>
            <el-tooltip
              content="五段耗时在端到端中的占比（蓝=检索 / 绿=生成 / 橙=数据库 / 紫=缓存 / 灰=其他）"
              placement="top"
            >
              <span class="th-help">构成</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span class="split-bar">
              <span
                v-for="m in STAGE_META"
                :key="m.key"
                class="split-bar__seg"
                :style="{
                  width: pctOf(m.key === 'otherMs' ? otherOf(row) : row[m.key], row.responseMs),
                  background: m.color,
                }"
              />
            </span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="112">
          <template #header>
            <el-tooltip content="按实际执行到的链路判定，而非仅看是否命中 FAQ" placement="top">
              <span class="th-help">来源</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <el-tooltip :content="sourceTag(row).tip" placement="top">
              <el-tag size="small" :type="sourceTag(row).type" effect="plain">
                {{ sourceTag(row).text }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="createdAt" label="时间" width="130" />
        <template #empty>
          <el-empty description="暂无问答记录" :image-size="60" />
        </template>
      </el-table>
      <p class="hint text-tertiary text-sm">
        数据层（数据库 + 缓存）在单次问答中通常只有个位数毫秒；若该项异常升高，优先排查 MySQL
        慢查询与 Redis 连接池。
      </p>
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-3) var(--space-4);
}
.toolbar__group {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.toolbar__label {
  font-size: 12px;
  line-height: var(--line-height-tight);
  color: var(--color-text-tertiary);
}

.cards {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: var(--space-3);
  margin-top: var(--space-4);
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

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1.4fr;
  gap: var(--space-4);
}
.chart {
  height: 268px;
}
.chart--pie {
  height: 216px;
}

.hint {
  margin: var(--space-2) 0 0;
  line-height: var(--line-height-base);
}

/* 阶段名：色点 + 名称，色点用于与图表、构成条对应 */
.stage-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.stage-name__dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex-shrink: 0;
}
.layer-tag {
  --el-tag-font-size: 11px;
}

/* 占比列：数值在上、细进度条占满整列宽 —— 小占比也能看清 */
.share {
  display: inline-flex;
  flex-direction: column;
  align-items: stretch;
  gap: 3px;
  width: 100%;
}
.share__bar {
  height: 4px;
  border-radius: 2px;
  background: var(--color-surface, #f2f3f5);
  overflow: hidden;
}
.share__fill {
  display: block;
  height: 100%;
  border-radius: 2px;
  transition: width 0.25s ease;
}

/* 数字列：等宽 + 右对齐，位数对齐后更容易竖向比较 */
.num {
  font-variant-numeric: tabular-nums;
  line-height: var(--line-height-table);
}
.num--strong {
  font-weight: 600;
}

/* 列头带说明时的提示样式 */
.th-help {
  border-bottom: 1px dashed var(--color-border-strong, #d8dade);
  cursor: help;
}

/* 耗时构成条：五段堆叠，一眼看出谁占大头 */
.split-bar {
  display: flex;
  height: 6px;
  border-radius: 3px;
  overflow: hidden;
  background: var(--color-surface, #f7f8fa);
}
.split-bar__seg {
  height: 100%;
  transition: width 0.2s ease;
}

@media (max-width: 1280px) {
  .cards {
    grid-template-columns: repeat(3, 1fr);
  }
  .grid-2 {
    grid-template-columns: 1fr;
  }
}
</style>
