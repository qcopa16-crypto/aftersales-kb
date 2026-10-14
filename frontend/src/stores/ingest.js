import { ref } from 'vue'
import { defineStore } from 'pinia'
import {
  fetchBatchDetail,
  fetchImportBatches,
  importKnowledge,
  qualityGate,
  retryImport,
} from '@/api/knowledge'
import { TERMINAL_STATUSES } from '@/constants/ingest'

const POLL_INTERVAL_MS = 5000

/** 入库工作台状态：批次列表 + 当前批次逐文件进度 + 轮询 */
export const useIngestStore = defineStore('ingest', () => {
  const batches = ref([])
  const batchTotal = ref(0)
  const currentBatchId = ref('')
  const files = ref([])
  const loadingBatches = ref(false)
  const loadingFiles = ref(false)
  const uploading = ref(false)

  let timer = null

  async function loadBatches(page = 1, size = 20) {
    loadingBatches.value = true
    try {
      const res = await fetchImportBatches({ page, size })
      batches.value = res.records
      batchTotal.value = res.total
    } finally {
      loadingBatches.value = false
    }
  }

  async function loadBatchDetail(batchId) {
    currentBatchId.value = batchId
    loadingFiles.value = true
    try {
      const res = await fetchBatchDetail(batchId)
      // 后端返回 { batchId, files }，不是数组；必须取 .files 再交给 el-table
      files.value = Array.isArray(res) ? res : (res?.files ?? [])
    } finally {
      loadingFiles.value = false
    }
  }

  /** 上传多文件，返回批次摘要 */
  async function upload(list, category) {
    uploading.value = true
    try {
      const res = await importKnowledge(list, category)
      await loadBatches()
      await loadBatchDetail(res.batchId)
      startPolling()
      return res
    } finally {
      uploading.value = false
    }
  }

  /** 是否存在仍在流水线中的文件（决定是否继续轮询） */
  function hasInFlight() {
    // 防御：files 非数组时（异常响应）不要抛错，否则轮询永不停止
    if (!Array.isArray(files.value)) return false
    return files.value.some((f) => !TERMINAL_STATUSES.includes(f.ingestStatus))
  }

  function startPolling() {
    stopPolling()
    // 轮询当前批次进度；全部到终态则自动停止
    timer = setInterval(async () => {
      if (!currentBatchId.value) return
      try {
        await loadBatchDetail(currentBatchId.value)
      } catch {
        stopPolling()
        return
      }
      if (!hasInFlight()) stopPolling()
    }, POLL_INTERVAL_MS)
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  /** 质检确认/调整 */
  async function confirmQualityGate(unitId, action, category, permissions) {
    await qualityGate(unitId, action, category, permissions)
    if (currentBatchId.value) await loadBatchDetail(currentBatchId.value)
  }

  /** 失败重试 */
  async function retry(unitId, fromStage) {
    await retryImport(unitId, fromStage)
    if (currentBatchId.value) await loadBatchDetail(currentBatchId.value)
    startPolling()
  }

  return {
    batches,
    batchTotal,
    currentBatchId,
    files,
    loadingBatches,
    loadingFiles,
    uploading,
    loadBatches,
    loadBatchDetail,
    upload,
    startPolling,
    stopPolling,
    confirmQualityGate,
    retry,
  }
})
