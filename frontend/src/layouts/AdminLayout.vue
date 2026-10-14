<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { adminRoutes } from '@/router'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const collapsed = ref(false)

// 侧边菜单：按权限码过滤 + 隐藏项剔除 + 按域分组（菜单项多了不分组很难扫读）
const MENU_GROUPS = [
  { title: '概览', paths: ['/dashboard', '/latency'] },
  { title: '知识', paths: ['/knowledge', '/ingest', '/permissions'] },
  { title: '智能', paths: ['/ai', '/faq', '/gaps'] },
  { title: '组织', paths: ['/org/users', '/org/departments', '/org/roles'] },
]

const menus = computed(() => {
  const visible = adminRoutes
    .filter((r) => !r.meta?.hidden && userStore.has(r.meta?.permission))
    .map((r) => ({
      path: `/${String(r.path)}`,
      title: r.meta?.title ?? '',
      icon: r.meta?.icon ?? 'Menu',
    }))

  const groups = []
  const used = new Set()
  for (const group of MENU_GROUPS) {
    const items = visible.filter((m) => group.paths.includes(m.path))
    if (items.length) {
      groups.push({ title: group.title, items })
      items.forEach((i) => used.add(i.path))
    }
  }
  const rest = visible.filter((m) => !used.has(m.path))
  if (rest.length) {
    groups.push({ title: '其他', items: rest })
  }
  return groups
})

/** 顶栏角色展示：优先中文名，回退编码 */
const roleText = computed(() => {
  const names = userStore.roleNames.length ? userStore.roleNames : userStore.roles
  return names.join('、') || '无角色'
})

const activeMenu = computed(() => {
  // 详情页高亮所属列表菜单
  if (route.path.startsWith('/knowledge/')) return '/knowledge'
  return route.path
})

async function onLogout() {
  await ElMessageBox.confirm('确认退出登录？', '提示', { type: 'warning' })
  userStore.logout()
  router.push('/login')
}
</script>

<template>
  <div class="layout">
    <aside class="layout__aside" :class="{ 'is-collapsed': collapsed }">
      <div class="layout__brand">
        <span class="layout__logo">KB</span>
        <span v-show="!collapsed" class="layout__brand-text">售后知识库</span>
      </div>
      <el-menu
        class="layout__menu"
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        router
      >
        <el-menu-item-group v-for="g in menus" :key="g.title" :title="g.title">
          <el-menu-item v-for="m in g.items" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>{{ m.title }}</template>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </aside>

    <div class="layout__main">
      <header class="layout__topbar">
        <el-button text :icon="collapsed ? 'Expand' : 'Fold'" @click="collapsed = !collapsed" />
        <div class="layout__crumb">{{ route.meta.title }}</div>
        <div class="layout__user">
          <div class="layout__user-info">
            <span class="layout__user-name">{{ userStore.displayName }}</span>
            <span class="layout__user-meta text-tertiary text-sm">
              {{ userStore.departmentName || '未分配部门' }} · {{ roleText }}
            </span>
          </div>
          <el-dropdown trigger="click" @command="(c) => c === 'logout' && onLogout()">
            <el-avatar :size="30" class="layout__avatar">
              {{ userStore.displayName.slice(0, 1) }}
            </el-avatar>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="layout__content">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  background: var(--color-surface);
}
.layout__aside {
  width: 216px;
  flex-shrink: 0;
  background: var(--color-bg);
  border-right: 1px solid var(--color-border);
  transition: width 0.2s ease;
  display: flex;
  flex-direction: column;
}
.layout__aside.is-collapsed {
  width: 64px;
}
.layout__brand {
  height: 52px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  border-bottom: 1px solid var(--color-border);
}
.layout__logo {
  width: 26px;
  height: 26px;
  border-radius: 4px;
  background: var(--brand-500);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.layout__brand-text {
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
}
.layout__menu {
  border-right: none;
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.layout__menu :deep(.el-menu-item) {
  height: 38px;
  line-height: 38px;
  border-radius: 4px;
  margin-bottom: 2px;
  font-size: 14px;
}
/* 分组标题：小字、字距略开，起"目录"作用 */
.layout__menu :deep(.el-menu-item-group__title) {
  padding: 12px 12px 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--color-text-tertiary, #9aa1ac);
}
/* 选中态：浅底 + 主色文字，靠底色而非加粗堆叠 */
.layout__menu :deep(.el-menu-item.is-active) {
  background: rgba(62, 99, 221, 0.08);
  color: var(--brand-500, #3e63dd);
  font-weight: 600;
}
.layout__menu :deep(.el-menu-item:hover) {
  background: var(--color-surface, #f7f8fa);
}
.layout__main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.layout__topbar {
  height: 52px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
}
.layout__crumb {
  font-size: 14px;
  font-weight: 600;
}
.layout__user {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
.layout__user-info {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.25;
}
.layout__user-name {
  font-size: 13px;
  font-weight: 500;
}
.layout__avatar {
  background: var(--brand-500);
  font-size: 13px;
  cursor: pointer;
}
.layout__content {
  flex: 1;
  overflow: auto;
  padding: 20px;
}
</style>
