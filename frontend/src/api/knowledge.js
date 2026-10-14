import { del, get, post, put, request } from './http'

/* ---------------- 知识单元 ---------------- */

/** 知识单元分页列表 */
export function fetchUnits(params) {
  return get('/knowledge/units', params)
}

/** 知识单元详情 */
export function fetchUnitDetail(id) {
  return get(`/knowledge/units/${id}`)
}

/** 更新知识单元（改正文会触发 re-ingest） */
export function updateUnit(id, body) {
  return put(`/knowledge/units/${id}`, body)
}

/** 批量删除知识单元 */
export function deleteUnits(ids) {
  return del('/knowledge/units', { ids })
}

/* ---------------- 导入与入库 ---------------- */

/** 批量导入（异步，仅接收文件并建批次） */
export function importKnowledge(files, category) {
  const form = new FormData()
  files.forEach((f) => form.append('files', f))
  if (category) form.append('category', category)
  return request({
    url: '/knowledge/import',
    method: 'POST',
    data: form,
    timeout: 120000,
  })
}

/** 入库批次列表 */
export function fetchImportBatches(params) {
  return get('/knowledge/import/batches', params)
}

/** 批次详情：逐文件阶段进度（返回 { batchId, files }，不是数组） */
export function fetchBatchDetail(batchId) {
  return get(`/knowledge/import/batches/${batchId}`)
}

/** 质检门确认/调整 */
export function qualityGate(unitId, action, category, permissions) {
  return post(`/knowledge/import/quality-gate/${unitId}`, { action, category, permissions })
}

/** 失败重试 */
export function retryImport(unitId, fromStage) {
  return post(`/knowledge/import/retry/${unitId}`, fromStage ? { fromStage } : {})
}

/* ---------------- 数据权限 ---------------- */

/** 设置知识单元四维数据权限 */
export function setUnitPermissions(id, items) {
  return post(`/knowledge/units/${id}/permissions`, { mode: 'replace', items })
}

/** 校验指定用户对一组知识单元的访问权限 */
export function checkPermissions(userId, unitIds) {
  return post('/knowledge/check-permissions', { userId, unitIds })
}
