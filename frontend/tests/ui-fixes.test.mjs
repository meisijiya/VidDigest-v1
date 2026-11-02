/**
 * 7 条 UI 反馈的回归测试。
 *
 * 为什么单独一个文件：这些是「用户真的会说『没反应』」的问题，
 * 而原有 91 条用例对这 7 处改动**全绿**——说明它们根本没被验到。
 * 绿色不等于在验，这组用例就是补上那个缺口。
 *
 * 测法沿用本仓约定：.vue 走源码接线断言（渲染验证需要挂载环境，
 * 超出「node --test 零额外依赖」的约定）；纯逻辑（错误分类）
 * 用真调用测。
 *
 * 每条都按「把这个功能整个删掉，它还会绿吗」自查过。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { classifyError } from '../src/lib/errors.ts'
import { sliceBetween } from './helpers/source-slice.mjs'

/** 归一化行尾符 */
function read(...parts) {
  return readFileSync(new URL(...parts, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
}

/** 去掉注释：断言要看代码，不是解释代码的散文 */
function stripComments(src) {
  return (
    src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .split('\n')
      // 协议里的 // 不是注释：只剥「前面不是冒号」的那种，否则
      // `https://x.com` 会被削成 `https:`，域名连同后面整行一起消失，
      // 扫源码的断言于是永远看不到它 —— 这条判据会变成死的。
      .map((l) => l.replace(/(^|[^:])\/\/.*$/, '$1'))
      .join('\n')
  )
}

const appVue = stripComments(read('../src/App.vue'))
const homeView = stripComments(read('../src/views/HomeView.vue'))

/**
 * App.vue 的 handleOpenRecord 函数体，模块顶层只切一次。
 *
 * 原先有两条用例各切一遍同样的区间、且都没有边界守卫。两条都不止是重复 ——
 * `appVue.slice(indexOf(start), indexOf(end))` 在任一锚点消失时会切到文件末尾，
 * 于是「handleOpenRecord 没查社区」和「失败时没退回手动」这两条断言
 * 实际在扫整个 App.vue，而 handleOpenRecord 前后总有能匹配上的东西。
 */
const openRecordBody = () =>
  sliceBetween(
    appVue,
    'async function handleOpenRecord(',
    'async function handleDownload',
    'App.vue 的 handleOpenRecord',
  )
const headerVue = stripComments(read('../src/components/AppHeader.vue'))
const footerVue = stripComments(read('../src/components/AppFooter.vue'))
const communityVue = stripComments(read('../src/components/CommunityPage.vue'))
const historyVue = stripComments(read('../src/components/HistoryPage.vue'))
const summaryVue = stripComments(read('../src/components/VideoSummary.vue'))
const errorModalVue = read('../src/components/ErrorModal.vue')

/** 从 script setup 里抽出 props 声明块 */
function propsBlock(src) {
  const i = src.indexOf('const props = defineProps(')
  if (i < 0) return src.slice(src.indexOf('defineProps('), src.indexOf('defineProps(') + 400)
  const start = src.indexOf('defineProps(', i)
  return src.slice(start, start + 500)
}

// ─────────────────────────────────────────────────────────
// 问题 1：历史记录打开后应该直接显示
// ─────────────────────────────────────────────────────────
describe('问题1 · 命中社区已有结果时自动展示，不扣额度', () => {
  test('组件有 hasCommunityResult 开关，且默认关（不自动）', () => {
    const p = propsBlock(summaryVue)
    assert.match(p, /hasCommunityResult/, '没有 hasCommunityResult 开关')
    // 默认必须是 false：默认 true 意味着一进来就自动扣额度。
    assert.match(
      p,
      /hasCommunityResult:\s*\{[^}]*default:\s*false/,
      'hasCommunityResult 默认必须是 false —— 默认 true 会在结果不存在时白扣额度',
    )
  })

  test('为真时自动发起，为假时不自动', () => {
    const w = summaryVue.slice(
      summaryVue.search(/watch\(\s*\(\)\s*=>\s*props\.videoUrl/),
      summaryVue.indexOf('function startSummarize'),
    )
    assert.notEqual(w.length, 0, '没找到 videoUrl 的 watch')
    // 两个入口都自动发起：社区已有结果（零成本复用），以及用户点了
    // 「重新解析」（他明确要求重跑，替他再点一次「开始」没有意义）。
    // 两个都为假时保持手动——那时是真要扣额度。
    assert.match(
      w,
      /if \(newUrl && \(props\.hasCommunityResult \|\| props\.regenerateRequested\)\)[\s\S]*startSummarize\(props\.regenerateRequested\)/,
      'hasCommunityResult 为真时没有自动发起',
    )
  })

  test('开关由服务端事实驱动（by-url），不是本地状态', () => {
    // 只断言实际那一种写法。这里曾写成 /:has-community-result="|hasCommunityResult/，
    // 那是一个**析取**：kebab 分支在 App.vue 里压根不存在（模板编译后绑定的
    // 仍是 :hasCommunityResult），真正兜住它的是弱得多的裸标识符 ——
    // 于是「属性名拼错了」这类回归照样绿。一条断言看着守两处，实际只守了一处。
    // 开关现在由 HomeView 传给 VideoSummary（首页模板搬进了 HomeView）。
    assert.match(
      homeView,
      /:hasCommunityResult=/,
      'HomeView 没把开关传给 VideoSummary（属性名是 :hasCommunityResult=）',
    )
    // 关键：这个开关的值必须来自 by-url（查**社区视频表**）。
    // 用「点过了解析」这种本地状态去推断，会在结果其实不存在时扣额度；
    // 反过来用**个人解析历史**去推断，陌生人打开社区视频时会被判成
    // 「社区里没有」——他于是看不到复用提示，「重新解析」按钮永不出现。
    // 这条断言以前写的是 fetchHistoryByUrl，与测试名自相矛盾，全绿了很久。
    assert.match(appVue, /fromCache\.value = !!found\?\.exists/, 'fromCache 没有来自社区视频表的 by-url 查询')
    assert.doesNotMatch(appVue, /fetchHistoryByUrl/, '仍在用个人解析历史判断社区里有没有这一份')
  })

  test('从历史页打开时会去查社区（原来压根没查， 自动展示永远不触发）', () => {
    const fn = openRecordBody()
    assert.match(
      fn,
      /fetchCommunityByUrl/,
      'handleOpenRecord 没有查社区 —— fromCache 恒为 false，自动展示在历史页这条路上永远不触发',
    )
  })

  test('查询失败一律当作「没有」，退回手动触发', () => {
    const fn = openRecordBody()
    // 宁可多点一次，也不能在结果不存在时自动发起并扣额度。
    assert.match(
      fn,
      /catch\s*\{\s*fromCache\.value = false/,
      '查询失败时没有退回手动触发 —— 会在结果不存在时自动扣额度',
    )
  })

  test('「零成本自动」这个前提在后端成立：reuse 在扣额度之前就 return', () => {
    // 这条不是前端断言，是把自动化的安全前提钉在测试里：
    // 删掉「reuse 直接 return」或把 consume_quota 提前，自动展示就会扣额度。
    //
    // 锚点跟着占位权 module 的形状走（ADR 0031）：reuse 的判定现在读
    // `claim.outcome`，不再是路由里的裸字符串。
    const api = read('../../backend/api_summarize.py')
    const reuseAt = api.indexOf('claim.outcome == video_claim.CLAIM_REUSE')
    const consumeAt = api.indexOf('consume_quota(user["id"], "parse")')
    assert.ok(reuseAt > 0, '没找到 reuse 分支')
    assert.ok(consumeAt > 0, '没找到 consume_quota')
    assert.ok(reuseAt < consumeAt, 'consume_quota 出现在 reuse 分支之前 —— 自动展示会扣掉用户额度')
  })
})

// ─────────────────────────────────────────────────────────
// 问题 2 / 批注3：回到起始页
// ─────────────────────────────────────────────────────────
describe('问题2 · 回到首页要真的回到起始页', () => {
  test('Logo 点击走 goHome，而不是只改 currentPage', () => {
    assert.match(headerVue, /@click="\$emit\('go-home'\)"/, 'Logo 没接 go-home')
    assert.match(appVue, /@go-home="goHome"/, 'App.vue 没接 goHome')
    // 原来就是 `currentPage = 'home'`：页面切了，但 videoData 还在，
    // 解析结果区仍然挂在下面，看起来像「点了没反应」。
    assert.doesNotMatch(appVue, /@go-home="currentPage = 'home'"/, '仍在只改 currentPage —— 解析结果区不会消失')
  })

  test('goHome 清掉解析态并回到顶部', () => {
    const fn = appVue.slice(appVue.indexOf('function goHome'), appVue.indexOf('function openHistory'))
    assert.notEqual(fn.length, 0, '没找到 goHome')
    for (const field of ['videoData', 'currentUrl', 'historyDetail', 'fromCache']) {
      assert.match(fn, new RegExp(`${field}\\.value = `), `goHome 没清掉 ${field} —— 回到首页后仍显示上一个视频`)
    }
    assert.match(fn, /window\.scrollTo/, 'goHome 没回到页面顶部')
  })

  test('页脚图标也能回首页（批注3）', () => {
    assert.match(footerVue, /@click="\$emit\('go-home'\)"/, '页脚 Logo 不可点')
    assert.match(footerVue, /defineEmits\(\['go-home'\]\)/, '页脚没声明 go-home 事件')
    assert.match(appVue, /<AppFooter @go-home="goHome"/, 'App.vue 没把 go-home 接到页脚')
  })

  test('两处 Logo 都是 button：键盘可达、语义正确', () => {
    assert.match(headerVue, /<button[^>]*@click="\$emit\('go-home'\)"/)
    assert.match(footerVue, /<button[^>]*@click="\$emit\('go-home'\)"/)
  })
})

// ─────────────────────────────────────────────────────────
// 问题 3：统一风格的错误弹窗
// ─────────────────────────────────────────────────────────
describe('问题3 · 失败要有统一弹窗，且说清下一步', () => {
  test('App.vue 里不再有裸 alert()', () => {
    const alerts = appVue.match(/\balert\s*\(/g) || []
    assert.equal(alerts.length, 0, `App.vue 还剩 ${alerts.length} 处 alert()：原生弹窗无视本仓视觉语言，也说不清下一步`)
  })

  test('HistoryPage 不再用 alert 报错', () => {
    const alerts = historyVue.match(/\balert\s*\(/g) || []
    assert.equal(alerts.length, 0, `HistoryPage 还剩 ${alerts.length} 处 alert()`)
  })

  test('弹窗已挂载并接上 close', () => {
    assert.match(appVue, /<ErrorModal/, 'App.vue 没挂 ErrorModal')
    assert.match(appVue, /@close="errorModal\.visible = false"/, '弹窗关不掉')
  })

  test('弹窗有标题、原因、提示、按钮四个部分', () => {
    // hint 是核心：只说「解析失败」用户什么也做不了。
    assert.match(errorModalVue, /\{\{ title \}\}/)
    assert.match(errorModalVue, /\{\{ message \}\}/)
    assert.match(errorModalVue, /v-if="hint"[\s\S]*\{\{ hint \}\}/, '弹窗没有「下一步该做什么」这一段')
    assert.match(errorModalVue, /\{\{ actionText \}\}/)
    assert.match(errorModalVue, /role="alertdialog"/, '缺 dialog 语义，屏幕阅读器读不出来')
  })

  test('请求前先本地校验链接，不把明显错误丢给后端', () => {
    assert.match(appVue, /function validateUrlInput/, '没有本地 URL 校验')
    const fn = sliceBetween(
      appVue,
      'function validateUrlInput',
      'async function handleParse',
      'App.vue 的 validateUrlInput',
    )
    // 这两条曾合成一条 `/还没有填链接|https?/` —— 同样是析取：
    // 只写了空输入检查、没写格式检查，也会因为命中「还没有填链接」而通过。
    assert.match(fn, /还没填链接/, '校验没覆盖空输入 —— 用户什么都没填就点了解析')
    assert.match(fn, /https\?/, '校验没覆盖「填了但不是链接」的输入')
  })

  test('校验失败时不发请求', () => {
    // 边界用下一个函数声明，不用注释锚点：appVue 已 stripComments，
    // 注释锚点在那里不存在，indexOf 返回 -1，切片会一直取到文件末尾，
    // 那样「文件里任何地方有 parseVideo」都能让这条断言通过。
    const start = appVue.indexOf('async function handleParse')
    const end = appVue.indexOf('function ', start + 10)
    assert.ok(start > 0, '没找到 handleParse')
    assert.ok(end > start, '切片边界失效')
    const fn = appVue.slice(start, end)
    assert.match(fn, /if \(!check\.ok\)[\s\S]*?return[\s\S]*?parseVideo/, '校验失败后仍然继续发请求了')
    // 并且 parseVideo 必须**在**校验之后，不能在其之前。
    const guardAt = fn.indexOf('if (!check.ok)')
    const callAt = fn.indexOf('parseVideo(')
    assert.ok(guardAt > -1 && callAt > guardAt, 'parseVideo 出现在校验之前 —— 校验形同虚设')
  })
})

// ─────────────────────────────────────────────────────────
// 问题 4 / 5：社区卡片铺满 + 点击可靠
// ─────────────────────────────────────────────────────────
describe('问题4+5 · 卡片铺满一行且点击可靠', () => {
  test('不再是两列布局', () => {
    assert.doesNotMatch(communityVue, /grid gap-4 sm:grid-cols-2/, '仍是两列 —— 大屏上右侧留大片空白，像没加载完')
  })

  test('卡片是 button：整块可点、键盘可达', () => {
    // div + @click 的真实症状是「按了没反应」：可点区小于视觉区。
    const ul = communityVue.slice(
      communityVue.indexOf('<ul v-else class="grid gap-4'),
      communityVue.indexOf('<!-- 翻页 -->'),
    )
    assert.notEqual(ul.length, 0, '没找到卡片列表')
    assert.match(
      ul,
      /<button\b[^>]*type="button"[^>]*@click="openDetail\(item\)"/,
      '卡片不是 button —— 只有内容区可点，空白处点了没反应',
    )
    assert.match(ul, /w-full h-full/, '卡片没有铺满列表项')
  })

  test('点卡片仍然走 open-video，事件名没变', () => {
    // 断言必须在**去掉注释后**的源码上做：openDetail 上面那段长注释
    // 会被 stripComments 删掉，直接在原文里匹配会误判成「找不到函数」。
    const code = stripComments(read('../src/components/CommunityPage.vue'))
    const fn = code.slice(code.indexOf('function openDetail'))
    assert.notEqual(fn.length, 0, '没找到 openDetail')
    assert.match(fn.slice(0, 200), /emit\('open-video', item\)/, 'openDetail 没有把视频交出去')
  })

  test('点卡片后必须离开社区页（这才是「点击没反应」的真因）', () => {
    // 实测踩过的坑：请求确实发出去了（by-url + /api/parse 都 200），
    // 但 currentPage 仍是 community，社区页继续盖在上面，用户看到的就是
    // 「点了卡片毫无反应」。所以断言的不是「点击有没有触发」，
    // 而是「触发之后有没有换页」——只断言前者会漏掉这个 bug。
    // 边界取**下一个函数声明**，不取注释锚点：appVue 已经 stripComments，
    // 注释锚点在去注释后的文本里根本不存在，indexOf 会返回 -1，
    // 于是 slice 拿到整份文件 —— 那样任何 currentPage 断言都会假通过。
    const start = appVue.indexOf('function openCommunityVideo')
    const end = appVue.indexOf('function ', start + 10)
    assert.ok(start > 0, '没找到 openCommunityVideo')
    assert.ok(end > start, '切片边界失效 —— 说明去注释后找不到下一个函数')
    const fn = appVue.slice(start, end)
    // 必须在**切回首页之后、解析之前**：这两个动作有先后，
    // 顺序反了的话解析结果会被社区页挡住。
    assert.match(
      fn,
      /router\.push\('\/'\)[\s\S]*handleParse\(item\.video_url\)/,
      'openCommunityVideo 没切回首页 —— 点卡片后社区页仍然盖着，用户看到的就是「没反应」',
    )
  })
})

// ─────────────────────────────────────────────────────────
// 问题 6：历史列表显示封面
// ─────────────────────────────────────────────────────────
describe('问题6 · 历史列表显示封面', () => {
  test('列表项渲染 cover_url，取不到时回落占位图标', () => {
    assert.match(historyVue, /v-if="item\.cover_url"/, '列表没渲染封面')
    assert.match(historyVue, /v-else[\s\S]*w-24 h-16/, '没有占位回落')
  })

  test('封面走代理：视频站有防盗链，直连一片灰', () => {
    assert.match(historyVue, /function proxyThumbnail/, '没有走代理')
    assert.match(historyVue, /\/api\/proxy\/thumbnail\?url=/, '代理地址不对')
  })

  test('后端列表接口真的返回 cover_url', () => {
    const db = read('../../backend/database.py')
    const fn = sliceBetween(
      db,
      'def get_parse_histories',
      'def get_parse_history_detail',
      'database.py 的 get_parse_histories',
    )
    assert.match(fn, /cover_url/, 'get_parse_histories 没返回 cover_url')
    // 必须有 json_valid 守卫：实测一行非法 JSON 会让整条查询抛
    // malformed JSON，也就是**一条坏记录足以让整个历史列表 500**。
    assert.match(fn, /json_valid\(video_data\)/, '缺 json_valid 守卫 —— 一条坏记录会让整个历史列表 500')
  })
})

// ─────────────────────────────────────────────────────────
// 问题 7 / 批注4：悬停邮箱看额度
// ─────────────────────────────────────────────────────────
describe('问题7 · 悬停邮箱显示额度面板', () => {
  test('邮箱可悬停展开', () => {
    assert.match(headerVue, /@mouseenter="onQuotaEnter"/, '邮箱没有悬停展开')
    assert.match(headerVue, /v-if="quotaOpen"/, '面板没有受控于展开状态')
  })

  test('两个额度都列出来（工单 #4 拆过计数器，只显示一个等于没拆）', () => {
    assert.match(
      headerVue,
      /\[\s*\[\s*'parse'\s*,\s*'解析'\s*\]\s*,\s*\[\s*'chat'\s*,\s*'追问'\s*\],?\s*\]/,
      '面板没有同时列出解析与追问两个额度',
    )
  })

  test('数据由 App.vue 提供，且悬停时才拉', () => {
    assert.match(appVue, /:quota="quotaInfo"/, 'App.vue 没把额度传给 Header')
    assert.match(appVue, /@request-quota="refreshQuota"/, '悬停没有触发拉取')
    const fn = appVue.slice(appVue.indexOf('async function refreshQuota'), appVue.indexOf('function showError'))
    assert.match(fn, /await fetchQuota\(\)/, 'refreshQuota 没有真的取额度')
  })

  test('用完时给出可辨识的样式', () => {
    assert.match(headerVue, /exhausted/, '没有用完状态的样式区分')
  })
})

// ─────────────────────────────────────────────────────────
// 像素母题归位：闪动从「历史页空状态」搬到「左上角 Logo」
// ─────────────────────────────────────────────────────────
//
// ⚠️ 这两个文件在**模块顶层**读，不在 describe 体里读（2026-10-06 实测）。
// node --test 对 **describe 回调体里抛出的异常**给出的退出码是 **0**，
// 那个 suite 的测试一条都没注册、没运行——实测把 PixelLogo.vue 改名后，
// 这 7 条静默消失而退出码仍是 0，`init.sh` 只看退出码所以完全看不见。
// 模块顶层读失败是**响**的（模块加载即失败，退出码非 0）。
const pixelLogoSource = stripComments(read('../src/components/PixelLogo.vue'))
const pixelStyleCss = read('../src/style.css')

describe('像素母题 · 闪动在左上角 Logo 上', () => {
  const pixelLogo = pixelLogoSource
  const styleCss = pixelStyleCss

  /** 取出所有带闪动 class 的 rect，按 SVG 里的书写顺序 */
  const litRects = [...pixelLogo.matchAll(/<rect[^>]*class="([^"]*)"[^>]*>/g)]
    .map((m) => m[1])
    .filter((c) => c.includes('animate-pixel-blink'))

  test('Logo 的六个色块全部在闪', () => {
    assert.equal(litRects.length, 6, `闪动的色块数是 ${litRects.length}，不是 6 —— 少闪的色块在页头上是肉眼可见的缺口`)
  })

  test('只有色块闪，底板和边框不闪（否则整块 logo 一起呼吸，母题就没了）', () => {
    const allRects = pixelLogo.match(/<rect[^>]*>/g) || []
    assert.equal(
      allRects.length,
      8,
      `rect 总数是 ${allRects.length}，不是 8（1 底板 + 1 边框 + 6 色块），说明 Logo 结构变了`,
    )
    assert.equal(allRects.filter((r) => r.includes('animate-pixel-blink')).length, 6, '闪动 class 挂到了底板或边框上')
  })

  test('六个色块错峰 0..5，顺序不乱（同时亮 = 没有母题，只有一起呼吸）', () => {
    const delays = litRects.map((c) => {
      const m = c.match(/delay-(\d)/)
      return m ? m[1] : '0'
    })
    assert.deepEqual(delays, ['0', '1', '2', '3', '4', '5'], `错峰序列是 ${delays.join(',')}，不是 0,1,2,3,4,5`)
  })

  test('闪动 class 真的在页头那个图标上（不是只在别处定义着）', () => {
    assert.match(
      headerVue,
      /<PixelLogo\s+:size="32"/,
      '页头左上角已经不是 PixelLogo 了，「左上角闪动」这个需求就落空了',
    )
  })

  test('style.css 里关键帧与 delay 阶梯都还在（class 挂着但 CSS 被删 = 静默失效）', () => {
    assert.match(
      styleCss,
      /@keyframes\s+pixelBlink/,
      '@keyframes pixelBlink 没了 —— class 挂着也不会动，且没有任何报错',
    )
    assert.match(
      styleCss,
      /\.animate-pixel-blink\s*\{[^}]*animation:\s*pixelBlink/,
      '.animate-pixel-blink 没有绑定到 pixelBlink 动画',
    )
    for (let i = 1; i <= 5; i++) {
      assert.match(
        styleCss,
        new RegExp(`\\.delay-${i}\\s*\\{[^}]*animation-delay`),
        `.delay-${i} 定义丢了，第 ${i} 个色块会和前一个同时亮`,
      )
    }
  })

  test('历史页空状态不再摆那份网格（是「移走」，不是「复制一份」）', () => {
    assert.doesNotMatch(
      historyVue,
      /grid-cols-3[^"]*"[^>]*>\s*(?:<span[^>]*>\s*){3}/,
      '历史页空状态还留着原来的 3×3 闪动网格',
    )
    assert.doesNotMatch(
      historyVue,
      /animate-pixel-blink/,
      '历史页里还有 animate-pixel-blink —— 母题被复制了一份，页脚/页头/空状态会三处一起闪',
    )
  })

  test('空状态文案还在（不能连提示一起删掉）', () => {
    assert.match(historyVue, /暂无解析历史/)
  })
})

// ─────────────────────────────────────────────────────────
// 错误分类器：真调用，不是源码断言
// ─────────────────────────────────────────────────────────
describe('错误分类器 · 每种失败都给出可执行的下一步', () => {
  test('链接不对 → 让他检查链接', () => {
    const r = classifyError({ response: { data: { detail: 'Unsupported URL: abc' } } })
    assert.match(r.title, /链接/)
    assert.ok(r.hint.length > 0, '没给下一步')
  })

  test('限流 / 风控 → 说清不是他的错，让他等', () => {
    const r = classifyError({ message: 'HTTP Error 429: Too Many Requests' })
    assert.match(r.hint, /不是你的问题|等/, '限流时没说清不是用户的问题')
  })

  test('自家限流 ≠ 平台风控：说清是服务器在限，别让他去怪视频站', () => {
    const r = classifyError({
      response: { data: { detail: { error: '请求过于频繁：服务器限流，请 42 秒后再试' } } },
    })
    assert.match(r.title, /频繁/, '自家限流没有单独分类')
    assert.doesNotMatch(r.title, /平台/, '自家限流被说成平台风控 —— 用户会去怪视频站，而不是等一分钟')
    assert.match(r.hint, /等/, '没告诉用户该等')
  })

  test('自家并发闸门 ≠ 速率闸门：让他等前面的任务结束，不是等一分钟', () => {
    const r = classifyError({
      response: {
        data: {
          detail: {
            error: '同时进行的任务太多：这个来源同时在跑的任务已超过 2 个，请等前面的任务结束再试',
          },
        },
      },
    })
    assert.match(r.title, /同时/, '并发闸门没有单独分类')
    assert.doesNotMatch(r.title, /平台/, '并发闸门被说成平台风控 —— 用户会去怪视频站')
    assert.match(r.hint, /结束/, '没告诉用户该等前面的任务结束')
  })

  // 下面四条钉的是「自家 429 的分类协议落在 detail.code 上，不是中文文案」
  // （工单 #89 复审里的 F7）。文案是给人看的，改它不该影响分类；
  // 反过来，code 在就说明是哪道闸门，文案说什么不算数。

  test('自家并发闸门：按 code 认，文案换成完全无关的话也一样', () => {
    const r = classifyError({
      response: {
        data: {
          detail: {
            code: 'concurrency_limit',
            error: '服务端繁忙：这个来源的槽位已占满，请稍后再试',
          },
        },
      },
    })
    assert.match(r.title, /同时/, '带 code 的并发 429 没被认成并发闸门')
    assert.doesNotMatch(r.title, /平台/, '自家并发闸门被说成平台风控')
  })

  test('自家限流：按 code 认，不靠「服务器限流」四个字', () => {
    const r = classifyError({
      response: {
        data: {
          detail: { code: 'rate_limit', error: '请求过于频繁：请 42 秒后再试' },
        },
      },
    })
    assert.match(r.title, /频繁/, '带 code 的限流 429 没被认成自家限流')
    assert.doesNotMatch(r.title, /平台/, '自家限流被说成平台风控')
  })

  test('code 优先于文案：code 说限流，文案说并发，要信 code', () => {
    // 反向对照：文案不该能推翻协议，否则「谁说了算」就没钉死。
    const r = classifyError({
      response: {
        data: {
          detail: {
            code: 'rate_limit',
            error: '同时进行的任务太多：这个来源同时在跑的任务已超过 2 个',
          },
        },
      },
    })
    assert.match(r.title, /频繁/, '文案推翻了 code 的判定')
    assert.doesNotMatch(r.title, /同时/, '文案推翻了 code 的判定')
  })

  test('老后端没给 code 时仍认得出来（本地后端没重启的兼容路径）', () => {
    const r = classifyError({
      response: {
        data: {
          detail: {
            error: '请求过于频繁：服务器限流，请 42 秒后再试',
          },
        },
      },
    })
    assert.match(r.title, /频繁/, '老后端（响应体没有 code）的限流 429 认不出来了')
  })

  test('拿不到响应 + 网络词 → 网络问题，让他检查网络', () => {
    const r = classifyError({ message: 'Network Error' })
    assert.match(r.title, /连不上/)
  })

  test('有响应就不是网络问题（别把 500 说成断网）', () => {
    const r = classifyError({ response: { data: { detail: { error: 'Internal Server Error' } } } })
    assert.doesNotMatch(r.title, /连不上/, '500 被说成断网了 —— 用户会去查网络，而真因在服务端')
  })

  test('剥掉后端重复前缀，不再出现「解析失败：解析失败」', () => {
    const r = classifyError({ message: '解析失败: 视频不存在' })
    assert.equal(r.message, '视频不存在')
  })

  test('每种分支的 hint 都不是空的（这是分类的真正目的）', () => {
    const cases = [
      { response: { data: { detail: 'Unsupported URL' } } },
      { message: 'HTTP Error 429: Too Many Requests' },
      { message: 'Network Error' },
      { response: { data: { detail: { error: 'Internal Server Error' } } } },
      {
        response: {
          data: {
            detail: { error: '同时进行的任务太多：这个来源同时在跑的任务已超过 2 个，请等前面的任务结束再试' },
          },
        },
      },
    ]
    for (const c of cases) {
      const r = classifyError(c)
      assert.ok(r.hint && r.hint.length > 0, `case=${JSON.stringify(c)} 的 hint 是空的`)
      assert.ok(r.message && r.message !== '未知错误', `case=${JSON.stringify(c)} 退化成了「未知错误」，用户看了更困惑`)
    }
  })
})

// ── VideoSummary ↔ App.vue 的事件契约（双向）────────────────

/**
 * 子组件 `defineEmits` 声明的事件集合，必须与父组件 `<VideoSummary>` 上
 * 实际监听的集合**完全相等** —— 多出来的（声明了没监听）与少掉的（监听了没声明）都红。
 *
 * 为什么这条值得守：Vue 对「emit 了但没声明」只打一条 warn，组件照常运行 ——
 * 点一下看起来完全正常，但父组件的状态永远不变：
 *
 * | 事件 | 漏掉后的症状 |
 * |---|---|
 * | `ownership` | `canRegenerate` 恒为 null，「重新解析」按钮永不出现 |
 * | `regenerating` | `reparseLoading` 恒 false，点完不变灰，可以连点 |
 * | `open-byok` | 点「去填写」弹窗不弹 |
 *
 * 三者都不报错，症状统一是「那个按钮好像坏了」，没有任何线索指向组件边界。
 *
 * **双向都要断**：只断「声明 ⊆ 监听」会漏掉「事件已从子组件删掉、
 * 父组件的监听还留着」那一种 —— 那种情况下监听是个死代码，父组件
 * 永远等不到那个事件，而两边看起来都「有」它。
 *
 * 这是 KEEP_TEXT：判据对象就是两份源码的集合相等，
 * 没有对应的运行时行为可断言（真挂 App.vue 要拉起整棵组件树，不划算）。
 */
describe('VideoSummary ↔ App.vue 的事件契约', () => {
  const declared = (() => {
    const m = /defineEmits\(\[([^\]]*)\]\)/.exec(summaryVue)
    assert.ok(m, 'VideoSummary 里找不到 defineEmits([...]) 的数组形式 —— 换成对象式写法了？')
    return [...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]).sort()
  })()

  const listened = (() => {
    // 只取 <VideoSummary ... /> 这一块：App.vue 里还有 AppHeader / VideoResult /
    // ByokDialog 各自的一堆 @监听，全局正则会把它们一起收进来。
    const tag = sliceBetween(homeView, '<VideoSummary', '/>', 'HomeView 的 <VideoSummary> 标签')
    return [...tag.matchAll(/@([a-z][a-z0-9-]*)=/g)].map((x) => x[1]).sort()
  })()

  test('声明的事件与监听的事件完全相等（多一个少一个都要红）', () => {
    assert.deepEqual(
      listened,
      declared,
      `组件声明了 ${JSON.stringify(declared)}，父组件监听的是 ${JSON.stringify(listened)} —— ` +
        'Vue 对多出来/少掉的事件只 warn，界面看起来一切正常而父组件的状态永远不变',
    )
  })

  test('HomeView 声明的事件在 App.vue 里都有落点（中间那层没吞掉）', () => {
    const declared = [...homeView.matchAll(/\(e: '([^']+)'/g)].map((x) => x[1])
    assert.ok(declared.length >= 8, `HomeView 的 defineEmits 没解析出事件名：${JSON.stringify(declared)}`)
    const listeners = sliceBetween(
      appVue,
      'function listenersFor(page: PageKey)',
      'const viewListeners',
      'App.vue 的 listenersFor',
    )
    for (const name of declared) {
      // 事件名在对象里可能带引号（'open-byok'）也可能不带（parse）。
      const wired = new RegExp(`['"]?${name}['"]?\\s*[,:}]`).test(listeners)
      assert.ok(wired, `App.vue 的 listenersFor 里没有 '${name}' —— HomeView 抛出来没人接`)
    }
  })

  test('这条判据自己在两个方向上都会红（不是恒真）', () => {
    // 纯内存自检，不碰文件：把一侧多出/少掉一个事件，两次都必须不相等。
    // 不做这一步的话，上面那条可能因为两边都空而恒绿。
    const base = ['a', 'b']
    assert.notDeepEqual([...base, 'c'].sort(), [...base].sort(), '多出一个事件没被抓')
    assert.notDeepEqual(['a'].sort(), [...base].sort(), '少掉一个事件没被抓')
    assert.deepEqual([...base].sort(), ['a', 'b'].sort(), '相同集合被误判成不同')
  })
})
