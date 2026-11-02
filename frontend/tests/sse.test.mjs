/**
 * SSE 读取契约。运行：node --test tests/
 * 覆盖：跨 chunk 事件名、终止回调保证、用户主动取消、UTF-8 分片、脏数据。
 */
import { test, describe, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { classifySseError, createSseParser, chatWithVideo, fetchQuota, summarizeVideo } from '../src/api/summarize.ts'

/** 造一个能响应 AbortSignal 的假流，行为对齐真实 fetch + ReadableStream。 */
function fakeStream(chunks, { signal, close = true } = {}) {
  const enc = new TextEncoder()
  let controller
  const body = new ReadableStream({
    start(c) {
      controller = c
      for (const ch of chunks) c.enqueue(enc.encode(ch))
      if (close) c.close()
    },
  })
  if (signal) {
    if (signal.aborted) controller.error(new DOMException('Aborted', 'AbortError'))
    else signal.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')))
  }
  return new Response(body)
}

let calls

beforeEach(() => {
  calls = { fetch: [] }
  globalThis.localStorage = {
    _v: { auth_token: 'test-token' },
    getItem(k) {
      return this._v[k] ?? null
    },
    setItem(k, v) {
      this._v[k] = String(v)
    },
    removeItem(k) {
      delete this._v[k]
    },
  }
})

/** 收集一次流式调用的全部回调触发情况 */
function recorder() {
  const log = { events: [], errors: [], cancelled: 0, done: 0 }
  let wake
  const gotOne = new Promise((r) => {
    wake = r
  })
  const push = (e) => {
    log.events.push(e)
    wake()
  }
  return {
    log,
    /** 等第一个事件到达 —— 用于「流保持打开」的场景，不能靠 await done */
    first: () => gotOne,
    cb: {
      onSubtitle: (d) => push(['subtitle', d]),
      onSummary: (t) => push(['summary', t]),
      onMindmap: (d) => push(['mindmap', d.markdown]),
      onQuota: (d) => push(['quota', d]),
      onAnswer: (t) => push(['answer', t]),
      onError: (e) => log.errors.push(e.message),
      onCancel: () => {
        log.cancelled++
      },
      onDone: () => {
        log.done++
      },
    },
  }
}

// 每个测试都有上限：死锁要变成失败，而不是把整个套件挂住
const LIMIT = { timeout: 5000 }

const FULL_STREAM = [
  'event: subtitle\n',
  'data: {"full_text":"hello","has_subtitle":true}\n',
  'event: quota\n',
  'data: {"remaining":2,"limit":3,"unlimited":false}\n',
  'event: summary\n',
  'data: "tok1"\n',
  'event: summary\n',
  'data: "tok2"\n',
  'event: mindmap\n',
  'data: {"markdown":"# x"}\n',
  'event: done\n',
  'data: [DONE]\n',
]

describe('createSseParser', () => {
  test('事件名跨 chunk 边界保持', () => {
    const p = createSseParser()
    const got = []
    for (const chunk of ['event: subtitle\n', 'data: {"a":1}\n', 'event: mindmap\n', 'data: {"b":2}\n']) {
      for (const line of p.feed(chunk)) {
        const ev = p.take(line)
        if (ev) got.push([ev.event, ev.data])
      }
    }
    assert.deepEqual(got, [
      ['subtitle', { a: 1 }],
      ['mindmap', { b: 2 }],
    ])
  })

  test('[DONE] 映射为 done 事件', () => {
    const p = createSseParser()
    const [line] = p.feed('data: [DONE]\n')
    assert.deepEqual(p.take(line), { event: 'done', data: null })
  })

  test('畸形 JSON 原样透传而不抛', () => {
    const p = createSseParser()
    const lines = p.feed('event: summary\ndata: {broken\n')
    p.take(lines[0])
    assert.deepEqual(p.take(lines[1]), { event: 'summary', data: '{broken' })
  })

  test('注释行与非事件行被忽略', () => {
    const p = createSseParser()
    const lines = p.feed(': ping\n\nid: 7\n')
    assert.equal(p.take(lines[0]), null)
    assert.equal(p.take(lines[1]), null)
    assert.equal(p.take(lines[2]), null)
  })
})

describe('summarizeVideo', () => {
  test('正常流：事件全部路由，onDone 恰好一次', async () => {
    globalThis.fetch = async () => fakeStream(FULL_STREAM)
    const r = recorder()
    await summarizeVideo('u', 'zh', r.cb).done
    assert.deepEqual(
      r.log.events.map((e) => e[0]),
      ['subtitle', 'quota', 'summary', 'summary', 'mindmap'],
    )
    assert.deepEqual(r.log.events[1][1], { remaining: 2, limit: 3, unlimited: false })
    assert.equal(r.log.done, 1)
    assert.deepEqual(r.log.errors, [])
  })

  test('断流（无 [DONE]）：onDone 仍然触发，UI 不会卡在 loading', async () => {
    globalThis.fetch = async () => fakeStream(['event: summary\n', 'data: "partial"\n'])
    const r = recorder()
    await summarizeVideo('u', 'zh', r.cb).done
    assert.equal(r.log.events.length, 1)
    assert.equal(r.log.done, 1, '缺 [DONE] 时 onDone 必须兜底')
  })

  test('传输异常：onError 与 onDone 都触发', async () => {
    globalThis.fetch = async () => {
      throw new Error('network down')
    }
    const r = recorder()
    await summarizeVideo('u', 'zh', r.cb).done
    assert.deepEqual(r.log.errors, ['network down'])
    assert.equal(r.log.done, 1, '异常路径也必须复位 loading')
  })

  test('用户主动取消：走 onCancel，不当错误，onDone 仍触发', LIMIT, async () => {
    globalThis.fetch = async (url, opts) =>
      fakeStream(['event: summary\n', 'data: "partial"\n'], { signal: opts.signal, close: false })
    const r = recorder()
    const stream = summarizeVideo('u', 'zh', r.cb)
    await r.first() // 流保持打开，done 不会 resolve —— 等首个事件而不是等 done
    stream.cancel()
    await stream.done
    assert.equal(r.log.cancelled, 1)
    assert.deepEqual(r.log.errors, [], '主动取消不应被当成错误展示')
    assert.equal(r.log.done, 1, '取消后仍要复位 loading')
  })

  test('重复 cancel 幂等', LIMIT, async () => {
    globalThis.fetch = async (url, opts) =>
      fakeStream(['event: summary\n', 'data: "a"\n'], { signal: opts.signal, close: false })
    const r = recorder()
    const stream = summarizeVideo('u', 'zh', r.cb)
    await r.first()
    stream.cancel()
    stream.cancel()
    stream.cancel()
    await stream.done
    assert.equal(r.log.cancelled, 1, 'onCancel 只触发一次')
    assert.equal(r.log.done, 1, 'onDone 恰好一次')
  })

  test('已结束后 cancel 不产生取消回调', async () => {
    globalThis.fetch = async () => fakeStream(FULL_STREAM)
    const r = recorder()
    const stream = summarizeVideo('u', 'zh', r.cb)
    await stream.done
    stream.cancel()
    assert.equal(r.log.cancelled, 0)
    assert.equal(r.log.done, 1)
  })

  test('多字节 UTF-8 在字节层面被劈开时不损坏', async () => {
    const enc = new TextEncoder()
    const prefix = enc.encode('event: subtitle\n')
    const payload = enc.encode('data: {"t":"中文标题"}\n')
    const cut = Math.floor(payload.length / 2)
    const ch1 = Buffer.concat([prefix, payload.subarray(0, cut)])
    const ch2 = payload.subarray(cut)
    globalThis.fetch = async () =>
      new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(new Uint8Array(ch1))
            c.enqueue(new Uint8Array(ch2))
            c.close()
          },
        }),
      )
    const r = recorder()
    await summarizeVideo('u', 'zh', r.cb).done
    assert.equal(r.log.events[0][1].t, '中文标题')
  })

  test('鉴权头带上 token', async () => {
    globalThis.fetch = async (url, opts) => {
      calls.fetch.push(opts)
      return fakeStream(FULL_STREAM)
    }
    const r = recorder()
    await summarizeVideo('u', 'zh', r.cb).done
    assert.equal(calls.fetch[0].headers.Authorization, 'Bearer test-token')
    assert.ok(calls.fetch[0].signal, '必须传 signal，否则无法取消')
  })
})

describe('chatWithVideo', () => {
  test('请求体不再带字幕全文（工单 #8）', async () => {
    let body = null
    globalThis.fetch = async (url, opts) => {
      body = JSON.parse(opts.body)
      return fakeStream(['event: done\n', 'data: [DONE]\n'])
    }
    const r = recorder()
    await chatWithVideo('u', 'q', r.cb).done
    assert.deepEqual(body, { url: 'u', question: 'q' })
  })

  test('断流也保证 onDone', async () => {
    globalThis.fetch = async () => fakeStream(['event: answer\n', 'data: "a"\n', 'data: "b"\n'])
    const r = recorder()
    await chatWithVideo('u', 'q', r.cb).done
    assert.deepEqual(r.log.events, [
      ['answer', 'a'],
      ['answer', 'b'],
    ])
    assert.equal(r.log.done, 1)
  })

  test('quota 事件在问答里也被路由', async () => {
    globalThis.fetch = async () =>
      fakeStream([
        'event: quota\n',
        'data: {"remaining":2,"limit":3,"unlimited":false}\n',
        'event: answer\n',
        'data: "x"\n',
        'event: done\n',
        'data: [DONE]\n',
      ])
    const r = recorder()
    await chatWithVideo('u', 'q', r.cb).done
    assert.deepEqual(r.log.events[0], ['quota', { remaining: 2, limit: 3, unlimited: false }])
  })

  test('取消生效', LIMIT, async () => {
    globalThis.fetch = async (url, opts) =>
      fakeStream(['event: answer\n', 'data: "a"\n'], { signal: opts.signal, close: false })
    const r = recorder()
    const stream = chatWithVideo('u', 'q', r.cb)
    await r.first()
    stream.cancel()
    await stream.done
    assert.equal(r.log.cancelled, 1)
    assert.equal(r.log.done, 1)
  })
})

describe('fetchQuota', () => {
  test('返回后端额度并带鉴权头', async () => {
    const seen = {}
    globalThis.fetch = async (url, opts) => {
      seen.url = url
      seen.auth = opts.headers.Authorization
      return new Response(JSON.stringify({ logged_in: true, unlimited: false, remaining: 2, limit: 3 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    const q = await fetchQuota()
    assert.equal(seen.url, '/api/quota')
    assert.equal(seen.auth, 'Bearer test-token')
    assert.deepEqual(q, { logged_in: true, unlimited: false, remaining: 2, limit: 3 })
  })

  test('非 2xx 时抛错，让调用方降级', async () => {
    globalThis.fetch = async () => new Response('nope', { status: 401 })
    await assert.rejects(() => fetchQuota())
  })
})

/**
 * `error` 事件的判类（ADR 0034）。
 *
 * 改造前这里是 `err.message.includes('没有可用的字幕')`——匹配中文文案。
 * 而 `_subtitle_failure` 会产出三种前半句，那条 `includes` 只接得住第三种，
 * 另外两种（「平台提供了字幕但没能取下来」「该视频没有字幕」）会被当成
 * 普通错误显示。下面第一条用例就是拿这两种文案跑的。
 */
describe('classifySseError', () => {
  test('无字幕的三种文案都判成 no_subtitle（改造前只接得住第三种）', () => {
    const cases = [
      ['该视频没有字幕，无法生成总结', 'no_subtitle_track'],
      ['平台提供了字幕但没能取下来，无法生成总结', 'subtitle_fetch_failed'],
      ['该视频没有可用的字幕，服务端未配置语音识别，无法生成总结', 'no_subtitle_track'],
    ]
    for (const [message, reason] of cases) {
      assert.equal(classifySseError({ message, reason }), 'no_subtitle', `${message} 没被判成无字幕`)
    }
  })

  test('反向对照：消息里带「字幕」字样但 reason 为空，仍是 other', () => {
    // 判类看 reason，不看文案。否则这条守卫会随着一句文案的改写而越权。
    assert.equal(classifySseError({ message: '字幕提取服务暂时不可用，请稍后重试', reason: '' }), 'other')
  })

  test('ASR 兜底没救回来也属于无字幕', () => {
    assert.equal(classifySseError({ message: 'x', reason: 'asr_failed' }), 'no_subtitle')
    assert.equal(classifySseError({ message: 'x', reason: 'asr_not_configured' }), 'no_subtitle')
  })

  test('reason 缺失但 asr_reason 有值（旧后端形状）仍判无字幕', () => {
    assert.equal(classifySseError({ message: 'x', reason: '', asr_reason: 'asr_failed' }), 'no_subtitle')
  })

  test('额度耗尽单独一类', () => {
    assert.equal(classifySseError({ message: '今日次数已用完', reason: 'quota_exhausted' }), 'quota_exhausted')
  })

  test('未知 reason 落进 other，不静默当成成功', () => {
    assert.equal(classifySseError({ message: 'x', reason: 'brand_new_reason' }), 'other')
    assert.equal(classifySseError({ message: 'x' }), 'other')
  })
})
