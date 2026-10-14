import { createApp } from 'vue'
import { createPinia } from 'pinia'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
// Element Plus 组件已改为按需引入（见 vite.config.js 的 unplugin-vue-components）。
// ElMessage / ElMessageBox 是全站以 `import { ... } from 'element-plus'` 显式调用的，
// 按需 resolver 不会为它们补样式，这里显式引入对应组件样式，避免弹层无样式。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import './styles/tokens.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)

// 全局注册 Element Plus 图标（供菜单/按钮按组件名动态使用）。
// 全站大量使用动态 <component :is="图标名">，resolver 无法静态识别，故保留全量注册。
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)

app.mount('#app')
