import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板中的 <el-xxx> 与 v-loading 等指令自动解析并补样式，
    // 不再整包 import + app.use(ElementPlus)，显著缩小主包体积。
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    sourcemap: false,
    rollupOptions: {
      output: {
        // 按依赖来源手动分包，避免单一 index/dashboard 包过大。
        //
        // 重要（实测结论）：不要把 element-plus / @element-plus 交给 manualChunks。
        // 一旦这些模块被指定到手动分包，Rollup 会把它们当成分包入口并保留其全部导出，
        // 使 element-plus 的 barrel（es/index.mjs）无法 tree-shaking —— 整个包（154 个组件、
        // 964KB 的 element-plus 分包）会被打进产物，总 JS 约 1897KB。
        // 让 element-plus 走 Vite 默认分包即可正确按需引入（74 个组件、总 JS 约 1473KB）。
        // echarts / vue / vendor 仍是纯叶子依赖，手动分包无副作用。
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          const norm = id.replace(/\\/g, '/')
          if (norm.includes('echarts') || norm.includes('zrender')) return 'echarts'
          if (norm.includes('element-plus') || norm.includes('@element-plus')) return undefined
          if (norm.includes('vue') || norm.includes('pinia')) return 'vue'
          return 'vendor'
        },
      },
    },
  },
  server: {
    port: 5173,
    // 开发期放开跨域与 Host 校验：默认只允许 localhost，
    // 用 IPv6 字面量（http://[::1]:5173）访问会被判为 "Invalid CORS request"。
    cors: true,
    allowedHosts: ['localhost', '127.0.0.1', '[::1]', '::1'],
    proxy: {
      // 开发环境把 /api 代理到 Java 业务服务，前端只访问 Java，禁止直连 AI 服务
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
