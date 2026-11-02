/**
 * HistoryPage.vue：搜索 / 标签 / 收藏 / AI 三档 / 分页 / 删除双重保险。
 *
 * 判据分三类，都按「能变红」的标准挑：
 *
 *  1. **纯函数真跑**（直接 import `src/lib/tag-filter.ts`）。多选/单选这两个
 *     分支写错的话，界面上是「点第二个把第一个也取消了」，从模板上看不出来。
 *  2. **`params()` 抽出来真跑**。四个筛选维度是「组合」的，而组合最容易被
 *     悄悄做丢一个 —— 只断源码里出现过 `ai` 这个词，测不到它有没有进查询。
 *  3. **API 层契约**。`deleteHistory` 把 409 翻译成 `refused` 而不是抛异常，
 *     这一条错了界面就会显示「删除失败」，而记录其实好好地还在。
 *
 * ⚠️ 本仓已知坑（`docs/TESTING-STRATEGY.md` §3.5 纪律 2）：`describe` 体里跑会抛异常的东西是隐形的 ——
 * node 对 describe 体抛异常给退出码 0 且报 fail 0，那个 suite 的测试一条都
 * 没注册。所以下面所有取值都是惰性的，进了 test() 体内才执行。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { tagsToQuery, toggleTag, needsExpand, shouldShowToggle, parseTagQuery } from '../src/lib/tag-filter.ts'
import { extractFn, sliceBetween } from './helpers/source-slice.mjs'
import { transpileTs } from './helpers/ts-sandbox.mjs'

function read(...parts) {
  return readFileSync(new URL(...parts, import.meta.url), 'utf8').replace(/\r\n/g, '\n')
}

/**
 * 剥注释：`//` **只**整行剥。行尾一刀切会把 URL 字面量从中间砍断，
 * 而那往往正是断言的对象（工单 #13 就这么被吃掉过一条判据）。
 */
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map((l) => (l.trim().startsWith('//') ? '' : l))
    .join('\n')
}

const vue = read('../src/components/HistoryPage.vue')
const code = stripComments(vue)
const api = read('../src/api/history.ts')

/**
 * 真跑组件里的 `params()`。它引用了一堆 ref，这里用同名形参把 scope 补上 ——
 * 注入的是真实形状（`{value: ...}`），不是随手编的值。
 */
function runParams({ query = '', tags = [], ai = '', favorite = false, page = 1, size = 20 }) {
  const src = `${transpileTs(extractFn(code, 'params'), 'history-page-script.ts')}; return params()`
  const fn = new Function('activeQuery', 'activeTags', 'ai', 'favoriteOnly', 'page', 'PAGE_SIZE', 'tagsToQuery', src)
  // activeQuery / activeTags / ai / favoriteOnly / page 在组件里都是 ref，
  // 源码取的是 `.value`；PAGE_SIZE 是普通常量，**不能**包成 ref。
  // 这个坑实测过一次：一开始五个都传裸值，于是 ai 与 favorite 全成
  // undefined，页面看着像「筛选没生效」，而失败点在测试的注入形状上。
  return fn({ value: query }, { value: tags }, { value: ai }, { value: favorite }, { value: page }, size, tagsToQuery)
}

// ── 纯逻辑 ────────────────────────────────────────────────

describe('标签多选的纯逻辑', () => {
  test('多选：再点一次同一个标签是取消', () => {
    assert.deepEqual(toggleTag([], '编程', true), ['编程'])
    assert.deepEqual(toggleTag(['编程'], '编程', true), [])
  })

  test('多选：加第二个不会顶掉第一个', () => {
    // 单选/多选写反的症状就是这里：['编程'] 点「架构设计」之后变成 ['架构设计']。
    assert.deepEqual(toggleTag(['编程'], '架构设计', true), ['编程', '架构设计'])
  })

  test('单选：点新的顶掉旧的', () => {
    assert.deepEqual(toggleTag(['编程'], '架构设计', false), ['架构设计'])
  })

  test('单选：点已选中的同一个 = 回到全量', () => {
    // 用「取反」实现单选会得到 ['编程','编程']：界面上看着没反应，数据已经脏了。
    assert.deepEqual(toggleTag(['编程'], '编程', false), [])
  })

  test('空选 → 不带 tag 参数（不是空串）', () => {
    assert.equal(tagsToQuery([]), '')
  })

  test('单选与多选序列化形状不同，且能原样解析回来', () => {
    assert.equal(tagsToQuery(['编程']), '编程')
    assert.equal(tagsToQuery(['编程', '架构设计']), '编程,架构设计')
    assert.deepEqual(parseTagQuery('编程,架构设计'), ['编程', '架构设计'])
    assert.deepEqual(parseTagQuery('编程'), ['编程'])
  })

  test('溢出判断只看宽度，展开态的规则归 shouldShowToggle', () => {
    assert.equal(needsExpand(1000, 300), true)
    assert.equal(needsExpand(300, 300), false)
    // 亚像素：等宽时 scrollWidth 常比 clientWidth 大零点几
    assert.equal(needsExpand(300.4, 300), false)
  })

  test('展开态必须留着「收起」入口', () => {
    // 真浏览器里点一次展开就复现过的 bug：measure 把 overflowing 抹成
    // false，按钮跟着消失，行已经换行了却再也收不回去。
    assert.equal(shouldShowToggle(4, false, true), true, '展开态下按钮不见了')
    assert.equal(shouldShowToggle(4, true, false), true, '溢出时不给展开键')
    assert.equal(shouldShowToggle(4, false, false), false, '没溢出却冒出展开键')
    assert.equal(shouldShowToggle(1, true, true), false, '只有一个标签还来什么展开')
    assert.equal(shouldShowToggle(0, true, false), false)
  })
})

// ── 四个筛选维度真的都进了查询 ────────────────────────────

describe('params() 把四个筛选维度都带上', () => {
  test('全空时只带分页', () => {
    const p = runParams({})
    assert.equal(p.q, '')
    assert.equal(p.tag, '')
    assert.equal(p.ai, '')
    assert.equal(p.favorite, undefined, '未开「仅收藏」时不该发 favorite')
    assert.equal(p.page, 1)
    assert.equal(p.page_size, 20)
  })

  test('关键词 / 标签 / AI 状态 / 仅收藏 同时生效', () => {
    const p = runParams({
      query: '秋促',
      tags: ['编程', '架构设计'],
      ai: 'ai',
      favorite: true,
      page: 3,
    })
    assert.equal(p.q, '秋促')
    assert.equal(p.tag, '编程,架构设计')
    assert.equal(p.ai, 'ai')
    assert.equal(p.favorite, true)
    assert.equal(p.page, 3)
  })

  test('每个键都在——少一个就是「组合筛选」名存实亡', () => {
    // 封口断言：只逐个断值的话，某个键整个消失时上面的用例仍会绿。
    assert.deepEqual(Object.keys(runParams({})).sort(), ['ai', 'favorite', 'page', 'page_size', 'q', 'tag'])
  })

  test('「仅解析」与「AI解析」是两个不同的值，不是同一个开关', () => {
    assert.notEqual(runParams({ ai: 'parse' }).ai, runParams({ ai: 'ai' }).ai)
  })
})

// ── 标签选项的来源 ────────────────────────────────────────

describe('标签选项来自服务端，不从当前页汇总', () => {
  test('组件真的调用了 /api/history/facets，不是只 import 了它', () => {
    // 断的是 `await fetchHistoryFacets()` —— 带调用括号。**不能**断
    // `includes('fetchHistoryFacets')`：import 行里也有这个词，
    // 于是「把唯一的调用删掉」这条变异照样绿（实测过一次）。
    assert.ok(/await\s+fetchHistoryFacets\s*\(\s*\)/.test(code), '没有真的拉取标签清单（或 import 了但从没调用）')
    assert.ok(api.includes('/api/history/facets'), 'API 层没有这个出口')
  })

  test('任何地方都不从 items 里长出标签选项', () => {
    // 比 F11 那条更宽：不只挡一种写法。整份组件里 items 只该喂列表
    // 与删除，**不该**参与决定筛选选项。
    assert.doesNotMatch(code, /new Set\(\s*items\.value/, '标签选项又从当前页汇总了')
    assert.doesNotMatch(code, /items\.value\.flatMap/, '标签选项又从当前页汇总了')
  })

  test('不再从 items 里汇总标签选项', () => {
    // 前端汇总 = 翻页或一筛选标签就会增减，用户读起来是「筛选不生效」。
    assert.doesNotMatch(code, /for \(const item of items\.value\)[^\n]*tags/, '标签选项又变回从当前页卡片汇总了')
  })

  test('标签行是共享组件，不是各写一套', () => {
    assert.ok(code.includes('<TagFilterRow'))
    assert.ok(code.includes('@update:selected'))
  })
})

// ── 模板结构 ──────────────────────────────────────────────

describe('模板把三组筛选和分页都摆出来了', () => {
  const AI_LABELS = ['全部', '仅解析', 'AI解析']

  test('AI 三档齐全', () => {
    for (const label of AI_LABELS) {
      assert.ok(code.includes(`label: '${label}'`), `缺档位 ${label}`)
    }
  })

  test('三档是互斥的单选组（aria-pressed，不是三个独立开关）', () => {
    assert.ok(code.includes('role="group"'))
    assert.ok(code.includes(':aria-pressed="ai === opt.value"'))
  })

  test('有「仅收藏」开关，且它与 AI 状态、标签互不干扰', () => {
    assert.ok(code.includes('favoriteOnly'))
    assert.ok(code.includes('仅收藏'))
  })

  test('有搜索框与翻页', () => {
    assert.ok(code.includes('v-model="keyword"'))
    assert.ok(code.includes('@submit.prevent="doSearch"'))
    assert.ok(code.includes('goPage(page - 1)'))
    assert.ok(code.includes('goPage(page + 1)'))
  })

  test('翻页时用服务端给的 total_pages，不用 items.length', () => {
    assert.ok(code.includes('body.total_pages'), '翻页总数没用服务端信封')
  })

  test('单页条数必须小于保留上限', () => {
    // 1000 是**保留**条数。把它当单页条数，浏览器会一次收一千行 ——
    // 所以判据不是「等于 20」（那只是把常量抄一份），而是「小于上限」。
    const m = code.match(/const PAGE_SIZE = (\d+)/)
    assert.ok(m, '找不到 PAGE_SIZE')
    const n = Number(m[1])
    assert.ok(n > 0 && n < 1000, `单页 ${n} 条：上限是保留 1000 条，单页不能也取 1000`)
  })

  test('每条记录有收藏星标，语义是「设为收藏」不是「翻转」', () => {
    assert.ok(code.includes('aria-pressed="item.is_favorite"'))
    assert.ok(code.includes('setHistoryFavorite(item.id, next)'), '收藏走的是翻转而不是幂等设置')
  })

  test('页头不再说「最近 30 条」', () => {
    assert.doesNotMatch(vue, /最近 30 条/, '上限已经不是 30 了')
  })

  test('故障与空状态是两条分支', () => {
    assert.ok(code.includes('v-else-if="loadError"'))
    assert.ok(code.includes('v-else-if="!items.length"'))
    assert.ok(
      code.indexOf('v-else-if="loadError"') < code.indexOf('v-else-if="!items.length"'),
      '故障分支必须排在空状态之前，否则一次 500 会被渲染成「暂无解析历史」',
    )
  })

  test('空状态区分「没有筛选命中」与「从来没有记录」', () => {
    assert.ok(code.includes("hasFilters ? '没有匹配的记录' : '暂无解析历史'"))
  })
})

// ── 删除双重保险 ──────────────────────────────────────────

describe('收藏项的删除有第二道确认', () => {
  test('第二道确认是页面上的一行，不是又弹一次 confirm', () => {
    assert.ok(code.includes('pendingDeleteId === item.id'), '收藏项没有被要求二次确认')
    assert.ok(code.includes('confirmDelete(item)'))
  })

  test('第一下不带 force，被拒之后才带 force', () => {
    assert.ok(code.includes('deleteHistory(item.id)'), '第一次删除没有不带 force 地试')
    assert.ok(code.includes('deleteHistory(item.id, { force: true })'))
  })

  // 这三条原来都只查 api **全文**包含某个子串 —— 同一个串出现在别的函数里
  // 一样算通过。deleteHistory 哪天不再翻译 409，只要文件别处碰巧有
  // `status === 409`，这三条仍然全绿。改成大括号配对抽真正的函数体。
  const deleteHistorySrc = () => extractFn(api, 'deleteHistory')

  test('服务端 409 被翻译成「被拦住」，不是抛异常', () => {
    const fn = deleteHistorySrc()
    assert.match(fn, /status === 409/, 'deleteHistory 里没有 409 分支')
    assert.match(fn, /refused: true/, '没有把 409 翻译成 refused')
    assert.ok(code.includes('if (res.refused)'), '组件没处理 refused，会直接当删除成功')
  })

  test('不是 409 的错误照常抛出', () => {
    assert.match(deleteHistorySrc(), /throw e/, '非 409 的失败被吞了')
  })

  test('收藏星标的失败不能被当成成功', () => {
    assert.ok(code.includes('await setHistoryFavorite(item.id, next)'))
    assert.ok(code.includes('starringId.value = item.id'))
  })

  test('清空走服务端一次性接口，不是前端循环删 N 次', () => {
    // 锚点跟着 api 层的新出口走（ADR 0032）：`client()` 那份复制品已经删掉，
    // 统一走 `http`。
    assert.ok(api.includes("http.delete('/api/history'"))
    assert.doesNotMatch(code, /Promise\.all\(items\.value\.map/, '又变回前端循环删了 —— 1000 条时那是 1000 个请求')
  })
})

// ── 列表响应形状 ──────────────────────────────────────────

describe('列表用的是完整信封', () => {
  test('fetchHistories 返回整个信封', () => {
    const fn = extractFn(api, 'fetchHistories')
    assert.match(fn, /return res\.data\b/, '只剩 items 就拿不到 total 了')
    // 原来用 `api.split('fetchHistoryFacets')[0]` 划范围，那是**位置型守卫**：
    // fetchHistoryFacets 一旦改名或挪位置，split 找不到就返回整份文件，
    // 断言退化成「全仓没有 return res.data.items」，观察范围悄悄放大而不报错。
    assert.doesNotMatch(fn, /return res\.data\.items\b/, 'fetchHistories 退回了只返回 items')
  })
})

// ── 未登录：给入口，不给错误码（工单 #95）────────────────────
//
// 这一页的记录全是私有的。未登录时它该说的是「登录后就能看到」，
// 而不是把服务端的 401 原样甩出来（错误码是写给开发者看的）。
//
// ⚠️ 下面两条是**接线**判据，行为判据在 `history-page.mount.spec.mjs`
// （真挂载，看 DOM 与发出的事件）。两边对着读：文本判据盯住「接线还在」，
// 挂载判据盯住「接线真的生效」。

describe('未登录的接线', () => {
  test('App.vue 的 history 分支接了 need-login → 弹登录框', () => {
    // ⚠️ 必须**按 history 那一段**断言，不能全文搜 'need-login'：
    // 社区页与首页各有一处同样的接线，全文搜的话把历史页那行删掉，
    // 这条断言照样绿 —— 而症状正是「点了去登录，登录框不开」。
    const historyCase = sliceBetween(
      read('../src/App.vue'),
      "case 'history':",
      "case 'community':",
      'App.vue 的 history 分支',
    )
    assert.match(
      historyCase,
      /'need-login':\s*(?:\(\)\s*=>\s*)?showAuthModal\('login'\)/,
      '历史页没接登录入口 —— 点「去登录」不会有任何反应',
    )
  })

  test('历史页模板里有「需要登录」这一块与去登录按钮', () => {
    assert.match(code, /v-else-if="needLogin"/, '模板里没有未登录这一支')
    assert.match(code, /\$emit\('need-login'\)/, '未登录提示没有去登录入口')
  })

  test('401 不进 loadError：它被翻译成 needLogin', () => {
    // 划到 load() 的函数体里判（不是全文搜）：别处出现同样的字样不算数。
    const fn = extractFn(code, 'load')
    assert.match(fn, /isUnauthorized\(e\)/, 'load() 没判 401')
    assert.match(fn, /needLogin\.value = true/, '401 没被翻译成 needLogin')
  })
})
