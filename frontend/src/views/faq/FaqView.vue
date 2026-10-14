<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchFaqRecommendations, fetchFaqs, offlineFaq, reviewFaq, updateFaq } from '@/api/faq'
import PageHeader from '@/components/PageHeader.vue'

const activeTab = ref('recommend')
const loading = ref(false)

/* ---- 推荐审核 ---- */
const recommendations = ref([])
const edited = reactive({})

async function loadRecommendations() {
  loading.value = true
  try {
    const res = await fetchFaqRecommendations({ page: 1, size: 100 })
    recommendations.value = res.records ?? []
    recommendations.value.forEach((r) => {
      edited[r.id] = r.answer
    })
  } finally {
    loading.value = false
  }
}

async function onReview(row, action) {
  await reviewFaq(row.id, action, action === 'approve' ? edited[row.id] : undefined)
  ElMessage.success(action === 'approve' ? '已采纳并发布' : '已拒绝')
  loadRecommendations()
}

/* ---- 已发布库 ---- */
const faqs = ref([])
const faqQuery = reactive({ keyword: '' })

async function loadFaqs() {
  loading.value = true
  try {
    const res = await fetchFaqs({ page: 1, size: 50, keyword: faqQuery.keyword })
    faqs.value = Array.isArray(res) ? res : res.records
  } finally {
    loading.value = false
  }
}

const editDialog = ref(false)
const editForm = reactive({ id: 0, question: '', answer: '' })

function openEdit(row) {
  Object.assign(editForm, { id: row.id, question: row.question, answer: row.answer })
  editDialog.value = true
}

async function submitEdit() {
  await updateFaq(editForm.id, { question: editForm.question, answer: editForm.answer })
  ElMessage.success('已保存')
  editDialog.value = false
  loadFaqs()
}

async function onOffline(row) {
  await ElMessageBox.confirm(`确认下线该 FAQ？「${row.question}」`, '下线确认', { type: 'warning' })
  await offlineFaq(row.id)
  ElMessage.success('已下线')
  loadFaqs()
}

function onTabChange(name) {
  if (name === 'recommend') loadRecommendations()
  else loadFaqs()
}

onMounted(loadRecommendations)
</script>

<template>
  <div>
    <PageHeader title="FAQ 管理" description="高频问题推荐审核与已发布库维护" />

    <div class="kb-card">
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane label="推荐审核" name="recommend">
          <div v-loading="loading">
            <el-empty v-if="!recommendations.length && !loading" description="暂无待审核推荐" />
            <div v-for="r in recommendations" :key="r.id" class="rec">
              <div class="rec__head">
                <span class="rec__question">{{ r.question }}</span>
                <el-tag size="small" type="info">被问 {{ r.hitCount }} 次</el-tag>
              </div>
              <el-input
                v-model="edited[r.id]"
                type="textarea"
                :rows="3"
                placeholder="建议答案（可编辑后采纳）"
                class="rec__answer"
              />
              <div class="rec__ops">
                <el-button type="primary" size="small" @click="onReview(r, 'approve')">
                  采纳发布
                </el-button>
                <el-button size="small" @click="onReview(r, 'reject')">拒绝</el-button>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="已发布库" name="published">
          <div class="toolbar">
            <el-input
              v-model="faqQuery.keyword"
              placeholder="搜索问题"
              clearable
              style="width: 240px"
              :prefix-icon="'Search'"
              @keyup.enter="loadFaqs"
              @clear="loadFaqs"
            />
            <el-button :icon="'Search'" @click="loadFaqs">查询</el-button>
          </div>
          <el-table v-loading="loading" :data="faqs" size="small" stripe>
            <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
            <el-table-column prop="answer" label="答案" min-width="280" show-overflow-tooltip />
            <el-table-column label="命中" width="90">
              <template #default="{ row }">{{ row.hitCount ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
                <el-button link type="danger" size="small" @click="onOffline(row)">下线</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog v-model="editDialog" title="编辑 FAQ" width="520px">
      <el-form :model="editForm" label-width="60px">
        <el-form-item label="问题">
          <el-input v-model="editForm.question" />
        </el-form-item>
        <el-form-item label="答案">
          <el-input v-model="editForm.answer" type="textarea" :rows="5" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog = false">取消</el-button>
        <el-button type="primary" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.rec {
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border);
}
.rec:last-child {
  border-bottom: none;
}
.rec__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.rec__question {
  font-size: 14px;
  font-weight: 500;
}
.rec__answer {
  margin-bottom: 8px;
}
.rec__ops {
  display: flex;
  gap: 8px;
}
</style>
