import { get } from './http'
import { fetchStream } from '@/utils/sse'

/**
 * AI 问答流式接口（POST + SSE，需手动解析分帧）。
 *
 * @param {string} sessionId 会话 id
 * @param {string} question 用户问题
 * @param {object} handlers SSE 事件回调
 * @param {AbortSignal} [signal] 中断信号
 * @param {number} [chosenUnitId] 澄清点选：上一轮候选里选中的知识单元 id
 */
export function chatStream(sessionId, question, handlers, signal, chosenUnitId) {
  const body = { sessionId, question }
  if (chosenUnitId != null) {
    body.chosenUnitId = chosenUnitId
  }
  return fetchStream('/api/ai/chat/stream', body, handlers, signal)
}

/** 历史会话列表（仅本人），按最近提问时间倒序 */
export function fetchChatSessions(limit) {
  return get('/ai/chat/sessions', limit ? { limit } : {})
}

/** 历史会话详情：按时间正序的问答轮次（含来源卡片与 tokens/耗时） */
export function fetchChatSession(sessionId) {
  return get(`/ai/chat/sessions/${encodeURIComponent(sessionId)}`)
}
