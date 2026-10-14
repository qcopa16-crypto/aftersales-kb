<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { fetchGaps, updateGapStatus } from '@/api/gap'
import PageHeader from '@/components/PageHeader.vue'

const router = useRouter()
const loading = ref(false)
const gaps = ref([])

const STATUS_META = {
  pending: { label: '待处理', type: 'warning' },
  open: { label: '待处理', type: 'warning' },
  resolved: { label: '已解决', type: 'success' },
  ignored: { label: '已忽略', type: 'info' },
}

function meta(status) {
  return STATUS_META[status] ?? { label: status, type: 'info' }
}

async function load() {
  loading.value = true
  try {
    const res = await fetchGaps({ page: 1, size: 100 })
    gaps.value = res.records ?? []
  } finally {
    loading.value = false
  }
}

async function onUpdate(row, status) {
  await updateGapStatus(row.id, status)
  ElMessage.success(status === 'resolved' ? '已标记为已解决' : '已忽略')
  load()
}

/** 一键跳转入库工作台，补充该问题的知识 */
function onCreateKnowledge(row) {
  ElMessage.info(`请上传可回答「${row.questionPattern}」的知识文件`)
  router.push({ path: '/ingest', query: { gap: String(row.id), q: row.questionPattern } })
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="知识缺口" description="未命中/低置信问题聚合，用于补录知识" />

    <div class="kb-card">
      <el-table v-loading="loading" :data="gaps" size="small" stripe>
        <el-table-column
          prop="questionPattern"
          label="问题"
          min-width="300"
          show-overflow-tooltip
        />
        <el-table-column label="被问次数" width="110" sortable :sort-by="'askCount'">
          <template #default="{ row }">{{ row.askCount }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="meta(row.status).type" size="small">{{ meta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="lastAskedAt" label="最近提问" width="170">
          <template #default="{ row }">
            <span class="text-tertiary text-md">{{ row.lastAskedAt || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="onCreateKnowledge(row)">
              去建知识
            </el-button>
            <el-button
              link
              type="success"
              size="small"
              :disabled="row.status === 'resolved'"
              @click="onUpdate(row, 'resolved')"
            >
              标记已解决
            </el-button>
            <el-button
              link
              type="info"
              size="small"
              :disabled="row.status === 'ignored'"
              @click="onUpdate(row, 'ignored')"
            >
              忽略
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !gaps.length" description="暂无知识缺口" />
    </div>
  </div>
</template>
