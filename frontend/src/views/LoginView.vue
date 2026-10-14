<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { firstAccessiblePath } from '@/router'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const demos = [
  { label: '管理员', username: 'admin', password: '123456' },
  { label: '售后', username: 'zhangsan', password: '123456' },
  { label: '销售', username: 'lisi', password: '123456' },
]

function fillDemo(demo) {
  form.username = demo.username
  form.password = demo.password
}

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await userStore.login(form.username, form.password)
    ElMessage.success('登录成功')
    const redirect = route.query.redirect
    router.push(
      typeof redirect === 'string' && redirect
        ? redirect
        : firstAccessiblePath(userStore.permissions),
    )
  } catch {
    /* 错误提示由 http 拦截器统一处理 */
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="login__panel">
      <div class="login__brand">
        <span class="login__logo">KB</span>
        <div>
          <h1 class="login__title">企业 IT 设备售后知识库</h1>
          <p class="login__subtitle">文档导入 · 细粒度数据权限 · AI 鉴权问答</p>
        </div>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" size="large" @submit.prevent="onSubmit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="'User'" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            :prefix-icon="'Lock'"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="w-full" :loading="loading" @click="onSubmit">
          登录
        </el-button>
      </el-form>

      <div class="login__demos">
        <span class="text-tertiary text-sm">演示账号：</span>
        <el-button v-for="d in demos" :key="d.username" link size="small" @click="fillDemo(d)">
          {{ d.label }} {{ d.username }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-surface);
}
.login__panel {
  width: 380px;
  padding: 32px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.login__brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
}
.login__logo {
  width: 40px;
  height: 40px;
  border-radius: 6px;
  background: var(--brand-500);
  color: #fff;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.login__title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.login__subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--color-text-tertiary);
}
.login__demos {
  margin-top: 16px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
