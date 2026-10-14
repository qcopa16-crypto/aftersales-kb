<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  assignRolePermissions,
  createRole,
  fetchRolePermissions,
  fetchRoles,
  updateRole,
} from '@/api/org'
import PageHeader from '@/components/PageHeader.vue'
import { PERMISSION_GROUPS } from '@/constants/permission'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const canEdit = userStore.has('org:role:edit')

const loading = ref(false)
const roles = ref([])

/* ---- 角色新增/编辑 ---- */
const roleDialog = ref(false)
const roleDialogTitle = ref('新增角色')
const editingRoleId = ref(null)
const roleFormRef = ref()
const roleForm = reactive({ roleName: '', roleCode: '', description: '' })
const roleRules = {
  roleName: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
  roleCode: [{ required: true, message: '请输入角色编码', trigger: 'blur' }],
}

/* ---- 权限分配 ---- */
const permDrawer = ref(false)
const permRole = ref(null)
const checked = ref([])
const savingPerm = ref(false)

async function load() {
  loading.value = true
  try {
    roles.value = await fetchRoles()
  } finally {
    loading.value = false
  }
}

function openCreateRole() {
  roleDialogTitle.value = '新增角色'
  editingRoleId.value = null
  Object.assign(roleForm, { roleName: '', roleCode: '', description: '' })
  roleDialog.value = true
}

function openEditRole(row) {
  roleDialogTitle.value = `编辑角色 · ${row.roleName}`
  editingRoleId.value = row.id
  Object.assign(roleForm, {
    roleName: row.roleName,
    roleCode: row.roleCode,
    description: row.description,
  })
  roleDialog.value = true
}

async function submitRole() {
  if (!roleFormRef.value) return
  const valid = await roleFormRef.value.validate().catch(() => false)
  if (!valid) return
  if (editingRoleId.value) {
    await updateRole(editingRoleId.value, { ...roleForm })
    ElMessage.success('已更新')
  } else {
    await createRole({ ...roleForm })
    ElMessage.success('已创建')
  }
  roleDialog.value = false
  load()
}

async function openPermissions(row) {
  permRole.value = row
  checked.value = await fetchRolePermissions(row.id)
  permDrawer.value = true
}

async function savePermissions() {
  if (!permRole.value) return
  savingPerm.value = true
  try {
    await assignRolePermissions(permRole.value.id, 'replace', checked.value)
    ElMessage.success('权限已保存')
    permDrawer.value = false
    load()
  } finally {
    savingPerm.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="角色权限" description="角色管理与操作权限码分配">
      <template #actions>
        <el-button v-if="canEdit" type="primary" :icon="'Plus'" @click="openCreateRole">
          新增角色
        </el-button>
      </template>
    </PageHeader>

    <div class="kb-card">
      <div class="panel-head">
        <div class="panel-title">角色与权限</div>
        <span class="text-tertiary text-sm">共 {{ roles.length }} 个角色</span>
      </div>
      <el-table v-loading="loading" :data="roles" size="small" stripe>
        <el-table-column label="角色名称" width="170">
          <template #default="{ row }">
            <div class="role-cell">
              <span class="role-cell__badge">{{ row.roleName.slice(0, 1) }}</span>
              <span>{{ row.roleName }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="roleCode" label="编码" width="150">
          <template #default="{ row }">
            <span class="text-mono text-md">{{ row.roleCode }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
        <el-table-column label="权限概览" min-width="300">
          <template #default="{ row }">
            <el-tag
              v-for="p in (row.permissions ?? []).slice(0, 3)"
              :key="p"
              size="small"
              effect="plain"
              class="mr-4"
            >
              {{ p }}
            </el-tag>
            <span v-if="(row.permissions?.length ?? 0) > 3" class="text-tertiary text-sm">
              +{{ row.permissions.length - 3 }}
            </span>
            <span v-if="!row.permissions?.length" class="text-tertiary">—</span>
          </template>
        </el-table-column>
        <el-table-column label="权限数" width="90" align="right">
          <template #default="{ row }">
            <span class="text-mono">{{ row.permissions?.length ?? 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="canEdit" label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEditRole(row)">编辑</el-button>
            <el-button link type="primary" size="small" @click="openPermissions(row)">
              分配权限
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 角色编辑 -->
    <el-dialog v-model="roleDialog" :title="roleDialogTitle" width="460px">
      <el-form ref="roleFormRef" :model="roleForm" :rules="roleRules" label-width="80px">
        <el-form-item label="角色名称" prop="roleName">
          <el-input v-model="roleForm.roleName" placeholder="如：售后工程师" />
        </el-form-item>
        <el-form-item label="角色编码" prop="roleCode">
          <el-input
            v-model="roleForm.roleCode"
            placeholder="如：after_sales"
            :disabled="!!editingRoleId"
          />
        </el-form-item>
        <el-form-item label="说明">
          <el-input
            v-model="roleForm.description"
            type="textarea"
            :rows="2"
            placeholder="角色职责说明"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialog = false">取消</el-button>
        <el-button type="primary" @click="submitRole">保存</el-button>
      </template>
    </el-dialog>

    <!-- 权限分配 -->
    <el-drawer v-model="permDrawer" :title="`分配权限 · ${permRole?.roleName ?? ''}`" size="420px">
      <div v-for="group in PERMISSION_GROUPS" :key="group.label" class="perm-group">
        <div class="perm-group__title">{{ group.label }}</div>
        <el-checkbox-group v-model="checked">
          <el-checkbox
            v-for="c in group.codes"
            :key="c.code"
            :value="c.code"
            class="perm-group__item"
          >
            <span>{{ c.label }}</span>
            <span class="text-mono text-tertiary text-sm">{{ c.code }}</span>
          </el-checkbox>
        </el-checkbox-group>
      </div>
      <template #footer>
        <el-button @click="permDrawer = false">取消</el-button>
        <el-button type="primary" :loading="savingPerm" @click="savePermissions">
          保存权限
        </el-button>
      </template>
    </el-drawer>
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
/* 角色名首字做成方形徽标，列表更容易扫读 */
.role-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.role-cell__badge {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border-radius: 4px;
  background: rgba(62, 99, 221, 0.1);
  color: var(--brand-500, #3e63dd);
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.mr-4 {
  margin-right: 4px;
}
.perm-group {
  margin-bottom: 16px;
}
.perm-group__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}
.perm-group__item {
  display: flex;
  width: 100%;
  height: 30px;
}
.perm-group__item :deep(.el-checkbox__label) {
  display: flex;
  justify-content: space-between;
  width: 100%;
  padding-right: 8px;
}
</style>
