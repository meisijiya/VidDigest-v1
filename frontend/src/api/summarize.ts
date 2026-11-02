/**
 * SSE 流读取。
 *
 * 之前 summarizeVideo / chatWithVideo 各自内联了一份 30 行的读取循环，
 * 两份各有一份自己的 `currentEvent` —— 而它被声明在 while 循环体内，
 * 跨 chunk 时会丢事件名；同时 onDone 只在收到 [DONE] 时触发，
 * 断流/异常时前端永远等不到，loading 卡死。
 */

import { httpFetch, httpStream } from '../lib/http.ts'
import type { QuotaPayload } from '../lib/quota.ts'

/**
 * 一条 SSE 事件。
 *
 * `data` 是 `unknown`：它来自 `JSON.parse`，运行时没有任何东西校验过它的
 * 形状。**这里不假装它是可信的**——真正给出形状的是下面那几组
 * `*Payload` 接口，它们描述的是**后端约定**（见 `backend/api_summarize.py`），
 * 调用方拿到的是已经被这里按事件名分派过的具体类型。
 */
export interface SseEvent {
  event: string
  data: unknown
}

/** `ownership` 事件：这份内容当前用户能不能改写（ADR 0007）。 */
export interface OwnershipPayload {
  can_regenerate: boolean
}

/** 字幕分段。 */
export interface SubtitleSegment {
  start: number
  end: number
  text: string
}

/** `subtitle` 事件。回放时 `segments` 为空（社区表只存全文）。 */
export interface SubtitlePayload {
  has_subtitle: boolean
  full_text: string
  segments: SubtitleSegment[]
}

/** `mindmap` 事件。 */
export interface MindmapPayload {
  markdown: string
}

/**
 * `error` 事件。后端六个键**总是出现**（`backend/sse_events.py`，ADR 0034），
 * 这里仍全部写成可选：本仓前端可能对着旧后端跑，而缺字段要能降级读。
 */
export interface SseErrorPayload {
  message: string
  need_login?: boolean
  need_vip?: boolean
  /** 机器可读的原因。空串表示「没有细类」。 */
  reason?: SseErrorReason
  asr_reason?: string
  byok?: boolean
}

/**
 * `reason` 的取值表——后端 `summarizer.FAIL_*` 与 `sse_events.QUOTA_EXHAUSTED`
 * 的镜像。两边各一份是有意的：Python 常量不能被 TS 直接 import，
 * 而前端若只写字符串字面量比较，拼错与新增取值都不会有人提示。
 */
export type SseErrorReason =
  'no_subtitle_track' | 'subtitle_fetch_failed' | 'asr_not_configured' | 'asr_failed' | 'quota_exhausted' | ''

/** 前端真正要分开处理的三类。 */
export type SseErrorClass = 'no_subtitle' | 'quota_exhausted' | 'other'

/**
 * 按 `reason` 判类。
 *
 * 改造前这里是 `err.message.includes('没有可用的字幕')`——匹配中文文案，
 * 而 `_subtitle_failure` 会产出三种前半句，那条 `includes` 只接得住第三种。
 * **未知 reason 一律落进 `other`**：宁可照旧显示消息，也不静默当成成功。
 */
export function classifySseError(err: SseErrorPayload): SseErrorClass {
  switch (err.reason ?? '') {
    // 字幕四类都是「没有可用字幕」：ASR 兜底失败也属于同一件事，只是多一层原因
    case 'no_subtitle_track':
    case 'subtitle_fetch_failed':
    case 'asr_not_configured':
    case 'asr_failed':
      return 'no_subtitle'
    case 'quota_exhausted':
      return 'quota_exhausted'
    default:
      // reason 缺失时靠 asr_reason 兜一次（旧后端的形状）
      return (err.asr_reason ?? '') === '' ? 'other' : 'no_subtitle'
  }
}

/**
 * SSE 行解析器。状态（未消费的半行 + 当前事件名）必须跨 feed 保持，
 * 因为 `event: x` 和 `data: {...}` 常落在不同的 chunk 里。
 */
export function createSseParser() {
  let buffer = ''
  let currentEvent = ''

  return {
    /** 喂入一段文本，返回本次已完整的行（最后半行留在 buffer 里等下个 chunk） */
    feed(text: string): string[] {
      buffer += text
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      return lines
    },

    /** 消费一行。返回 { event, data }，非事件行返回 null */
    take(line: string): SseEvent | null {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim()
        return null
      }
      if (!line.startsWith('data: ')) return null

      const data = line.slice(6).trim()
      if (data === '[DONE]') return { event: 'done', data: null }
      try {
        return { event: currentEvent, data: JSON.parse(data) }
      } catch {
        return { event: currentEvent, data } // 非 JSON 负载原样透传
      }
    },
  }
}

/** `streamSse` 的行为开关。 */
interface StreamOptions {
  /** 按事件名分派负载。 */
  route: (event: string, data: unknown) => void
  onError?: (error: SseErrorPayload) => void
  onCancel?: () => void
  onDone?: () => void
}

/** 一条 SSE 流的句柄。 */
export interface SseStream {
  /** 结束的 Promise。断流 / 异常 / 取消都会让它 settle。 */
  done: Promise<void>
  /** 用户主动停止。 */
  cancel: () => void
}

/**
 * 读一个 SSE 端点。
 * onDone 保证被调用恰好一次：正常收到 [DONE]、流自然结束、抛异常、用户取消，四条路径都会走到。
 * 返回 { done, cancel }：done 是结束的 Promise，cancel 是用户主动停止。
 */
function streamSse(
  path: string,
  payload: Record<string, unknown>,
  { route, onError, onCancel, onDone }: StreamOptions,
): SseStream {
  const parser = createSseParser()
  const controller = new AbortController()
  let cancelled = false
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    onDone?.()
  }

  const done = (async () => {
    try {
      const res = await httpStream(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })

      if (!res.body) {
        throw new Error('响应没有可读流')
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()

      while (true) {
        const { done: ended, value } = await reader.read()
        if (ended) break

        for (const line of parser.feed(decoder.decode(value, { stream: true }))) {
          const ev = parser.take(line)
          if (!ev) continue
          if (ev.event === 'done') finish()
          else route(ev.event, ev.data)
        }
      }
    } catch (err) {
      // 主动取消不算错误：走 onCancel，让调用方区分「用户停了」和「出错了」
      const e = err as { name?: string; message?: string }
      if (cancelled || e?.name === 'AbortError') onCancel?.()
      else
        onError?.({
          message: e?.message || '未知错误',
          need_login: false,
          need_vip: false,
          reason: '',
          asr_reason: '',
          byok: false,
        })
    } finally {
      finish() // 断流 / 异常 / 取消都收尾，UI 不会永久停在 loading
    }
  })()

  return {
    done,
    cancel() {
      if (finished) return
      cancelled = true
      controller.abort()
    },
  }
}

/**
 * 解析事件对应的回调名。
 *
 * 这张表是「事件名 → 前端回调」的**唯一**映射：前端不按事件名 if/else
 * 拼一遍，后端加一个事件时这里加一行就够。
 */
const SUMMARY_ROUTES = {
  ownership: 'onOwnership',
  subtitle: 'onSubtitle',
  summary: 'onSummary',
  mindmap: 'onMindmap',
  tags: 'onTags',
  quota: 'onQuota',
  error: 'onError',
} as const

const CHAT_ROUTES = {
  answer: 'onAnswer',
  quota: 'onQuota',
  error: 'onError',
} as const

/** 解析事件（`/api/summarize`）的回调。 */
export interface SummaryCallbacks {
  onOwnership?: (data: OwnershipPayload) => void
  onSubtitle?: (data: SubtitlePayload) => void
  onSummary?: (token: string) => void
  onMindmap?: (data: MindmapPayload) => void
  onTags?: (data: string[]) => void
  onQuota?: (data: QuotaPayload) => void
  onError?: (error: SseErrorPayload) => void
  onCancel?: () => void
  onDone?: () => void
}

/** 追问事件（`/api/chat`）的回调。 */
export interface ChatCallbacks {
  onAnswer?: (token: string) => void
  onQuota?: (data: QuotaPayload) => void
  onError?: (error: SseErrorPayload) => void
  onCancel?: () => void
  onDone?: () => void
}

/**
 * 内部分派用的回调形状。
 *
 * `payload: any` 只存在于**这一层**：负载是 `JSON.parse` 出来的，运行时
 * 没有任何东西校验过它的形状。对外的 `SummaryCallbacks` / `ChatCallbacks`
 * 给的是具体类型（`onTags?: (data: string[]) => void`），组件拿到的
 * 仍然是**被类型管过的**那一份。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 见上面那段说明：负载是未校验的 JSON，any 只存在于这一层
type RouteCallback = (payload: any) => void

/**
 * 按事件名分派负载。
 *
 * 事件名不在表里就没人接（后端加一个事件时，这里加一行）。
 * 负载的形状由**调用方那侧的接口**声明（`SummaryCallbacks` 等），
 * 这里不重复声明第二份——两份声明会各自漂移，而漂移的那份没人会红。
 */
function dispatch(
  routes: Record<string, string>,
  callbacks: SummaryCallbacks | ChatCallbacks,
  event: string,
  data: unknown,
): void {
  const name = routes[event]
  if (!name) return
  const fn = (callbacks as Record<string, RouteCallback | undefined>)[name]
  if (typeof fn === 'function') fn(data)
}

/** 自带凭据（来自 lib/byok）。逐字段列举，理由见下面的注释。 */
export interface SummarizeCredential {
  apiKey: string
  baseUrl?: string
  model?: string
}

/** `summarizeVideo` 的可选项。 */
export interface SummarizeOptions {
  /** 覆盖社区里已有的那一份（ADR 0007）。 */
  overwrite?: boolean
  /** 自带凭据。 */
  credential?: SummarizeCredential
  /** 平台元数据（工单 #17 第 2 项）。 */
  videoTitle?: string
  coverUrl?: string
  /**
   * 提示词模板 id（工单 #105）。
   *
   * 空串 = 让服务端用它自己的默认模板。**不传**与**传默认那个 id**
   * 在服务端是同一件事，所以这里不做任何本地兜底：本地猜一个 id 出来，
   * 症状是「选了 A 却拿到 B 的总结」，界面上完全看不出错。
   */
  summaryTemplate?: string
}

/** 一份总结模板（工单 #105）。`id` 是服务端清单里的真值。 */
export interface SummaryTemplate {
  id: string
  name: string
  description: string
}

/** 模板清单端点的响应形状（工单 #105）。 */
export interface SummaryTemplateList {
  /** 服务端默认用哪一份。不传模板时就用它。 */
  default: string
  templates: SummaryTemplate[]
}

/** `chatWithVideo` 的可选项。 */
export interface ChatOptions {
  /** 自带凭据（工单 #9）。 */
  credential?: SummarizeCredential
  /** 兼容旧调用方：只给 key 不给完整凭据。 */
  userApiKey?: string
}

/**
 * 拉一次总结（SSE）。
 *
 * `options.overwrite` 为真时请求覆盖社区里已有的那一份（ADR 0007）。
 * 权限由服务端判：不是首次解析者就回一条 error 事件，这里不预判——
 * 前端手上没有「这份是谁解析的」这个事实（它由 ownership 事件给出），
 * 预判只会让按钮变成一个自己也不知道对不对的开关。
 *
 * `options.credential` 是自带凭据（来自 lib/byok）。逐字段列举而不展开整个
 * 对象：写成 `...options` 会让将来任何顺手加进 options 的东西跟着上车。
 *
 * `options.videoTitle` / `options.coverUrl` 是平台元数据（工单 #17 第 2 项）。
 * 服务端在**抢到占位那一刻**就把它们写进社区卡片行；此前是前端事后打
 * POST /api/community/cards 回填，而那条路要求 status='ready'——
 * 首次解析的那一刻那一行还不存在，那次 UPDATE 永远匹配 0 行，
 * 卡片标题/封面只能等第二个访问者来填。
 *
 * 两个字段一律归一成空串，不原样发出去：服务端那两列是 `str`，
 * 收到 null 直接 422——而症状是「AI 解析整个坏了」，离真实原因很远。
 * undefined 更麻烦：JSON.stringify 会把它悄悄丢掉，于是请求体的形状
 * 取决于调用方写了什么。`videoData` 来自历史记录里存下来的 JSON，
 * 老数据里出现 `"thumbnail": null` 是完全可能的。
 *
 * `options.summaryTemplate` 是提示词模板 id（工单 #105）。同样归一成空串：
 * 空串在服务端就是「用默认模板」，而请求体的形状不该取决于调用方写了什么。
 */
export function summarizeVideo(
  url: string,
  language: string,
  callbacks: SummaryCallbacks,
  options: SummarizeOptions = {},
): SseStream {
  const payload: Record<string, unknown> = {
    url,
    language,
    overwrite: !!options.overwrite,
    video_title: options.videoTitle || '',
    cover_url: options.coverUrl || '',
    summary_template: options.summaryTemplate || '',
  }
  const cred = options.credential
  if (cred) {
    payload.user_api_key = cred.apiKey
    if (cred.baseUrl) payload.base_url = cred.baseUrl
    if (cred.model) payload.model = cred.model
  }
  return streamSse('/api/summarize', payload, {
    route: (event, data) => dispatch(SUMMARY_ROUTES, callbacks, event, data),
    onError: callbacks.onError,
    onCancel: callbacks.onCancel,
    onDone: callbacks.onDone,
  })
}

/**
 * 追问。不再传字幕全文——服务端从社区视频表取（工单 #8）。
 * 字幕全文在网络上白跑两趟没有意义，还让前端有机会篡改它。
 *
 * `options.userApiKey` 是用户自带凭据（工单 #9）。它**只**在这里、且**只**拼进
 * /api/chat 这一个请求体——凭据不进任何别的请求，也不进 URL、请求头、localStorage 以外的任何地方。
 * 留成可选的第 4 个参数而不是塞进 callbacks：签名改动会波及既有调用方，
 * 而凭据是「有没有都行」的东西，不该强迫不带它的调用方改写法。
 */
export function chatWithVideo(
  url: string,
  question: string,
  callbacks: ChatCallbacks,
  options: ChatOptions = {},
): SseStream {
  const payload: Record<string, unknown> = { url, question }
  const cred = options?.credential
  if (cred) {
    payload.user_api_key = cred.apiKey
    if (cred.baseUrl) payload.base_url = cred.baseUrl
    if (cred.model) payload.model = cred.model
  } else if (options?.userApiKey) {
    // 兼容旧调用方：只给 key 不给完整凭据时也走得通。
    payload.user_api_key = options.userApiKey
  }
  return streamSse('/api/chat', payload, {
    route: (event, data) => dispatch(CHAT_ROUTES, callbacks, event, data),
    onError: callbacks.onError,
    onCancel: callbacks.onCancel,
    onDone: callbacks.onDone,
  })
}

/** 当前剩余额度。只读，不消耗。 */
export async function fetchQuota(): Promise<QuotaPayload> {
  const res = await httpFetch('/api/quota')
  if (!res.ok) throw new Error('获取额度失败')
  return (await res.json()) as QuotaPayload
}

/**
 * 拉一次总结模板清单（工单 #105）。
 *
 * 清单只有**一份**真值，在服务端（`prompts/templates.json`）。前端不硬编码
 * 模板名单：名单一改，硬编码的那份就会悄悄过期——下拉框里多出一个后端
 * 没配过的选项，或者少一个后端已经支持的选项，而界面不会报任何错。
 *
 * 只读、不消耗额度。这个端点匿名可读：清单里没有用户相关的东西。
 */
export async function fetchSummaryTemplates(): Promise<SummaryTemplateList> {
  const res = await httpFetch('/api/summary-templates', { method: 'GET' })
  if (!res.ok) throw new Error('获取总结模板失败')
  return (await res.json()) as SummaryTemplateList
}
