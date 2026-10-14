import axios from 'axios'
import { ElMessage } from 'element-plus'

/** localStorage 中存放访问令牌的键名 */
export const TOKEN_KEY = 'kb_access_token'

/** 业务错误码（与后端 ErrorCode 枚举一致） */
export const ErrCode = {
  OK: 0,
  PARAM: 400,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  /** 数据权限不足；HTTP 仍为 200，交由页面按业务码处理 */
  DATA_PERMISSION: 40001,
  SERVER: 500,
  AI_UNAVAILABLE: 50001,
  AI_FAILED: 50002,
}

/** 带业务码的 API 错误，便于页面按 code 分支处理 */
export class ApiError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

let redirecting = false

/** 401 统一跳登录（去重，避免并发请求多次跳转） */
function toLogin() {
  localStorage.removeItem(TOKEN_KEY)
  if (redirecting) return
  redirecting = true
  const redirect = encodeURIComponent(window.location.hash.replace(/^#/, '') || '/')
  window.location.hash = `/login?redirect=${redirect}`
  setTimeout(() => {
    redirecting = false
  }, 800)
}

const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截：注入 Bearer token
http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

// 响应拦截：统一解 Result 包装
http.interceptors.response.use(
  (response) => {
    const body = response.data
    // 非标准包装（如文件流）直接返回
    if (body == null || typeof body !== 'object' || !('code' in body)) {
      return response.data
    }
    if (body.code === ErrCode.OK) {
      return body.data
    }
    if (body.code === ErrCode.UNAUTHORIZED) {
      toLogin()
      return Promise.reject(new ApiError(body.code, body.message || '登录已失效，请重新登录'))
    }
    // 40001 数据权限不足：不弹全局提示，交由页面处理
    if (body.code === ErrCode.DATA_PERMISSION) {
      return Promise.reject(new ApiError(body.code, body.message || '数据权限不足'))
    }
    ElMessage.error(body.message || '请求失败')
    return Promise.reject(new ApiError(body.code, body.message || '请求失败'))
  },
  (error) => {
    const status = error?.response?.status
    const body = error?.response?.data
    if (status === 401 || body?.code === ErrCode.UNAUTHORIZED) {
      toLogin()
      return Promise.reject(new ApiError(ErrCode.UNAUTHORIZED, '登录已失效，请重新登录'))
    }
    const message =
      body?.message ||
      (status === 502 ? 'AI 服务不可用' : '') ||
      error?.message ||
      '网络异常，请稍后重试'
    ElMessage.error(message)
    return Promise.reject(new ApiError(body?.code ?? status ?? -1, message))
  },
)

/** 统一请求：返回解包后的 data */
export function request(config) {
  return http.request(config)
}

export function get(url, params) {
  return request({ url, method: 'GET', params })
}

export function post(url, data) {
  return request({ url, method: 'POST', data })
}

export function put(url, data) {
  return request({ url, method: 'PUT', data })
}

export function patch(url, data) {
  return request({ url, method: 'PATCH', data })
}

export function del(url, data) {
  return request({ url, method: 'DELETE', data })
}

export default http
