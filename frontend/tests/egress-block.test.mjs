/**
 * 出口维度失败的两类：平台按 IP 风控（412）、服务器连不到境外平台。
 *
 * 为什么单独测：这两种失败的症状和「链接贴错了」几乎一样，用户看到的
 * 提示如果落在「换个视频链接试试」那一档，就会一直换链接、白等。
 *
 * 每条都配了**反向对照**：本来不该管的失败必须保持原分类——
 * 那里转红比这里转红更严重（守卫越权）。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'

import { classifyError } from '../src/lib/errors.ts'

/** 后端 400 的错误体形状（detail 是 { success, error }）。 */
function backendError(message) {
  return { response: { data: { detail: { success: false, error: `解析失败: ${message}` } } } }
}

describe('出口被平台风控（412）', () => {
  test('B 站 412 → 说清不是链接的问题', () => {
    const r = classifyError(
      backendError('ERROR: [BiliBili] 1GJ411x7h7: Unable to download webpage: HTTP Error 412: Precondition Failed'),
    )
    assert.match(r.title, /出口/, '没有把「出口被拦」单独分类')
    assert.match(r.hint, /不是你的链接|不是链接/, '没排除「链接贴错了」这个可能')
    assert.doesNotMatch(r.hint, /换一个视频链接试试/, '还在让用户换链接——出口被拦时换链接没用')
  })

  test('平台在境外的地区限制 → 同一类', () => {
    const r = classifyError(
      backendError('ERROR: [youtube] dQw4w9WgXcQ: Video unavailable. This video is not available in your country'),
    )
    assert.match(r.title, /出口/)
  })

  test('反向对照：单个视频不可用仍归「链接不对」', () => {
    const r = classifyError(backendError('ERROR: Video unavailable. This video has been removed'))
    assert.match(r.title, /链接/)
    assert.doesNotMatch(r.title, /出口/, '单个视频被删被误判成出口被拦')
  })

  test('反向对照：自家限流仍归「请求太频繁」', () => {
    const r = classifyError({
      response: { data: { detail: { error: '请求过于频繁：服务器限流，请 42 秒后再试' } } },
    })
    assert.match(r.title, /频繁/)
    assert.doesNotMatch(r.title, /出口/, '自家限流被说成出口被拦')
  })
})

describe('服务器连不上境外平台', () => {
  test('unreachable → 说清断的是服务器到平台那一段', () => {
    const r = classifyError(
      backendError('ERROR: Unable to download webpage: <urlopen error [Errno 101] Network is unreachable>'),
    )
    assert.match(r.title, /服务器连不上/)
    assert.match(r.hint, /不是你的网络/, '没排除用户网络这一可能')
  })

  test('反向对照：用户侧超时仍归网络问题', () => {
    const r = classifyError({ message: 'timeout of 10000ms exceeded' })
    assert.match(r.title, /连不上/)
    assert.doesNotMatch(r.title, /平台/, '用户自己的超时被说成平台问题')
  })
})
