/**
 * HTTP 出口：token 注入、401 判定、两种传输的统一入口。
 *
 * ## 为什么有这一个 module
 *
 * 之前凭据注入散在**五个**入口：`api/auth.ts` 的全局拦截器、`api/admin.ts` /
 * `api/community.ts` / `api/history.ts` 三份复制粘贴的 `client()`，以及
 * `api/summarize.ts` 里的三处原生 `fetch`。同一件事有五份写法，
 * 于是「这个请求带没带凭据」要读五个文件才能回答。
 *
 * 现在凭据只有一个出处：{@link http} / {@link httpFetch} / {@link httpStream}
 * 注入，{@link httpAnonymous} 显式不注入。调用点不再出现 token 逻辑。
 *
 * ## 两个出口而不是一个
 *
 * 公开端点**不该**带凭据——token 发到用不上的端点只是扩大泄露面。
 * 过去「公开端点不带 Authorization」靠的是副作用：`api/auth.ts` 被 import
 * 时才注册全局拦截器，而它是否被 import 取决于模块加载顺序。
 * 现在这条契约写进了类型：公开端点用 {@link httpAnonymous}。
 *
 * ## 两种传输
 *
 * `http` / `httpAnonymous` 走 axios（需要 `params`、`responseType: 'blob'`
 * 这类能力）；`httpFetch` / `httpStream` 走原生 `fetch`（SSE 要读响应流，
 * axios 的 XHR 路径拿不到 `res.body` 的 reader）。
 *
 * ## 为什么不是 `axios.create()`
 *
 * `axios.create()` 在**创建那一刻**就把 `axios.defaults` 拷走——adapter 与
 * baseURL 都在里面。Node 下的测试会在那之后改它们（把 adapter 换成 fetch、
 * 把 baseURL 指向假主机，好让请求不真的出网），实例上那份是旧的，请求就
 * 真的去解析域名了（`ERR_INVALID_URL` / `ENOTFOUND`）。
 *
 * 所以这里用**薄包装**而不是独立实例：请求时现读 `axios.defaults`，
 * 「实例创建之后 defaults 被改过」也不会走错。包装只有一层，
 * `http.get(...)` 的写法与 axios 一致。
 */

import axios from 'axios'
import type { AxiosRequestConfig, AxiosResponse } from 'axios'

/** token 的落盘键。**唯一**真值出口，别处不要自己拼这个字符串。 */
const TOKEN_KEY = 'auth_token'

/**
 * 当前 token（未登录为 null）。
 *
 * localStorage 只是**种子**，不是安全边界——服务端才是权威（ADR 0010）。
 */
export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

/** 本地有没有 token。注意：这不代表服务端认它。 */
export function isLoggedIn(): boolean {
  return !!getToken()
}

/** 落盘 token。登录成功时调一次。 */
export function saveToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

/** 清掉 token。登出时调一次。 */
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

/**
 * Authorization 头。没有 token 就不发这个头——**不发空的 Bearer**。
 *
 * 空 Bearer 与不发的区别是服务端会把它当成「一个无效凭据」而不是
 * 「没带凭据」，两者的拒绝路径不一样。
 */
function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * 这个失败是不是「服务端说这个 token 不认了」（401）。
 *
 * 只看状态码，不看错误文案：文案是给用户看的，改一次就飘一次。
 *
 * 认两种形状：axios 的错误对象（状态码挂在 `response.status` 上）与
 * 原生 fetch 常见的 `{ status }`。三处调用点原来各抄了一份
 * axios 形状的判据，现在只有这一份。
 */
export function isUnauthorized(err: unknown): boolean {
  const e = err as { response?: { status?: number }; status?: number } | null
  return e?.response?.status === 401 || e?.status === 401
}

/**
 * 一个 axios 出口。方法名与签名跟 axios 一致，调用点写法不变。
 *
 * 泛型默认 `any`（与 axios 一致）：api 层拿到的 `res.data` 照旧是不带约束的，
 * 与收口之前完全一样。给全仓 api 调用点补类型义务是另一件事（响应的形状由
 * 服务端契约给出，前端各处另有形状检查），不在本次收口的范围内。
 */
/* eslint-disable @typescript-eslint/no-explicit-any -- 见上面那段说明：与 axios 的默认泛型一致，响应体形状由服务端契约给出 */
export interface HttpClient {
  get<T = any>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>
  post<T = any>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>
  patch<T = any>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>
  delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>>
}
/* eslint-enable @typescript-eslint/no-explicit-any */

/** 带上凭据的请求配置。调用方自己给的头优先（它更具体）。 */
function withAuth(config?: AxiosRequestConfig): AxiosRequestConfig {
  return {
    ...config,
    headers: { ...authHeaders(), ...(config?.headers ?? {}) },
  }
}

/**
 * 带凭据的 axios 出口。
 *
 * token 在**请求发生时**读，不是在模块加载时读——登录发生在加载之后，
 * 绑在那之前的 token 会一直是旧的。
 */
export const http: HttpClient = {
  get: (url, config) => axios.get(url, withAuth(config)),
  post: (url, data, config) => axios.post(url, data, withAuth(config)),
  patch: (url, data, config) => axios.patch(url, data, withAuth(config)),
  delete: (url, config) => axios.delete(url, withAuth(config)),
}

/**
 * 匿名 axios 出口：**不带** Authorization。
 *
 * 给公开端点用（模型清单、注册、登录）。用错的后果是把 token 发去用不上的
 * 地方——所以公开端点请显式选它，而不是「默认那个」。
 */
export const httpAnonymous: HttpClient = {
  get: (url, config) => axios.get(url, config),
  post: (url, data, config) => axios.post(url, data, config),
  patch: (url, data, config) => axios.patch(url, data, config),
  delete: (url, config) => axios.delete(url, config),
}

/** 一次 fetch 的公共实现：按需注入凭据。 */
async function request(path: string, init: RequestInit = {}, withAuth = true): Promise<Response> {
  const headers = {
    ...(withAuth ? authHeaders() : {}),
    ...(init.headers ?? {}),
  }
  return await fetch(path, { ...init, headers })
}

/**
 * 普通 fetch（JSON 响应）。带凭据。
 *
 * 调用点自己判断 `res.ok` —— fetch 不会像 axios 那样对 4xx/5xx 抛异常。
 */
export function httpFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return request(path, init)
}

/**
 * 流式 fetch（SSE）。带凭据。
 *
 * 与 {@link httpFetch} 的差别只有**语义**：响应体要当流读
 * （`res.body.getReader()`），取消靠 `init.signal`（原样透传给 fetch）。
 * 单独给它一个名字，是为了让「这里必须流式读」在调用点一眼可见。
 */
export function httpStream(path: string, init: RequestInit = {}): Promise<Response> {
  return request(path, init)
}
