/**
 * VideoSummary.vue 真挂载（工单 #19 第 2 项）。
 *
 * ## 这个文件**不迁移任何既有用例**，只补行为侧的真空
 *
 * 逐条判定 6 个文件里涉及 VideoSummary.vue 的 19 条断言，结论是 **19 条全是源码文本
 * 断言，一条都不该迁**——判据对象就是源码本身（「模板里不许出现裸 `@click="startSummarize"`」
 * 「`quotaLabel` 必须作为插值出现」），挂载后看不见模板源码，迁过去会退化成
 * 「界面上没出现那串字」。所以本文件是**纯新增**。
 *
 * ## 变异探针说 VideoSummary「6/6 失明」，逐条复核后是 3/6 —— 这个差值本身是结论
 *
 * 探针报 6 条变异全部 SURVIVED。复核发现其中 3 条**压根不是缺陷**，为它们写断言会
 * 写出结构上无法失败的测试：
 *
 * | 变异 | 复核结论 | 依据 |
 * |---|---|---|
 * | V1 `started` 守卫 | **不可达** | `App.vue:69-82` 的 `<VideoSummary>` 没有 `ref`，父组件拿不到 `startSummarize`；按钮 `:71` 的 `v-else-if="!started"` 在点完就整体卸载；而 watch `:506` 在调 `startSummarize` 前**自己**把 `started` 重置成 false，守卫拦不到它 |
 * | V5 `sanitizeMindmap` 空兜底 `:418` | **语义空操作** | 探针实测 11 类输入（空串 / 空格 / 换行 / Tab / null / undefined / 0 / 正常 md / 无标题 / 仅 H2 / 带围栏）**输出逐字符相同**：`:418` 的兜底串与 `:448` 的 `items ? ... :` 兜底串是同一句 |
 * | V6 `applyChatHistory` 空问题守卫 | **不可达** | `chatHistoryList` 只有三个来源：服务端 `fetchChatSession`、`initialHistory.chat_history`、以及 `handleChat` 里 `push({question, …})`——那一处的 `question` 已被 `:644` 的 `.trim()` 且非空校验过。要触发它得先有一条服务端返回空问题的畸形记录 |
 *
 * 剩下 V2 / V3 / V4 是**真实且用户可达**的，本文件守它们。
 *
 * ## V2 必须从回车路径验，不能从按钮验
 *
 * 提问按钮 `:206` 有 `:disabled="chatLoading || !chatQuestion.trim()"`，按钮点第二次
 * 本来就被禁用。而输入框 `:204` 的 `@keyup.enter="handleChat"` **没有任何禁用门**——
 * 所以「追问进行中再按一次回车」是用户真的能走出来的路径，`chatStream` 守卫
 * （`:644`）是这条路上唯一的拦截点。删掉它就是两个并发流、两次扣额度。
 *
 * ## 每条守卫都配一条对照组
 *
 * 只写「不该发生」的那一半会恒真：本文件对 V2 / V3 / V4 各配一条「确实该发生」的
 * 对照（结束后能再问、有总结会写、未登录之外会发），确保断言在能区分两种实现的状态上。
 *
 * ## 判据只走「用户看到什么」与「后端收到什么」
 *
 * `<script setup>` 不暴露内部状态，DOM 是唯一出口。本文件不 import 也不调用
 * `started` / `chatStream` / `applyQuotaEvent`——那些是实现细节。
 * 唯一的例外是「点完之后后端收到什么」：那本来就是接口契约，从 mock 的调用记录读。
 *
 * ## 思维导图那一节（工单 #111 / ADR 0029）
 *
 * 导图的结构规则只有一份：`backend/mindmap.py`。前端**不再自己洗一遍**——
 * 洗一遍就会有一处规则在改规则时没被一起改。本节盯住这件事：后端交来的
 * markdown 原样渲染，第三级节点不许在前端这一层消失。
 */
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// vi.mock 必须在顶部：它靠「被提升到组件 import 之前」生效。下沉到共用夹具就依赖导入
// 顺序，失效是静默的——组件真去发 axios 请求，jsdom 没有 XHR，形态是「挂载即炸」。
vi.mock('../src/api/summarize.ts', () => ({
  summarizeVideo: vi.fn(),
  chatWithVideo: vi.fn(),
  fetchQuota: vi.fn(),
  // 工单 #105 新增的清单端点。本文件不验模板选择，给一份空清单，
  // 让新增的下拉框不渲染——这个文件的判据与模板无关，DOM 得保持原样。
  fetchSummaryTemplates: vi.fn(),
}))
vi.mock('../src/api/history.ts', () => ({
  fetchChatSession: vi.fn(),
  saveHistory: vi.fn(),
}))
// 真实现读 localStorage；这里钉成固定值，让「凭据有没有跟着走」在任何环境下同形。
vi.mock('../src/lib/byok.ts', () => ({
  getRequestCredential: vi.fn(() => ({ provider: 'openai' })),
}))

import VideoSummary from '../src/components/VideoSummary.vue'
import { summarizeVideo, chatWithVideo, fetchQuota, fetchSummaryTemplates } from '../src/api/summarize.ts'
import { fetchChatSession, saveHistory } from '../src/api/history.ts'

// ── 造数据 ─────────────────────────────────────────────────

const VIDEO_URL = 'https://youtu.be/dQw4w9WgXcQ'

/**
 * `api/summarize.ts` 的 `streamSse` 返回 `{ done, cancel }`：`done` 是结束的 Promise，
 * `cancel` 是用户主动停止。这里照抄那个契约——事件什么时候发由测试自己点，
 * 而不是像既有 text 测试那样 `enqueue` 一次性灌完。
 */
function makeStream() {
  let finish
  const done = new Promise((res) => {
    finish = res
  })
  return {
    handle: { done, cancel: vi.fn() },
    finish: () => finish({ done: true, value: undefined }),
  }
}

let summarizeCb
let chatCb
let summarizeStream
let chatStream

/**
 * 挂载并等 watch 的 `immediate` 跑完。
 *
 * `hasCommunityResult: true` 时 `watch(() => props.videoUrl, …, {immediate: true})`
 * 会自动发起 `startSummarize(false)`，于是 `started` 变真、组件进入总结 Tab——
 * 这正是「从社区页点进来直接看到内容」的真实路径，也省掉了手动点开始按钮。
 */
async function mountSummary(props = {}) {
  summarizeStream = makeStream()
  chatStream = makeStream()
  summarizeVideo.mockImplementation((_url, _lang, cb) => {
    summarizeCb = cb
    return summarizeStream.handle
  })
  chatWithVideo.mockImplementation((_url, _q, cb) => {
    chatCb = cb
    return chatStream.handle
  })
  fetchQuota.mockResolvedValue({
    logged_in: true,
    labels: { parse: '解析', chat: '追问' },
    parse: { remaining: 3, limit: 3 },
    chat: { remaining: 10, limit: 10 },
  })
  fetchSummaryTemplates.mockResolvedValue({ default: 'full', templates: [] })
  fetchChatSession.mockResolvedValue([])

  const w = mount(VideoSummary, {
    props: { videoUrl: VIDEO_URL, user: { id: 1 }, ...props },
  })
  await flushPromises()
  return w
}

/** Tab 栏按钮的文本就是 label（图标没文字，激活态那条下划线 div 也没文字）。 */
async function openTab(w, label) {
  const btn = w.findAll('button').find((b) => b.text().includes(label))
  expect(btn, `找不到 Tab「${label}」`).toBeTruthy()
  await btn.trigger('click')
  await flushPromises()
  return btn
}

/** 全组件只有一个 `<input>`（`:200` 的追问框），所以不必按位置找。 */
const chatInput = (w) => w.find('input[type="text"]')

beforeEach(() => {
  // reset 而不是 clear：clear 只清调用历史、不清实现，会让漏设前提的用例默默继承上一条
  // 的种子，然后断言了另一个场景还照样绿。每条用例要的实现都在 mountSummary 里现设。
  vi.resetAllMocks()
})

// ── 基础通路 ───────────────────────────────────────────────

describe('VideoSummary 挂载 · 总结渲染', () => {
  test('流式 token 累积后渲染成 HTML', async () => {
    const w = await mountSummary({ hasCommunityResult: true })

    summarizeCb.onSummary('# 第一章\n\n这是**加粗**正文。')
    summarizeCb.onSummary('第二段。')
    await flushPromises()

    const html = w.html()
    // marked 真跑：换行成 <p>，** 加粗成 <strong>
    expect(html).toContain('<h1')
    expect(html).toContain('<strong>加粗</strong>')
    expect(html).toContain('第二段。')
  })
})

// ── V2：重复追问 ───────────────────────────────────────────

describe('VideoSummary 挂载 · 追问不重复发', () => {
  test('追问流还开着时按第二次回车，不会再发一次请求', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    await openTab(w, 'AI 问答')

    const input = chatInput(w)
    await input.setValue('这个视频讲了什么')
    await input.trigger('keyup.enter')
    await flushPromises()
    expect(chatWithVideo, '第一次回车必须真的发出去了').toHaveBeenCalledTimes(1)

    // 流还开着（没有 finish），再按一次回车——这是用户真能走出来的路径：
    // 输入框 `@keyup.enter` 没有任何禁用门，拦不住的。
    await input.trigger('keyup.enter')
    await flushPromises()
    expect(chatWithVideo, '追问进行中按第二次回车，多发了一条').toHaveBeenCalledTimes(1)
  })

  test('对照组：上一条追问结束后，再问一次会真的发第二次请求', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    await openTab(w, 'AI 问答')

    const input = chatInput(w)
    await input.setValue('第一个问题')
    await input.trigger('keyup.enter')
    await flushPromises()
    chatCb.onDone()
    await flushPromises()

    await input.setValue('第二个问题')
    await input.trigger('keyup.enter')
    await flushPromises()

    // 没有这条，上面那条会恒真：守卫和「追问压根发不出去」对它同形。
    expect(chatWithVideo).toHaveBeenCalledTimes(2)
    expect(chatWithVideo.mock.calls[1][1]).toBe('第二个问题')
  })
})

// ── V3：空结果不写历史 ─────────────────────────────────────

describe('VideoSummary 挂载 · 空结果不写历史', () => {
  test('流结束时既没有总结也没有导图，不往解析历史塞空记录', async () => {
    await mountSummary({ hasCommunityResult: true })

    summarizeCb.onDone()
    await flushPromises()

    expect(saveHistory, '失败/空结果也写了历史，刷新后是一次凭空消失的解析').not.toHaveBeenCalled()
  })

  test('对照组：有总结的流会写一条历史', async () => {
    await mountSummary({ hasCommunityResult: true })

    summarizeCb.onSummary('一段总结')
    summarizeCb.onDone()
    await flushPromises()

    // 没有这条，上面那条会恒真：守卫和「persistHistory 压根不工作」对它同形。
    expect(saveHistory).toHaveBeenCalledTimes(1)
    expect(saveHistory.mock.calls[0][0].summary_md).toBe('一段总结')
  })
})

// ── V4：访客不回填问答历史 ─────────────────────────────────

describe('VideoSummary 挂载 · 访客不回填问答历史', () => {
  test('未登录访客不发会话回填请求', async () => {
    await mountSummary({ hasCommunityResult: true, user: null })

    summarizeCb.onDone()
    await flushPromises()

    // 服务端会 401，白等一趟；而且这条请求带的就是用户的视频链接。
    expect(fetchChatSession).not.toHaveBeenCalled()
  })

  test('对照组：登录用户会发会话回填请求', async () => {
    await mountSummary({ hasCommunityResult: true, user: { id: 7 } })

    summarizeCb.onDone()
    await flushPromises()

    expect(fetchChatSession).toHaveBeenCalledTimes(1)
    expect(fetchChatSession.mock.calls[0][0]).toBe(VIDEO_URL)
  })
})

// ── 行为侧的加强：overwrite 与额度 ─────────────────────────

describe('VideoSummary 挂载 · 接口契约', () => {
  test('点「开始 AI 解析」发出的是新增而不是覆盖', async () => {
    const w = await mountSummary({ hasCommunityResult: false })

    const btn = w.findAll('button').find((b) => b.text().includes('开始 AI 解析'))
    expect(btn, '没有 hasCommunityResult 时应停在待启动区块').toBeTruthy()
    await btn.trigger('click')

    expect(summarizeVideo).toHaveBeenCalledTimes(1)
    const options = summarizeVideo.mock.calls[0][3]
    // 这一条是 `reparse-owner.test.mjs:237`（文本：模板里不许出现裸 `@click="startSummarize"`）
    // 的行为对应物：写回裸引用会把 MouseEvent 当 overwrite 传进来，于是「开始」变成「覆盖」。
    expect(options.overwrite).toBe(false)
  })

  test('额度拆分后两个计数器同时显示', async () => {
    const w = await mountSummary({ hasCommunityResult: true })

    summarizeCb.onQuota({
      remaining: 1,
      limit: 3,
      labels: { parse: '解析', chat: '追问' },
      parse: { remaining: 2, limit: 3 },
      chat: { remaining: 7, limit: 10 },
    })
    await flushPromises()

    const badge = w.html()
    // `quota.test.mjs:159`（文本：`{{ quotaLabel }}` 必须是插值）挡不住 quotaLabel 被换成
    // 另一个仍然存在的插值；这条断言的是用户真的同时看到两个数字。
    expect(badge).toContain('解析 2 / 3')
    expect(badge).toContain('追问 7 / 10')
  })
})

// ── 开关的契约默认值 ──────────────────────────────────────

/**
 * `hasCommunityResult` / `regenerateRequested` 决定**用户会不会白扣额度**：
 * 为真时 watch 的 immediate 自动发起，为假时保持手动触发。
 *
 * ## 这条测试守的是什么（先说清，免得后人误读成「生产里存在这条路径」）
 *
 * **当前唯一调用方 `App.vue:69` 总是显式传值**（`:hasCommunityResult="fromCache"`），
 * 所以「不传」这个形状今天在生产里走不到。它守的是**契约默认值**：
 * 把 `default: false` 改成 `true` 看起来是无害的改动（毕竟没有谁在显式传 false），
 * 但未来任何一个新增的调用方省略这个 prop 时就会自动发起、白扣额度，
 * 而今天**没有任何一条测试会响**。
 *
 * 既有那条「点开始 AI 解析」用的是 `mountSummary({ hasCommunityResult: false })` ——
 * 显式传 false，`default` 根本没参与，所以它挡不住这类改动。这两处很容易被当成同一件事。
 *
 * ## 与本文件头部那三条「不可达」的区别
 *
 * V1 / V5 / V6 是**组件内部**的死代码，断言对象在产品里永远不会被执行。
 * prop 默认值是**对外契约**的一部分，取决于谁调用、有没有传值 ——
 * 所以为它写断言是前瞻性的，不是形式的。这个区别是本文件成立的前提。
 *
 * ## 对照不可省
 *
 * 只写「不传就不发起」的话，把 watch 的 `immediate` 摘掉（功能整体坏掉）
 * 这条照样绿。下面两条必须成对读。
 */
describe('VideoSummary 挂载 · 开关的契约默认值', () => {
  test('两个开关都不传时不自动发起（守住 default: false）', async () => {
    await mountSummary()
    expect(summarizeVideo, '两个开关都没传却自动发起了 —— 用户什么都没点就扣额度').not.toHaveBeenCalled()
  })

  test('对照：传 hasCommunityResult: true 时确实自动发起（immediate 在跑）', async () => {
    await mountSummary({ hasCommunityResult: true })
    expect(
      summarizeVideo,
      '对照不成立 —— watch 的 immediate 没在跑，下面那条「不传就不发起」就成了恒真',
    ).toHaveBeenCalled()
  })

  test('对照：regenerateRequested 单独为真也自动发起（|| 的另一半）', async () => {
    await mountSummary({ regenerateRequested: true })
    expect(summarizeVideo, 'regenerateRequested 这一支没生效 —— || 的后半段没人守').toHaveBeenCalled()
  })
})

// ── byok 额度提示的三条互斥分支 ─────────────────────────────

/**
 * 界面说的「用谁的额度」与**请求里实际带不带 key** 必须是同一个决定。
 * 工单 #26 把 mode 变成真开关之后这一点尤其要紧：界面上写着「用平台额度」
 * 而请求里带着用户自己的 key（或者反过来），用户没有任何线索能发现。
 *
 * ## 三条分支的依据不同，判别力也不同
 *
 * | 分支 | 依据 | 怎么验 |
 * |---|---|---|
 * | `v-if="byokNotice"` | **服务端回报**（SSE quota 事件里的 `byok: true`） | 必须真发一次事件 |
 * | `v-else-if` 还没填 | prop（前端自己的状态） | 直接喂不同 prop |
 * | `v-else-if` 已填 | prop | 直接喂不同 prop |
 *
 * 只测后两条的话，「服务端说这次用了自己的 key 时界面要跟着说」这条完全没人守 ——
 * 而它恰恰是三条里**唯一不由前端推断**的那条。
 *
 * 此前本文件把 `lib/byok.ts` mock 掉了，但 byok 提示读的是 **prop** 不是那个模块，
 * 所以这里不需要动 mock。
 */
describe('VideoSummary 挂载 · byok 额度提示', () => {
  const byokProp = (over = {}) => ({
    mode: 'byok',
    hasKey: true,
    baseUrl: '',
    model: '',
    ...over,
  })

  test('不传 byok prop 时三条分支都不渲染', async () => {
    const w = await mountSummary()
    const html = w.html()
    expect(html).not.toContain('用了你自己的 API Key')
    expect(html).not.toContain('但还没填')
    expect(html).not.toContain('当前使用你自己的 API Key')
  })

  test('选了自带但没填：说清「仍会走平台额度」，并给一个去填写的入口', async () => {
    const w = await mountSummary({ hasCommunityResult: true, byok: byokProp({ hasKey: false }) })
    expect(w.html()).toContain('你选了「使用自己的 API Key」但还没填')
    // 这句话是 #26 修复之后仍然成立的那一版：mode 是 byok 但没 key 时，
    // getRequestCredential() 在第一个 guard（!state.apiKey）就返回 null。
    // 万一将来把 mode 的判断挪到 api 层、而这一句没跟着改，用户就被骗了。
    expect(w.html(), '没告诉用户实际会走平台额度 —— 他会以为在用自己的 key').toContain('仍会走平台额度')
    const btn = w.findAll('button').find((b) => b.text().includes('去填写'))
    expect(btn, '没有「去填写」入口 —— 用户只能自己去顶栏找那个图标').toBeTruthy()
    await btn.trigger('click')
    expect(w.emitted('open-byok'), '点了「去填写」却没有 emit open-byok —— 弹窗不会开').toBeTruthy()
  })

  test('选了自带且已填：显示当前模型名', async () => {
    const w = await mountSummary({
      hasCommunityResult: true,
      byok: byokProp({ model: 'deepseek-chat' }),
    })
    expect(w.html()).toContain('当前使用你自己的 API Key')
    expect(w.html(), '没显示当前模型名 —— 用户确认不了自己在用哪个').toContain('deepseek-chat')
  })

  test('服务端回报 byok=true 时才显示「本次用了自己的 key」，且余额原样留着', async () => {
    // 前端不推断「这次有没有用自己的 key」—— 它是服务端在 quota 事件里回报的
    // （applyQuotaEvent 的 d?.byok 分支）。所以判据必须真的发一次事件。
    const w = await mountSummary({ hasCommunityResult: true })
    expect(w.html(), '还没发任何事件就先显示了').not.toContain('用了你自己的 API Key')

    // 先给一条正常额度，让「余额」有个可见的基线
    summarizeCb.onQuota({
      remaining: 2,
      limit: 3,
      labels: { parse: '解析', chat: '追问' },
      parse: { remaining: 1, limit: 3 },
      chat: { remaining: 9, limit: 10 },
    })
    await flushPromises()
    expect(w.html()).toContain('解析 1 / 3')

    // byok 事件**没有余额可报**，所以它只该记一条提示、不该动余额
    summarizeCb.onQuota({ byok: true })
    await flushPromises()
    expect(w.html()).toContain('本次解析用了你自己的 API Key')
    expect(w.html(), 'byok 事件把余额抹掉了 —— 用户看不到自己还剩多少').toContain('解析 1 / 3')

    // 后续再来的正常额度仍然照常更新
    summarizeCb.onQuota({
      remaining: 1,
      limit: 3,
      labels: { parse: '解析', chat: '追问' },
      parse: { remaining: 0, limit: 3 },
      chat: { remaining: 9, limit: 10 },
    })
    await flushPromises()
    expect(w.html(), 'byok 之后余额再也不更新了').toContain('解析 已用完（0 / 3）')
  })

  test('追问那次说的是「本次追问」，不是「本次解析」', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    summarizeCb.onSummary('总结好了')
    await flushPromises()

    await openTab(w, 'AI 问答')
    await chatInput(w).setValue('问题')
    await chatInput(w).trigger('keyup.enter')
    await flushPromises()
    chatCb.onQuota({ byok: true })
    await flushPromises()

    expect(w.html()).toContain('本次追问用了你自己的 API Key')
    expect(w.html(), '说成了「本次解析」—— 用户以为刚才那次解析用了自己的 key').not.toContain(
      '本次解析用了你自己的 API Key',
    )
  })

  test('三条互斥：服务端回报为真时不该同时还挂着「还没填」那句', async () => {
    const w = await mountSummary({
      hasCommunityResult: true,
      byok: byokProp({ hasKey: false }),
    })
    summarizeCb.onQuota({ byok: true })
    await flushPromises()
    const html = w.html()
    expect(html).toContain('本次解析用了你自己的 API Key')
    expect(html, '两条提示同时出现 —— 用户会读到互相矛盾的话').not.toContain('但还没填')
  })
})

// ── 思维导图：前端不再自己洗一遍（工单 #111 / ADR 0029）────────────────

/**
 * markmap-view 在 jsdom 里要两样东西才画得出来：
 *
 * - `ResizeObserver`：markmap-view 内部直接 new 一个，缺了抛
 *   `ReferenceError`，而组件把异常接住会切成「渲染失败」兜底——用例会
 *   安静地测到另一条分支上。
 * - `requestAnimationFrame`：d3 的补间要动 `transform.baseVal`，jsdom 的
 *   SVG 没这个属性，补间一跑就抛。
 *
 * 缺任何一个都会让本节退化成兜底分支，所以这里显式补上。
 */
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** 导图节点：markmap 把每个节点画成一个 `<g class="markmap-node">`。 */
function mindmapNodes(w) {
  return w.findAll('g.markmap-node')
}

function nodeByText(w, text) {
  return mindmapNodes(w).find((n) => n.text().trim() === text)
}

describe('VideoSummary 挂载 · 思维导图渲染', () => {
  beforeEach(() => {
    vi.stubGlobal('ResizeObserver', StubResizeObserver)
    vi.stubGlobal('requestAnimationFrame', () => 0)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test('后端交来的 markdown 原样渲染：第三级节点不会在前端这一层消失', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    // 根写在 `##` 上（老库存量的形状）。前端若再自己「把首个 ## 提成 #」，
    // 它的后代不会跟着下移——`###` 就比根深了两级。markmap 建树时遇到这种
    // 孤儿标题会把父节点的孩子整个清空，第三级节点就凭空消失了。
    // 前端不做这件事，第三级节点就还在。
    summarizeCb.onMindmap({ markdown: '## 视频主题\n### 主题细节\n## 关键分支' })
    await flushPromises()
    await openTab(w, '思维导图')

    const detail = nodeByText(w, '主题细节')
    expect(detail, '第三级节点「主题细节」没渲染出来 —— 少了一层内容').toBeTruthy()
    expect(detail.attributes('data-depth'), '第三级节点不在第 3 层 —— 层级被改动了').toBe('3')

    const parent = nodeByText(w, '视频主题')
    expect(parent, '第二级节点「视频主题」没渲染出来').toBeTruthy()
    expect(detail.attributes('data-path'), '第三级节点没挂在第二级节点下面 —— 层级错了').toContain(
      `${parent.attributes('data-path')}.`,
    )
  })

  test('对照组：后端形状的导图（# 根）第三级照样在第三层', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    summarizeCb.onMindmap({
      markdown: '# 视频主题\n## 分支一\n### 细节节点\n## 分支二',
    })
    await flushPromises()
    await openTab(w, '思维导图')

    const detail = nodeByText(w, '细节节点')
    expect(detail, '后端给的第三级节点没渲染出来').toBeTruthy()
    expect(detail.attributes('data-depth')).toBe('3')
  })

  test('没有导图时是空态，一个节点都不画', async () => {
    const w = await mountSummary({ hasCommunityResult: true })
    summarizeCb.onDone()
    await flushPromises()
    await openTab(w, '思维导图')

    expect(w.text()).toContain('暂无思维导图')
    expect(mindmapNodes(w).length, '没有导图却画出了节点').toBe(0)
  })
})
