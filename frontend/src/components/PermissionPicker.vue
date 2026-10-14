<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchDepartmentTree, fetchRoles, fetchUsers } from '@/api/org'
import { PERMISSION_TARGET_LABELS } from '@/constants/permission'

// 四维数据权限选择器：global / department / role / user（OR 组合）
const props = defineProps({
  modelValue: { type: Array, required: true },
  /** 是否允许选择 global（全局可见） */
  allowGlobal: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue'])

const departments = ref([])
const roles = ref([])
const userOptions = ref([])
const userLoading = ref(false)

const targetType = ref('department')
const deptId = ref(undefined)
const roleId = ref(undefined)
const userId = ref(undefined)

const items = computed(() => props.modelValue)
const hasGlobal = computed(() => items.value.some((i) => i.targetType === 'global'))

function deptName(id) {
  const walk = (list) => {
    for (const d of list) {
      if (d.id === id) return d.name
      if (d.children?.length) {
        const found = walk(d.children)
        if (found) return found
      }
    }
    return null
  }
  return walk(departments.value) ?? `部门#${id}`
}

function targetLabel(item) {
  switch (item.targetType) {
    case 'global':
      return '全局可见'
    case 'department':
      return deptName(item.targetId)
    case 'role':
      return roles.value.find((r) => r.id === item.targetId)?.roleName ?? `角色#${item.targetId}`
    case 'user':
      return (
        userOptions.value.find((u) => u.id === item.targetId)?.displayName ??
        `用户#${item.targetId}`
      )
    default:
      return String(item.targetId)
  }
}

function isDuplicate(item) {
  return items.value.some((i) => i.targetType === item.targetType && i.targetId === item.targetId)
}

function addItem(item) {
  if (isDuplicate(item)) {
    ElMessage.warning('该授权对象已存在')
    return
  }
  emit('update:modelValue', [...items.value, item])
}

function removeItem(index) {
  const next = [...items.value]
  next.splice(index, 1)
  emit('update:modelValue', next)
}

function addGlobal() {
  if (hasGlobal.value) return
  addItem({ targetType: 'global', targetId: 0 })
}

function removeGlobal() {
  const index = items.value.findIndex((i) => i.targetType === 'global')
  if (index >= 0) removeItem(index)
}

/** 复选框要双向生效：勾上=加全局，取消=去掉全局（此前只能勾不能取消，得去点标签的叉） */
function toggleGlobal(checked) {
  if (checked) addGlobal()
  else removeGlobal()
}

function addCurrent() {
  if (targetType.value === 'department') {
    if (!deptId.value) return ElMessage.warning('请选择部门')
    addItem({ targetType: 'department', targetId: deptId.value })
    deptId.value = undefined
  } else if (targetType.value === 'role') {
    if (!roleId.value) return ElMessage.warning('请选择角色')
    addItem({ targetType: 'role', targetId: roleId.value })
    roleId.value = undefined
  } else {
    if (!userId.value) return ElMessage.warning('请选择用户')
    addItem({ targetType: 'user', targetId: userId.value })
    userId.value = undefined
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

onMounted(async () => {
  const [deptRes, roleRes] = await Promise.all([fetchDepartmentTree(), fetchRoles()])
  departments.value = deptRes
  roles.value = roleRes
})
</script>

<template>
  <div class="perm-picker">
    <!-- 全局快捷 -->
    <div v-if="allowGlobal" class="perm-picker__global">
      <el-checkbox :model-value="hasGlobal" @change="toggleGlobal">
        全局可见（所有用户可访问）
      </el-checkbox>
    </div>

    <!-- 添加一行 -->
    <div class="perm-picker__add">
      <el-radio-group v-model="targetType" size="small">
        <el-radio-button value="department">部门</el-radio-button>
        <el-radio-button value="role">角色</el-radio-button>
        <el-radio-button value="user">个人</el-radio-button>
      </el-radio-group>

      <el-tree-select
        v-if="targetType === 'department'"
        v-model="deptId"
        class="perm-picker__select"
        :data="departments"
        node-key="id"
        :props="{ label: 'name', children: 'children' }"
        check-strictly
        clearable
        placeholder="选择部门"
        size="small"
      />
      <el-select
        v-else-if="targetType === 'role'"
        v-model="roleId"
        class="perm-picker__select"
        clearable
        placeholder="选择角色"
        size="small"
      >
        <el-option v-for="r in roles" :key="r.id" :label="r.roleName" :value="r.id" />
      </el-select>
      <el-select
        v-else
        v-model="userId"
        class="perm-picker__select"
        clearable
        filterable
        remote
        :remote-method="searchUsers"
        :loading="userLoading"
        placeholder="搜索用户"
        size="small"
      >
        <el-option
          v-for="u in userOptions"
          :key="u.id"
          :label="`${u.displayName}（${u.username}）`"
          :value="u.id"
        />
      </el-select>

      <el-button size="small" :icon="'Plus'" @click="addCurrent">添加</el-button>
    </div>

    <!-- 已选摘要 -->
    <div class="perm-picker__chips">
      <el-tag
        v-for="(item, index) in items"
        :key="`${item.targetType}-${item.targetId}`"
        :type="item.targetType === 'global' ? 'primary' : 'info'"
        closable
        size="small"
        @close="removeItem(index)"
      >
        {{ PERMISSION_TARGET_LABELS[item.targetType] }}：{{ targetLabel(item) }}
      </el-tag>
      <span v-if="!items.length" class="text-tertiary text-sm">尚未配置权限，默认仅管理员可见</span>
    </div>
  </div>
</template>

<style scoped>
.perm-picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.perm-picker__global {
  padding: 8px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
}
.perm-picker__add {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.perm-picker__select {
  width: 220px;
}
.perm-picker__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  min-height: 24px;
}
</style>
