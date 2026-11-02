"""SSE `error` 事件的唯一构造点（ADR 0034）。

线上形状：**六个键总是出现**。

| 键 | 缺省 | 谁读 |
|---|---|---|
| `message` | 必填 | 前端直接显示 |
| `need_login` | `False` | 前端该弹登录框（路由信号，前端暂未接） |
| `need_vip` | `False` | 该弹 VIP 入口（同上） |
| `reason` | `""` | 前端判类：无字幕 / 额度耗尽 / 其它 |
| `asr_reason` | `""` | 诊断用，与 `reason` 一起走 |
| `byok` | `False` | 自带凭据失败（此时不拼异常文本） |

**为什么总是出现**：改造前 11 处各写各的 `json.dumps`，语义相同的两次拒绝
长得不一样——`_check_quota_permission` 拒绝时带 `need_vip`，而并发扣减没扣成
（同样是「今日次数已用完」）却没带。前端要判类就得同时处理「有这个键」与
「没这个键」两种形状，而它当时两种都没判，只匹配了中文文案。

`reason` 的取值：字幕四类住在 `summarizer`（`FAIL_NO_TRACK` /
`FAIL_FETCH_FAILED` / `FAIL_ASR_NOT_CONFIGURED` / `FAIL_ASR_FAILED`，由
`_subtitle_failure` 随事件传进来），额度耗尽这一条住在本模块，其余情况为空串。
词表不搬到这里：`summarizer` 顶部会拖 `openai` / `yt_dlp`，本模块若 import 它，
任何只想构造一个事件的地方都要跟着拖那两个重依赖。
"""
import json

from fastapi.sse import ServerSentEvent

#: 本次请求的额度已耗尽（并发扣减没扣成，或只读判定就拒绝）。
#: 与字幕失败的四类同层，但那四个住在产生方 `summarizer` 里。
QUOTA_EXHAUSTED = "quota_exhausted"


def error_event(
    message: str,
    *,
    need_login: bool = False,
    need_vip: bool = False,
    reason: str = "",
    asr_reason: str = "",
    byok: bool = False,
) -> ServerSentEvent:
    """构造一条 `error` 事件。调用方只传有意义的键，其余走缺省。"""
    return ServerSentEvent(
        raw_data=json.dumps(
            {
                "message": message,
                "need_login": need_login,
                "need_vip": need_vip,
                "reason": reason,
                "asr_reason": asr_reason,
                "byok": byok,
            },
            ensure_ascii=False,
        ),
        event="error",
    )
