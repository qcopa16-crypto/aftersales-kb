<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import SourceCard from '@/components/SourceCard.vue'
import { categoryLabel } from '@/constants/knowledge'
import { useChatStore } from '@/stores/chat'

const chat = useChatStore()
const input = ref('')
const listRef = ref(null)

const messages = computed(() => chat.currentSession?.messages ?? [])

const suggestions = [
  '客户屏幕碎了能保修吗？',
  '路由器安装后无法上网，如何排查？',
  '供应商结算对账流程是怎样的？',
]

/** 会话时间只保留到分钟，列表里不需要年份 */
function shortTime(value) {
  return value ? value.slice(5) : ''
}

function scrollToBottom() {
  nextTick(() => {
    const el = listRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function send() {
  const text = input.value.trim()
  if (!text) return
  chat.ask(text)
  input.value = ''
  scrollToBottom()
}

function useSuggestion(suggestion) {
  input.value = suggestion
  send()
}

function newSession() {
  chat.newSession()
}

async function openSession(id) {
  await chat.switchSession(id)
  scrollToBottom()
}

// 流式追加时持续滚动
watch(
  () => messages.value.map((m) => m.content.length).join(','),
  () => scrollToBottom(),
)

onMounted(async () => {
  await chat.loadSessions()
  // 刷新后自动回到最近一次会话：否则历史虽在列表里、主区域却是空欢迎页，
  // 看起来就像「对话丢了」。
  if (chat.sessions.length) {
    await openSession(chat.sessions[0].id)
  }
})
</script>

<template>
  <div class="chat-page">
    <PageHeader title="AI 问答" description="基于授权知识的流式回答，来源与权限可见">
      <template #actions>
        <el-button :icon="'Plus'" @click="newSession">新建会话</el-button>
      </template>
    </PageHeader>

    <div class="chat">
      <!-- 会话列表：历史来自后端（按 session_id 聚合），本次新建的会话置顶 -->
      <aside v-loading="chat.loadingSessions" class="chat__sessions">
        <div class="chat__sessions-head">
          <span class="text-secondary text-md">历史会话</span>
          <span class="text-tertiary text-sm">共 {{ chat.sessions.length }} 个</span>
        </div>
        <ul class="session-list">
          <li
            v-for="s in chat.sessions"
            :key="s.id"
            class="session-item"
            :class="{ 'is-active': s.id === chat.currentSessionId }"
            @click="openSession(s.id)"
          >
            <div class="session-item__main">
              <span class="session-item__title">{{ s.title }}</span>
              <span class="session-item__meta text-tertiary">
                {{ s.messageCount }} 轮
                <template v-if="s.lastAskedAt">· {{ shortTime(s.lastAskedAt) }}</template>
              </span>
            </div>
            <el-icon
              v-if="!s.persisted"
              class="session-item__del"
              title="移除未落库的会话"
              @click.stop="chat.removeSession(s.id)"
            >
              <Close />
            </el-icon>
          </li>
        </ul>
        <el-empty
          v-if="!chat.loadingSessions && !chat.sessions.length"
          description="暂无会话，提问后自动记录"
          :image-size="50"
        />
      </aside>

      <!-- 对话区 -->
      <section class="chat__main">
        <div ref="listRef" v-loading="chat.loadingMessages" class="chat__messages">
          <div v-if="!messages.length" class="chat__welcome">
            <el-icon class="chat__welcome-icon"><ChatDotRound /></el-icon>
            <h3>向售后知识库提问</h3>
            <p class="text-tertiary">回答仅基于你有权限访问的知识，无权限内容会单独提示。</p>
            <div class="chat__suggestions">
              <el-button
                v-for="s in suggestions"
                :key="s"
                plain
                size="small"
                @click="useSuggestion(s)"
              >
                {{ s }}
              </el-button>
            </div>
          </div>

          <div v-for="m in messages" :key="m.id" class="msg" :class="`msg--${m.role}`">
            <div class="msg__role">
              <!-- 生成中显示旋转 spinner；结束后固定为对话气泡图标，避免被误读成"一直在加载" -->
              <el-icon v-if="m.role !== 'user' && m.streaming" class="is-loading">
                <Loading />
              </el-icon>
              <el-icon v-else>
                <component :is="m.role === 'user' ? 'User' : 'ChatDotRound'" />
              </el-icon>
              {{ m.role === 'user' ? '我' : '知识库助手' }}
            </div>
            <div class="msg__body">
              <div v-if="m.role === 'user'" class="msg__content">
                <el-tag
                  v-if="m.choice"
                  size="small"
                  type="primary"
                  effect="plain"
                  class="msg__choice-tag"
                >
                  已选知识
                </el-tag>
                <el-tag
                  v-else-if="m.supplement"
                  size="small"
                  type="info"
                  effect="plain"
                  class="msg__choice-tag"
                >
                  补充说明
                </el-tag>
                {{ m.content }}
              </div>
              <template v-else>
                <!-- 闲聊兜底：显著提示"这是 AI 通用知识，非公司规定"，放在回答之上 -->
                <div v-if="m.notice" class="msg__notice">
                  <el-icon><InfoFilled /></el-icon>
                  <span>{{ m.notice }}</span>
                </div>
                <div v-if="m.content" class="msg__content">{{ m.content }}</div>
                <div v-else-if="m.streaming" class="msg__thinking text-tertiary">
                  正在检索并生成…
                </div>

                <div v-if="m.error" class="msg__error">
                  <el-icon><WarningFilled /></el-icon>
                  {{ m.error }}
                </div>

                <!--
                  澄清有两种形态：
                  · ask      问题信息不足 → 追问缺的那一项，给可点的补充选项（点选/打字都会拼进原问题重问）
                  · reference 问题已问清楚但知识库没命中 → 给"可能相关的资料"作参考，点不点都行
                -->
                <div v-if="m.clarifyOptions?.length" class="clarify">
                  <div class="clarify__head">
                    {{ m.clarifyKind === 'ask' ? '补充这些信息后我再回答' : '可能相关的资料' }}
                  </div>

                  <!-- 追问：补充信息（现象/设备），不是让用户挑资料 -->
                  <div v-if="m.clarifyKind === 'ask'" class="clarify__hints">
                    <button
                      v-for="o in m.clarifyOptions"
                      :key="o.title"
                      type="button"
                      class="clarify__hint"
                      :disabled="chat.streaming"
                      @click="chat.chooseClarify(m, o)"
                    >
                      {{ o.title }}
                    </button>
                  </div>

                  <!-- 参考：可点开查看的知识单元 -->
                  <template v-else>
                    <button
                      v-for="o in m.clarifyOptions"
                      :key="o.unitId"
                      type="button"
                      class="clarify__option"
                      :disabled="chat.streaming"
                      @click="chat.chooseClarify(m, o)"
                    >
                      <span class="clarify__title">{{ o.title }}</span>
                      <span class="clarify__meta">
                        {{ o.unitCode }} · {{ categoryLabel(o.category) }}
                      </span>
                      <span v-if="o.snippet" class="clarify__snippet">{{ o.snippet }}</span>
                    </button>
                  </template>
                </div>
                <div v-else-if="m.chosenTitle" class="clarify__chosen text-tertiary text-sm">
                  已按所选知识作答：{{ m.chosenTitle }}
                </div>

                <SourceCard v-if="m.sources" :sources="m.sources" class="msg__sources" />

                <div v-if="m.tokens !== undefined" class="msg__meta text-tertiary text-sm">
                  <span class="text-mono">tokens {{ m.tokens }}</span>
                  <span>·</span>
                  <span class="text-mono">耗时 {{ m.elapsedMs }}ms</span>
                  <span v-if="m.faqHit" class="msg__faq">· 命中 FAQ 缓存</span>
                </div>
              </template>
            </div>
          </div>
        </div>

        <div class="chat__input">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="输入问题，Enter 发送 / Shift+Enter 换行"
            @keydown.enter.exact.prevent="send"
          />
          <div class="chat__input-actions">
            <el-button v-if="chat.streaming" @click="chat.stop">停止生成</el-button>
            <el-button type="primary" :disabled="chat.streaming || !input.trim()" @click="send">
              发送
            </el-button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.chat {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: 16px;
}
.chat__sessions {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 12px;
  overflow-y: auto;
}
.chat__sessions-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  font-weight: 600;
  margin-bottom: 8px;
}
.session-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 8px 7px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  border: 1px solid transparent;
}
.session-item:hover {
  background: var(--color-surface);
}
.session-item.is-active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.session-item__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.session-item__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: var(--line-height-tight);
}
.session-item__meta {
  font-size: 11px;
  line-height: var(--line-height-tight);
}
.session-item__del {
  color: var(--color-text-tertiary);
  opacity: 0;
  flex-shrink: 0;
}
.session-item:hover .session-item__del {
  opacity: 1;
}
.chat__main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.chat__messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
.chat__welcome {
  text-align: center;
  padding-top: 60px;
}
.chat__welcome-icon {
  font-size: 36px;
  color: var(--brand-500);
}
.chat__welcome h3 {
  margin: 12px 0 4px;
  font-size: 16px;
}
.chat__suggestions {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
}
.msg {
  display: flex;
  gap: 12px;
  padding: 14px 0;
  border-bottom: 1px solid var(--color-border);
}
.msg:last-child {
  border-bottom: none;
}
.msg__role {
  width: 110px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
}
.msg--assistant .msg__role {
  color: var(--brand-500);
}
.msg__body {
  flex: 1;
  min-width: 0;
}
.msg__content {
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg--user .msg__content {
  color: var(--color-text);
}
.msg__thinking {
  font-size: 13px;
}
.msg__error {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 13px;
  color: var(--stage-failed);
}
/* 闲聊兜底横幅：warning 色、显著，提醒"这是 AI 通用知识，非公司规定" */
.msg__notice {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-bottom: 8px;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-warning) 35%, transparent);
  border-radius: 6px;
}
.msg__notice .el-icon {
  flex: none;
  margin-top: 2px;
}
/* 用户消息里的「已选知识」标记：区分普通提问与澄清点选 */
.msg__choice-tag {
  margin-right: 6px;
  vertical-align: middle;
}
/* 澄清候选：可点选的整块按钮，而不是小 chip —— 标题+编码+片段才有足够信息量做判断 */
.clarify {
  margin-top: 12px;
  padding: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
}
.clarify__head {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}
/* 追问的补充选项：小圆角 chip，一行能放好几个，扫读成本低 */
.clarify__hints {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.clarify__hint {
  padding: 5px 12px;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 13px;
  line-height: var(--line-height-tight);
  cursor: pointer;
  transition:
    border-color 0.15s ease,
    background 0.15s ease,
    color 0.15s ease;
}
.clarify__hint:hover:not(:disabled) {
  border-color: var(--brand-500, #3e63dd);
  background: var(--el-color-primary-light-9);
  color: var(--brand-500, #3e63dd);
}
.clarify__hint:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.clarify__option {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  text-align: left;
  padding: 9px 12px;
  margin-bottom: 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  cursor: pointer;
  font: inherit;
  transition:
    border-color 0.15s ease,
    background 0.15s ease;
}
.clarify__option:last-child {
  margin-bottom: 0;
}
.clarify__option:hover:not(:disabled) {
  border-color: var(--brand-500, #3e63dd);
  background: var(--el-color-primary-light-9);
}
.clarify__option:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.clarify__title {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  line-height: var(--line-height-tight);
}
.clarify__meta {
  font-size: 11px;
  color: var(--color-text-tertiary);
  line-height: var(--line-height-tight);
}
.clarify__snippet {
  margin-top: 2px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: var(--line-height-base);
}
.clarify__chosen {
  margin-top: 8px;
}
.msg__sources {
  margin-top: 12px;
  padding: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
}
.msg__meta {
  margin-top: 10px;
  display: flex;
  gap: 6px;
}
.msg__faq {
  color: var(--color-success);
}
.chat__input {
  border-top: 1px solid var(--color-border);
  padding: 12px 16px;
  display: flex;
  gap: 12px;
  align-items: flex-end;
}
.chat__input :deep(.el-textarea) {
  flex: 1;
}
.chat__input-actions {
  display: flex;
  gap: 8px;
}
</style>
