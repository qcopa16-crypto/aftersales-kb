/**
 * 知识域常量：分类枚举 + 业务状态文案。
 *
 * 分类枚举与后端 KnowledgeCategory 一致（product / policy / sop / sales / finance）。
 */

/** 分类枚举 → 中文 */
export const CATEGORY_LABELS = {
  product: '产品手册',
  policy: '制度政策',
  sop: '操作流程',
  sales: '销售资料',
  finance: '财务资料',
}

/** 分类下拉选项 */
export const CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(([value, label]) => ({
  value,
  label,
}))

/** 分类转中文；未分类/未知码原样兜底 */
export function categoryLabel(category) {
  if (!category) return '未分类'
  return CATEGORY_LABELS[category] ?? category
}

/** 业务状态（status）→ 中文 */
export const UNIT_STATUS_LABELS = {
  active: '可用',
  disabled: '已下线',
  deleting: '删除中',
  superseded: '已被替换',
}

/** 列表页筛选用（含已被替换） */
export const UNIT_STATUS_FILTER_OPTIONS = [
  { value: 'active', label: UNIT_STATUS_LABELS.active },
  { value: 'disabled', label: UNIT_STATUS_LABELS.disabled },
  { value: 'superseded', label: UNIT_STATUS_LABELS.superseded },
]

/** 详情页编辑用（已被替换不可手动改回） */
export const UNIT_STATUS_EDIT_OPTIONS = [
  { value: 'active', label: UNIT_STATUS_LABELS.active },
  { value: 'disabled', label: UNIT_STATUS_LABELS.disabled },
]

/** 业务状态 → el-tag type */
export function unitStatusTagType(status) {
  if (status === 'active') return 'success'
  if (status === 'disabled') return 'info'
  return 'warning'
}
