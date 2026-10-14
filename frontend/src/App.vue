<script setup>
// 根组件：承载 Element Plus 全局配置（中文 locale）与路由出口。
// 由于已移除 app.use(ElementPlus, { locale: zhCn })，改用 ElConfigProvider 注入 locale，
// 保证日期选择、分页、空状态等组件的内置文案仍为中文。
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view />
  </el-config-provider>
</template>
