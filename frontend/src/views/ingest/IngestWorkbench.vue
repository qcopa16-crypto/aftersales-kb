<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import PageHeader from '@/components/PageHeader.vue'
import StageProgress from '@/components/StageProgress.vue'
import PermissionPicker from '@/components/PermissionPicker.vue'
import { CATEGORY_OPTIONS, categoryLabel } from '@/constants/knowledge'
import { TERMINAL_STATUSES, stageText } from '@/constants/ingest'
import { useIngestStore } from '@/stores/ingest'
import { useUserStore } from '@/stores/user'

const ingest = useIngestStore()
const userStore = useUserStore()
const route = useRoute()
const canImport = userStore.has('knowledge:import')

const uploadDialog = ref(false)
const fileList = ref([])
const uploadCategory = ref('')

const detailLoading = computed(() => ingest.loadingFiles)
const currentBatch = computed(() => ingest.batches.find((b) => b.batchId === ingest.currentBatchId))
/** 仍在流水线中的文件数（未到终态） */
const inFlightCount = computed(
  () => ingest.files.filter((f) => !TERMINAL_STATUSES.includes(f.ingestStatus)).length,
)
/** 停在质检门、等待人工确认的文件数（阶段 3 新增） */
const pendingManualCount = computed(
  () => ingest.files.filter((f) => f.ingestStatus === 'parsed' && f.stage === 'quality_gate').length,
)

/* ---------------- 上传 ---------------- */
function onChange(_file, files) {
  fileList.value = files
}

function onRemove(_file, files) {
  fileList.value = files
}

function openUpload() {
  fileList.value = []
  uploadCategory.value = ''
  uploadDialog.value = true
}

async function submitUpload() {
  const raw = fileList.value.map((f) => f.raw).filter(Boolean)
  if (!raw.length) return ElMessage.warning('请先选择文件')
  const res = await ingest.upload(raw, uploadCategory.value || undefined)
  uploadDialog.value = false
  ElMessage.success(`已接收 ${res.acceptedCount} 个文件，进入入库流水线`)
}

/* ---------------- 批次 ---------------- */
async function selectBatch(batchId) {
  await ingest.loadBatchDetail(batchId)
  ingest.startPolling()
}

/* ---------------- 质检抽屉 ---------------- */
const gateDrawer = ref(false)
const gateFile = ref(null)
const gateCategory = ref('')
const gatePermissions = ref([])
const gateSubmitting = ref(false)

const isProductManual = computed(() => gateFile.value?.category === 'product')
// 抽屉内按钮的可用性（避免在模板中做非空断言）
const gateCanGate = computed(() => !!gateFile.value && canGate(gateFile.value))
const gateCanRetry = computed(() => !!gateFile.value && canRetry(gateFile.value))

/* ---------------- AI 质检结论展示 ---------------- */
const REVIEW_VERDICT_TEXT = { auto_pass: 'AI 自动通过', human_required: '需人工确认' }
const REVIEW_SOURCE_TEXT = { ai: 'AI 判定', human: '人工判定' }
// AI 预审意见（阶段 4 新增）：AI 自己的判定，与最终决策 reviewVerdict 区分
const AI_VERDICT_TEXT = { pass: 'AI 预审：建议通过', review: 'AI 预审：建议复核' }
function reviewVerdictText(v) {
  return REVIEW_VERDICT_TEXT[v] ?? v ?? ''
}
function reviewSourceText(s) {
  return REVIEW_SOURCE_TEXT[s] ?? s ?? ''
}
function aiVerdictText(v) {
  return AI_VERDICT_TEXT[v] ?? v ?? ''
}
/** AI 预审标签类型：建议通过→绿色(success)，建议复核→warning */
function aiVerdictTagType(v) {
  return v === 'pass' ? 'success' : 'warning'
}
/** 抽屉内是否存在可展示的 AI 质检结论（老数据无新字段则不渲染区块） */
const gateHasReview = computed(() => {
  const f = gateFile.value
  if (!f) return false
  return Boolean(
    f.reviewVerdict ||
      f.reviewSource ||
      f.aiVerdict ||
      (f.reviewReasons?.length ?? 0) ||
      (f.vlmFindings?.length ?? 0),
  )
})
/** manual 类别 + AI 预审建议通过：提示人工拍板后入库（阶段 4 新增） */
const gateManualAiHint = computed(
  () => gateFile.value?.reviewVerdict === 'human_required' && gateFile.value?.aiVerdict === 'pass',
)
/** 是否停在质检门（决定 product 人工确认警告是否展示） */
const gateAtQualityGate = computed(() => gateFile.value?.stage === 'quality_gate')
/** product 人工警告文案（带上 AI 结论） */
const productGateHint = computed(() => {
  const v = gateFile.value?.reviewVerdict
  return v ? `需人工确认（AI 判定：${reviewVerdictText(v)}）` : '需人工确认'
})

function openGate(row) {
  gateFile.value = row
  gateCategory.value = row.category
  // 默认权限建议：product → global，其余留空由管理员指定
  gatePermissions.value = row.category === 'product' ? [{ targetType: 'global', targetId: 0 }] : []
  gateDrawer.value = true
}

async function submitGate(action) {
  if (!gateFile.value) return
  gateSubmitting.value = true
  try {
    await ingest.confirmQualityGate(
      gateFile.value.unitId,
      action,
      gateCategory.value,
      gatePermissions.value,
    )
    ElMessage.success(action === 'confirm' ? '已确认，进入向量化' : '已按调整结果提交')
    gateDrawer.value = false
  } finally {
    gateSubmitting.value = false
  }
}

/* ---------------- 重试 ---------------- */
async function onRetry(row) {
  await ingest.retry(row.unitId, row.stage)
  ElMessage.success('已提交重试')
}

function canGate(row) {
  return row.ingestStatus === 'parsed'
}

function canRetry(row) {
  return row.ingestStatus === 'failed'
}

onMounted(async () => {
  // 从知识缺口页「去建知识」跳转而来：提示待补录的问题
  const gapQuestion = route.query.q
  if (typeof gapQuestion === 'string' && gapQuestion) {
    ElMessage.info(`请上传可回答「${gapQuestion}」的知识文件`)
  }
  await ingest.loadBatches()
  if (ingest.batches.length && !ingest.currentBatchId) {
    await selectBatch(ingest.batches[0].batchId)
  }
})
onUnmounted(() => ingest.stopPolling())
</script>

<template>
  <div>
    <PageHeader title="入库工作台" description="批量上传 · 批次进度 · 质检确认 · 失败重试">
      <template #actions>
        <el-button :icon="'Refresh'" @click="ingest.loadBatches()">刷新批次</el-button>
        <el-button v-if="canImport" type="primary" :icon="'Upload'" @click="openUpload">
          上传文件
        </el-button>
      </template>
    </PageHeader>

    <div class="workbench">
      <!-- 批次列表 -->
      <aside class="kb-card workbench__batches">
        <div class="workbench__batches-head text-secondary text-md">入库批次</div>
        <el-empty
          v-if="!ingest.batches.length && !ingest.loadingBatches"
          description="暂无批次"
          :image-size="60"
        />
        <ul class="batch-list">
          <li
            v-for="b in ingest.batches"
            :key="b.batchId"
            class="batch-item"
            :class="{ 'is-active': b.batchId === ingest.currentBatchId }"
            @click="selectBatch(b.batchId)"
          >
            <div class="batch-item__id text-mono text-md">{{ b.batchId }}</div>
            <div class="batch-item__meta text-tertiary text-sm">
              {{ b.totalCount }} 文件 · 成功 {{ b.successCount }} / 失败 {{ b.failedCount }}
            </div>
            <div class="batch-item__time text-tertiary text-sm">{{ b.createdAt }}</div>
          </li>
        </ul>
      </aside>

      <!-- 批次详情 -->
      <section class="kb-card workbench__detail">
        <div v-if="!ingest.currentBatchId" class="workbench__empty">
          <el-empty description="选择左侧批次查看逐文件进度" />
        </div>
        <template v-else>
          <div class="flex-between mb-16">
            <div>
              <div class="workbench__batch-id text-mono">{{ ingest.currentBatchId }}</div>
              <div class="text-tertiary text-sm">
                {{ currentBatch?.createdBy }} · {{ currentBatch?.createdAt }}
              </div>
            </div>
            <div class="flex-center" style="gap: 8px">
              <el-tag size="small" type="info">在途 {{ inFlightCount }}</el-tag>
              <el-tag v-if="pendingManualCount" size="small" type="warning">
                待人工质检 {{ pendingManualCount }}
              </el-tag>
            </div>
          </div>

          <el-table v-loading="detailLoading" :data="ingest.files" size="small">
            <el-table-column prop="title" label="文件" min-width="200" show-overflow-tooltip />
            <el-table-column label="分类" width="110">
              <template #default="{ row }">{{ categoryLabel(row.category) }}</template>
            </el-table-column>
            <el-table-column label="阶段" width="120">
              <template #default="{ row }">
                <span class="text-md">{{ stageText(row.stage) || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="进度" min-width="220">
              <template #default="{ row }">
                <StageProgress
                  :status="row.ingestStatus"
                  :stage="stageText(row.stage)"
                  :progress="row.progress"
                  :error-message="row.errorMessage"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="canGate(row)"
                  link
                  type="warning"
                  size="small"
                  @click="openGate(row)"
                >
                  质检确认
                </el-button>
                <el-button
                  v-if="canRetry(row)"
                  link
                  type="primary"
                  size="small"
                  @click="onRetry(row)"
                >
                  重试
                </el-button>
                <el-button link type="primary" size="small" @click="openGate(row)">详情</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </section>
    </div>

    <!-- 上传对话框 -->
    <el-dialog v-model="uploadDialog" title="批量上传知识文件" width="560px">
      <el-upload
        drag
        multiple
        :auto-upload="false"
        :file-list="fileList"
        :on-change="onChange"
        :on-remove="onRemove"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          拖拽文件到此处，或
          <em>点击选择</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">
            支持 PDF / Markdown / DOCX / TXT，可多选。上传后异步入库，可离开页面。
          </div>
        </template>
      </el-upload>
      <div class="upload-category">
        <span class="text-secondary text-md">分类（可选）：</span>
        <el-select
          v-model="uploadCategory"
          clearable
          placeholder="不指定则由 AI 建议"
          style="width: 220px"
        >
          <el-option
            v-for="c in CATEGORY_OPTIONS"
            :key="c.value"
            :label="c.label"
            :value="c.value"
          />
        </el-select>
      </div>
      <template #footer>
        <el-button @click="uploadDialog = false">取消</el-button>
        <el-button type="primary" :loading="ingest.uploading" @click="submitUpload">
          开始上传
        </el-button>
      </template>
    </el-dialog>

    <!-- 质检抽屉 -->
    <el-drawer v-model="gateDrawer" :title="`质检 · ${gateFile?.title ?? ''}`" size="480px">
      <template v-if="gateFile">
        <el-alert
          v-if="isProductManual && gateAtQualityGate"
          type="warning"
          :closable="false"
          show-icon
          :title="productGateHint"
          description="请核对标题、目录结构与切片抽样无误后，再确认入库。"
          class="mb-16"
        />

        <div class="gate-section">
          <div class="gate-section__title">文件信息</div>
          <div class="gate-kv">
            <span>标题</span>
            <span>{{ gateFile.title }}</span>
          </div>
          <div class="gate-kv">
            <span>当前阶段</span>
            <span>{{ stageText(gateFile.stage) || '—' }}</span>
          </div>
          <div class="gate-kv">
            <span>状态</span>
            <span><StageProgress :status="gateFile.ingestStatus" :show-bar="false" /></span>
          </div>
          <div v-if="gateFile.errorMessage" class="gate-kv gate-kv--error">
            <span>错误</span>
            <span>{{ gateFile.errorMessage }}</span>
          </div>
        </div>

        <div v-if="gateHasReview" class="gate-section">
          <div class="gate-section__title">AI 质检结论</div>

          <!-- AI 预审意见：AI 自己的判定，作为人工拍板的参考（老数据无该字段则不渲染） -->
          <div v-if="gateFile.aiVerdict" class="gate-preaudit">
            <div class="gate-preaudit__head">
              <el-tag size="small" :type="aiVerdictTagType(gateFile.aiVerdict)">
                {{ aiVerdictText(gateFile.aiVerdict) }}
              </el-tag>
              <span v-if="gateFile.aiModel" class="gate-preaudit__model text-tertiary text-sm">
                模型 {{ gateFile.aiModel }}
              </span>
            </div>
            <div v-if="gateFile.aiReasons?.length" class="gate-tags">
              <el-tag v-for="r in gateFile.aiReasons" :key="r" size="small" type="info">
                {{ r }}
              </el-tag>
            </div>
            <div v-if="gateManualAiHint" class="gate-preaudit__hint">
              AI 预审建议通过，请确认后入库
            </div>
          </div>

          <div class="gate-kv">
            <span>判定结论</span>
            <span>{{ reviewVerdictText(gateFile.reviewVerdict) || '—' }}</span>
          </div>
          <div class="gate-kv">
            <span>置信度</span>
            <span>{{ gateFile.reviewConfidence != null ? gateFile.reviewConfidence : '—' }}</span>
          </div>
          <div class="gate-kv">
            <span>判定来源</span>
            <span>
              <el-tag v-if="gateFile.reviewSource === 'ai'" size="small" type="success">
                AI 判定
              </el-tag>
              <template v-else>{{ reviewSourceText(gateFile.reviewSource) || '—' }}</template>
            </span>
          </div>
          <div v-if="gateFile.reviewReasons?.length" class="gate-kv">
            <span>触发原因</span>
            <span class="gate-tags">
              <el-tag v-for="r in gateFile.reviewReasons" :key="r" size="small" type="info">
                {{ r }}
              </el-tag>
            </span>
          </div>
          <div v-if="gateFile.vlmFindings?.length" class="gate-kv">
            <span>图片抽检</span>
            <span class="gate-vlm">
              <div
                v-for="(f, i) in gateFile.vlmFindings"
                :key="i"
                :class="f.includes('不可用') ? 'is-bad' : ''"
              >
                {{ f }}
              </div>
            </span>
          </div>
        </div>

        <div class="gate-section">
          <div class="gate-section__title">切片抽样</div>
          <div v-if="gateFile.samples?.length" class="gate-samples">
            <div v-for="(s, i) in gateFile.samples" :key="i" class="gate-sample">{{ s }}</div>
          </div>
          <p v-else class="text-tertiary text-sm">暂无抽样数据（解析完成后由后端返回随机切片）。</p>
        </div>

        <div class="gate-section">
          <div class="gate-section__title">建议分类</div>
          <el-select v-model="gateCategory" style="width: 220px">
            <el-option
              v-for="c in CATEGORY_OPTIONS"
              :key="c.value"
              :label="c.label"
              :value="c.value"
            />
          </el-select>
        </div>

        <div class="gate-section">
          <div class="gate-section__title">数据权限</div>
          <PermissionPicker v-model="gatePermissions" />
        </div>
      </template>
      <template #footer>
        <el-button @click="gateDrawer = false">关闭</el-button>
        <el-button v-if="gateCanRetry" type="primary" @click="gateFile && onRetry(gateFile)">
          重试
        </el-button>
        <el-button
          v-if="gateCanGate"
          type="primary"
          :loading="gateSubmitting"
          @click="submitGate(isProductManual ? 'adjust' : 'confirm')"
        >
          {{ isProductManual ? '按调整结果入库' : '确认入库' }}
        </el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.workbench {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  align-items: start;
}
.workbench__batches {
  padding: 12px;
}
.workbench__batches-head {
  font-weight: 600;
  margin-bottom: 8px;
}
.batch-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.batch-item {
  padding: 8px 10px;
  border-radius: 4px;
  cursor: pointer;
  border: 1px solid transparent;
}
.batch-item:hover {
  background: var(--color-surface);
}
.batch-item.is-active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.batch-item__id {
  font-weight: 600;
}
.batch-item__meta {
  margin-top: 2px;
}
.batch-item__time {
  margin-top: 2px;
}
.workbench__detail {
  min-height: 320px;
}
.workbench__empty {
  padding: 40px 0;
}
.workbench__batch-id {
  font-weight: 600;
  font-size: 14px;
}
.upload-category {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.gate-section {
  margin-bottom: 20px;
}
.gate-section__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}
.gate-kv {
  display: flex;
  gap: 12px;
  font-size: 13px;
  padding: 4px 0;
}
.gate-kv > span:first-child {
  width: 72px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}
.gate-kv--error {
  color: var(--stage-failed);
}
.gate-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.gate-vlm {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  line-height: 1.5;
}
.gate-vlm .is-bad {
  color: var(--stage-failed);
}
.gate-preaudit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  margin-bottom: 10px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
}
.gate-preaudit__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.gate-preaudit__hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
}
.gate-samples {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.gate-sample {
  padding: 8px 10px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
</style>
