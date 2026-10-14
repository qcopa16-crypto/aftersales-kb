/**
 * 权限域常量：操作权限码分组 + 四维数据权限目标类型文案。
 *
 * 权限码必须与后端 PermissionCode 枚举保持一致；
 * 目标类型与后端 PermissionTargetType 一致（OR 组合生效）。
 */

/** 操作权限码分组（角色分配抽屉用） */
export const PERMISSION_GROUPS = [
  {
    label: '用户管理',
    codes: [
      { code: 'org:user:list', label: '用户查看' },
      { code: 'org:user:edit', label: '用户编辑' },
    ],
  },
  {
    label: '部门管理',
    codes: [
      { code: 'org:dept:list', label: '部门查看' },
      { code: 'org:dept:edit', label: '部门编辑' },
    ],
  },
  {
    label: '角色管理',
    codes: [
      { code: 'org:role:list', label: '角色查看' },
      { code: 'org:role:edit', label: '角色编辑' },
    ],
  },
  {
    label: '知识管理',
    codes: [
      { code: 'knowledge:list', label: '知识查看' },
      { code: 'knowledge:import', label: '批量导入' },
      { code: 'knowledge:edit', label: '知识编辑' },
      { code: 'knowledge:delete', label: '知识删除' },
      { code: 'knowledge:perm', label: '权限配置' },
      { code: 'knowledge:ai', label: 'AI 问答' },
    ],
  },
  {
    label: '沉淀与看板',
    codes: [
      { code: 'faq:review', label: 'FAQ 审核' },
      { code: 'gap:view', label: '知识缺口' },
      { code: 'dashboard:view', label: '数据看板' },
    ],
  },
]

/** 四维数据权限目标类型 → 中文（global / department / role / user） */
export const PERMISSION_TARGET_LABELS = {
  global: '全局',
  department: '部门',
  role: '角色',
  user: '个人',
}
