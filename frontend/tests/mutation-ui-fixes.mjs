#!/usr/bin/env node
/**
 * 变异验证：新测试是否真的在验。
 *
 * 全绿不等于在验 —— 只有「把功能破坏掉，测试转红」才证明它有鉴别力。
 * 每条变异改一处真实实现，跑一次对应的测试套件，确认：
 *   1) 真的跑了测试（ran > 0，不把「0 条测试」当通过）
 *   2) 真的转红
 *   3) 转红是**断言**转红（AssertionError），不是变异体自己坏了
 * 改完立刻还原，绝不留下变异体。
 */
import { readFileSync, writeFileSync, existsSync, copyFileSync, unlinkSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const FE = join(HERE, '..')
const BE = join(FE, '..', 'backend')

/**
 * 变异体：工单 #111 之前，前端**自己**洗一遍导图 markdown 的那份复制品
 * （现在 VideoSummary.vue 里已经没有了，backend/mindmap.py 是唯一真源）。
 *
 * 这里原样写一遍，是为了让「前端恢复复制品」这条变异**可复现**：
 * 写进脚本文本就得把正则和模板串再转义一遍，转义错了，变异体测的
 * 就不再是那件事。用 toString() 取自己的源码文本，绕开这层转义。
 */
function legacySanitizeMindmap(md) {
  let text = (md || '').trim()
  if (!text) return '# 视频思维导图\n- 暂无内容'

  // 1. 去除代码块围栏
  text = text.replace(/^```(?:markdown|md)?\s*\n?/i, '').replace(/\n?```\s*$/i, '')

  // 2. 截掉第一个任意级别标题之前的内容
  const headingMatch = text.match(/^#{1,6}\s+\S/m)
  if (headingMatch) {
    const idx = text.indexOf(headingMatch[0])
    text = text.slice(idx)
    // 3. 保证存在 `# ` 一级根标题
    if (!/^#\s+\S/m.test(text)) {
      const firstH2 = text.match(/^##\s+(.+)$/m)
      if (firstH2) {
        text = `# ${firstH2[1].trim()}\n${text.slice(text.indexOf(firstH2[0]) + firstH2[0].length)}`
      } else {
        text = `# 视频思维导图\n${text}`
      }
    }
    return text.trim()
  }

  // 4. 兜底：没有任何 Markdown 标题时，把非空文本行转为根节点下的列表
  const lines = text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
  const items = lines
    .map((l) => l.replace(/^[-*•#\d.、\s]+/, '').trim())
    .filter(Boolean)
    .slice(0, 50)
    .map((l) => `- ${l}`)
    .join('\n')
  return items ? `# 视频思维导图\n${items}` : '# 视频思维导图\n- 暂无内容'
}

/** 装回 VideoSummary.vue 时用的源码文本（函数名改回当年那个）。 */
const LEGACY_SANITIZE_SOURCE = legacySanitizeMindmap.toString().replace('legacySanitizeMindmap', 'sanitizeMindmap')

/**
 * 剥掉 ANSI 转义序列（终端颜色）。
 *
 * 为什么需要：vitest 会给 "FAIL" 标色，Linux CI 上那行长这样：
 *   `ESC[41m FAIL ESC[22m ESC[49m tests/x.spec.mjs > 套件 > 用例名`
 * 直接按行首 "FAIL " 去认，行首是 ESC 而不是 F，于是**一条真被杀掉��变异
 * 会被报成「该转红的用例没红」**。本机 Windows 上 vitest 不上色、CI 上色，
 * 同一份守卫在两个平台上给出相反结论——这种「平台差异」比没有守卫更坏，
 * 因为它让人以为守卫还在。
 */
function stripAnsi(text) {
  // eslint-disable-next-line no-control-regex -- 就是要匹配 ANSI 转义序列本身
  return text.replace(/\u001b\[[0-9;]*m/g, '')
}

/** 每个变异：破坏什么、期望哪条测试转红 */
// 空白宽松匹配：模式串的每个字符之间都允许插入任意空白。
// 非空白字符仍要逐字相同——所以它不放宽判据，只是不怕 prettier 重排。
function looseFind(text, pattern) {
  // 在 text 里找 pattern，忽略空白差异，返回 [start, end] 或 null。
  //
  // 非空白字符仍要逐字相同、顺序不变，只允许它们之间多出空白（换行、缩进、
  // 逗号后加空格……）。这些是格式化工具造成的样子，不是代码变了。
  //
  // 刻意不用正则：把 \s* 挂在每个字符后面会让回溯指数爆炸，源文件一大
  // 就能把进程卡死。这里是双指针扫描，复杂度 O(n*m)，没有回溯。
  const chars = Array.from(pattern).filter((ch) => !/\s/.test(ch))
  if (chars.length === 0) return null
  const n = text.length
  for (let start = 0; start < n; start += 1) {
    if (text[start] !== chars[0]) continue
    let ti = start
    let ok = true
    for (const c of chars) {
      while (ti < n && /\s/.test(text[ti])) ti += 1
      if (ti >= n || text[ti] !== c) {
        ok = false
        break
      }
      ti += 1
    }
    if (ok) return [start, ti]
  }
  return null
}

function replaceLoose(src, from, to) {
  if (src.includes(from)) return { out: src.replace(from, to), matched: true }
  const span = looseFind(src, from)
  if (!span) return { out: src, matched: false }
  return { out: src.slice(0, span[0]) + to + src.slice(span[1]), matched: true }
}

const MUTANTS = [
  {
    name: 'M1 卡片退回 div+@click（点空白处没反应）',
    file: join(FE, 'src/components/CommunityPage.vue'),
    from: '<button type="button" @click="openDetail(item)"',
    to: '<div @click="openDetail(item)"',
    expect: /卡片不是 button|整块可点/,
  },
  {
    name: 'M2 卡片恢复两列（大屏留白）',
    file: join(FE, 'src/components/CommunityPage.vue'),
    from: '<ul v-else class="grid gap-4">',
    to: '<ul v-else class="grid gap-4 sm:grid-cols-2">',
    expect: /仍是两列/,
  },
  {
    name: 'M3 goHome 不清 videoData（点了像没反应）',
    file: join(FE, 'src/App.vue'),
    // ⚠️ 锚点必须匹配目标文件的**实际换行符**：App.vue 在 Windows 上是
    // CRLF，锚点写成 \n 会一个字符都匹配不上，装置静默报「锚点没命中」。
    from: "  videoData.value = null\r\n  currentUrl.value = ''",
    to: "  currentUrl.value = ''",
    expect: /解析结果区不会消失|goHome 没清掉/,
  },
  {
    name: 'M4 hasCommunityResult 默认 true（会白扣额度）',
    file: join(FE, 'src/components/VideoSummary.vue'),
    from: 'hasCommunityResult: { type: Boolean, default: false }',
    to: 'hasCommunityResult: { type: Boolean, default: true }',
    expect: /默认必须是 false/,
  },
  {
    name: 'M5 自动拉取条件去掉开关（无脑自动 = 会扣额度）',
    file: join(FE, 'src/components/VideoSummary.vue'),
    from: 'if (newUrl && (props.hasCommunityResult || props.regenerateRequested)) {',
    to: 'if (newUrl) {',
    expect: /没有自动发起|为真时自动发起/,
  },
  {
    name: 'M6 后端去掉 json_valid 守卫（一条坏记录打挂整个列表）',
    file: join(BE, 'database.py'),
    from: 'CASE WHEN json_valid(video_data)',
    to: 'CASE WHEN 1 = 1',
    expect: /缺 json_valid 守卫/,
  },
  {
    name: 'M7 错误分类器恒返回第一个分支（分类形同虚设）',
    file: join(FE, 'src/lib/errors.ts'),
    from: '  if (looksLikeThrottled(err)) {',
    to: '  if (false) {',
    expect: /限流时没说清/,
  },
  {
    name: 'M8 弹窗去掉 hint（只说失败不说怎么办）',
    file: join(FE, 'src/components/ErrorModal.vue'),
    from: '<div v-if="hint"',
    to: '<div v-if="false"',
    expect: /下一步该做什么/,
  },
  {
    name: 'M9 历史列表封面改回固定图标',
    file: join(FE, 'src/components/HistoryPage.vue'),
    from: '<img v-if="item.cover_url" :src="proxyThumbnail(item.cover_url)"',
    to: '<img v-if="false" :src="proxyThumbnail(item.cover_url)"',
    expect: /列表没渲染封面/,
  },
  {
    name: 'M10 额度面板退回硬写一种计数器（拆分对用户不可见）',
    file: join(FE, 'src/components/AppHeader.vue'),
    // 改造前这里硬写 [['parse','解析'],['chat','追问']]；现在行由 lib/quota
    // 依据服务端的 labels 构造。这条变异把「按 labels 渲染」换回一行硬写的
    // 种类名——加第三种额度时面板会静默少一行，正是要挡的回归。
    from: 'const quotaRows = computed(() => buildQuotaRows(props.quota))',
    to: "const quotaRows = computed(() => [{ key: 'parse', label: '解析', text: '', exhausted: false }])",
    expect: /面板不再从 lib\/quota 的 quotaRows 取行/,
  },
  {
    name: 'M11 复用分支后移到扣额度之后（自动展示会扣额度）',
    file: join(BE, 'api_summarize.py'),
    from: 'if claim.outcome == video_claim.CLAIM_REUSE:',
    to: 'if claim.outcome == "nonexistent-reuse-check":',
    expect: /consume_quota 出现在 reuse 分支之前|没找到 reuse 分支/,
  },
  {
    name: 'M12 handleOpenRecord 不再查社区（历史页自动展示永不触发）',
    file: join(FE, 'src/App.vue'),
    from: 'async function handleOpenRecord(detail: HistoryDetail) {',
    to: 'async function handleOpenRecordUnused(detail: HistoryDetail) {',
    expect: /没找到|没有查社区/,
  },
  {
    // 真实 bug：点击请求都发出去了，但社区页没让开，用户看到的是「没反应」。
    name: 'M13 点卡片不切回首页（请求发出但页面不动）',
    file: join(FE, 'src/App.vue'),
    from: "  router.push('/')\n  handleParse(item.video_url)",
    to: '  handleParse(item.video_url)',
    expect: /没切回首页/,
  },
  {
    name: 'M14 抽掉一个色块的闪动（页头 Logo 少亮一块，肉眼可见的缺口）',
    file: join(FE, 'src/components/PixelLogo.vue'),
    from: 'fill="#EC4899" class="animate-pixel-blink delay-2"',
    to: 'fill="#EC4899"',
    expect: /闪动的色块数/,
  },
  {
    name: 'M15 错峰塌成两簇（delay-3 改成 delay-1，同时亮 = 没有母题）',
    file: join(FE, 'src/components/PixelLogo.vue'),
    from: 'class="animate-pixel-blink delay-3"',
    to: 'class="animate-pixel-blink delay-1"',
    expect: /错峰序列/,
  },
  {
    name: 'M16 删掉 .delay-4 的 CSS 定义（class 挂着但静默不生效）',
    file: join(FE, 'src/style.css'),
    from: '.delay-4 { animation-delay: 0.4s; }',
    to: '',
    expect: /delay-4 定义丢了/,
  },
  {
    name: 'M17 把像素网格搬回历史页空状态（母题被复制，三处一起闪）',
    file: join(FE, 'src/components/HistoryPage.vue'),
    from: "<p class=\"text-sm mb-1\">{{ hasFilters ? '没有匹配的记录' : '暂无解析历史' }}</p>",
    to: '<div class="grid grid-cols-3 gap-1 mb-5" aria-hidden="true"><span class="w-2.5 h-2.5 rounded-[2px] bg-violet/40 animate-pixel-blink"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-purple/40 animate-pixel-blink delay-1"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-pink/40 animate-pixel-blink delay-2"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-transparent"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-purple/40 animate-pixel-blink delay-3"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-cyan-500/40 animate-pixel-blink delay-4"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-transparent"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-transparent"></span><span class="w-2.5 h-2.5 rounded-[2px] bg-cyan-500/40 animate-pixel-blink delay-5"></span></div><p class="text-sm mb-1">{{ hasFilters ? \'没有匹配的记录\' : \'暂无解析历史\' }}</p>',
    expect: /母题被复制了一份/,
  },
  {
    name: 'M18 并发分类器忽略 detail.code（退回只认中文文案）',
    file: join(FE, 'src/lib/errors.ts'),
    from: "  if (errorCodeOf(err) === 'concurrency_limit') return true",
    to: "  if (errorCodeOf(err) === '__mutant_never__') return true",
    expect: /文案换成完全无关的话/,
  },
  {
    name: 'M19 限流分类器忽略 detail.code（自家限流被说成平台风控）',
    file: join(FE, 'src/lib/errors.ts'),
    from: "  if (errorCodeOf(err) === 'rate_limit') return true",
    to: "  if (errorCodeOf(err) === '__mutant_never__') return true",
    expect: /不靠「服务器限流」四个字/,
  },
  {
    // 工单 #95：历史页没接登录入口。锚点带 `back: goHome,`，
    // 因为社区页与首页也各有一处一模一样的 'need-login' 接线 ——
    // 锚点太短会打在别人身上，变异就打偏了。
    name: 'M20 历史页不接 need-login（点了「去登录」登录框不开）',
    file: join(FE, 'src/App.vue'),
    from: "        back: goHome,\r\n        'need-login': () => showAuthModal('login'),",
    to: "        back: goHome,\r\n        'need-login-unused': () => showAuthModal('login'),",
    expect: /历史页没接登录入口/,
  },
  {
    name: 'M21 历史页模板里没有未登录这一支',
    file: join(FE, 'src/components/HistoryPage.vue'),
    from: 'v-else-if="needLogin"',
    to: 'v-else-if="neverNeedLogin"',
    expect: /模板里没有未登录这一支/,
  },
  {
    // 锚点只取**第一个** isUnauthorized —— load() 在 loadFacets() 前面，
    // 而 node --test 那条断言判的就是 load() 的函数体。
    name: 'M22 load() 不再把 401 翻译成 needLogin（错误码又甩回用户脸上）',
    file: join(FE, 'src/components/HistoryPage.vue'),
    from: '    if (isUnauthorized(e)) {',
    to: '    if (false) {',
    expect: /401 没被翻译成 needLogin/,
  },
  // ── 工单 #99：邀请码注册的前端接线 ──
  // 这几条破坏的是「注册要邀请码」「管理员要发码」这两处接线。
  // 断了都不报错：输入框没了只是少一格，邀请码没进请求体只是后端说无效。
  {
    name: 'M23 注册表单的邀请码输入框没了（少一格，后端只说「需要邀请码」）',
    file: join(FE, 'src/components/AuthModal.vue'),
    from: 'v-model="inviteCode"',
    to: 'v-model="email"',
    expect: /注册模式有一格邀请码输入/,
  },
  {
    name: 'M24 邀请码不进请求体（码填了也白填）',
    file: join(FE, 'src/components/AuthModal.vue'),
    from: 'invite_code: code',
    to: "invite_code: ''",
    expect: /请求体里多出 invite_code 这一键/,
  },
  {
    name: 'M25 注册走的还是不带邀请码那条路（后端永远说「需要邀请码」）',
    file: join(FE, 'src/components/AuthModal.vue'),
    from: 'registerWithInviteCode(email.value, password.value, inviteCode.value.trim())',
    to: 'register(email.value, password.value)',
    expect: /注册走的是带邀请码的那条路/,
  },
  {
    name: 'M26 失败提示不取后端 detail（四类失效原因被一句通用话盖住）',
    file: join(FE, 'src/components/AuthModal.vue'),
    from: 'e.response?.data?.detail',
    to: "'注册失败'",
    expect: /读的是后端给的 detail/,
  },
  {
    name: 'M27 管理后台发码请求打错地址（管理员点了没反应）',
    file: join(FE, 'src/api/admin.ts'),
    from: "post('/api/admin/invite-codes', {",
    to: "post('/api/admin/invite-codes-mutant', {",
    expect: /有发码入口/,
  },
  {
    name: 'M28 管理后台列表请求打错地址（列表永远是空的）',
    file: join(FE, 'src/api/admin.ts'),
    from: "get('/api/admin/invite-codes', {",
    to: "get('/api/admin/invite-codes-mutant', {",
    expect: /有列表入口/,
  },
  {
    name: 'M29 管理后台作废请求打错地址（点了作废，码还能用）',
    file: join(FE, 'src/api/admin.ts'),
    from: 'post(`/api/admin/invite-codes/${inviteId}/revoke`)',
    to: 'post(`/api/admin/invite-codes/${inviteId}/revoke-mutant`)',
    expect: /有作废入口/,
  },
  {
    name: 'M30 列表改成显示完整码（明文只给一次这条规矩破了）',
    file: join(FE, 'src/components/InviteCodesPage.vue'),
    from: '{{ row.codeMasked }}',
    to: '{{ row.code }}',
    expect: /列表只显示脱敏码/,
  },
  {
    name: 'M31 打开页面时不加载列表（页面是一片空白）',
    file: join(FE, 'src/components/InviteCodesPage.vue'),
    // 锚点带上 onMounted 的开头：`void loadCodes()` 在页面里出现三次
    // （翻页、换筛选、挂载），只锚这一句的话，装置改的是翻页那处，
    // 挂载那处照样在跑 —— 变异会「存活」，而它压根没碰到被测机制。
    from: 'onMounted(() => {\n  void loadCodes()',
    to: 'onMounted(() => {\n  void Promise.resolve()',
    expect: /挂载即加载列表/,
  },
  // ── 工单 #105：总结模板选择 ──
  // 这几条破坏的是「用户在下拉框里选的模板真的被用上」。
  // 断了都不报错：请求照样发出去、后端照样返回总结，
  // 症状是「选了精简摘要，拿回来的还是详尽总结」——用户只能靠字数去猜。
  {
    name: 'M32 选中的模板不进请求体（下拉框选了个寂寞）',
    file: join(FE, 'src/api/summarize.ts'),
    from: "summary_template: options.summaryTemplate || '',",
    to: "summary_template: '',",
    expect: /选中的模板没进请求体/,
  },
  {
    name: 'M33 组件不把下拉框的选择传出去（选了也不生效）',
    file: join(FE, 'src/components/VideoSummary.vue'),
    from: 'summaryTemplate: selectedTemplate.value,',
    to: "summaryTemplate: '',",
    expect: /组件没有把用户选的模板传给请求/,
  },
  {
    name: 'M34 总结正文里的表格没有排版（模型一输出表格就散架）',
    file: join(FE, 'src/style.css'),
    from: `.summary-content table {
  display: block;
  width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 0.9rem;
}`,
    to: '',
    expect: /总结正文里的表格没有排版/,
  },
  {
    // 工单 #111 判据 1 + 2：前端不再有第二份清洗规则，导图结构由
    // backend/mindmap.py 说了算。这条变异把当年那份复制品**原样装回去**：
    // 它把首个 `##` 提升成 `# ` 却不把后代同步下移，于是 `###` 变成
    // 「比根深两级」的孤儿标题，markmap 建树时把父节点的孩子整个清掉 ——
    // 第三级节点凭空消失。挂载用例必须为此转红。
    name: 'M35 前端把导图再洗一遍（旧复制品被恢复）',
    file: join(FE, 'src/components/VideoSummary.vue'),
    edits: [
      { from: 'function renderMindmap() {', to: `${LEGACY_SANITIZE_SOURCE}\n\nfunction renderMindmap() {` },
      {
        from: 'transformer.transform(mindmapMd.value)',
        to: 'transformer.transform(sanitizeMindmap(mindmapMd.value))',
      },
    ],
    runner: 'vitest',
    spec: 'tests/video-summary.mount.spec.mjs',
    // 转红必须是断言转红（不是变异体自己坏了），且只该转红这一条：
    // 同文件里那两条反向对照（后端形状的导图 / 空态）必须保持绿。
    failedTests: ['后端交来的 markdown 原样渲染：第三级节点不会在前端这一层消失'],
    keptGreen: ['对照组：后端形状的导图（# 根）第三级照样在第三层', '没有导图时是空态，一个节点都不画'],
  },
  {
    // 会话回查：服务端回 401 时本地会话必须作废。
    // 断掉的症状不是报错，是「明明没登录，顶栏却显示着旧邮箱和「退出」」——
    // 而同一刻服务端把每个请求都判成未登录。两处自相矛盾。
    name: 'M36 401 不作废本地会话（顶栏显示成已登录，其实没登录）',
    file: join(FE, 'src/stores/session.ts'),
    from: '        if (isUnauthorized(err)) signOut()',
    to: '        if (false) signOut()',
    runner: 'vitest',
    spec: 'tests/session-store.spec.mjs',
    // 只该转红 401 那一条；500 与「本地没 token」两条反向对照必须保持绿。
    failedTests: ['401：顶栏回到未登录态，localStorage 里的 token 与用户一起清掉'],
    keptGreen: [
      '反向对照：500 是「问不着」不是「被拒」，会话必须留着',
      '本地本来就没有 token：不发请求，也不碰 localStorage',
    ],
  },
  {
    // 401 判定收进 lib/http 之后，它自己成了唯一的判据。上面 M22 断的是
    // **调用点**（还在不在翻译），这条断的是**判定本身**：判据从 401 改成
    // 别的，服务端的 401 就不再被认成「token 不认了」。
    //
    // 没有这条的话，把 lib/http 的判定改坏只会表现为「用户莫名被登出」，
    // 而没有任何一条用例会红。
    name: 'M37 401 判定被改成别的（token 不认了也认不出来）',
    file: join(FE, 'src/lib/http.ts'),
    from: 'return e?.response?.status === 401 || e?.status === 401',
    to: 'return e?.response?.status === 403 || e?.status === 403',
    runner: 'vitest',
    spec: 'tests/session-store.spec.mjs',
    failedTests: ['401：顶栏回到未登录态，localStorage 里的 token 与用户一起清掉'],
    keptGreen: [
      '反向对照：500 是「问不着」不是「被拒」，会话必须留着',
      '本地本来就没有 token：不发请求，也不碰 localStorage',
    ],
  },
  {
    // 退回到「按中文文案判类」——ADR 0034 之前的样子。它只接得住三种
    // 无字幕文案里的一种，而这三种在 `_subtitle_failure` 里是并列产出的。
    name: 'M38 判类退回按中文文案（无字幕的另两种说法被当成普通错误）',
    file: join(FE, 'src/api/summarize.ts'),
    from: "  switch (err.reason ?? '') {",
    to: '  switch (err.message) {',
    // node runner 跑全部 tests/**/*.test.mjs，spec 字段对它无效
    failedTests: [
      '无字幕的三种文案都判成 no_subtitle（改造前只接得住第三种）',
      'ASR 兜底没救回来也属于无字幕',
      '额度耗尽单独一类',
    ],
    keptGreen: ['反向对照：消息里带「字幕」字样但 reason 为空，仍是 other', '未知 reason 落进 other，不静默当成成功'],
  },
]

let killed = 0
const survivors = []
const broken = []

for (const m of MUTANTS) {
  if (!existsSync(m.file)) {
    broken.push(`${m.name} —— 文件不存在: ${m.file}`)
    continue
  }
  const backup = m.file + '.mutbak'
  let src
  try {
    src = readFileSync(m.file, 'utf8')
  } catch (e) {
    broken.push(`${m.name} —— 读取失败: ${e.message}`)
    continue
  }
  // 锚点没命中**必须**算失败。静默跳过会让「这条变异没跑过」
  // 看起来像「这条变异存活了」，或者干脆消失在报告里 ——
  // 而这两件事的处置完全相反。
  // 但「重排版」（prettier 改了换行与缩进）不算代码变了：
  // 先精确匹配，再退到空白宽松（只改空白，非空白字符仍逐字相同）。
  // 一条变异可以有多处改动（`edits`）；每处都必须命中。
  const edits = m.edits || [{ from: m.from, to: m.to }]
  let mutated = src
  let matchedAll = true
  for (const e of edits) {
    const r = replaceLoose(mutated, e.from, e.to)
    if (!r.matched) {
      matchedAll = false
      break
    }
    mutated = r.out
  }
  if (!matchedAll) {
    broken.push(`${m.name} —— 锚点没命中（代码已变或换行符不符？先更新变异脚本，别跳过）`)
    continue
  }

  copyFileSync(m.file, backup)
  try {
    writeFileSync(m.file, mutated, 'utf8')
    let out = ''
    let ran = -1
    let nRan = 0
    let failedTests = []
    if (m.runner === 'vitest') {
      // 挂载用例走 vitest（`npm run test:mount`）。node --test 那条 glob
      // 只收 .test.mjs，收不到 .spec.mjs —— 挂载用例必须走这一条。
      try {
        out = execFileSync(process.execPath, ['node_modules/vitest/vitest.mjs', 'run', m.spec], {
          cwd: FE,
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        })
        ran = 0
      } catch (e) {
        out = (e.stdout || '') + (e.stderr || '')
        ran = 1
      }
      const mRan = out.match(/Tests\s+[^\n]*\((\d+)\)/)
      nRan = mRan ? Number(mRan[1]) : 0
      // 失败用例名要从 vitest 的输出里读。**必须先剥掉 ANSI 转义序列**：
      // vitest 会给 "FAIL" 上色（Linux CI 上是 `ESC[41m FAIL ESC[22m ...`），
      // 不剥的话这行根本不是以 "FAIL " 开头，解析器会以为「没有用例转红」，
      // 于是把一条明明被杀掉的变异报成「该转红的用例没红」——
      // 同一台机器上 Windows 与 Linux 结论相反，守卫就成了摆设。
      failedTests = stripAnsi(out)
        .split('\n')
        .filter((l) => l.trimStart().startsWith('FAIL '))
        .map((l) => l.split(' > ').pop().trim())
    } else {
      try {
        out = execFileSync(
          process.execPath,
          // 必须用与 `npm test` 相同的 glob：传单个文件路径时 node --test
          // 不产出 TAP 计数，于是「变异后测试跑不起来」和「测试全过」长得
          // 一模一样，12 条变异会假性全部存活。
          ['--test', '--test-timeout=10000', 'tests/**/*.test.mjs'],
          { cwd: FE, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
        )
        ran = 0
      } catch (e) {
        out = (e.stdout || '') + (e.stderr || '')
        ran = 1
      }
      // node 的失败行是 `  ✖ <用例名> (0.17ms)`。解析出来才能像 vitest 那样
      // 声明「该转红的用例」与「反向对照必须保持绿」——没有它，node 变异
      // 只能判「红没红」，反向对照就成了空话。
      failedTests = stripAnsi(out)
        .split('\n')
        .map((l) => l.match(/✖\s+(.+?)(?:\s+\(\d+(?:\.\d+)?ms\))?$/))
        .filter(Boolean)
        .map((mm) => mm[1].trim())
      // 判据必须看**实际跑了多少条**：
      // 变异可能把源码改坏到测试导入失败，那不是「测试通过」。
      const mRan = out.match(/#\s*tests\s+(\d+)/) || out.match(/tests (\d+)/)
      nRan = mRan ? Number(mRan[1]) : 0
    }
    if (nRan === 0) {
      survivors.push(`${m.name} —— NO-TESTS-RAN（ran=0，判据无效；变异可能把源码改坏到无法导入）`)
      continue
    }
    if (ran === 0) {
      survivors.push(`${m.name} —— 存活（${nRan} 条测试全绿，测试没在验这一条）`)
    } else if (!out.includes('AssertionError')) {
      // 转红但不是断言失败：多半是变异把源码改坏到**导入不了**（语法错误之类）。
      // 那种红不证明任何东西——它证明的是「这个文件坏了」，不是「这条守卫在验这条约束」。
      // 所以它既不算杀、也不算存活：算变异体自坏。
      broken.push(`${m.name} —— 变异体自坏（转红但不是 AssertionError：源码可能被改坏到无法导入）`)
    } else if (m.failedTests && failedTests.length === 0) {
      // 测试确实红了一大片，却一个失败用例名都没读到：不是「守卫够不着」，
      // 而是**输出格式变了**（比如 vitest 给 FAIL 行上了 ANSI 颜色，
      // 行首不是 "FAIL "）。这种情况必须单独说，否则会被误判成存活。
      broken.push(`${m.name} —— 装置故障（测试转红了但没读到失败用例名：输出格式变了？）`)
    } else if (m.failedTests && !m.failedTests.every((t) => failedTests.includes(t))) {
      // 该转红的没转红 = 变异没被真正杀掉（护栏够不着它）。
      const missing = m.failedTests.filter((t) => !failedTests.includes(t))
      broken.push(`${m.name} —— 该转红的用例没红：${missing.join(' / ')}`)
    } else if (m.keptGreen && m.keptGreen.some((t) => failedTests.includes(t))) {
      // 反向对照转红了 = 守卫越权（它管了不该管的事），比变异存活更严重。
      const over = m.keptGreen.filter((t) => failedTests.includes(t))
      broken.push(`${m.name} —— 反向对照转红了（守卫越权）：${over.join(' / ')}`)
    } else {
      killed++
      const which =
        m.runner === 'vitest'
          ? failedTests[0]
          : (out.match(/✖ ([^\n(]+)/) || out.match(/^not ok \d+ - (.+)$/m) || [])[1]
      console.log(`KILLED  ${m.name}  (ran=${nRan})`)
      console.log(`        被杀于: ${which ? which.trim() : '(见输出)'}`)
    }
  } finally {
    copyFileSync(backup, m.file)
    unlinkSync(backup)
  }
}

console.log('='.repeat(72))
console.log(`变异结果：${killed}/${MUTANTS.length} 杀，${survivors.length} 存活，${broken.length} 装置故障`)
if (survivors.length) {
  console.log('--- 存活（测试没在验，或装置坏了）---')
  for (const s of survivors) console.log('  ' + s)
}
if (broken.length) {
  console.log('--- 装置故障 ---')
  for (const b of broken) console.log('  ' + b)
}
process.exit(survivors.length || broken.length ? 1 : 0)
