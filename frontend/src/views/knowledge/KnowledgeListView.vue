<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteUnits, fetchUnits } from '@/api/knowledge'
import PageHeader from '@/components/PageHeader.vue'
import StageProgress from '@/components/StageProgress.vue'
import { useUserStore } from '@/stores/user'
import {
  CATEGORY_OPTIONS,
  UNIT_STATUS_FILTER_OPTIONS,
  UNIT_STATUS_LABELS,
  categoryLabel,
  unitStatusTagType,
} from '@/constants/knowledge'

const router = useRouter()
const userStore = useUserStore()
const canImport = userStore.has('knowledge:import')
const canDelete = userStore.has('knowledge:delete')

const loading = ref(false)
const list = ref([])
const total = ref(0)
const selected = ref([])
const query = reactive({ page: 1, size: 20, keyword: '', category: '', status: '' })

async function load() {
  loading.value = true
  try {
    const res = await fetchUnits({ ...query })
    list.value = res.records
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function onSearch() {
  query.page = 1
  load()
}

function goDetail(row) {
  router.push(`/knowledge/${row.id}`)
}

async function onDelete(rows) {
  if (!rows.length) return ElMessage.warning('请选择要删除的知识')
  await ElMessageBox.confirm(
    `确认删除选中的 ${rows.length} 条知识？将级联删除向量数据，不可恢复。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  await deleteUnits(rows.map((r) => r.id))
  ElMessage.success('已提交删除')
  selected.value = []
  load()
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="知识管理" description="知识单元检索、状态与入库进度">
      <template #actions>
        <el-button
          v-if="canDelete"
          :icon="'Delete'"
          :disabled="!selected.length"
          @click="onDelete(selected)"
        >
          删除
        </el-button>
        <el-button v-if="canImport" type="primary" :icon="'Upload'" @click="router.push('/ingest')">
          批量导入
        </el-button>
      </template>
    </PageHeader>

    <div class="kb-card">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="搜索标题/编码"
          clearable
          style="width: 240px"
          :prefix-icon="'Search'"
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-select
          v-model="query.category"
          placeholder="分类"
          clearable
          style="width: 140px"
          @change="onSearch"
        >
          <el-option
            v-for="c in CATEGORY_OPTIONS"
            :key="c.value"
            :label="c.label"
            :value="c.value"
          />
        </el-select>
        <el-select
          v-model="query.status"
          placeholder="状态"
          clearable
          style="width: 130px"
          @change="onSearch"
        >
          <el-option
            v-for="s in UNIT_STATUS_FILTER_OPTIONS"
            :key="s.value"
            :label="s.label"
            :value="s.value"
          />
        </el-select>
        <el-button :icon="'Search'" @click="onSearch">查询</el-button>
      </div>

      <el-table
        v-loading="loading"
        :data="list"
        size="small"
        stripe
        @selection-change="(rows) => (selected = rows)"
      >
        <el-table-column type="selection" width="40" />
        <el-table-column prop="unitCode" label="编码" width="140">
          <template #default="{ row }">
            <span class="text-mono text-md">{{ row.unitCode }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="goDetail(row)">
              {{ row.title }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">{{ categoryLabel(row.category) }}</template>
        </el-table-column>
        <el-table-column label="业务状态" width="100">
          <template #default="{ row }">
            <el-tag :type="unitStatusTagType(row.status)" size="small">
              {{ UNIT_STATUS_LABELS[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="入库进度" width="180">
          <template #default="{ row }">
            <StageProgress
              :status="row.ingestStatus"
              :show-bar="true"
              :progress="row.ingestStatus === 'active' ? 100 : undefined"
            />
          </template>
        </el-table-column>
        <el-table-column prop="fileType" label="类型" width="80">
          <template #default="{ row }">
            <span class="text-tertiary text-md">{{ row.fileType }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="updatedAt" label="更新时间" width="160">
          <template #default="{ row }">
            <span class="text-tertiary text-md">{{ row.updatedAt }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="goDetail(row)">详情</el-button>
            <el-button v-if="canDelete" link type="danger" size="small" @click="onDelete([row])">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="onSearch"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
</style>
