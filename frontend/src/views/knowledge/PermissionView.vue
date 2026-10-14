<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { checkPermissions, fetchUnitDetail, fetchUnits, setUnitPermissions } from '@/api/knowledge'
import { fetchUsers } from '@/api/org'
import PageHeader from '@/components/PageHeader.vue'
import PermissionPicker from '@/components/PermissionPicker.vue'
import { categoryLabel } from '@/constants/knowledge'

const loading = ref(false)
const units = ref([])
const total = ref(0)
const query = reactive({ page: 1, size: 10, keyword: '' })

const selected = ref(null)
const permissions = ref([])
const saving = ref(false)

const userId = ref(undefined)
const userOptions = ref([])
const userLoading = ref(false)
const checkResult = ref(null)
const checking = ref(false)

async function loadUnits() {
  loading.value = true
  try {
    const res = await fetchUnits({ ...query })
    units.value = res.records
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function selectUnit(row) {
  selected.value = row
  checkResult.value = null
  const detail = await fetchUnitDetail(row.id)
  permissions.value = detail.permissions ? [...detail.permissions] : []
}

async function save() {
  if (!selected.value) return
  saving.value = true
  try {
    await setUnitPermissions(selected.value.id, permissions.value)
    ElMessage.success('权限已保存')
  } finally {
    saving.value = false
  }
}

async function searchUsers(keyword) {
  userLoading.value = true
  try {
    const res = await fetchUsers({ page: 1, size: 20, keyword })
    userOptions.value = res.records
  } finally {
    userLoading.value = false
  }
}

async function runCheck() {
  if (!userId.value) return ElMessage.warning('请选择要校验的用户')
  const unitIds = units.value.map((u) => u.id)
  if (!unitIds.length) return ElMessage.warning('当前页无可校验知识')
  checking.value = true
  try {
    checkResult.value = await checkPermissions(userId.value, unitIds)
  } finally {
    checking.value = false
  }
}

function unitTitle(id) {
  return units.value.find((u) => u.id === id)?.title ?? `#${id}`
}

onMounted(loadUnits)
</script>

<template>
  <div>
    <PageHeader title="权限配置" description="四维数据权限（全局/部门/角色/个人）与访问校验" />

    <div class="perm-layout">
      <!-- 知识单元选择 -->
      <aside class="kb-card perm-layout__list">
        <el-input
          v-model="query.keyword"
          placeholder="搜索知识"
          clearable
          size="small"
          :prefix-icon="'Search'"
          @keyup.enter="loadUnits"
          @clear="loadUnits"
        />
        <ul v-loading="loading" class="unit-list">
          <li
            v-for="u in units"
            :key="u.id"
            class="unit-item"
            :class="{ 'is-active': selected?.id === u.id }"
            @click="selectUnit(u)"
          >
            <div class="unit-item__title">{{ u.title }}</div>
            <div class="text-tertiary text-sm">
              {{ categoryLabel(u.category) }} · {{ u.unitCode }}
            </div>
          </li>
        </ul>
        <el-pagination
          v-model:current-page="query.page"
          :page-size="query.size"
          :total="total"
          small
          layout="prev, pager, next"
          @current-change="loadUnits"
        />
      </aside>

      <!-- 权限编辑 -->
      <section class="kb-card perm-layout__editor">
        <template v-if="selected">
          <div class="flex-between mb-16">
            <div>
              <div class="perm-layout__title">{{ selected.title }}</div>
              <div class="text-tertiary text-sm">配置该知识单元的数据访问权限</div>
            </div>
            <el-button type="primary" :loading="saving" @click="save">保存权限</el-button>
          </div>

          <PermissionPicker v-model="permissions" />

          <el-divider />

          <div class="check-demo">
            <div class="perm-layout__title">权限校验演示</div>
            <p class="text-tertiary text-sm">
              选择用户，校验其对当前页知识单元的访问权限（返回授权/未授权集合）。
            </p>
            <div class="flex gap-8" style="margin: 12px 0">
              <el-select
                v-model="userId"
                filterable
                remote
                clearable
                :remote-method="searchUsers"
                :loading="userLoading"
                placeholder="搜索用户"
                style="width: 240px"
              >
                <el-option
                  v-for="u in userOptions"
                  :key="u.id"
                  :label="`${u.displayName}（${u.username}）`"
                  :value="u.id"
                />
              </el-select>
              <el-button type="primary" :loading="checking" @click="runCheck">校验</el-button>
            </div>

            <div v-if="checkResult" class="check-result">
              <div class="check-result__group">
                <div class="check-result__head check-result__head--ok">
                  <el-icon><CircleCheck /></el-icon>
                  授权（{{ checkResult.authorizedUnitIds.length }}）
                </div>
                <el-tag
                  v-for="id in checkResult.authorizedUnitIds"
                  :key="id"
                  size="small"
                  type="success"
                  class="mr-4"
                >
                  {{ unitTitle(id) }}
                </el-tag>
                <span v-if="!checkResult.authorizedUnitIds.length" class="text-tertiary text-sm">
                  无
                </span>
              </div>
              <div class="check-result__group">
                <div class="check-result__head check-result__head--warn">
                  <el-icon><Lock /></el-icon>
                  未授权（{{ checkResult.unauthorizedUnitIds.length }}）
                </div>
                <el-tag
                  v-for="id in checkResult.unauthorizedUnitIds"
                  :key="id"
                  size="small"
                  type="warning"
                  class="mr-4"
                >
                  {{ unitTitle(id) }}
                </el-tag>
                <span v-if="!checkResult.unauthorizedUnitIds.length" class="text-tertiary text-sm">
                  无
                </span>
              </div>
            </div>
          </div>
        </template>
        <el-empty v-else description="请选择左侧知识单元进行权限配置" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.perm-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  align-items: start;
}
.perm-layout__list {
  padding: 12px;
}
.unit-list {
  list-style: none;
  margin: 12px 0;
  padding: 0;
  max-height: 460px;
  overflow-y: auto;
}
.unit-item {
  position: relative;
  padding: 10px 12px 10px 14px;
  border-radius: 4px;
  cursor: pointer;
  border: 1px solid transparent;
  transition:
    background 0.15s ease,
    border-color 0.15s ease;
}
/* 选中态用左侧主色条 + 浅底：比只换背景更明确，也不喧宾夺主 */
.unit-item.is-active::before {
  content: '';
  position: absolute;
  left: 4px;
  top: 10px;
  bottom: 10px;
  width: 2px;
  border-radius: 1px;
  background: var(--brand-500);
}
.unit-item:hover {
  background: var(--color-surface);
}
.unit-item.is-active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.unit-item__title {
  font-size: 13px;
  font-weight: 500;
  line-height: var(--line-height-tight);
  margin-bottom: 2px;
}
.perm-layout__list :deep(.el-pagination) {
  justify-content: center;
  margin-top: 12px;
}
.perm-layout__title {
  font-size: 14px;
  font-weight: 600;
}
.check-result__group {
  margin-bottom: 12px;
}
.check-result__head {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 500;
  margin-bottom: 6px;
}
.check-result__head--ok {
  color: var(--color-success);
}
.check-result__head--warn {
  color: var(--color-warning);
}
.mr-4 {
  margin: 0 4px 4px 0;
}
</style>
