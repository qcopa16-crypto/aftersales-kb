<script setup>
// 来源卡片：授权用中性浅底；未授权用 warning 色 + 锁图标（不大面积红）
defineProps({
  sources: { type: Object, required: true },
})

function toItems(list) {
  return list ?? []
}
</script>

<template>
  <div class="sources">
    <div class="sources__group">
      <div class="sources__head">
        <el-icon><Document /></el-icon>
        <span>已授权来源（{{ toItems(sources.authorized).length }}）</span>
      </div>
      <div v-if="toItems(sources.authorized).length" class="sources__list">
        <span v-for="s in toItems(sources.authorized)" :key="s.id" class="source-chip">
          <el-icon><Document /></el-icon>
          {{ s.title }}
        </span>
      </div>
      <p v-else class="sources__empty text-tertiary text-sm">本次回答未命中已授权知识</p>
    </div>

    <div v-if="toItems(sources.unauthorized).length" class="sources__group">
      <div class="sources__head sources__head--warn">
        <el-icon><Lock /></el-icon>
        <span>无权限来源（{{ toItems(sources.unauthorized).length }}）</span>
      </div>
      <div class="sources__list">
        <span
          v-for="s in toItems(sources.unauthorized)"
          :key="s.id"
          class="source-chip source-chip--warn"
          :title="'无权访问：' + s.title"
        >
          <el-icon><Lock /></el-icon>
          {{ s.title }}
        </span>
      </div>
      <p class="sources__hint text-sm">该内容与问题相关，但你暂无数据权限，已从回答中排除。</p>
    </div>
  </div>
</template>

<style scoped>
.sources {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sources__head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}
.sources__head--warn {
  color: var(--color-warning);
}
.sources__list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-surface);
  font-size: 12px;
  color: var(--color-text-secondary);
}
.source-chip--warn {
  border-color: #f6d99b;
  background: #fdf6e8;
  color: #9a6b00;
}
.sources__hint {
  margin: 6px 0 0;
  color: var(--color-text-tertiary);
}
.sources__empty {
  margin: 0;
}
</style>
