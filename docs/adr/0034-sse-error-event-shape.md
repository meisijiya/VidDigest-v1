# ADR 0034：`error` 事件只有一个构造点，形状固定为六键；前端按 `reason` 判类

- 状态：已采纳（2026-11-02）
- 相关：[ADR 0004 用户自带密钥不落盘](0004-byok-forwarded-not-stored.md)、[ADR 0033 额度种类注册表](0033-quota-kinds-registry.md)

## 背景

`backend/api_summarize.py` 里 11 处各写各的 `json.dumps` 造 `error` 事件
（另有一个只服务总结流的 `fail()` 辅助函数，覆盖 9 个出口）。字段出现得很随意：

| 站点 | 发出的键 |
| --- | --- |
| `fail()`（总结的拒绝出口） | `message` + `need_login` + `need_vip` |
| 字幕失败 ×2 | `message` + `reason` + `asr_reason` |
| 额度耗尽 ×2 | `message` + `reason` |
| 模型失败 ×4 | `message`（BYOK 那条另有 `byok`） |

两条后果：

1. **同类拒绝长得不一样**。`_check_quota_permission` 拒绝时带 `need_vip`，
   而并发扣减没扣成（同样是「今日次数已用完」）不带——前端要判类就得同时处理
   「有这个键」与「没这个键」两种形状。
2. **机器可读的那一层没人读**。`reason` 的注释写着「前端将来要分开提示时不必再猜」，
   而前端实际做的是 `err.message.includes('没有可用的字幕')`——匹配中文文案。
   更糟的是 `_subtitle_failure` 会产出**三种**前半句（有轨道但取不下来 / 没轨道 /
   没有可用字幕），那条 `includes` 只接得住第三种，另两种被当作普通错误显示。
   改一个字也会静默失效。

`need_login` / `need_vip` / `reason` / `asr_reason` / `byok` 在
`frontend/src/api/summarize.ts` 有类型声明，全仓没有一处读取；
`SseErrorPayload` 这个类型本身也没有使用者。

## 决策

**一个构造点**：新增 `backend/sse_events.py`，只放 `error_event()` 与
`QUOTA_EXHAUSTED`。18 个 error 出口全部改成一行调用，`fail()` 辅助函数删除。

**形状固定为六键**：`message` 必填；`need_login` / `need_vip` / `byok` 缺省
`False`；`reason` / `asr_reason` 缺省空串（与 `summarizer` 现有
`fail_reason=""` 的约定一致）。前端因此不必判存在性。

**前端按 `reason` 判类**：`classifySseError()` 把负载归成三类——`no_subtitle`
（字幕四类 reason，或 `reason` 缺失但 `asr_reason` 有值）、`quota_exhausted`、
`other`。**未知 reason 落进 `other`**：宁可照旧显示消息，也不静默当成成功。
`VideoSummary` 的 `noSubtitle` 标记改为由 `reason` 驱动，删除中文 `includes`；
额度耗尽那一类顺手刷新额度（此时前端手上的余量已经过期）。

**词表不搬家**：字幕四类建在产生方 `summarizer.FAIL_*`（`_subtitle_failure`
随事件传进来），`sse_events` 只加额度那一条。若把四类搬进 `sse_events`，
`summarizer` 就得反过来 import 它，而 `summarizer` 顶部拖 `openai` / `yt_dlp`——
那会让任何只想构造一个事件的地方跟着拖两个重依赖。

## 代价

- `need_login` / `need_vip` 今天仍无消费者，保留是因为它们是**路由信号**
  （该弹登录框还是 VIP 入口），删掉等于把这个意图从协议上抹掉；
  所幸固定形状让它们不再有「有/没有」两种写法。
- 事件多几个字节。相对「前端判类只接得住三分之一的文案」这件事，不构成取舍。
- 前端 `onError` 的参数类型从 `{ message: string }` 变成 `SseErrorPayload`；
  传输层异常（fetch 失败、超时）也要造一份完整形状，否则组件里会出现
  「有没有 reason」的分支。

## 影响

- `backend/sse_events.py`（新）、`backend/api_summarize.py`（18 处出口收口，
  删 `fail()`）
- `frontend/src/api/summarize.ts`（`SseErrorPayload` 补 `asr_reason`、
  `SseErrorReason` 联合类型、`classifySseError`、`onError` 签名加宽）
- `frontend/src/components/VideoSummary.vue`（`noSubtitle` 改由 reason 驱动，
  额度耗尽时刷新额度）
- 守卫：新增 `backend/tests/test_sse_error_shape.py`（六键形状 + 「不许再手写
  error 帧」的 AST 扫描 + 扫描器的正例）；`frontend/tests/sse.test.mjs` 加
  `classifySseError` 五条，含一条反向对照（消息里带「字幕」但 reason 为空 → `other`）
