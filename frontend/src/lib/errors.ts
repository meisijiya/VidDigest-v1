/**
 * 错误分类：把「链接不对」「网络问题」「服务端故障」分成不同提示。
 *
 * 为什么值得单独一个模块：用户看到「解析失败：解析失败: ...」时
 * 什么也做不了——他不知道是链接贴错了、被限流了、还是服务挂了。
 * 三种情况的下一步完全不同，提示也必须不同：
 *
 *   链接不对   → 让他重新检查链接（换个视频 / 确认是不是完整地址）
 *   平台在拦   → 让他等一会儿（平台在拦，不是他的错）
 *   服务器在限 → 让他等一会儿（**我们**的护栏，别让他去怪视频站）
 *   同时在跑太多 → 让他等前面的任务结束（**我们**的护栏，但下一步不同）
 *   网络/服务  → 让他重试
 *
 * 判据要能**变异**：只断言「出现了某种提示」的话，把分类逻辑整个
 * 删掉、恒返回第一个分支，它照样绿。所以下面每条分支都有对应用例。
 */

/**
 * 错误对象里我们真正关心的那几个字段。
 *
 * 只声明**用到**的：axios 的错误对象有一大堆字段，全抄一遍等于把
 * 第三方库的形状复制一份到本仓库，而它会随 axios 版本漂移。
 */
interface ErrorLike {
  message?: string
  response?: {
    data?: {
      detail?: string | { error?: string; code?: string }
    }
  }
}

/** 从各种错误形状里把「人话」挖出来。 */
function messageOf(err: unknown): string {
  const e = (err ?? {}) as ErrorLike
  const detail = e?.response?.data?.detail
  // 后端有两种 detail 形状：字符串，以及 {success, error}
  const raw = (detail && typeof detail === 'object' ? detail.error : detail) || e?.message || '未知错误'
  // 后端常把原因包在「解析失败: xxx」里，剥掉这层重复前缀，
  // 否则界面会显示「解析失败：解析失败: xxx」这种叠字。
  return (
    String(raw)
      .replace(/^(解析|下载)失败[:：]\s*/, '')
      .trim() || '未知错误'
  )
}

/** 链接本身有问题：不是有效的视频地址，或平台明确说不认识。 */
function looksLikeBadLink(err: unknown): boolean {
  const m = messageOf(err).toLowerCase()
  if (/unsupported url|invalid url|not a valid|不支持|无效的?url|无法识别|unknown url/.test(m)) return true
  // yt-dlp 的典型报错：它认得这个域名，但上面没有视频
  if (/video unavailable|requested format is not available|no video|private video|稿件不可见|视频不存在|已失效/.test(m))
    return true
  return false
}

/**
 * 取后端给的**机器可读**错误码（`detail.code`）。
 *
 * 为什么需要它：过去前端靠中文文案认出「这是我们自己的闸门」，两边分头改
 * 就会把自家闸门误报成平台风控。协议落在字段上之后，文案只给人看。
 *
 * 值的来源：`backend/ratelimit.py` 的 `RATE_LIMIT_CODE` / `CONCURRENCY_LIMIT_CODE`。
 */
function errorCodeOf(err: unknown): string | undefined {
  const e = (err ?? {}) as ErrorLike
  const detail = e?.response?.data?.detail
  if (detail && typeof detail === 'object') return detail.code
  return undefined
}

/** 平台在拦：风控 / 限流 / 需要登录。这类**不是用户的错**。 */
function looksLikeThrottled(err: unknown): boolean {
  const m = messageOf(err).toLowerCase()
  return /429|rate limit|too many|blocked|forbidden|风控|限流|频率|稍后重试|访问过于频繁/.test(m)
}

/**
 * 服务器自己在限流（我们自己的护栏），不是视频站的风控。
 *
 * 为什么必须分开说：后端给公开端点加了限流之后，超限的响应是 429，
 * 而 429 这个词原本一律被当成「平台在拦」。那样用户会以为视频站在封他，
 * 于是去换视频、反复重试——不但没解决，还把触发得更狠。
 *
 * 判据（按可靠性排序）：
 * 1. **机器可读的 `detail.code`**（`backend/ratelimit.py` 的 `RATE_LIMIT_CODE`）——
 *    这是协议，前端与后端认的是同一个字段。
 * 2. 老后端兼容：响应体里还没带 `code` 时才退回认文案。本仓前后端同仓同
 *    构建发布，老响应只出现在「本地后端进程没重启」这种开发期场景。
 */
function looksLikeServerRateLimit(err: unknown): boolean {
  if (errorCodeOf(err) === 'rate_limit') return true
  const m = messageOf(err).toLowerCase()
  return /服务器限流|server rate limit/.test(m)
}

/**
 * 服务器的并发闸门：同一来源同时在跑的任务太多（还是**我们**自己的护栏）。
 *
 * 为什么必须和「服务器限流」分开说：两件事的下一步不一样——
 * 限流是「来得太频繁，等一分钟」，并发是「你前面那个任务还没跑完，
 * 等它结束」。照着「等一分钟」的提示做，用户会白等一分钟。
 *
 * 判据（按可靠性排序）：
 * 1. **机器可读的 `detail.code`**（`backend/ratelimit.py` 的
 *    `CONCURRENCY_LIMIT_CODE`）——这是协议。文案只给人看，改它不再影响分类。
 * 2. 老后端兼容：没有 `code` 时才退回认文案（见上面 `looksLikeServerRateLimit`
 *    的第 2 条说明）。
 */
function looksLikeServerConcurrencyLimit(err: unknown): boolean {
  if (errorCodeOf(err) === 'concurrency_limit') return true
  const m = messageOf(err).toLowerCase()
  return /同时进行的任务太多|同时在跑的任务已超过|并发上限/.test(m)
}

/** 网络层问题：连不上、超时。 */
function looksLikeNetwork(err: unknown): boolean {
  const e = (err ?? {}) as ErrorLike
  if (e?.response) return false // 拿到了 HTTP 响应就不是网络问题
  const m = messageOf(err).toLowerCase()
  return /network|timeout|timed out|econnrefused|failed to fetch|load failed|网络|超时|连接/.test(m)
}

/**
 * 平台拦住了**这台服务器的出口**：按 IP 维度风控，或视频本身在境外。
 *
 * 为什么要单独分一类：症状看着像「链接坏了」，其实不是。机房 IP 段被
 * B 站按 IP 风控时，视频页与 view API 一律 412，换视频、换 UA、重试都没用；
 * 视频在平台所在地区之外时也是这一类。落到「换个链接试试」那一档，
 * 用户会一直换链接、白等——提示必须说清「不是链接的问题」。
 *
 * 判据只取**出口维度**的信号（412 / 反爬验证 / 地区限制），
 * 不取 403 这种通用状态码：同一个 403 也可能只是单个视频被删。
 */
function looksLikeEgressBlocked(err: unknown): boolean {
  const m = messageOf(err).toLowerCase()
  if (/http error 412|precondition failed/.test(m)) return true
  if (/not a bot|sign in to confirm|too many requests from/.test(m)) return true
  if (/not available in your country|not available in your region|geo.?restricted|geo.?block/.test(m)) return true
  return false
}

/**
 * 服务器连不上这个平台：平台在境外，而这台部署没有出墙线路。
 *
 * 同样不能落到「连不上服务器」那一档——那说的是用户自己的网络，
 * 而这里断的是服务器到平台那一段。
 */
function looksLikePlatformUnreachable(err: unknown): boolean {
  const m = messageOf(err).toLowerCase()
  return /network is unreachable|no route to host|name or service not known|temporary failure in name resolution/.test(
    m,
  )
}

/**
 * 返回统一弹窗需要的 { title, message, hint }。
 * hint 是「用户下一步该做什么」——这是分类的真正目的。
 */
export interface ErrorPresentation {
  title: string
  message: string
  hint: string
}

export function classifyError(err: unknown, action: 'parse' | 'download' | 'payment' = 'parse'): ErrorPresentation {
  const message = messageOf(err)

  // 出口维度的拦截必须排在「链接不对」之前：「Video unavailable ... not
  // available in your country」里也有 "video unavailable"，先判链接就会把
  // 「这个平台在境外/这台机器被风控」说成「你链接贴错了」。
  if (looksLikeEgressBlocked(err)) {
    return {
      title: '平台拦住了这台服务器的出口',
      message: message,
      hint:
        '这不是你的链接有问题：当前部署的出口 IP 被平台按 IP 维度风控' +
        '（常见症状是 B 站视频页 412、海外平台提示验证）。换个视频也一样。' +
        '要用这些平台，需要管理员换一个出口（代理），或者在本地解析。',
    }
  }

  if (looksLikePlatformUnreachable(err)) {
    return {
      title: '服务器连不上这个平台',
      message: message,
      hint: '这个平台在境外，而这台部署没有出墙线路——不是你的网络，也不是链接的问题。' + '境内平台不受影响。',
    }
  }

  if (looksLikeBadLink(err)) {
    return {
      title: '这个链接好像不对',
      message: message,
      hint: '请检查链接是否完整、是否还有效。B 站请用 ' + 'bilibili.com/video/BV... 这样的完整地址，不要只贴一段文字。',
    }
  }

  if (looksLikeServerRateLimit(err)) {
    return {
      title: '请求太频繁',
      message: message,
      hint:
        '服务器在限制请求速度，等一分钟左右再试一次。' +
        '如果你刚才连着解析了好几个视频，那就是触发得太快了——一次解析一个就好。',
    }
  }

  // 并发闸门必须排在 looksLikeThrottled 前面：并发超限也是 429，
  // 而 429 这个词本来一律被当成「平台在拦」。排后面的话，用户会以为
  // 视频站在封他——可占着他名额的其实是他自己那个还没跑完的任务。
  if (looksLikeServerConcurrencyLimit(err)) {
    return {
      title: '同时在跑的任务太多',
      message: message,
      hint:
        '服务器上你这个来源同时在跑的任务已经到上限了。' + '等前面那个任务结束再试就好——不用换链接，也不用干等一分钟。',
    }
  }

  if (looksLikeThrottled(err)) {
    return {
      title: '平台暂时拦住了',
      message: message,
      hint: '这不是你的问题——视频站对频繁请求做了限制。等一两分钟再试就好，' + '换个视频通常也能立刻解析。',
    }
  }

  if (looksLikeNetwork(err)) {
    return {
      title: '连不上服务器',
      message: message,
      hint: '请检查网络后重试。如果一直失败，可能是本地服务没有启动。',
    }
  }

  // 支付走的是另一条路（跳转到收银台），提示语不能再说「换个视频链接」。
  if (action === 'payment') {
    return {
      title: '支付失败',
      message: message,
      hint: '这通常是支付通道的临时问题，可以重试一次。如果反复失败，' + '请稍后再试，或换一个支付方式。',
    }
  }

  // 兜底：拿不准就归到服务端问题，并说清可以重试。
  // 不用「未知错误」这种说法——用户看到它只会更困惑。
  return {
    title: action === 'download' ? '下载失败' : '解析失败',
    message: message,
    hint: '这通常是服务端临时的问题，可以重试一次。如果反复失败，' + '请换一个视频链接试试。',
  }
}
