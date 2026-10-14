<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createDepartment, fetchDepartmentTree, fetchUsers, updateDepartment } from '@/api/org'
import PageHeader from '@/components/PageHeader.vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const canEdit = userStore.has('org:dept:edit')

const loading = ref(false)
const tree = ref([])

const dialogVisible = ref(false)
const dialogTitle = ref('新增部门')
const editingId = ref(null)
const formRef = ref()
const form = reactive({
  name: '',
  parentId: undefined,
  leaderId: undefined,
  sortOrder: 0,
})

const rules = {
  name: [{ required: true, message: '请输入部门名称', trigger: 'blur' }],
}

const userOptions = ref([])
const userLoading = ref(false)

/** 部门总数（含子部门） */
const deptCount = computed(() => {
  const count = (nodes) =>
    nodes.reduce((sum, n) => sum + 1 + (n.children?.length ? count(n.children) : 0), 0)
  return count(tree.value)
})

async function load() {
  loading.value = true
  try {
    tree.value = await fetchDepartmentTree()
  } finally {
    loading.value = false
  }
}

function openCreate(parent) {
  dialogTitle.value = parent ? `新增子部门 · ${parent.name}` : '新增部门'
  editingId.value = null
  Object.assign(form, {
    name: '',
    parentId: parent?.id,
    leaderId: undefined,
    sortOrder: 0,
  })
  dialogVisible.value = true
}

function openEdit(node) {
  dialogTitle.value = `编辑部门 · ${node.name}`
  editingId.value = node.id
  Object.assign(form, {
    name: node.name,
    parentId: node.parentId ?? undefined,
    leaderId: node.leaderId ?? undefined,
    sortOrder: node.sortOrder,
  })
  dialogVisible.value = true
}

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  const body = {
    name: form.name,
    parentId: form.parentId ?? null,
    leaderId: form.leaderId ?? null,
    sortOrder: form.sortOrder,
  }
  if (editingId.value) {
    await updateDepartment(editingId.value, body)
    ElMessage.success('已更新')
  } else {
    await createDepartment(body)
    ElMessage.success('已创建')
  }
  dialogVisible.value = false
  load()
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

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="部门管理" description="组织架构树与负责人维护">
      <template #actions>
        <el-button v-if="canEdit" type="primary" :icon="'Plus'" @click="openCreate()">
          新增部门
        </el-button>
      </template>
    </PageHeader>

    <div class="kb-card">
      <div class="panel-head">
        <div class="panel-title">组织架构</div>
        <span class="text-tertiary text-sm">共 {{ deptCount }} 个部门</span>
      </div>
      <el-tree
        v-loading="loading"
        :data="tree"
        node-key="id"
        :props="{ label: 'name', children: 'children' }"
        default-expand-all
        :expand-on-click-node="false"
      >
        <template #default="{ data }">
          <div class="dept-node">
            <span class="dept-node__name">
              <el-icon><OfficeBuilding /></el-icon>
              {{ data.name }}
            </span>
            <span class="dept-node__meta">
              <el-tag size="small" effect="plain" :type="data.leaderName ? 'info' : 'warning'">
                {{ data.leaderName ? `负责人 ${data.leaderName}` : '未设负责人' }}
              </el-tag>
              <span class="text-tertiary dept-node__sort">排序 {{ data.sortOrder }}</span>
            </span>
            <span v-if="canEdit" class="dept-node__ops">
              <el-button link type="primary" size="small" @click.stop="openCreate(data)">
                加子部门
              </el-button>
              <el-button link type="primary" size="small" @click.stop="openEdit(data)">
                编辑
              </el-button>
            </span>
          </div>
        </template>
      </el-tree>
      <el-empty v-if="!loading && !tree.length" description="暂无部门数据" />
    </div>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="460px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="部门名称" prop="name">
          <el-input v-model="form.name" placeholder="如：售后部" />
        </el-form-item>
        <el-form-item label="上级部门">
          <el-tree-select
            v-model="form.parentId"
            :data="tree"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            check-strictly
            clearable
            class="w-full"
            placeholder="不选则为顶级部门"
          />
        </el-form-item>
        <el-form-item label="负责人">
          <el-select
            v-model="form.leaderId"
            filterable
            remote
            clearable
            :remote-method="searchUsers"
            :loading="userLoading"
            class="w-full"
            placeholder="搜索用户"
          >
            <el-option
              v-for="u in userOptions"
              :key="u.id"
              :label="`${u.displayName}（${u.username}）`"
              :value="u.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.sortOrder" :min="0" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="onSubmit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.panel-head .panel-title {
  margin-bottom: 0;
}
/* 每行做成"列表行"：高度统一、hover 有反馈、操作区靠右 */
.dept-node {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 34px;
  padding-right: 8px;
}
.dept-node__name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  color: var(--color-text-primary, #1f2328);
}
.dept-node__meta {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-3);
}
/* 排序值做成等宽小字，与 tag 拉开层级 */
.dept-node__sort {
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--color-text-tertiary);
  min-width: 56px;
  text-align: right;
}
/* 操作按钮默认淡出，hover 整行才出现，避免视觉噪音 */
.dept-node__ops {
  display: inline-flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.dept-node:hover .dept-node__ops {
  opacity: 1;
}
:deep(.el-tree-node__content) {
  height: 40px;
  border-radius: 4px;
}
:deep(.el-tree-node__content:hover) {
  background: var(--color-surface, #f7f8fa);
}
</style>
