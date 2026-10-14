import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { chatStream, fetchChatSession, fetchChatSessions } from '@/api/ai'

/** 一次拉取的会话条数（后端上限 50） */
const SESSION_LIMIT = 20

let seq = 0

function uid(prefix) {
  seq += 1
  return `${prefix}_${Date.now()}_${seq}`
}

/**
 * 把后端落库的一轮问答还原成对话消息。
 *
 * 后端按「轮次」存储（question + answer + 来源 + 指标），这里摊平成
 * user / assistant 两条消息，字段名与 SSE 回调一致，渲染层无需区分来源。
 */
function roundsToMessages(rounds) {
  const messages = []
  rounds.forEach((round, index) => {
    messages.push({ id: `h${index}q`, role: 'user', content: round.question })
    messages.push({
      id: `h${index}a`,
      role: 'assistant',
      content: round.answer,
      sources: round.sources,
      tokens: round.tokens,
      elapsedMs: round.elapsedMs,
      faqHit: round.faqHit,
      createdAt: round.createdAt,
      // 这一轮是澄清点选作答时，回看要能看出选了哪条知识
      chosenTitle: round.chosenTitle || undefined,
      // 答案来源（kb/faq/llm_general/clarify/refusal），用于看板来源标签与闲聊兜底标识
      answerSource: round.answerSource || undefined,
    })
  })
  return messages
}

/**
 * AI 对话工作台状态：历史会话 + 流式回答。
 *
 * 会话本身不落库，历史从 `qa_access_logs` 按 session_id 聚合而来（见后端 ChatHistoryService）：
 * 列表只拉轻量的会话摘要，点开时才按需拉该会话的完整问答轮次。
 */
export const useChatStore = defineStore('chat', () => {
  /** 会话列表（历史 + 本次新建）；消息按需加载 */
  const sessions = ref([])
  const currentSessionId = ref('')
  const streaming = ref(false)
  const loadingSessions = ref(false)
  const loadingMessages = ref(false)
  let controller = null
  /**
   * 上一轮是「缺信息追问」时的原问题。
   *
   * 追问后用户无论是点选项还是直接打字，输入的都是**补充信息**，
   * 必须和原问题拼起来才能检索（「指示灯异常」单独检索是没有意义的）。
   */
  let pendingClarifyBase = ''

  const currentSession = computed(
    () => sessions.value.find((s) => s.id === currentSessionId.value) ?? null,
  )

  /** 拉取历史会话列表，并把本次已打开会话的消息保留下来 */
  async function loadSessions() {
    loadingSessions.value = true
    try {
      const list = await fetchChatSessions(SESSION_LIMIT)
      const known = new Map(sessions.value.map((s) => [s.id, s]))
      const history = list.map((item) => {
        const existing = known.get(item.sessionId)
        return {
          id: item.sessionId,
          title: item.title || '未命名会话',
          messageCount: item.messageCount,
          lastAskedAt: item.lastAskedAt,
          persisted: true,
          // 已打开过的会话保留本地消息，避免列表刷新把对话内容清空
          loaded: existing?.loaded ?? false,
          messages: existing?.messages ?? [],
        }
      })
      // 本地新建但尚未落库的会话（如刚发完问、后端还没写入）排在最前
      const localOnly = sessions.value.filter(
        (s) => !s.persisted && !list.some((item) => item.sessionId === s.id),
      )
      sessions.value = [...localOnly, ...history]
    } finally {
      loadingSessions.value = false
    }
  }

  /** 打开会话；历史会话首次打开时按需拉取问答轮次 */
  async function openSession(id) {
    currentSessionId.value = id
    const session = sessions.value.find((s) => s.id === id)
    if (!session || session.loaded) return
    loadingMessages.value = true
    try {
      const detail = await fetchChatSession(id)
      session.messages = roundsToMessages(detail?.rounds ?? [])
      session.loaded = true
      session.messageCount = detail?.rounds?.length ?? session.messageCount
    } finally {
      loadingMessages.value = false
    }
  }

  function newSession() {
    const session = {
      id: uid('s'),
      title: '新会话',
      messageCount: 0,
      lastAskedAt: '',
      persisted: false,
      loaded: true,
      messages: [],
    }
    sessions.value.unshift(session)
    currentSessionId.value = session.id
    pendingClarifyBase = ''
    return session
  }

  function switchSession(id) {
    pendingClarifyBase = ''
    return openSession(id)
  }

  function ensureSession() {
    return currentSession.value ?? newSession()
  }

  /** 仅本地新建（未落库）的会话可从列表移除；历史会话属于审计日志，不在此处删除 */
  function removeSession(id) {
    const session = sessions.value.find((s) => s.id === id)
    if (session?.persisted) return
    sessions.value = sessions.value.filter((s) => s.id !== id)
    if (currentSessionId.value === id) {
      currentSessionId.value = sessions.value[0]?.id ?? ''
    }
  }

  /** 发起提问，流式接收回答 */
  function ask(question) {
    const text = question.trim()
    if (!text || streaming.value) return

    const session = ensureSession()
    // 上一轮问过"缺什么"时，本次输入视为补充：与原问题拼成完整问题再检索
    const base = pendingClarifyBase
    pendingClarifyBase = ''
    const effective = base ? `${base} ${text}` : text

    // 用首问作为会话标题
    if (session.messages.length === 0) {
      session.title = effective.slice(0, 20)
    }
    // 消息里只展示用户实际输入的内容，拼进去的原问题不重复显示
    session.messages.push({ id: uid('m'), role: 'user', content: text, supplement: !!base })
    stream(session, effective, null)
  }

  /**
   * 点选澄清候选：用「上一轮的原问题 + 选中的知识单元」重新作答。
   *
   * 原问题从该候选消息之前的最后一条用户消息取——后端拿它做问题、拿 chosenUnitId 做范围，
   * 因此不需要用户再输一遍。
   */
  function chooseClarify(assistant, option) {
    if (streaming.value) return
    const session = currentSession.value
    if (!session) return

    // 选完就收起候选，避免重复点选
    assistant.clarifyOptions = []

    if (option.type === 'hint') {
      // 补充信息：拼上原问题重新检索（用户补的是"现象"，不是"资料"）
      pendingClarifyBase = ''
      session.messages.push({
        id: uid('m'),
        role: 'user',
        content: option.title,
        supplement: true,
      })
      const base = assistant.question || ''
      stream(session, base ? `${base} ${option.title}` : option.title, null)
      return
    }

    // 指定知识单元：跳过检索，直接基于它作答
    const index = session.messages.findIndex((m) => m.id === assistant.id)
    const previousUser = [...session.messages.slice(0, Math.max(index, 0))]
      .reverse()
      .find((m) => m.role === 'user')
    if (!previousUser) return

    pendingClarifyBase = ''
    assistant.chosenTitle = option.title
    session.messages.push({
      id: uid('m'),
      role: 'user',
      content: option.title,
      choice: true,
    })
    stream(session, previousUser.content, option.unitId)
  }

  /** 实际的流式请求：chosenUnitId 非空表示这是澄清点选后的一轮 */
  function stream(session, text, chosenUnitId) {
    const assistant = {
      id: uid('m'),
      role: 'assistant',
      content: '',
      streaming: true,
      // 记下本轮实际发给后端的问题：追问后补充信息时要拼上它
      question: text,
    }
    session.messages.push(assistant)
    streaming.value = true
    controller = new AbortController()

    chatStream(
      session.id,
      text,
      {
        onSources: (sources) => {
          assistant.sources = sources
        },
        onNotice: (payload) => {
          // 闲聊兜底横幅：让用户知道"这是 AI 通用知识，非公司规定"
          assistant.notice = payload?.text ?? ''
          assistant.answerSource = payload?.kind
        },
        onClarify: (payload) => {
          assistant.clarifyKind = payload?.kind ?? 'reference'
          assistant.clarifyPrompt = payload?.prompt ?? ''
          assistant.clarifyOptions = payload?.options ?? []
          // 追问：进入"等待补充"状态，用户下一次输入会与原问题拼接
          if (assistant.clarifyKind === 'ask') {
            pendingClarifyBase = text
          }
        },
        onDelta: (chunk) => {
          assistant.content += chunk
        },
        onDone: (done) => {
          assistant.tokens = done.tokens
          assistant.elapsedMs = done.elapsedMs
          assistant.faqHit = done.faqHit
          assistant.streaming = false
          streaming.value = false
          session.messageCount += 1
        },
        onError: (err) => {
          assistant.error = err.message
          assistant.streaming = false
          streaming.value = false
        },
      },
      controller.signal,
      chosenUnitId,
    ).finally(() => {
      assistant.streaming = false
      streaming.value = false
    })
  }

  /** 中断当前流式回答 */
  function stop() {
    controller?.abort()
    controller = null
    streaming.value = false
    const last = currentSession.value?.messages.at(-1)
    if (last?.streaming) last.streaming = false
  }

  function reset() {
    stop()
    pendingClarifyBase = ''
    sessions.value = []
    currentSessionId.value = ''
  }

  return {
    sessions,
    currentSessionId,
    currentSession,
    streaming,
    loadingSessions,
    loadingMessages,
    loadSessions,
    openSession,
    newSession,
    switchSession,
    removeSession,
    ask,
    chooseClarify,
    stop,
    reset,
  }
})
