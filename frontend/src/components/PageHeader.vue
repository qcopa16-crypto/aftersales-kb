<script setup>
// 页面标题区：统一标题/描述/右侧操作位，保证各页层级一致
defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
})
</script>

<template>
  <div class="page-header">
    <div class="page-header__main">
      <h2 class="page-header__title">{{ title }}</h2>
      <p v-if="description" class="page-header__desc">{{ description }}</p>
    </div>
    <div class="page-header__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-header__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
  letter-spacing: -0.01em;
}
.page-header__desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}
.page-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
</style>
