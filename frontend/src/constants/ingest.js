/**
 * 入库流水线常量：状态展示元数据 + 阶段码文案。
 *
 * 状态口径对齐 DESIGN.md §6.1：
 *   status       业务可用性 active / disabled / deleting / superseded
 *   ingestStatus 入库进度   draft / queued / parsing / parsed / embedding / active / failed
 */

/** 状态展示元数据：文案 / 颜色变量 / 图标组件名 / 是否终态 */
const STAGE_META = {
  draft: { label: '草稿', color: 'var(--stage-queued)', icon: 'Document', terminal: false },
  queued: { label: '排队中', color: 'var(--stage-queued)', icon: 'Clock', terminal: false },
  parsing: { label: '解析中', color: 'var(--stage-running)', icon: 'Loading', terminal: false },
  parsed: { label: '待人工质检', color: 'var(--stage-review)', icon: 'Warning', terminal: true },
  embedding: { label: '向量化中', color: 'var(--stage-running)', icon: 'Loading', terminal: false },
  active: { label: '已就绪', color: 'var(--stage-ready)', icon: 'CircleCheck', terminal: true },
  failed: { label: '失败', color: 'var(--stage-failed)', icon: 'CircleClose', terminal: true },
  disabled: { label: '已下线', color: 'var(--stage-queued)', icon: 'Remove', terminal: true },
  deleting: { label: '删除中', color: 'var(--stage-running)', icon: 'Delete', terminal: false },
  superseded: {
    label: '已被替换',
    color: 'var(--stage-queued)',
    icon: 'RefreshLeft',
    terminal: true,
  },
}

/** 未知状态的兜底元数据 */
const FALLBACK_META = {
  label: '未知',
  color: 'var(--stage-queued)',
  icon: 'QuestionFilled',
  terminal: false,
}

/** 已进入终态、无需继续轮询的 ingestStatus */
export const TERMINAL_STATUSES = ['active', 'failed', 'parsed', 'disabled']

/** 取状态展示元数据；未知状态回退为灰色"未知"并保留原始码作为文案 */
export function stageMeta(status) {
  if (!status) return FALLBACK_META
  return STAGE_META[status] ?? { ...FALLBACK_META, label: status }
}

/** 判断字符串是否为已知入库状态 */
export function isIngestStatus(value) {
  return value in STAGE_META
}

/** 入库阶段码 → 中文文案 */
const STAGE_TEXT = {
  upload: '上传',
  parse: '解析',
  chunk_qc: '切片质检',
  classify: '分类建议',
  auto_review: 'AI 质检',
  quality_gate: '待人工质检',
  embed: '向量化',
  finalize: '收尾',
}

/** 阶段码转中文；未知码原样返回 */
export function stageText(stage) {
  if (!stage) return ''
  return STAGE_TEXT[stage] ?? stage
}

/** 入库流水线阶段顺序（时间线/进度展示用） */
export const PIPELINE_STAGES = ['upload', 'parse', 'chunk_qc', 'classify', 'embed', 'finalize']
