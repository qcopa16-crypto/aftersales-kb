import { get, post } from './http'

/** 登录，返回 accessToken 与用户信息 */
export function login(username, password) {
  return post('/auth/login', { username, password })
}

/** 获取当前登录用户信息与权限码 */
export function fetchMe() {
  return get('/auth/me')
}
