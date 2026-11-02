// 前端接线守卫的变异核验：每条变异都必须让 npm test 转红。
// 按字节读写（仓库是 CRLF，文本模式会污染全文行尾符）。
//
// 用法：node tests/mutation_wiring.mjs
// 放在 tests/ 下但文件名不带 .test.mjs —— npm test 只收 *.test.mjs，
// 不会把这个脚本自己收进去递归。
import { readFileSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const VUE = new URL('../src/components/VideoSummary.vue', import.meta.url)

// nth: 第几次出现（1 起）。两条 SSE 流的 onQuota 文本完全相同，
// 只靠字符串匹配区分不了「总结那条」和「追问那条」。
//
// W1 / W2 的锚点是 `applyQuotaEvent` 里那一行整体展开（ADR 0033 之后
// 前端不再逐项挑字段：槽位与中文名都由服务端给）。原来那两条锚点写的是
// `chat: d.chat ?? null,` / `parse: d.parse ?? null,`——那两行在 ADR 0033
// 之后已经不存在，锚点失配，变异「未命中」而不是「存活」：装置坏了，
// 不是守卫有牙。变异语义保持不变：把展开改成逐项挑字段，丢掉其中一种额度。
const MUTATIONS = [
  [
    'W1 applyQuotaEvent 丢掉 chat',
    'quotaInfo.value = { ...d, logged_in: true, unlimited: !!d.unlimited }',
    'quotaInfo.value = { parse: d.parse, labels: d.labels, logged_in: true, unlimited: !!d.unlimited }',
  ],
  [
    'W2 applyQuotaEvent 丢掉 parse',
    'quotaInfo.value = { ...d, logged_in: true, unlimited: !!d.unlimited }',
    'quotaInfo.value = { chat: d.chat, labels: d.labels, logged_in: true, unlimited: !!d.unlimited }',
  ],
  ['W3 追问流摘掉 onQuota 接线', 'onQuota: applyQuotaEvent,', 'onQuota: null,', 2],
  ['W4 模板不再渲染 quotaLabel', '{{ quotaLabel }}', '{{ quotaText }}'],
  ['W5 模板不再绑定额度徽章样式', ':class="quotaBadgeClass"', ':class="quotaBadgeClassX"'],
]

const original = readFileSync(VUE, 'utf8')
const normalized = original.replace(/\r\n/g, '\n')
let failed = false

function applyMutation(text, pattern, repl, nth) {
  if (nth) {
    let idx = -1
    for (let i = 0; i < nth; i += 1) {
      idx = text.indexOf(pattern, idx + 1)
      if (idx === -1) return null
    }
    return text.slice(0, idx) + repl + text.slice(idx + pattern.length)
  }
  return text.includes(pattern) ? text.replace(pattern, repl) : null
}

try {
  for (const [name, pattern, repl, nth] of MUTATIONS) {
    const mutated = applyMutation(normalized, pattern, repl, nth)
    if (mutated === null) {
      console.log(`  [未命中] ${name} —— 模式没匹配上，变异无效`)
      failed = true
      continue
    }
    writeFileSync(VUE, mutated.replace(/\n/g, '\r\n'), 'utf8')
    const r = spawnSync('npm', ['test'], { cwd: ROOT, encoding: 'utf8', shell: true })
    writeFileSync(VUE, original, 'utf8')
    if (r.status === 0) {
      console.log(`  [存活] ${name}  <-- 测试守不住`)
      failed = true
    } else {
      console.log(`  [杀死] ${name}`)
    }
  }
} finally {
  writeFileSync(VUE, original, 'utf8')
}

const restored = readFileSync(VUE, 'utf8') === original
console.log(`\n还原自检: ${restored ? 'VideoSummary.vue 逐字节一致' : '还原失败！'}`)
process.exit(failed || !restored ? 1 : 0)
