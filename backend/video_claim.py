"""解析占位权（Parse Claim）。

**谁能调模型、谁只能等着**，这件事在这里决定，不在路由层。

## 为什么单独一个 module

协议原先写在路由的三个私有函数里：轮询顺序、等待超时、闸门配对、失败释放
全靠调用方手写，正确顺序只存在于 docstring 里。任何一次「多等一会儿」或
「失败时少还一次位置」的改动，都要先读懂那段循环才能动。

这里把协议收成两个 interface：

- :func:`acquire_claim` —— 首次解析的占位权
- :func:`reclaim` —— 覆盖自己那一份的闸门（ADR 0007）

两者都是 **async context manager**：退出即释放。于是「抢到了但忘了还回去」
这种形状在语言层面就不可能发生——不需要靠 finally 里的自觉。

## 三个必须记住的不变量

1. **只有 owner 能调模型。** 这条落在 ``acquire_claim`` 的 outcome 上：
   ``reuse`` 与 ``busy`` 的持有者一次模型都不该调。
2. **已落库 ≠ 位置还在手上。** ``complete_video`` 之后要调
   :meth:`ParseClaim.settle`：此后不再还位。否则会删掉刚写进去的社区视频。
   反过来，占位被别人完成（``complete_video`` 返回 0）时位置**已经结清**，
   同样要 settle——不拥有，就别去动它。
3. **覆盖不占位。** 覆盖路径全程不删那一行（ADR 0007）；它的释放是「放闸」，
   不是「还位」。

## 哪些东西故意不在这里

- **额度与退款**：额度是独立领域概念（见 GLOSSARY.md），与占位权不混。
- **视频行读取**：本 module 只管抢权，不当数据入口。
- **追问路径**：追问不占位，也不接入本 module。

陈旧占位的回收沿用 ``probe_video`` / ``reserve_video`` 内部的 TTL 判定，
不另建后台清理任务——那件事与占位权无关。
"""

import asyncio
import time
from contextlib import asynccontextmanager

from database import (
    acquire_regenerate_gate,
    get_video_by_url,
    probe_video,
    release_regenerate_gate,
    release_video,
    reserve_video,
)

#: 后来者等待占位者出结果的上限（秒）。到点还没好就明确告诉他
#: 「正在解析」，而不是占着一条连接无限期等下去。
VIDEO_WAIT_TIMEOUT_SECONDS = 30.0

#: 等待期间的轮询间隔。调大则复用方白等，调小则空转烧 CPU。
VIDEO_POLL_INTERVAL_SECONDS = 0.05

# ── outcome 词汇 ──────────────────────────────────────────────
#
#: 抢到了：这个持有者可以调模型。
CLAIM_OWNER = "owner"

#: 社区里已有成品：直接复用，不调模型、不扣额度。
CLAIM_REUSE = "reuse"

#: 别人正在解析，等不到：明确告诉用户稍后再来。
CLAIM_BUSY = "busy"

#: 覆盖成功：这一行属于本人，且闸门已抢到。
RECLAIM_REGENERATE = "regenerate"

#: 这一行不是本人的，别人那份不许改写（ADR 0007）。
RECLAIM_FORBIDDEN = "forbidden"

#: 本人已有一条覆盖在跑，再点一次只会白烧一次额度。
RECLAIM_BUSY = "busy"

#: 社区里没有可覆盖的成品，按首次解析走。
RECLAIM_SKIP = "skip"


class ParseClaim:
    """一次占位的所有权凭证。

    ``outcome`` 说明这次拿到的是什么；:meth:`settle` 说明**位置已经结清**，
    此后退出上下文时不再还位。
    """

    def __init__(self, outcome: str):
        self.outcome = outcome
        self._settled = False

    def settle(self) -> None:
        """标记「位置已经结清」，退出上下文时不再还位。

        两种情形都要调它：结果已落库（``complete_video`` 成功），或位置
        已经不归自己（``complete_video`` 返回 0）。后一种尤其要紧——
        不拥有，就别去动它。
        """
        self._settled = True

    @property
    def settled(self) -> bool:
        return self._settled


class Reclaim:
    """一次覆盖闸门的判定结果。"""

    def __init__(self, outcome: str):
        self.outcome = outcome


async def _wait_for_claim(
    video_url: str,
    user_id: int,
    video_title: str,
    cover_url: str,
    timeout: float,
    poll_interval: float,
) -> str:
    """抢一次解析权，返回 outcome。

    ## 为什么每轮不是直接调 reserve_video

    ``reserve_video`` 每次调用都是一个**写事务**：先 INSERT 让唯一索引裁决，
    被拒了才另开事务重读。而这个循环每 50ms 跑一次——实测每个等待者
    17 次写事务/秒，30 秒内 510 次，5 个等待者 2550 次。SQLite 是 WAL，
    这些事务抢的是**同一把写锁**；争锁的代价由真正有活要干的人承担，
    也就是占位者那几十秒后要 ``complete_video`` 写回结果的那一刻。

    所以这里把「该不该抢」（只读，``probe_video``）与「去抢」（写，
    ``reserve_video``）拆开：轮询只读，判定需要抢时才花那一次写事务。
    「每轮重新抢」的语义一字未改——``probe_video`` 在还位后会返回
    ``claimable``，于是等待者下一轮照样能成为首次解析者。

    ⚠️ ``probe_video`` 返回 claimable **不等于**抢到了：读到「没人占」到
    真正 INSERT 之间仍有竞态，那一段由 ``reserve_video`` 的唯一索引兜住。
    两种可能（抢到 / 仍被别人占着）都必须显式处理，不能假设。
    """
    deadline = time.monotonic() + timeout
    while True:
        # 先只读地问一句：绝大多数轮次在这里就结束了，一个字节都没写。
        state = probe_video(video_url)
        if state == "ready":
            return CLAIM_REUSE
        if state == "claimable":
            outcome, _row = reserve_video(video_url, user_id, video_title, cover_url)
            if outcome == "reserved":
                return CLAIM_OWNER
            if outcome == "ready":
                return CLAIM_REUSE
            # 仍是 pending：探到与写入之间被别人抢走了，继续等下一轮。
            # 这里**不能**当成 owner 也不能当成 reuse——两条路都跳过
            # 一次模型调用，而这一行还什么都没产出。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return CLAIM_BUSY
        # await 而不是 time.sleep：这是 async 生成器，睡死了会卡住整个事件循环
        await asyncio.sleep(min(poll_interval, remaining))


def _decide_reclaim(video_url: str, user_id: int) -> str:
    """判定这次请求能不能覆盖，返回 outcome。

    - ``regenerate`` 可以覆盖，旧内容保持原样直到新结果算完
    - ``forbidden`` 这一行属于别人，直接拒绝（不调模型、不扣额度）
    - ``busy``     同一个链接已有一次覆盖在跑，再点一次只会白烧一遍额度
    - ``skip``     这一行还没 ready，压根没有可覆盖的东西，按首次解析走

    抢锁本身是**一条带条件的 UPDATE**（``acquire_regenerate_gate``），
    而 SQLite 串行化写事务 —— 所以两个 worker 同时进来时只有一个拿到。
    这就是跨进程的那道保证，不再依赖「后端只有一个进程」这个部署事实
    （ADR 0015）。

    前两次读仍然是读：它们要区分「不归我」和「没人占着」这两种 outcome，
    而它们**不是**判据——判据在 UPDATE 的 WHERE 里，那一句把 status /
    parsed_by / 锁是否过期一起判掉。所以这里不存在「先读后判」的窗口。
    """
    row = get_video_by_url(video_url)
    if row is None or row.get("status") != "ready":
        return RECLAIM_SKIP
    if row.get("parsed_by") != user_id:
        return RECLAIM_FORBIDDEN
    if acquire_regenerate_gate(video_url, user_id):
        return RECLAIM_REGENERATE
    # 抢不到。重读一次来区分「别人占着」与「这一行刚被别人改掉 / 删了」——
    # 两者要报的 outcome 不同，而只有重读能分。失败路径才多这一次读。
    fresh = get_video_by_url(video_url)
    if fresh is None or fresh.get("status") != "ready":
        return RECLAIM_SKIP
    if fresh.get("parsed_by") != user_id:
        return RECLAIM_FORBIDDEN
    return RECLAIM_BUSY


@asynccontextmanager
async def acquire_claim(
    video_url: str,
    user_id: int,
    video_title: str = "",
    cover_url: str = "",
    *,
    timeout: float | None = None,
    poll_interval: float | None = None,
):
    """抢一次解析权；抢不到就等它完成，等不到就退回去。

    产出一个 :class:`ParseClaim`：``outcome`` 是 ``owner`` / ``reuse`` /
    ``busy`` 三者之一，**只有 owner 允许去调模型**。

    退出上下文时，若本次抢到了位置且尚未 settle，就把位置还回去。
    抢到之后必须调 :meth:`ParseClaim.settle` 才不会还位——那是「结果已经
    落库」或「位置已经不归自己」的信号。

    ``timeout`` / ``poll_interval`` 缺省时取本 module 的常量，且是**调用时**
    才读——写成默认参数的话那个值在 import 时就定死了，改常量不生效。
    """
    if timeout is None:
        timeout = VIDEO_WAIT_TIMEOUT_SECONDS
    if poll_interval is None:
        poll_interval = VIDEO_POLL_INTERVAL_SECONDS
    outcome = await _wait_for_claim(
        video_url, user_id, video_title, cover_url, timeout, poll_interval
    )
    claim = ParseClaim(outcome)
    try:
        yield claim
    finally:
        # 只删 pending 行；已经 ready 的社区内容永远不会被这一步碰到。
        if outcome == CLAIM_OWNER and not claim.settled:
            release_video(video_url, user_id)


@asynccontextmanager
async def reclaim(video_url: str, user_id: int):
    """抢一次覆盖权；退出上下文时无条件放闸。

    产出一个 :class:`Reclaim`：``outcome`` 是 ``regenerate`` /
    ``forbidden`` / ``busy`` / ``skip`` 四者之一。只有 ``regenerate``
    允许继续往下走（改写自己那一份，ADR 0007）。

    覆盖路径**不占位**：旧内容全程保持对所有人可见，退出时放的是闸，
    不是位置。
    """
    outcome = _decide_reclaim(video_url, user_id)
    try:
        yield Reclaim(outcome)
    finally:
        # 闸门必须无条件放掉，异常路径也不能漏——漏一次，这个链接此后就
        # 再也覆盖不了了（而内容明明还在）。
        if outcome == RECLAIM_REGENERATE:
            release_regenerate_gate(video_url, user_id)
