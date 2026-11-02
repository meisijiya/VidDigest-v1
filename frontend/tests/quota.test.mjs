/**
 * 额度展示契约。运行：node --test tests/
 *
 * 背景：额度拆成解析 / 对话两个独立计数器（工单 #4）。
 * 这组测试锁的是「用户能看到每个计数器的独立数字」——
 * 只有一个数字的话，拆分对用户就是不可见的。
 *
 * 种类清单与中文名由服务端随 payload 下发（`labels`，ADR 0033）。
 * 这组用例**故意带 labels**：前端不认种类名，认的是后端给的那张表。
 *
 * 测纯函数而不是 Vue 组件：组件要挂载环境，额度文案是纯展示逻辑，
 * 抽出来才能在没有构建工具的条件下被测。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { describeQuota, quotaBadgeClass, quotaRows } from '../src/lib/quota.ts'

//: 后端 `quota_kinds.labels()` 的形状。顺序即渲染顺序。
const LABELS = { parse: '解析', chat: '追问' }

// ⚠️ **顶层 eager 读**，不是 describe 体里（工单 #49）。
//
// 实测：放在 `describe('VideoSummary 组件接线', …)` 的第一行时，
// 把 VideoSummary.vue 改名 → 那 3 条用例**一条都没注册**（19 → 16），
// 而 `node --test` 的退出码**仍是 0** —— 门禁只读退出码，这个回归完全看不见。
//
// 原因：node 对 **describe 体里抛出的异常**给出的退出码是 0。
// 模块顶层抛则会让整个文件加载失败（退出码 1），响得多。
//
// 归一化行尾：仓库已统一为 LF，这一步只是防止将来混入 CRLF 时切函数体切空。
const videoSummarySource = readFileSync(new URL('../src/components/VideoSummary.vue', import.meta.url), 'utf8').replace(
  /\r\n/g,
  '\n',
)

describe('describeQuota', () => {
  test('未登录时不显示任何数字', () => {
    const text = describeQuota({ logged_in: false })
    assert.match(text, /登录/)
    assert.doesNotMatch(text, /\d+\s*\/\s*\d+/, '未登录不该显示成 "3 / 3"')
  })

  test('VIP 显示无限', () => {
    const text = describeQuota({
      logged_in: true,
      unlimited: true,
      labels: LABELS,
      parse: { remaining: -1, limit: 3 },
      chat: { remaining: -1, limit: 10 },
    })
    assert.match(text, /无限/)
  })

  test('同时显示解析与追问两个额度', () => {
    const text = describeQuota({
      logged_in: true,
      unlimited: false,
      labels: LABELS,
      parse: { remaining: 2, limit: 3 },
      chat: { remaining: 7, limit: 10 },
    })
    // 两个数字都要在，且各自的 remaining/limit 配对正确
    assert.match(text, /解析/)
    assert.match(text, /追问/)
    assert.match(text, /2\s*\/\s*3/, '解析额度应为 2 / 3')
    assert.match(text, /7\s*\/\s*10/, '追问额度应为 7 / 10')
    assert.ok(text.indexOf('解析') < text.indexOf('追问'), '顺序跟着 labels 走')
  })

  test('两个额度数字不串位', () => {
    // 解析用满、追问充足：两个数字必须各自独立，不能都显示成同一个
    const text = describeQuota({
      logged_in: true,
      unlimited: false,
      labels: LABELS,
      parse: { remaining: 0, limit: 3 },
      chat: { remaining: 9, limit: 10 },
    })
    assert.match(text, /0\s*\/\s*3/)
    assert.match(text, /9\s*\/\s*10/)
  })

  test('额度用完时明确说用完', () => {
    const text = describeQuota({
      logged_in: true,
      unlimited: false,
      labels: LABELS,
      parse: { remaining: 0, limit: 3 },
      chat: { remaining: 4, limit: 10 },
    })
    assert.match(text, /用完/)
  })

  test('信息缺失时给出占位而不是崩溃', () => {
    assert.equal(describeQuota(null), '—')
    assert.equal(describeQuota({ logged_in: true, labels: LABELS, parse: null, chat: null }), '—')
  })

  test('没有 labels 时退回总量文案，而不是猜种类名', () => {
    // 兼容期后端可能只给顶层 remaining/limit；不能因此渲染成 undefined，
    // 也不能退化成写死的中文名——那等于把注册表抄回前端。
    const text = describeQuota({
      logged_in: true,
      unlimited: false,
      remaining: 2,
      limit: 3,
    })
    assert.match(text, /2\s*\/\s*3/)
    assert.doesNotMatch(text, /解析|追问/, '没有 labels 就不该出现任何种类名')
    assert.doesNotMatch(text, /undefined|NaN/)
  })
})

/**
 * 额度行由 labels 驱动（ADR 0033）。
 *
 * 改造前 `describeQuota` / `quotaBadgeClass` 在 `src/lib/quota.ts` 里硬写
 * `quotaOf(q, 'parse')` 与 `'chat'`，后端加第三种额度时前端**安静地少显示一种**
 * 而不报任何错。现在种类与中文名都来自 payload 的 `labels`，多一种额度
 * 就多一行；没有 `labels` 时退回总量文案，不猜种类名。
 */
describe('额度行由 labels 驱动', () => {
  const base = {
    logged_in: true,
    unlimited: false,
    labels: LABELS,
    parse: { remaining: 2, limit: 3 },
    chat: { remaining: 7, limit: 10 },
  }

  test('payload 多出一种额度就多一行，前端不用改', () => {
    const withExport = {
      ...base,
      labels: { ...LABELS, export: '导出' },
      export: { remaining: 4, limit: 8 },
    }
    assert.deepEqual(
      quotaRows(withExport).map((r) => [r.key, r.label, r.text]),
      [
        ['parse', '解析', '2 / 3'],
        ['chat', '追问', '7 / 10'],
        ['export', '导出', '4 / 8'],
      ],
    )
    assert.match(describeQuota(withExport), /导出 4 \/ 8/)
  })

  test('labels 里有、槽位缺失的种类不出行', () => {
    const rows = quotaRows({ ...base, chat: undefined })
    assert.deepEqual(
      rows.map((r) => r.key),
      ['parse'],
      '槽位缺失时不出半行',
    )
  })

  test('没有 labels 时不出分种类的行，退回总量', () => {
    assert.deepEqual(quotaRows({ logged_in: true, parse: base.parse, chat: base.chat }), [])
    const text = describeQuota({
      logged_in: true,
      remaining: 2,
      limit: 3,
      parse: base.parse,
      chat: base.chat,
    })
    assert.match(text, /2\s*\/\s*3/)
    assert.doesNotMatch(text, /解析|追问/)
  })

  test('新增的额度用完时徽章转红', () => {
    const cls = quotaBadgeClass({
      ...base,
      labels: { ...LABELS, export: '导出' },
      export: { remaining: 0, limit: 8 },
    })
    assert.match(cls, /red/, '任一额度耗尽就该警示，不管它是第几种')
  })
})

describe('quotaBadgeClass', () => {
  test('未登录走中性色', () => {
    const cls = quotaBadgeClass({ logged_in: false })
    assert.match(cls, /gray/)
  })

  test('VIP 走琥珀色', () => {
    const cls = quotaBadgeClass({
      logged_in: true,
      unlimited: true,
      labels: LABELS,
      parse: { remaining: -1, limit: 3 },
      chat: { remaining: -1, limit: 10 },
    })
    assert.match(cls, /amber/)
  })

  test('任一额度用完即走红色', () => {
    const cls = quotaBadgeClass({
      logged_in: true,
      unlimited: false,
      labels: LABELS,
      parse: { remaining: 0, limit: 3 },
      chat: { remaining: 4, limit: 10 },
    })
    assert.match(cls, /red/)
  })

  test('都充足走蓝色', () => {
    const cls = quotaBadgeClass({
      logged_in: true,
      unlimited: false,
      labels: LABELS,
      parse: { remaining: 2, limit: 3 },
      chat: { remaining: 8, limit: 10 },
    })
    assert.match(cls, /blue/)
  })

  test('空数据不抛异常', () => {
    assert.doesNotThrow(() => quotaBadgeClass(null))
    assert.doesNotThrow(() => quotaBadgeClass({ logged_in: true }))
  })
})

/**
 * 组件接线守卫（静态检查，不是渲染测试）。
 *
 * 独立复审时发现：整套前端测试里对 .vue 的引用数为 0。也就是说
 * applyQuotaEvent 只要丢掉事件里的槽位，界面就会无声退回单数字，
 * 而纯函数测试照样全绿。
 *
 * 这里只做源码层面的存在性检查——它证明不了渲染结果，但能挡住
 * 「有人把某条流的 onQuota 摘掉 / 把事件里的槽位丢掉」这类接线回归。
 * 真正的渲染验证需要挂载环境 —— 那个环境**已经有了**（工单 #18 建的
 * vitest + jsdom + @vue/test-utils，走 `npm run test:mount`），
 * 本条仍留在文本断言是因为它守的是「函数体里必须整体展开事件」，
 * 挂载后看不到函数体。详见 TESTING-STRATEGY §3.5。
 */
describe('VideoSummary 组件接线', () => {
  // 源码在**模块顶层**已读进 videoSummarySource —— 这里不再读。
  const source = videoSummarySource

  test('applyQuotaEvent 整体展开事件而不是逐项挑字段', () => {
    const start = source.indexOf('function applyQuotaEvent(')
    assert.notEqual(start, -1, '找不到 applyQuotaEvent')
    // 用紧跟其后的顶层 "}\n" 收尾——切窄了会切到内层对象字面量的括号，
    // 那样下面的断言就成了空断言。
    const body = source.slice(start, source.indexOf('\n}\n', start) + 2)
    assert.match(body, /quotaInfo\.value\s*=\s*\{\s*\.\.\.d\b/, 'expand 事件本身，别逐项挑字段')
    assert.match(body, /logged_in:\s*true/, '写成 logged_in: true（事件不带这个标记）')
    assert.doesNotMatch(body, /parse:/, '又按种类名挑字段了——种类是服务端决定的')
  })

  test('总结流与追问流都把 quota 事件接到同一个处理器', () => {
    const hooks = source.match(/onQuota:\s*applyQuotaEvent/g) ?? []
    assert.equal(hooks.length, 2, `应有 2 条流接上 applyQuotaEvent，实际 ${hooks.length}`)
  })

  test('模板渲染的是 describeQuota 的结果', () => {
    assert.match(source, /\{\{\s*quotaLabel\s*\}\}/, '模板不再渲染 quotaLabel')
    assert.match(source, /:class="quotaBadgeClass"/, '模板不再绑定额度徽章样式')
  })
})
