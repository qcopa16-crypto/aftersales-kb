import { get, post, put } from './http'

/* ---------------- 部门 ---------------- */

export function fetchDepartmentTree() {
  return get('/org/departments')
}

export function createDepartment(body) {
  return post('/org/departments', body)
}

export function updateDepartment(id, body) {
  return put(`/org/departments/${id}`, body)
}

/* ---------------- 用户 ---------------- */

export function fetchUsers(params) {
  return get('/org/users', params)
}

export function createUser(body) {
  return post('/org/users', body)
}

export function updateUser(id, body) {
  return put(`/org/users/${id}`, body)
}

/** 重置密码，返回 { password } 新密码 */
export function resetUserPassword(id, password) {
  return post(`/org/users/${id}/reset-password`, password ? { password } : {})
}

/* ---------------- 角色 ---------------- */

export function fetchRoles() {
  return get('/org/roles')
}

export function createRole(body) {
  return post('/org/roles', body)
}

export function updateRole(id, body) {
  return put(`/org/roles/${id}`, body)
}

export function fetchRolePermissions(id) {
  return get(`/org/roles/${id}/permissions`)
}

export function assignRolePermissions(id, mode, permissions) {
  return post(`/org/roles/${id}/permissions`, { mode, permissions })
}
