<script setup>
import { computed } from 'vue'
import { stageMeta } from '@/constants/ingest'

const props = defineProps({
  /** 入库状态 ingestStatus */
  status: { type: String, default: '' },
  /** 当前阶段码 */
  stage: { type: String, default: '' },
  /** 进度 0-100 */
  progress: { type: Number, default: 0 },
  /** 失败原因 */
  errorMessage: { type: String, default: null },
  /** 是否显示 4px 进度条 */
  showBar: { type: Boolean, default: true },
})

const meta = computed(() => stageMeta(props.status))
const isFailed = computed(() => props.status === 'failed')

// 非终态按 progress 展示，running 状态无 progress 时给 60% 脉冲感
const barPercent = computed(() => {
  if (props.status === 'active') return 100
  if (props.status === 'failed') return 100
  if (props.progress && props.progress > 0) return Math.min(100, props.progress)
  if (props.status === 'parsing' || props.status === 'embedding' || props.status === 'deleting') {
    return 60
  }
  return props.status === 'queued' || props.status === 'draft' ? 0 : 100
})
</script>

<template>
  <div class="stage">
    <div class="stage__row" :aria-live="isFailed ? 'assertive' : 'polite'">
      <span class="stage__label" :style="{ color: meta.color }">
        <el-icon :class="{ 'is-spin': meta.icon === 'Loading' }">
          <component :is="meta.icon" />
        </el-icon>
        {{ meta.label }}
      </span>
      <span v-if="stage" class="stage__stage text-tertiary text-md">{{ stage }}</span>
      <span v-if="showBar && progress !== undefined" class="stage__pct text-tertiary text-sm">
        {{ barPercent }}%
      </span>
    </div>
    <div v-if="showBar" class="stage__bar" role="progressbar" :aria-valuenow="barPercent">
      <div
        class="stage__bar-fill"
        :class="{ 'is-indeterminate': meta.icon === 'Loading' && !progress }"
        :style="{ width: `${barPercent}%`, background: meta.color }"
      />
    </div>
    <p v-if="isFailed && errorMessage" class="stage__error" tabindex="0">
      {{ errorMessage }}
    </p>
  </div>
</template>

<style scoped>
.stage {
  width: 100%;
}
.stage__row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.stage__label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 500;
}
.stage__pct {
  margin-left: auto;
}
.stage__bar {
  margin-top: 6px;
  height: 4px;
  border-radius: 2px;
  background: var(--color-surface-hover);
  overflow: hidden;
}
.stage__bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.4s ease;
}
.is-indeterminate {
  animation: pulse 1.4s ease-in-out infinite;
}
.stage__error {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--stage-failed);
  outline: none;
}
.stage__error:focus {
  text-decoration: underline;
}
.is-spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}
</style>
