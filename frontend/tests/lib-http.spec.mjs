/**
 * lib/http：凭据注入与 401 判定。
 *
 * ## 这个接缝在哪
 *
 * `frontend/src/lib/http.ts` 的三个出口（`http` / `httpFetch` / `httpStream`）
 * 与一个匿名出口（`httpAnonymous`）。断言都从这层出发：外面的人只该知道
 * 「带凭据的请求」与「不带凭据的请求」的区别，不该知道 token 存在哪。
 *
 * ## 为什么这里替身是**传输**，不是实现
 *
 * 判据是「这个请求带没带凭据」——那是请求**出去时**的事实，所以要真的
 * 发一次请求、看它出去的形状。替掉传输（fetch）而不是替掉 axios 实例：
 * 替掉 axios 会把「拦截器有没有注入」这件事一起替掉，那正是要验的东西。
 *
 * ## 为什么这里改的是 `axios.defaults`，而不是替身
 *
 * 替掉传输（fetch）而不是替掉 axios：替掉 axios 会把「凭据有没有注入」
 * 这件事一起替掉，那正是要验的东西。而 adapter 与 baseURL 必须在
 * **请求发出之前**设好——lib/http 的出口是薄包装，请求时才现读
 * `axios.defaults`，所以这里设完就能用，不需要动态 import。
 */

import { describe, test, expect, beforeEach, afterEach } from 'vitest'
import axios from 'axios'

import {
  http,
  httpAnonymous,
  httpFetch,
  httpStream,
  isUnauthorized,
  getToken,
  saveToken,
  clearToken,
  isLoggedIn,
} from '../src/lib/http.ts'

// axios 在 Node 里默认走 http adapter（会真的去解析域名）。换成 fetch，
// 下面的 globalThis.fetch 假实现才拦得住。
axios.defaults.adapter = 'fetch'
// 相对路径 '/api/...' 在 Node 里是 ERR_INVALID_URL。
axios.defaults.baseURL = 'http://lib-http.test'

/**
 * 假 fetch：记下每次请求的形状，然后回一个 200。
 *
 * axios 的 fetch adapter 直接传一个 **Request 对象**，而手写 fetch 是
 * `(url, init)`——两种形状都要认。头在 Request 上是**小写**的，用
 * `'Authorization' in headers` 去问永远问不到，那条断言恒真，等于没断言。
 */
function spyFetch() {
  const calls = []
  globalThis.fetch = async (input, init = {}) => {
    const asRequest = input instanceof Request
    calls.push({
      url: asRequest ? input.url : String(input),
      auth: asRequest ? input.headers.get('authorization') : (init.headers?.Authorization ?? null),
      signal: init.signal ?? (asRequest ? input.signal : null),
    })
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  return calls
}

let originalFetch

beforeEach(() => {
  originalFetch = globalThis.fetch
  localStorage.clear()
})

afterEach(() => {
  globalThis.fetch = originalFetch
  localStorage.clear()
})

describe('凭据注入：带 token 的出口', () => {
  test('http 带上 Bearer —— 且是**请求时**读的 token', async () => {
    const calls = spyFetch()
    // 登录发生在实例创建之后：绑在创建那一刻的 token 会一直是旧的。
    saveToken('token-abc')

    await http.get('/api/history')

    expect(calls.length).toBe(1)
    expect(calls[0].auth).toBe('Bearer token-abc')
  })

  test('没有 token 就不发这个头（连空的 Bearer 都不发）', async () => {
    const calls = spyFetch()

    await http.get('/api/history')

    expect(calls.length).toBe(1)
    expect(calls[0].auth).toBeNull()
  })

  test('httpFetch 带上 Bearer', async () => {
    const calls = spyFetch()
    saveToken('token-abc')

    await httpFetch('/api/quota')

    expect(calls[0].auth).toBe('Bearer token-abc')
  })

  test('httpStream 带上 Bearer，并把 signal 原样透传', async () => {
    const calls = spyFetch()
    saveToken('token-abc')
    const controller = new AbortController()

    await httpStream('/api/summarize', { method: 'POST', signal: controller.signal })

    expect(calls[0].auth).toBe('Bearer token-abc')
    // 取消靠 signal：透传丢了它，「用户点停止」就停不下来。
    expect(calls[0].signal).toBe(controller.signal)
  })
})

describe('匿名出口：公开端点不带凭据', () => {
  test('httpAnonymous 即使有 token 也不带 Authorization', async () => {
    const calls = spyFetch()
    saveToken('token-abc')

    await httpAnonymous.get('/api/models')

    expect(calls.length).toBe(1)
    expect(calls[0].auth).toBeNull()
  })

  test('匿名与带凭据是**两个**出口，不是同一个出口的两种参数', () => {
    // 这条守的是形状：将来有人把「匿名」改成 http.get(url, { auth: false })，
    // 契约就散进每个调用点，又回到「靠人记得别带凭据」的状态。
    expect(http).not.toBe(httpAnonymous)
  })
})

describe('401 判定只有一份', () => {
  test('axios 形状：状态码挂在 response.status 上', () => {
    expect(isUnauthorized({ response: { status: 401 } })).toBe(true)
  })

  test('原生形状：status 直接挂在错误对象上', () => {
    expect(isUnauthorized({ status: 401 })).toBe(true)
  })

  test('别的状态码不算「token 不认了」', () => {
    // 5xx 是「问不着」，不是「被拒」——把它当成 401 会把人踢下线。
    expect(isUnauthorized({ response: { status: 500 } })).toBe(false)
    expect(isUnauthorized({ response: { status: 403 } })).toBe(false)
    expect(isUnauthorized(null)).toBe(false)
    expect(isUnauthorized(undefined)).toBe(false)
  })
})

describe('token 的落盘', () => {
  test('存进去能读出来，读得出就算登录了', () => {
    expect(isLoggedIn()).toBe(false)
    saveToken('token-abc')
    expect(getToken()).toBe('token-abc')
    expect(isLoggedIn()).toBe(true)
  })

  test('清掉之后就没有了', () => {
    saveToken('token-abc')
    clearToken()
    expect(getToken()).toBeNull()
    expect(isLoggedIn()).toBe(false)
  })
})
