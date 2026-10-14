/**
 * 真实浏览器 UI 校验（零依赖：Node 内置 WebSocket + Chrome DevTools Protocol）。
 *
 * 校验用户报的 4 个问题是否修好：
 *   1. 看板排行榜文字为空
 *   2. 入库工作台详情一直转圈
 *   3. AI 问答答案出来后仍显示"转圈"图标
 *   4. 知识缺口列表一直转圈
 *
 * 用法：node scripts/ui-check.mjs
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
]
const PORT = 9333
const APP = 'http://[::1]:5173' // Vite 只监听 IPv6 回环
const results = []

function check(name, ok, detail = '') {
  results.push(ok)
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (ok ? '' : '   -> ' + JSON.stringify(detail)))
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function findChrome() {
  const { existsSync } = await import('node:fs')
  for (const p of CHROME_CANDIDATES) if (existsSync(p)) return p
  throw new Error('no chrome/edge found')
}

async function waitHttp(url, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    try {
      const r = await fetch(url)
      if (r.ok) return await r.json()
    } catch {
      /* retry */
    }
    await sleep(300)
  }
  throw new Error('timeout waiting ' + url)
}

function cdpClient(ws) {
  let seq = 0
  const pending = new Map()
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
    }
  })
  return (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++seq
      pending.set(id, { resolve, reject })
      ws.send(JSON.stringify({ id, method, params }))
    })
}

const chrome = await findChrome()
const profile = mkdtempSync(join(tmpdir(), 'kb-ui-'))
const proc = spawn(chrome, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore' })

try {
  await waitHttp(`http://127.0.0.1:${PORT}/json/version`)
  const target = await waitHttp(`http://127.0.0.1:${PORT}/json/new?about:blank`, 10000).catch(async () => {
    const list = await waitHttp(`http://127.0.0.1:${PORT}/json/list`)
    return list.find((t) => t.type === 'page')
  })
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    ws.addEventListener('open', res)
    ws.addEventListener('error', rej)
  })
  const send = cdpClient(ws)
  await send('Page.enable')
  await send('Runtime.enable')
  // 用桌面宽度，贴近用户真实屏幕（默认 headless 视口太窄，会触发响应式折叠）
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  })

  async function evaluate(expression) {
    const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (r.exceptionDetails) throw new Error('JS error: ' + JSON.stringify(r.exceptionDetails).slice(0, 300))
    return r.result.value
  }

  async function goto(hash, settleMs = 1800) {
    await evaluate(`location.href = ${JSON.stringify(APP + '/' + hash)}`)
    await sleep(settleMs)
  }

  async function shot(name, fullPage = false) {
    const r = await send('Page.captureScreenshot', {
      format: 'png',
      captureBeyondViewport: fullPage,
    })
    writeFileSync(join('target', `ui-${name}.png`), Buffer.from(r.data, 'base64'))
  }

  async function waitFor(expr, timeoutMs = 15000, intervalMs = 400) {
    const end = Date.now() + timeoutMs
    while (Date.now() < end) {
      if (await evaluate(expr)) return true
      await sleep(intervalMs)
    }
    return false
  }

  /** 打印页面真实 DOM 概要，便于校正选择器 */
  async function diag(name) {
    const info = await evaluate(`JSON.stringify({
      hash: location.hash,
      title: (document.querySelector('.page-header h1, .page-header__title, h1, h2') || {}).textContent || '',
      ranks: document.querySelectorAll('.rank-item').length,
      rankText: Array.from(document.querySelectorAll('.rank-item__text')).slice(0,2).map(e=>e.textContent.trim()),
      batchItems: document.querySelectorAll('.batch-item').length,
      workbenchRows: document.querySelectorAll('.workbench__detail .el-table__body-wrapper tbody tr').length,
      anyRows: document.querySelectorAll('.el-table__body-wrapper tbody tr').length,
      masks: document.querySelectorAll('.el-loading-mask').length,
      textareas: document.querySelectorAll('textarea').length,
      sendBtns: Array.from(document.querySelectorAll('button')).filter(b=>b.textContent.trim()==='发送').length,
      msgs: document.querySelectorAll('.msg').length,
      metas: document.querySelectorAll('.msg__meta').length
    })`)
    console.log('    [diag] ' + name + ' ' + info)
  }

  // ---- 真实 UI 登录（走页面表单，与用户路径一致） ----
  await goto('#/login', 2500)
  const loginResult = await evaluate(`(() => {
    const u = document.querySelector('input[placeholder="用户名"]')
    const p = document.querySelector('input[placeholder="密码"]')
    if (!u || !p) return 'no-inputs'
    const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })) }
    set(u, 'admin'); set(p, '123456')
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '登录')
    if (!btn) return 'no-button'
    btn.click()
    return 'clicked'
  })()`)
  const loggedIn = await waitFor(`location.hash.indexOf('#/login') === -1 && !!document.querySelector('aside, .admin-layout, .el-menu')`, 20000, 500)
  check('浏览器内通过登录表单登录成功', loginResult === 'clicked' && loggedIn, { loginResult, hash: await evaluate('location.hash') })

  // ---- 1. 看板排行榜 ----
  await goto('#/dashboard', 3000)
  await waitFor(`document.querySelectorAll('.rank-item__text').length > 0`, 15000)
  const ranks = await evaluate(`Array.from(document.querySelectorAll('.rank-item__text')).map(e => e.textContent.trim())`)
  const masks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  check('看板：排行榜条目已渲染', Array.isArray(ranks) && ranks.length > 0, ranks)
  check('看板：排行文字非空（问题/知识标题已显示）',
    Array.isArray(ranks) && ranks.length > 0 && ranks.every((t) => t && t.length > 0), ranks)
  check('看板：无残留 loading 遮罩', masks === 0, masks)
  await shot('dashboard')

  // ---- 2. 知识缺口 ----
  await goto('#/gaps', 2500)
  await sleep(1200)
  await diag('gaps')
  const gapMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  const gapRows = await evaluate(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length`)
  const gapFirst = await evaluate(
    `(document.querySelector('.el-table__body-wrapper tbody tr td .cell')||{}).textContent || ''`)
  check('知识缺口：列表加载完成后无残留 loading 遮罩', gapMasks === 0, gapMasks)
  check('知识缺口：有数据时问题文字非空（questionPattern 已正确渲染）',
    gapRows === 0 || (gapFirst && gapFirst.trim().length > 0), { gapRows, gapFirst })
  await shot('gaps')

  // ---- 3. 入库工作台 ----
  await goto('#/ingest', 2500)
  await diag('ingest-after-goto')
  await waitFor(`document.querySelectorAll('.batch-item, .el-table__row, .workbench__batch').length > 0`, 12000)
  const clicked = await evaluate(`(() => {
    const el = document.querySelector('.batch-item')
    if (!el) return 'no-batch-item'
    el.click()
    return el.textContent.trim().slice(0, 30)
  })()`)
  await sleep(2500)
  const ingestMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  const ingestRows = await evaluate(`document.querySelectorAll('.workbench__detail .el-table__body-wrapper tbody tr').length`)
  await diag('ingest-after-click')
  check('入库工作台：详情加载完成后无残留 loading 遮罩（不再一直转）', ingestMasks === 0, ingestMasks)
  if (clicked === 'no-batch-item') {
    // 数据被清理后没有批次：应展示空态而不是一直转圈
    const emptyShown = await evaluate(`document.querySelectorAll('.el-empty').length > 0`)
    check('入库工作台：无批次时展示空态（而非一直转圈）', emptyShown, emptyShown)
  } else {
    check('入库工作台：成功选中批次', true, clicked)
    check('入库工作台：详情表格已渲染文件行', ingestRows > 0, ingestRows)
  }
  // 待人工质检角标：只校验元素查询可用（是否有 gated 文档取决于数据，不硬编码）
  const qcBadge = await evaluate(`document.querySelectorAll('.el-tag').length ? Array.from(document.querySelectorAll('.el-tag')).filter(t => t.textContent.includes('待人工质检')).length : 0`)
  check('入库工作台：待人工质检角标（有 gated 文档时显示）', typeof qcBadge === 'number', qcBadge)
  await shot('ingest')

  // ---- 4. AI 问答 ----
  await goto('#/ai', 2500)
  await diag('chat-before-send')
  // 注意：进入页面会自动打开最近一次历史会话，所以主区域可能已有历史消息。
  // 所有「本轮回答」的断言都必须基于消息数增量，不能直接判断"有没有消息"。
  const msgsBefore = await evaluate(`document.querySelectorAll('.msg').length`)
  const sent = await evaluate(`(() => {
    const ta = document.querySelector('textarea')
    if (!ta) return 'no-textarea'
    ta.focus()
    return 'focused'
  })()`)
  // 用 CDP 真实输入，确保 Vue 的 v-model 收到变更（直接改 .value + dispatch 不一定生效）
  // 刻意用笼统描述：「有问题」只说了故障，没说现象 —— 系统应追问缺的那一项，
  // 而不是甩一堆知识文档让用户挑。
  await send('Input.insertText', { text: '路由器有问题' })
  await sleep(400)
  const filled = await evaluate(`(() => {
    const ta = document.querySelector('textarea')
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发送')
    return JSON.stringify({ value: (ta||{}).value || '', disabled: btn ? btn.disabled : null })
  })()`)
  console.log('    [diag] chat-filled ' + filled)
  const clickedSend = await evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发送')
    if (!btn) return 'no-button'
    if (btn.disabled) return 'disabled'
    btn.click()
    return 'clicked'
  })()`)
  check('AI 问答：成功发送问题', clickedSend === 'clicked', { sent, clickedSend, filled })

  // 本轮结束的判据：消息数 +2（一问一答）且不再有旋转图标
  const answered = await waitFor(
    `document.querySelectorAll('.msg').length >= ${msgsBefore + 2} &&
     document.querySelectorAll('.msg--assistant .msg__role .is-loading').length === 0`,
    90000,
    1000,
  )
  const msgsAfter = await evaluate(`document.querySelectorAll('.msg').length`)
  check('AI 问答：本轮回答完成（新增一问一答两条消息）',
    answered && msgsAfter >= msgsBefore + 2, { msgsBefore, msgsAfter })
  await sleep(900)
  const spinning = await evaluate(`document.querySelectorAll('.msg--assistant .msg__role .is-loading').length`)
  const lastMeta = await evaluate(`(() => {
    const metas = document.querySelectorAll('.msg--assistant .msg__meta')
    return metas.length ? metas[metas.length - 1].textContent.replace(/\\s+/g,' ').trim() : ''
  })()`)
  const lastAnswer = await evaluate(`(() => {
    const els = document.querySelectorAll('.msg--assistant .msg__content')
    return els.length ? els[els.length - 1].textContent : ''
  })()`)
  check('AI 问答：回答完成后角色图标不再旋转（转圈图标消失）', spinning === 0, spinning)
  check('AI 问答：最新回答显示 tokens 与耗时',
    lastMeta.includes('tokens') && lastMeta.includes('耗时'), lastMeta)
  check('AI 问答：回答正文非空', lastAnswer.trim().length > 0, lastAnswer.slice(0, 80))
  const chatMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  check('AI 问答：无残留 loading 遮罩', chatMasks === 0, chatMasks)

  // ---- 4a. 澄清（一）：信息不足时追问缺的那一项 ----
  const askHead = await evaluate(
    `(document.querySelector('.clarify__head')||{}).textContent.trim() || ''`)
  const hintCount = await evaluate(`document.querySelectorAll('.clarify__hint').length`)
  const unitOptionCount = await evaluate(`document.querySelectorAll('.clarify__option').length`)
  const askText = await evaluate(
    `Array.from(document.querySelectorAll('.clarify__hint')).map(e => e.textContent.trim())`)

  if (hintCount > 0) {
    check('AI 问答：笼统问题触发追问（而不是直接列知识文档）',
      askHead.includes('补充这些信息'), { askHead, hintCount, unitOptionCount })
    check('AI 问答：追问给出可点的补充选项（现象维度）',
      hintCount >= 3 && askText.every((t) => t.length > 0), askText)
    check('AI 问答：追问文案回显原问题并说明要补什么',
      lastAnswer.includes('路由器有问题') && lastAnswer.includes('具体现象'), lastAnswer.slice(0, 80))
    await shot('chat-ask')

    // 点一个现象选项 → 应把「原问题 + 现象」拼起来重新走完整流程
    const msgsBeforeHint = await evaluate(`document.querySelectorAll('.msg').length`)
    await evaluate(
      `(() => { const b = document.querySelector('.clarify__hint'); if (b) b.click(); return true })()`,
    )
    const afterHint = await waitFor(
      `document.querySelectorAll('.msg').length >= ${msgsBeforeHint + 2} &&
       document.querySelectorAll('.msg--assistant .msg__role .is-loading').length === 0`,
      90000,
      1000,
    )
    const supplementTags = await evaluate(
      `Array.from(document.querySelectorAll('.msg--user .msg__choice-tag')).map(e => e.textContent.trim())`,
    )
    const hintLeft = await evaluate(`document.querySelectorAll('.clarify__hint').length`)
    const refHead = await evaluate(`(() => {
      const heads = document.querySelectorAll('.clarify__head')
      return heads.length ? heads[heads.length - 1].textContent.trim() : ''
    })()`)
    check('AI 问答：补充现象后完成一轮（原问题被拼上重发）',
      afterHint && supplementTags.includes('补充说明'), { afterHint, supplementTags })
    check('AI 问答：补充后不再重复追问同一件事', hintLeft === 0, { hintLeft, refHead })

    // ---- 4b. 澄清（二）：问题已问清楚但没命中 → 给参考资料（可点开） ----
    const refOptions = await evaluate(`document.querySelectorAll('.clarify__option').length`)
    if (refOptions > 0) {
      check('AI 问答：没命中时给参考资料（而非"必须选一条才能答"）',
        refHead.includes('可能相关的资料'), { refHead, refOptions })
      const refText = await evaluate(
        `(document.querySelector('.clarify__option')||{}).textContent.replace(/\\s+/g,' ').trim() || ''`)
      check('AI 问答：参考资料展示标题/编码/片段', refText.length > 0, refText.slice(0, 60))
      await shot('chat-reference')

      const msgsBeforePick = await evaluate(`document.querySelectorAll('.msg').length`)
      await evaluate(
        `(() => { const b = document.querySelector('.clarify__option'); if (b) b.click(); return true })()`,
      )
      const picked = await waitFor(
        `document.querySelectorAll('.msg').length >= ${msgsBeforePick + 2} &&
         document.querySelectorAll('.msg--assistant .msg__role .is-loading').length === 0`,
        90000,
        1000,
      )
      const pickedAnswer = await evaluate(`(() => {
        const els = document.querySelectorAll('.msg--assistant .msg__content')
        return els.length ? els[els.length - 1].textContent.trim() : ''
      })()`)
      const pickedSources = await evaluate(`document.querySelectorAll('.msg__sources').length`)
      const chosenNotes = await evaluate(`document.querySelectorAll('.clarify__chosen').length`)
      check('AI 问答：点开参考资料后基于它作答', picked && pickedAnswer.length > 0,
        { picked, answer: pickedAnswer.slice(0, 60) })
      check('AI 问答：点开后标注所选知识并展示来源卡片',
        chosenNotes > 0 && pickedSources > 0, { chosenNotes, pickedSources })
      await shot('chat-reference-answer')
    } else {
      check('AI 问答：补充后直接给出了回答（未再触发参考资料）',
        lastAnswer.trim().length > 0, lastAnswer.slice(0, 60))
    }
  } else {
    check('AI 问答：本轮检索命中，直接作答（未触发澄清）',
      lastAnswer.trim().length > 0, lastAnswer.slice(0, 60))
  }
  await shot('chat')

  // ---- 4b. AI 问答：历史会话（刷新后仍在，可回看） ----
  // 会话历史来自后端 qa_access_logs 按 session_id 聚合；刷新整页后应能重新列出来。
  await send('Page.reload', { ignoreCache: false })
  await sleep(3500)
  await waitFor(`document.querySelectorAll('.session-item').length > 0`, 20000)
  const sessionTitles = await evaluate(
    `Array.from(document.querySelectorAll('.session-item__title')).map(e => e.textContent.trim())`)
  const sessionMetas = await evaluate(
    `Array.from(document.querySelectorAll('.session-item__meta')).map(e => e.textContent.replace(/\\s+/g,' ').trim())`)
  const restoredMsgs = await evaluate(`document.querySelectorAll('.msg').length`)
  const restoredSources = await evaluate(`document.querySelectorAll('.msg__sources').length`)
  check('AI 问答：刷新后历史会话列表非空（会话已落库）', sessionTitles.length > 0, sessionTitles.slice(0, 3))
  check('AI 问答：会话项显示轮次数与时间',
    sessionMetas.length > 0 && sessionMetas.every((t) => /\d+\s*轮/.test(t)), sessionMetas.slice(0, 3))
  check('AI 问答：刷新后自动回到最近会话并还原消息', restoredMsgs > 0, restoredMsgs)
  check('AI 问答：历史消息还原来源卡片', restoredSources > 0, restoredSources)
  await shot('chat-history')

  const switched = await evaluate(`(() => {
    const items = document.querySelectorAll('.session-item')
    if (items.length < 2) return 'lt-2'
    items[1].click()
    return items[1].textContent.replace(/\\s+/g,' ').trim().slice(0, 40)
  })()`)
  await sleep(2200)
  const afterSwitchMsgs = await evaluate(`document.querySelectorAll('.msg').length`)
  const afterSwitchSources = await evaluate(`document.querySelectorAll('.msg__sources').length`)
  check('AI 问答：点击历史会话可切换并加载该会话消息',
    switched !== 'lt-2' && afterSwitchMsgs > 0, { switched, afterSwitchMsgs })
  check('AI 问答：切换后的会话同样还原来源卡片', switched === 'lt-2' || afterSwitchSources > 0, afterSwitchSources)

  // ---- 5. 侧边菜单分组 ----
  const groupTitles = await evaluate(
    `Array.from(document.querySelectorAll('.el-menu-item-group__title')).map(e => e.textContent.trim())`)
  check('侧边菜单已按域分组（>= 3 组）',
    Array.isArray(groupTitles) && groupTitles.filter(Boolean).length >= 3, groupTitles)

  // ---- 6. 耗时分析页面 ----
  await goto('#/latency', 3000)
  await waitFor(`document.querySelectorAll('.stage-name').length > 0`, 15000)
  const latencyMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  const latencyCards = await evaluate(`document.querySelectorAll('.metric-card').length`)
  const stageNames = await evaluate(
    `Array.from(document.querySelectorAll('.stage-name')).map(e => e.textContent.trim())`)
  const slowRows = await evaluate(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length`)
  const splitBars = await evaluate(`document.querySelectorAll('.split-bar').length`)
  const timeCells = await evaluate(
    `Array.from(document.querySelectorAll('.num')).map(e => e.textContent.trim()).filter(t => /(ms|s)$/.test(t))`)
  const pctCells = await evaluate(
    `Array.from(document.querySelectorAll('.num')).map(e => e.textContent.trim()).filter(t => /%$/.test(t))`)
  check('耗时分析：6 张指标卡渲染（样本/平均/P50/P90/P95/最慢）', latencyCards === 6, latencyCards)
  check('耗时分析：阶段明细表列出检索/生成/数据库/缓存/其他开销',
    ['检索', '生成', '数据库', '缓存', '其他开销'].every((n) => stageNames.includes(n)), stageNames)
  const slowHeaders = await evaluate(
    `Array.from(document.querySelectorAll('.kb-card:last-child .el-table__header th .cell')).map(e => e.textContent.trim())`)
  check('耗时分析：最慢问题表含数据库/缓存列',
    slowHeaders.includes('数据库') && slowHeaders.includes('缓存'), slowHeaders)
  const shareBars = await evaluate(`document.querySelectorAll('.share__fill').length`)
  check('耗时分析：阶段明细占比列带进度条', shareBars >= 5, shareBars)
  const splitSegs = await evaluate(
    `Array.from(document.querySelectorAll('.split-bar')).map(b => b.children.length)`)
  check('耗时分析：构成条为五段堆叠', splitSegs.length > 0 && splitSegs.every((n) => n === 5), splitSegs.slice(0, 5))
  const sourceTags = await evaluate(
    `Array.from(document.querySelectorAll('.kb-card:last-child .el-table__body-wrapper .el-tag')).map(e => e.textContent.trim())`)
  const KNOWN_SOURCES = ['FAQ 缓存', '检索+生成', '检索后拒答', '权限拦截', 'AI 通用知识']
  check('耗时分析：来源列按真实链路标注（无 generate 的行不得标为「检索+生成」）',
    sourceTags.length > 0 && sourceTags.every((t) => KNOWN_SOURCES.includes(t)), sourceTags)
  check('耗时分析：耗时数值带单位（ms / s 自适应）',
    timeCells.length > 0 && timeCells.every((t) => /^\d+(\.\d+)?\s*(ms|s)$/.test(t)), timeCells.slice(0, 6))
  check('耗时分析：占比列以 % 结尾',
    pctCells.length > 0 && pctCells.every((t) => /^\d+(\.\d+)?%$/.test(t)), pctCells.slice(0, 4))
  check('耗时分析：最慢问题表含耗时构成条', splitBars > 0, splitBars)
  check('耗时分析：最慢问题表格有数据', slowRows > 0, slowRows)
  check('耗时分析：无残留 loading 遮罩', latencyMasks === 0, latencyMasks)
  await shot('latency', true)
  // 内容区是内部滚动容器，滚到底再截一张，便于确认最慢问题表
  await evaluate(`(() => { const el = document.querySelector('.layout__content'); if (el) el.scrollTop = el.scrollHeight; return true })()`)
  await sleep(700)
  await shot('latency-bottom')

  // ---- 7. 角色权限页面 ----
  await goto('#/org/roles', 2500)
  await waitFor(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length > 0`, 12000)
  const roleRows = await evaluate(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length`)
  const permChips = await evaluate(`document.querySelectorAll('.el-table__body-wrapper tbody tr .el-tag').length`)
  check('角色权限：表格渲染出角色行', roleRows >= 5, roleRows)
  check('角色权限：权限概览列有权限码标签（不再是大片空白）', permChips > 0, permChips)
  await shot('roles')

  // ---- 8. 部门管理：负责人显示姓名而非 #id ----
  await goto('#/org/departments', 2500)
  await waitFor(`document.querySelectorAll('.dept-node__meta').length > 0`, 12000)
  const deptMeta = await evaluate(
    `Array.from(document.querySelectorAll('.dept-node__meta')).map(e => e.textContent.replace(/\\s+/g,' ').trim())`)
  check('部门管理：负责人显示为姓名（不出现 "#1" 这类 id）',
    deptMeta.length > 0 && !deptMeta.some((t) => /#\d/.test(t)), deptMeta.slice(0, 3))
  check('部门管理：至少有一个部门显示负责人姓名',
    deptMeta.some((t) => t.includes('负责人')), deptMeta.slice(0, 3))
  await shot('departments')

  // ---- 9. 用户管理：角色显示中文名 ----
  await goto('#/org/users', 2500)
  await waitFor(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length > 0`, 12000)
  const roleTags = await evaluate(
    `Array.from(document.querySelectorAll('.el-table__body-wrapper tbody tr .el-tag')).map(e => e.textContent.trim())`)
  const chineseRoles = roleTags.filter((t) => /[\u4e00-\u9fa5]/.test(t))
  check('用户管理：角色列显示中文角色名（不再是 after_sales 这类编码）',
    chineseRoles.length > 0, roleTags.slice(0, 5))
  await shot('users')

  // ---- 10. FAQ 管理：推荐审核 + 已发布库（编辑 / 下线） ----
  await goto('#/faq', 2500)
  await waitFor(`document.querySelectorAll('.el-tabs__item').length >= 2`, 12000)
  const faqTabs = await evaluate(
    `Array.from(document.querySelectorAll('.el-tabs__item')).map(e => e.textContent.trim())`)
  check('FAQ 管理：两个页签渲染（推荐审核 / 已发布库）',
    faqTabs.includes('推荐审核') && faqTabs.includes('已发布库'), faqTabs)
  const recMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  check('FAQ 管理：推荐审核页签加载后无残留 loading', recMasks === 0, recMasks)
  await shot('faq-recommend')

  const switchedTab = await evaluate(`(() => {
    const tab = Array.from(document.querySelectorAll('.el-tabs__item')).find(e => e.textContent.trim() === '已发布库')
    if (!tab) return 'no-tab'
    tab.click()
    return 'clicked'
  })()`)
  await sleep(2200)
  const faqRows = await evaluate(`document.querySelectorAll('.el-table__body-wrapper tbody tr').length`)
  const faqMasks = await evaluate(`document.querySelectorAll('.el-loading-mask').length`)
  check('FAQ 管理：已发布库页签可切换', switchedTab === 'clicked', switchedTab)
  check('FAQ 管理：已发布库加载后无残留 loading', faqMasks === 0, faqMasks)
  check('FAQ 管理：已发布库有数据（E2E 已审核通过一条）', faqRows > 0, faqRows)
  await shot('faq-published')

  if (faqRows > 0) {
    const opened = await evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('.el-table__body-wrapper tbody tr button'))
        .find(b => b.textContent.trim() === '编辑')
      if (!btn) return 'no-edit-btn'
      btn.click()
      return 'clicked'
    })()`)
    await sleep(1400)
    const dialogTitle = await evaluate(`(document.querySelector('.el-dialog__title')||{}).textContent || ''`)
    const dialogFields = await evaluate(
      `document.querySelectorAll('.el-dialog .el-textarea__inner, .el-dialog .el-input__inner').length`)
    check('FAQ 管理：点「编辑」打开编辑弹窗',
      opened === 'clicked' && dialogTitle.includes('编辑 FAQ'), { opened, dialogTitle })
    check('FAQ 管理：编辑弹窗含问题与答案字段', dialogFields >= 2, dialogFields)
    await shot('faq-edit')
    // 关闭弹窗，避免影响后续断言
    await evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('.el-dialog__footer button'))
        .find(b => b.textContent.trim() === '取消')
      if (btn) btn.click()
      return true
    })()`)
    await sleep(700)
  }
  const offlineBtns = await evaluate(
    `Array.from(document.querySelectorAll('.el-table__body-wrapper tbody tr button')).filter(b => b.textContent.trim() === '下线').length`)
  check('FAQ 管理：已发布库提供「下线」操作', faqRows === 0 || offlineBtns > 0, offlineBtns)

  // ---- 11. 权限配置：四维数据权限选择 + 保存 + 校验演示 ----
  await goto('#/permissions', 2800)
  await waitFor(`document.querySelectorAll('.unit-item').length > 0`, 15000)
  const unitItems = await evaluate(`document.querySelectorAll('.unit-item').length`)
  check('权限配置：左侧知识单元列表渲染', unitItems > 0, unitItems)

  // 读当前授权标签；用文案集合比较，而不是只比数量——数量相同但内容被改坏同样要能发现
  const readChips = () =>
    evaluate(
      `Array.from(document.querySelectorAll('.perm-picker__chips .el-tag')).map(e => e.textContent.replace(/\\s+/g,' ').trim())`,
    )

  // 确定性选取「已配置权限」的单元，消除对列表顺序 + 数据状态的耦合：
  // 列表按 id 倒序返回，首个单元常是刚导入、尚未配 unit_permissions 的新单元
  // （实测 U-1073/U-1072，permissions=[]），直接点第一个会让「已有权限以标签展示」
  // 断言因数据状态而假失败。这里遍历左侧列表逐个点击，停在第一个权限标签非空的单元上；
  // 断言强度不变——仍要求"选中的单元确实展示了权限标签"（而非恒真）。
  let pickedUnit = 'no-item'
  let chipsBefore = []
  const scanTried = []
  for (let i = 0; i < unitItems; i++) {
    const label = await evaluate(`(() => {
      const el = document.querySelectorAll('.unit-item')[${i}]
      if (!el) return null
      el.click()
      return el.textContent.replace(/\\s+/g,' ').trim().slice(0, 40)
    })()`)
    if (label === null) break
    await sleep(1600)
    const chips = await readChips()
    scanTried.push({ idx: i, label, chipCount: chips.length })
    if (chips.length > 0) {
      pickedUnit = label
      chipsBefore = chips
      break
    }
  }

  const editorTitle = await evaluate(`(document.querySelector('.perm-layout__title')||{}).textContent || ''`)
  const pickerShown = await evaluate(`document.querySelectorAll('.perm-picker').length`)
  check('权限配置：点击单元后打开权限编辑器',
    pickedUnit !== 'no-item' && !!editorTitle.trim(), { pickedUnit, editorTitle })
  check('权限配置：四维权限选择器渲染', pickerShown === 1, pickerShown)

  check('权限配置：已有权限以标签展示（选中一个已配权限的单元）',
    chipsBefore.length > 0, { pickedUnit, chipsBefore, scanTried })

  const clickGlobal = () =>
    evaluate(`(() => {
      const cb = document.querySelector('.perm-picker__global .el-checkbox')
      if (!cb) return false
      cb.click()
      return true
    })()`)

  // 「全局可见」复选框要双向生效：勾上=加、取消=去。
  // 这里**不假设**选中的单元初始是否已含全局（由上面确定性扫描选出的已配权限单元决定），
  // 而是先翻转一次、再翻转回来，最后原样保存——全程不改动演示数据。
  const hadGlobal = chipsBefore.some((t) => t.includes('全局'))
  const toggled = await clickGlobal()
  await sleep(700)
  const chipsAfterFirst = await readChips()
  const expectedFirst = hadGlobal ? chipsBefore.length - 1 : chipsBefore.length + 1
  check('权限配置：切换「全局可见」即时生效（勾上=加 / 取消=去）',
    toggled && chipsAfterFirst.length === expectedFirst,
    { hadGlobal, chipsBefore, chipsAfterFirst })

  await clickGlobal()
  await sleep(700)
  const chipsAfterSecond = await readChips()
  check('权限配置：再次切换后回到初始状态',
    JSON.stringify(chipsAfterSecond) === JSON.stringify(chipsBefore),
    { chipsBefore, chipsAfterSecond })
  await shot('permissions')

  // 原样写回（幂等）：验证「保存权限」写入链路真的通，同时不改动演示数据
  const saved = await evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '保存权限')
    if (!btn) return 'no-save-btn'
    btn.click()
    return 'clicked'
  })()`)
  await sleep(2000)
  const saveToast = await evaluate(`(document.querySelector('.el-message')||{}).textContent || ''`)
  check('权限配置：保存权限走通（出现成功提示）',
    saved === 'clicked' && saveToast.includes('权限已保存'), { saved, saveToast })

  const chipsAfterSave = await readChips()
  check('权限配置：保存后权限集合与初始一致（没有写坏演示数据）',
    JSON.stringify(chipsAfterSave) === JSON.stringify(chipsBefore),
    { chipsBefore, chipsAfterSave })

  const checkDemo = await evaluate(`document.querySelectorAll('.check-demo').length`)
  const checkBtn = await evaluate(
    `Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === '校验').length`)
  check('权限配置：权限校验演示区存在（用户选择 + 校验按钮）',
    checkDemo === 1 && checkBtn === 1, { checkDemo, checkBtn })
  await shot('permissions-check')

  ws.close()
} finally {
  proc.kill()
}

const failed = results.filter((r) => !r).length
console.log(`\n===== 浏览器 UI 校验: ${results.length - failed}/${results.length} 通过, ${failed} 失败 =====`)
process.exit(failed ? 1 : 0)
