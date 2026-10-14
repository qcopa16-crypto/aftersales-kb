<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createUser,
  fetchDepartmentTree,
  fetchRoles,
  fetchUsers,
  resetUserPassword,
  updateUser,
} from '@/api/org'
import PageHeader from '@/components/PageHeader.vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const canEdit = userStore.has('org:user:edit')

const loading = ref(false)
const list = ref([])
const total = ref(0)
const query = reactive({ page: 1, size: 20, keyword: '' })

const departments = ref([])
const roles = ref([])

const dialogVisible = ref(false)
const dialogTitle = ref('新增用户')
const editingId = ref(null)
const formRef = ref()
const form = reactive({
  username: '',
  password: '',
  displayName: '',
  departmentId: undefined,
  status: 1,
  roleIds: [],
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  displayName: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入初始密码', trigger: 'blur' }],
}

async function load() {
  loading.value = true
  try {
    const res = await fetchUsers({ ...query })
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

function openCreate() {
  dialogTitle.value = '新增用户'
  editingId.value = null
  Object.assign(form, {
    username: '',
    password: '123456',
    displayName: '',
    departmentId: undefined,
    status: 1,
    roleIds: [],
  })
  dialogVisible.value = true
}

function openEdit(row) {
  dialogTitle.value = `编辑用户 · ${row.displayName}`
  editingId.value = row.id
  Object.assign(form, {
    username: row.username,
    password: '',
    displayName: row.displayName,
    departmentId: row.departmentId ?? undefined,
    status: row.status,
    roleIds: [...row.roleIds],
  })
  dialogVisible.value = true
}

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  if (editingId.value) {
    await updateUser(editingId.value, {
      displayName: form.displayName,
      departmentId: form.departmentId ?? null,
      status: form.status,
      roleIds: form.roleIds,
    })
    ElMessage.success('已更新')
  } else {
    await createUser({
      username: form.username,
      password: form.password,
      displayName: form.displayName,
      departmentId: form.departmentId ?? null,
      roleIds: form.roleIds,
    })
    ElMessage.success('已创建')
  }
  dialogVisible.value = false
  load()
}

async function onResetPassword(row) {
  await ElMessageBox.confirm(`确认重置「${row.displayName}」的密码？`, '重置密码', {
    type: 'warning',
  })
  const res = await resetUserPassword(row.id)
  ElMessageBox.alert(`新密码：${res.password}`, '重置成功', { confirmButtonText: '我已记录' })
}

onMounted(async () => {
  const [deptRes, roleRes] = await Promise.all([fetchDepartmentTree(), fetchRoles()])
  departments.value = deptRes
  roles.value = roleRes
  load()
})
</script>

<template>
  <div>
    <PageHeader title="用户管理" description="账号、部门归属与角色分配">
      <template #actions>
        <el-button v-if="canEdit" type="primary" :icon="'Plus'" @click="openCreate">
          新增用户
        </el-button>
      </template>
    </PageHeader>

    <div class="kb-card">
      <div class="toolbar">
        <div class="toolbar__left">
          <el-input
            v-model="query.keyword"
            placeholder="搜索用户名/姓名"
            clearable
            style="width: 260px"
            :prefix-icon="'Search'"
            @keyup.enter="onSearch"
            @clear="onSearch"
          />
          <el-button :icon="'Search'" @click="onSearch">查询</el-button>
        </div>
        <span class="text-tertiary text-sm">共 {{ total }} 个用户</span>
      </div>

      <el-table v-loading="loading" :data="list" size="small" stripe>
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column prop="displayName" label="姓名" min-width="100" />
        <el-table-column prop="departmentName" label="部门" min-width="120">
          <template #default="{ row }">{{ row.departmentName || '—' }}</template>
        </el-table-column>
        <el-table-column label="角色" min-width="180">
          <template #default="{ row }">
            <el-tag
              v-for="r in row.roleNames?.length ? row.roleNames : row.roles"
              :key="r"
              size="small"
              type="info"
              effect="plain"
              class="mr-4"
            >
              {{ r }}
            </el-tag>
            <span v-if="!row.roles?.length" class="text-tertiary">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 1 ? 'success' : 'info'" size="small">
              {{ row.status === 1 ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="onResetPassword(row)">
              重置密码
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

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="480px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" :disabled="!!editingId" placeholder="登录账号" />
        </el-form-item>
        <el-form-item v-if="!editingId" label="初始密码" prop="password">
          <el-input v-model="form.password" placeholder="初始密码" />
        </el-form-item>
        <el-form-item label="姓名" prop="displayName">
          <el-input v-model="form.displayName" placeholder="显示姓名" />
        </el-form-item>
        <el-form-item label="部门">
          <el-tree-select
            v-model="form.departmentId"
            :data="departments"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            check-strictly
            clearable
            class="w-full"
            placeholder="选择部门"
          />
        </el-form-item>
        <el-form-item label="角色">
          <el-select
            v-model="form.roleIds"
            multiple
            clearable
            class="w-full"
            placeholder="选择角色"
          >
            <el-option v-for="r in roles" :key="r.id" :label="r.roleName" :value="r.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editingId" label="状态">
          <el-radio-group v-model="form.status">
            <el-radio :value="1">启用</el-radio>
            <el-radio :value="0">禁用</el-radio>
          </el-radio-group>
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
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.toolbar__left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.mr-4 {
  margin-right: 4px;
}
</style>
