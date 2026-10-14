import { TOKEN_KEY } from '@/api/http'

/**
 * 解析单个 SSE 帧的原始文本。
 *
 * 帧格式（W3C EventSource）：
 *   event: <name>\n
 *   data: <payload>\n
 * 冒号后如有一个空格需去掉；以 ':' 开头的是注释（心跳），直接忽略。
 */
function parseFrame(raw) {
  let event = 'message'
  const dataLines = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const idx = line.indexOf(':')
    const field = idx === -1 ? line : line.slice(0, idx)
    let value = idx === -1 ? '' : line.slice(idx + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}

/**
 * 用 fetch + ReadableStream 手动解析 SSE 分帧。
 *
 * POST 请求无法使用 EventSource，必须自行读取流并按 \n\n 分帧。
 *
 * @param {string} url 接口地址
 * @param {unknown} body 请求体（会 JSON 序列化）
 * @param {SseHandlers} handlers 事件回调
 * @param {AbortSignal} [signal] 中断信号
 * @returns {Promise<void>}
 *
 * @typedef {object} SseHandlers
 * @property {(data: object) => void} [onSources] 检索来源（授权/未授权分开）
 * @property {(data: object) => void} [onNotice]  显著提示横幅（如"以下为 AI 通用知识"）
 * @property {(data: object) => void} [onClarify] 无召回时的澄清候选（让用户点选知识）
 * @property {(text: string) => void} [onDelta]    增量文本片段
 * @property {(data: object) => void} [onDone]     结束，携带 tokens/耗时/是否 FAQ 命中
 * @property {(err: object) => void}  [onError]    服务端 error 事件或传输层错误
 */
export async function fetchStream(url, body, handlers, signal) {
  const token = localStorage.getItem(TOKEN_KEY)
  let resp
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    })
  } catch (e) {
    if (e?.name === 'AbortError') return
    handlers.onError?.({ code: 'NETWORK', message: '网络异常，无法连接问答服务' })
    return
  }

  // 非 2xx：后端可能返回统一 Result JSON
  if (!resp.ok || !resp.body) {
    let message = 'AI 服务异常，请稍后重试'
    let code = String(resp.status)
    try {
      const json = await resp.json()
      if (json?.message) message = json.message
      if (json?.code !== undefined) code = String(json.code)
    } catch {
      /* 忽略解析失败，用默认文案 */
    }
    handlers.onError?.({ code, message })
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  function dispatch(frame) {
    let payload
    try {
      payload = JSON.parse(frame.data)
    } catch {
      return // 忽略无法解析的帧
    }
    switch (frame.event) {
      case 'sources':
        handlers.onSources?.(payload)
        break
      case 'notice':
        handlers.onNotice?.(payload)
        break
      case 'clarify':
        handlers.onClarify?.(payload)
        break
      case 'delta':
        handlers.onDelta?.(payload.text ?? '')
        break
      case 'done':
        handlers.onDone?.(payload)
        break
      case 'error':
        handlers.onError?.(payload)
        break
      default:
        break
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 兼容 \r\n / \r
      buffer = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const rawFrame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const frame = parseFrame(rawFrame)
        if (frame) dispatch(frame)
        sep = buffer.indexOf('\n\n')
      }
    }
    // 处理结尾可能残留的最后一帧（无尾随空行）
    if (buffer.trim()) {
      const frame = parseFrame(buffer)
      if (frame) dispatch(frame)
    }
  } catch (e) {
    if (e?.name !== 'AbortError') {
      handlers.onError?.({ code: 'STREAM', message: '流式响应中断，请重试' })
    }
  } finally {
    reader.releaseLock()
  }
}
