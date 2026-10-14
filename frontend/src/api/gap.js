import { get, patch } from './http'

/** 知识缺口列表（后端返回分页结构 { total, page, size, records }） */
export function fetchGaps(params) {
  return get('/settlement/knowledge-gaps', params ?? {})
}

/** 更新缺口状态：resolved / ignored；resolved 时可关联解决的知识单元（F-08） */
export function updateGapStatus(id, status, resolvedUnitId) {
  const body = { status }
  if (resolvedUnitId != null) {
    body.resolvedUnitId = resolvedUnitId
  }
  return patch(`/settlement/knowledge-gaps/${id}`, body)
}
