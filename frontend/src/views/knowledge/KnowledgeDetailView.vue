<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { fetchUnitDetail, updateUnit } from '@/api/knowledge'
import PageHeader from '@/components/PageHeader.vue'
import StageProgress from '@/components/StageProgress.vue'
import { useUserStore } from '@/stores/user'
import { CATEGORY_OPTIONS, UNIT_STATUS_EDIT_OPTIONS, categoryLabel } from '@/constants/knowledge'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const canEdit = userStore.has('knowledge:edit')

const id = Number(route.params.id)
const loading = ref(false)
const saving = ref(false)
const detail = ref(null)
const form = reactive({ title: '', category: '', status: '', content: '' })

async function load() {
  loading.value = true
  try {
    const data = await fetchUnitDetail(id)
    detail.value = data
    Object.assign(form, {
      title: data.title,
      category: data.category,
      status: data.status,
      content: data.content ?? '',
    })
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    await updateUnit(id, {
      title: form.title,
      category: form.category,
      status: form.status,
      content: form.content,
    })
    ElMessage.success('已保存；若修改了正文，将触发重新入库')
    load()
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <PageHeader :title="detail?.title || '知识详情'" :description="detail?.unitCode">
      <template #actions>
        <el-button :icon="'Back'" @click="router.push('/knowledge')">返回列表</el-button>
        <el-button v-if="canEdit" type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </PageHeader>

    <div v-if="detail" class="detail">
      <div class="kb-card detail__meta">
        <div class="detail__row">
          <span class="detail__label">入库进度</span>
          <StageProgress :status="detail.ingestStatus" :show-bar="false" />
        </div>
        <div class="detail__row">
          <span class="detail__label">源文件</span>
          <span class="text-md">{{ detail.sourceFileName }}（{{ detail.fileType }}）</span>
        </div>
        <div class="detail__row">
          <span class="detail__label">切片 / 向量</span>
          <span class="text-md">
            {{ detail.chunkCount ?? '—' }} 片 / {{ detail.vectorCount ?? '—' }} 向量
          </span>
        </div>
        <div class="detail__row">
          <span class="detail__label">数据权限</span>
          <span v-if="detail.permissions?.length" class="flex gap-8" style="flex-wrap: wrap">
            <el-tag v-for="(p, i) in detail.permissions" :key="i" size="small" type="info">
              {{ p.targetType }}#{{ p.targetId }}
            </el-tag>
          </span>
          <span v-else class="text-tertiary text-md">未配置（默认仅管理员）</span>
        </div>
        <div class="detail__row">
          <span class="detail__label">更新时间</span>
          <span class="text-tertiary text-md">{{ detail.updatedAt }}</span>
        </div>
      </div>

      <div class="kb-card mt-16">
        <el-form :model="form" label-width="72px" :disabled="!canEdit">
          <el-form-item label="标题">
            <el-input v-model="form.title" />
          </el-form-item>
          <el-form-item label="分类">
            <el-select v-model="form.category" style="width: 200px">
              <el-option
                v-for="c in CATEGORY_OPTIONS"
                :key="c.value"
                :label="c.label"
                :value="c.value"
              />
            </el-select>
            <span class="text-tertiary text-md" style="margin-left: 8px">
              当前：{{ categoryLabel(detail.category) }}
            </span>
          </el-form-item>
          <el-form-item label="业务状态">
            <el-radio-group v-model="form.status">
              <el-radio v-for="s in UNIT_STATUS_EDIT_OPTIONS" :key="s.value" :value="s.value">
                {{ s.label }}
              </el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="正文">
            <el-input
              v-model="form.content"
              type="textarea"
              :rows="14"
              placeholder="知识正文内容"
            />
          </el-form-item>
        </el-form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.detail__meta {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.detail__row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.detail__label {
  width: 88px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}
</style>
