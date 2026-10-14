import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchMe, login as loginApi } from '@/api/auth'
import { TOKEN_KEY } from '@/api/http'

/** 当前用户状态：令牌 + 用户信息 + 权限码判断 */
export const useUserStore = defineStore('user', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const userInfo = ref(null)

  const isLoggedIn = computed(() => !!token.value)
  const permissions = computed(() => userInfo.value?.permissions ?? [])
  const displayName = computed(() => userInfo.value?.displayName ?? '')
  const departmentName = computed(() => userInfo.value?.departmentName ?? '')
  const roles = computed(() => userInfo.value?.roles ?? [])
  const roleNames = computed(() => userInfo.value?.roleNames ?? [])

  /** 是否拥有某权限码（不传视为放行） */
  function has(permission) {
    if (!permission) return true
    return permissions.value.includes(permission)
  }

  /** 拥有任一权限码（空数组视为放行） */
  function hasAny(codes) {
    if (codes.length === 0) return true
    return codes.some((c) => permissions.value.includes(c))
  }

  function setToken(value) {
    token.value = value
    if (value) localStorage.setItem(TOKEN_KEY, value)
    else localStorage.removeItem(TOKEN_KEY)
  }

  async function login(username, password) {
    const res = await loginApi(username, password)
    setToken(res.accessToken)
    userInfo.value = res.userInfo
    return res.userInfo
  }

  /** 刷新用户信息（登录后 / 刷新页面时调用，拿到最新权限码） */
  async function loadProfile() {
    const info = await fetchMe()
    userInfo.value = info
    return info
  }

  function logout() {
    setToken('')
    userInfo.value = null
  }

  return {
    token,
    userInfo,
    isLoggedIn,
    permissions,
    displayName,
    departmentName,
    roles,
    roleNames,
    has,
    hasAny,
    setToken,
    login,
    loadProfile,
    logout,
  }
})
