#!/usr/bin/env node
/**
 * 前端 API 契约静态检查（审计复核 §4.6）——零外部依赖，供 CI 的 lint 门禁调用。
 *
 * 规则：
 *  1) src/api 中所有请求路径必须以白名单前缀开头（auth / ai / knowledge / org / settlement / dashboard），
 *     防止新增接口时误写到未知命名空间；
 *  2) 请求层（src/api、src/stores）禁止直连 AI 服务（:8000 / 绝对 URL）——
 *     架构约束是「前端只访问 Java 业务端」，AI 服务仅由 Java 经内部 token 调用。
 *
 * 退出码：0 通过；1 存在违规。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const API_DIR = join(ROOT, 'src', 'api')
const REQUEST_DIRS = [API_DIR, join(ROOT, 'src', 'stores')]
const ALLOWED_PREFIXES = ['/auth', '/ai', '/knowledge', '/org', '/settlement', '/dashboard']
const VERB_CALL = /\b(?:get|post|put|patch|del|delete|request)\s*\(\s*(['"`])([^'"`]*)\1/g
const FORBIDDEN_PATTERNS = [
  { re: /:\s*8000\b/, why: '禁止直连 AI 服务端口 :8000（必须经 Java 业务端）' },
  { re: /https?:\/\//, why: 'API/状态层禁止绝对 URL（须走 baseURL=/api 的相对路径）' },
]

const violations = []

function walk(dir) {
  let entries = []
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return []
  }
  const files = []
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      files.push(...walk(full))
    } else if (/\.(js|vue)$/.test(entry.name)) {
      files.push(full)
    }
  }
  return files
}

// 规则 1：请求路径前缀白名单
for (const file of walk(API_DIR)) {
  const text = readFileSync(file, 'utf8')
  for (const match of text.matchAll(VERB_CALL)) {
    const raw = match[2]
    if (!raw || raw.includes('${') || raw.startsWith('http')) {
      continue
    }
    const path = raw.replace(/^\/api/, '')
    if (!path.startsWith('/')) {
      continue
    }
    const ok = ALLOWED_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix + '/'))
    if (!ok) {
      violations.push(`${file}: 请求路径 ${raw} 不在白名单前缀内（${ALLOWED_PREFIXES.join(', ')}）`)
    }
  }
}

// 规则 2：请求层禁止直连 AI 服务
for (const dir of REQUEST_DIRS) {
  for (const file of walk(dir)) {
    const text = readFileSync(file, 'utf8')
    for (const { re, why } of FORBIDDEN_PATTERNS) {
      if (re.test(text)) {
        violations.push(`${file}: ${why}`)
      }
    }
  }
}

if (violations.length > 0) {
  console.error('API 契约检查未通过：')
  for (const violation of violations) {
    console.error('  - ' + violation)
  }
  process.exit(1)
}
console.log('API 契约检查通过：请求路径前缀与「前端不直连 AI 服务」约束均满足。')
