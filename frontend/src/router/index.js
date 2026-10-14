import { createRouter, createWebHashHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

/**
 * 路由 meta 约定：
 *   title       菜单/页面标题（同时写入 document.title）
 *   icon        侧边菜单图标名（Element Plus 图标组件名）
 *   permission  访问所需权限码；缺省表示仅需登录
 *   hidden      是否在侧边菜单中隐藏
 *   public      是否免登录
 */

/** 后台管理区菜单路由（供布局侧边栏与权限守卫复用） */
export const adminRoutes = [
  {
    path: 'dashboard',
    name: 'dashboard',
    component: () => import('@/views/dashboard/DashboardView.vue'),
    meta: { title: '数据看板', icon: 'DataLine', permission: 'dashboard:view' },
  },
  {
    path: 'latency',
    name: 'latency',
    component: () => import('@/views/dashboard/LatencyView.vue'),
    meta: { title: '耗时分析', icon: 'Timer', permission: 'dashboard:view' },
  },
  {
    path: 'knowledge',
    name: 'knowledge',
    component: () => import('@/views/knowledge/KnowledgeListView.vue'),
    meta: { title: '知识管理', icon: 'Document', permission: 'knowledge:list' },
  },
  {
    path: 'knowledge/:id',
    name: 'knowledge-detail',
    component: () => import('@/views/knowledge/KnowledgeDetailView.vue'),
    meta: { title: '知识详情', permission: 'knowledge:list', hidden: true },
  },
  {
    path: 'ingest',
    name: 'ingest',
    component: () => import('@/views/ingest/IngestWorkbench.vue'),
    meta: { title: '入库工作台', icon: 'Upload', permission: 'knowledge:import' },
  },
  {
    path: 'permissions',
    name: 'permissions',
    component: () => import('@/views/knowledge/PermissionView.vue'),
    meta: { title: '权限配置', icon: 'Lock', permission: 'knowledge:perm' },
  },
  {
    path: 'ai',
    name: 'ai',
    component: () => import('@/views/ai/ChatWorkbench.vue'),
    meta: { title: 'AI 问答', icon: 'ChatDotRound', permission: 'knowledge:ai' },
  },
  {
    path: 'org/users',
    name: 'org-users',
    component: () => import('@/views/org/UserListView.vue'),
    meta: { title: '用户管理', icon: 'User', permission: 'org:user:list' },
  },
  {
    path: 'org/departments',
    name: 'org-departments',
    component: () => import('@/views/org/DepartmentView.vue'),
    meta: { title: '部门管理', icon: 'OfficeBuilding', permission: 'org:dept:list' },
  },
  {
    path: 'org/roles',
    name: 'org-roles',
    component: () => import('@/views/org/RoleView.vue'),
    meta: { title: '角色权限', icon: 'Key', permission: 'org:role:list' },
  },
  {
    path: 'faq',
    name: 'faq',
    component: () => import('@/views/faq/FaqView.vue'),
    meta: { title: 'FAQ 管理', icon: 'Collection', permission: 'faq:review' },
  },
  {
    path: 'gaps',
    name: 'gaps',
    component: () => import('@/views/gap/GapView.vue'),
    meta: { title: '知识缺口', icon: 'Warning', permission: 'gap:view' },
  },
]

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/',
    component: () => import('@/layouts/AdminLayout.vue'),
    redirect: '/dashboard',
    children: adminRoutes,
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在', public: true, hidden: true },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

/** 从菜单路由中挑出当前用户可访问的第一个路径 */
export function firstAccessiblePath(permissions) {
  const item = adminRoutes.find((r) => {
    const p = r.meta?.permission
    return !p || permissions.includes(p)
  })
  return item ? `/${String(item.path)}` : '/login'
}

router.beforeEach(async (to) => {
  const userStore = useUserStore()
  document.title = to.meta.title ? `${to.meta.title} · 售后知识库` : '售后知识库'

  if (to.meta.public) {
    // 已登录访问登录页则跳回首页
    if (to.name === 'login' && userStore.isLoggedIn) {
      return firstAccessiblePath(userStore.permissions)
    }
    return true
  }

  if (!userStore.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 刷新页面后补齐用户信息/权限码
  if (!userStore.userInfo) {
    try {
      await userStore.loadProfile()
    } catch {
      userStore.logout()
      return { path: '/login', query: { redirect: to.fullPath } }
    }
  }

  if (to.meta.permission && !userStore.has(to.meta.permission)) {
    // 无权限：回到该用户首个可访问页
    const fallback = firstAccessiblePath(userStore.permissions)
    if (fallback === to.path) return true
    return fallback
  }

  return true
})

export default router
