"""等待者轮询不该产生写事务（工单 #19 第 1 项）。

## 这条缺陷是什么

`_claim_video` 的等待循环每 50ms 调一次 `reserve_video`，而
`reserve_video` 每次调用都是一个**写事务**——它先 INSERT 让唯一索引裁决，
被拒绝了才另开事务重读。实测（生产常量不动，只把等待上限缩到 1s 观察）：

    单个等待者每秒写事务      17 次   （理论上限 20 = 1/50ms）
    外推到生产值 30s          510 次 / 每个等待者
    同一视频 5 个等待者       2550 次

SQLite 是 WAL，这些事务抢的是**同一把写锁**。而争锁的代价不是等待者自己付：
占位者要在几十秒后 `complete_video` 写回结果，正好撞上等待者最密的轮询。
**一个什么都没干的人在和真正有活要干的人抢同一把锁。**

## 为什么不能简单改成「抢一次就睡」

代码里那句注释是刻意的：占位者中途失败并 `release_video` 还位时，等待者
必须在下一轮成为首次解析者，否则这个链接会卡在「位置已经还回去了」的
状态上空等到超时。所以「每轮重新抢」必须保留。

## 本文件的判据

改法是**把裁决与写入拆开**：轮询走 `probe_video`（只读）判断该不该抢，
只有真的要抢时才调 `reserve_video`（写）。于是守的是三件事：

1. 等待者不产生写事务 —— 本缺陷的正身
2. 判定与写入拆开之后，「每轮重新抢」一字未改 —— 还位后仍能接管
3. 只读探测本身不写库 —— 否则拆开只是把写事务挪了个地方

## 判据的方向性（写之前先自问）

`test_waiter_writes_nothing` 的上限**不能**取 0。取 0 的话，
「占位者一死、等待者什么都抢不到」这个实现也能过——而那正是
#8 踩过的坑（功能缺失长得像隔离生效）。所以上限留了余量，
并且由 `test_dead_placeholder_still_gets_taken_over` 从正面钉住那条能力：
把那条能力整体打断，这两条必须一起转红。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import database
import video_claim

URL = "https://example.com/wait"


# ── 工具 ────────────────────────────────────────────────────

def claim(url, uid, title="", cover="", timeout=0.3, poll_interval=0.01):
    """跑一次 acquire_claim，返回 outcome。

    直接调 module 而不是走 HTTP：这里要观察的是**轮询期间数据库被写了
    几次**，而 HTTP 层会把 summarize 自己的写入（额度、parse_history）
    一起算进来，量不到目标。module 这一层才是轮询的真实边界。

    超时与间隔**显式传**：缺省时它们取 module 常量（调用时才读），
    而这里要的是小值——生产的 30 秒等待没法写进用例。
    """
    async def scenario():
        async with video_claim.acquire_claim(
            url, uid, title, cover, timeout=timeout, poll_interval=poll_interval
        ) as c:
            return c.outcome
    return asyncio.run(scenario())


def claim_inside(url, uid, body, title="", cover="", timeout=0.3, poll_interval=0.01):
    """在占位上下文**里面**跑 body，返回 body 的返回值。

    占位是真的被占着的：退出上下文就还回去了。所以「占位期间那一行是
    什么状态」这类断言只能写在上下文里面——写在外面只会看到「行没了」。
    """
    async def scenario():
        async with video_claim.acquire_claim(
            url, uid, title, cover, timeout=timeout, poll_interval=poll_interval
        ) as c:
            return body(c)
    return asyncio.run(scenario())


def writes_during(fn):
    """跑 fn，统计它期间 videos 表上的写事务次数，返回 (结果, 次数)。

    ## 为什么不用逐方法计数

    「等待者调了 N 次 reserve_video」量不到这次要防的东西：改完之后
    那个方法**不再被调用**，而另一个实现也可能仍然调它却已经只读。
    逐方法计数会把「调用过」当成「写了库」。

    ## 为什么用 trace 回调而不是 total_changes

    `conn.total_changes` 是**连接级累计量**，于是它会把
    `db` 夹具的 `init_db()`（建十几张表）和 `make_user` 的 INSERT
    一并算进来——实测「复用零写」那条本该是 0，却报 1。
    判据把噪声算成信号，比没有判据更贵。

    真正要数的是**写到 videos 表上的那几条语句**。`set_trace_callback`
    把每条执行的 SQL 原文交出来，数它既不漏也不多：DDL、users 写入、
    夹具建表都不碰 videos，一个都进不来。

    ⚠️ 回调挂在**连接**上，而 `get_db()` 按线程复用连接，所以同线程内
    有效。夹具建表发生在这段区间之前——它写的是别的表，本来也进不来。

    ## 只数写语句

    返回 (结果, 写语句列表, 读语句条数)。读条数是**自证用的**：一条
    只读的等待路径本该「读很多、写零」。曾经这里用 `"videos" in sql`
    判一把，结果 21 条 SELECT 全被判成「写了库」——**判据把读当成写，
    报出来的数字比真的还大**，而实测写事务确实是 0。教训与本仓那条
    「残留为 0 只在我枚举的那几个串上成立」同源：判据必须按**类别**
    枚举（这里是写动词），不是按表名出现。
    """
    statements: list[str] = []
    conn = database._thread_local.conn
    assert conn is not None, (
        "前提不成立：当前线程还没有连接（db 夹具没先跑过？）——"
        "trace 回调挂不上，这条用例会静默数到 0"
    )
    conn.set_trace_callback(statements.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)

    def _kind(sql: str) -> str:
        head = sql.lstrip().split(None, 1)
        if not head:
            return "other"
        verb = head[0].upper()
        if verb in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            return "write"
        if verb in ("SELECT", "PRAGMA", "WITH", "EXPLAIN", "BEGIN", "COMMIT"):
            return "read"
        return "other"

    writes = [s for s in statements if _kind(s) == "write" and "videos" in s.lower()]
    reads = sum(1 for s in statements if _kind(s) == "read")
    return result, writes, reads


def age_row(seconds: int) -> None:
    """把占位行的 updated_at 往前推，模拟「占位者已经死了这么久」。"""
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()
    with database.get_db() as conn:
        conn.execute("UPDATE videos SET updated_at = ?", (old,))


# ── probe_video：只读探测的判据 ─────────────────────────────

class TestProbeIsReadOnly:
    """`probe_video` 承诺不写库。不守住这条，拆分就只是挪位置。"""

    def test_probe_writes_nothing_for_every_state(self, db, make_user):
        """三种状态各探一次，**一条写 videos 的语句都不许发生**。

        分别喂「没人占位 / 新鲜占位 / 陈旧占位」——只测一种的话，
        另外两种走的是不同分支，量不到。
        """
        owner = make_user("owner@example.com")
        # 1) 没人占位
        (_r, vacant, _rd) = writes_during(lambda: database.probe_video(URL + "/vacant"))
        # 2) 新鲜占位
        database.reserve_video(URL + "/fresh", owner)
        (_r, fresh, _rd) = writes_during(lambda: database.probe_video(URL + "/fresh"))
        # 3) 陈旧占位
        database.reserve_video(URL + "/stale", owner)
        age_row(database.VIDEO_PENDING_TTL_SECONDS + 60)
        (_r, stale, _rd) = writes_during(lambda: database.probe_video(URL + "/stale"))

        assert not (vacant or fresh or stale), (
            f"probe_video 写了 videos：空位={vacant} 新鲜占位={fresh} "
            f"陈旧占位={stale}。它必须是纯读——否则等待者还在写，"
            f"只是把写事务从 reserve_video 挪到了它这里。"
        )

    def test_probe_answers_the_three_states_correctly(self, db, make_user):
        """它得真的答对，否则等待者会在该抢的时候干等、该等的时候抢。"""
        owner = make_user("owner@example.com")
        assert database.probe_video(URL + "/none") == "claimable", "没人占位时该说可以抢"

        database.reserve_video(URL + "/pending", owner)
        assert database.probe_video(URL + "/pending") == "waiting", (
            "新鲜占位被说成可抢：等待者会去偷一个正在工作的位置——"
            "两个人同时调模型、同时扣额度，正是这套设计要防的那件事"
        )

        database.reserve_video(URL + "/stale", owner)
        age_row(database.VIDEO_PENDING_TTL_SECONDS + 60)
        assert database.probe_video(URL + "/stale") == "claimable", (
            "陈旧占位没被判成可抢：这个链接会永久卡死"
        )

        database.complete_video(URL + "/pending", summary_md="好了")
        assert database.probe_video(URL + "/pending") == "ready", (
            "成品没被认出来：等待者会一直等一份已经存在的结果"
        )


# ── 正身：等待期不写库 ──────────────────────────────────────

class TestWaiterDoesNotWrite:
    def test_waiter_writes_nothing_while_placeholder_is_alive(self, db, make_user, monkeypatch):
        """本缺陷的正身：占位者还在干活，等待者一个字节都不许写。

        上限取 1 而不是 0：0 太紧，会把「等待者什么都抢不到所以不写」
        这种功能缺失也判成通过（见文件头「判据的方向性」）。
        1 留的余量给真正需要写的那一次——还位之后接管，或探到与写入之间
        的竞态走的那一次，两者都合法。
        """
        stuck = make_user("stuck@example.com")
        assert database.reserve_video(URL, stuck)[0] == "reserved"
        waiter = make_user()

        (outcome, writes, reads), = [writes_during(lambda: claim(URL, waiter))]

        assert outcome == "busy", (
            f"占位者一直不出现，等待者应当超时退回去，实得 {outcome}"
        )
        assert reads > 1, (
            f"前提不成立：只读了 {reads} 次，说明这个循环压根没在轮询——"
            f"那么「没写库」就只是没跑过，不是守住了"
        )
        assert len(writes) <= 1, (
            f"等待者在 {0.3}s 内写了 {len(writes)} 条 videos 写语句：{writes}。"
            "改法是让轮询走只读探查（probe_video），只有真要抢时才写。"
            "注意这些语句抢的是 WAL 写锁，代价由真正要 complete_video "
            "写回结果的占位者承担。"
        )

    def test_thirty_second_wait_produces_no_burst(self, db, make_user, monkeypatch):
        """按生产轮询间隔跑一次，钉住「放大」这个形状而不是钉死秒数。

        这条不是上一条的重复：上一条把间隔缩到 10ms，压的是**次数**；
        这条用生产的 50ms 间隔跑满 1 秒（0.3s 等待上限），量的是
        **每秒的写事务率**。若实现退化成「轮询时也走 reserve_video」，
        两者都会红，但报出来的数字不同——一条报次数、一条报速率，
        修的时候能立刻看出是次数问题还是速率问题。
        """
        # 间隔保持生产值 0.05 不动（只有等待上限缩到 1 秒）

        stuck = make_user("stuck@example.com")
        assert database.reserve_video(URL, stuck)[0] == "reserved"
        waiter = make_user()

        (outcome, writes, reads), = [
            writes_during(lambda: claim(URL, waiter, timeout=1.0, poll_interval=0.05))
        ]

        assert outcome == "busy"
        assert reads > 1, f"前提不成立：只读了 {reads} 次，循环没在轮询"
        # 1 秒 / 50ms = 20 轮。老实现是 20 次写；新实现是 0。
        # 上限 2 留余量给「该抢」的那一次写。
        assert len(writes) <= 2, (
            f"生产轮询间隔下，1 秒等待写了 {len(writes)} 条 videos 写语句："
            f"{writes}。外推到生产的 30 秒等待就是 {len(writes) * 30} 次——"
            f"这正是工单里实测到的 510 次的来源。"
        )


# ── 拆分没有削弱「每轮重新抢」 ──────────────────────────────

class TestPerRoundReclaimIsPreserved:
    """`reserve_video` 从循环里移走了，但**每轮重新抢**的语义必须一字未改。

    代码注释里写明这是刻意的：占位者中途失败还位时，等待者要在下一轮
    成为首次解析者。所以这里把「写事务」优化掉的同时，必须验它没顺手
    把这条语义一起优化掉。
    """

    def test_dead_placeholder_still_gets_taken_over(self, db, make_user, monkeypatch):
        """正面能力：占位者死了，等待者必须**真的能抢到**。

        这条是上面那条上限的上界依据。把它整体打断（probe 永远返回
        waiting），等待者就会一路等到超时——而那条「等待者不写库」的
        断言仍会绿。两条一起才锁住这个能力。
        """
        dead = make_user("dead@example.com")
        assert database.reserve_video(URL, dead)[0] == "reserved"
        waiter = make_user()

        # 占位者进程死了：行还在 pending，但已经老到 TTL
        age_row(database.VIDEO_PENDING_TTL_SECONDS + 60)

        def body(c):
            assert c.outcome == "owner", (
                f"陈旧占位没被接管，等待者实得 {c.outcome}。"
                "这会让死掉的占位把链接永久卡死——而只读探测优化掉写事务时，"
                "最容易顺手丢的就是这条。"
            )
            # 断言写在占位上下文里面：退出上下文就还位了。
            row = database.get_video_by_url(URL)
            assert row["parsed_by"] == waiter, "接管之后 parsed_by 没换成等待者"
            assert row["status"] == "pending", "接管的是别人的占位行，状态不该变"

        claim_inside(URL, waiter, body)

    def test_waiter_takes_over_the_moment_placeholder_releases(self, db, make_user, monkeypatch):
        """核心语义：占位者中途还位，等待者**下一轮**就能成为首次解析者。

        这条守的是注释里那句「每轮都重新抢」的**真实用途**。它与上面
        「陈旧占位」那条不是一回事：陈旧靠 TTL 判定能不能抢，而这条
        靠的是行被真的删掉。分开测，因为实现上它们可能走不同分支
        （一个走 probe 的 stale 判定，一个走 row is None）。
        """
        owner = make_user("owner@example.com")
        waiter = make_user("waiter@example.com")
        assert database.reserve_video(URL, owner)[0] == "reserved"

        async def scenario():
            # 等待者已经在等；占位者现在失败还位（删行）。
            # 放行必须**并发**发生——acquire_claim 的进入过程自己就 await，
            # 所以这个任务能在等待期间跑起来。
            async def release_soon():
                await asyncio.sleep(0.05)
                assert database.release_video(URL, owner) == 1, (
                    "前提不成立：还位没删掉行，这条测的就不是「行没了」这条路径"
                )

            releaser = asyncio.create_task(release_soon())
            async with video_claim.acquire_claim(
                URL, waiter, timeout=5.0, poll_interval=0.01
            ) as c:
                await releaser
                assert c.outcome == "owner", (
                    f"占位者还位之后等待者实得 {c.outcome}，实得 owner 才对。"
                    "抢一次就睡的写法在这里会一路等到超时——"
                    "而「等待者不写库」那条断言对它仍然是绿的。"
                )
                assert database.get_video_by_url(URL)["parsed_by"] == waiter

        asyncio.run(scenario())

    def test_ready_row_is_reused_without_writing(self, db, make_user, monkeypatch):
        """成品路径：零写事务，且那一行原样还在。

        上半段钉住「复用不写库」，下半段钉住「复用的内容还是社区里那一份」——
        只断前者的话，一个恒返回 reuse 的实现也能过，而调用方重放时
        拿到的会是空内容。
        """
        owner = make_user("owner@example.com")
        assert database.reserve_video(URL, owner)[0] == "reserved"
        assert database.complete_video(URL, summary_md="社区里那一份", tags=["编程"]) == 1

        (outcome, writes, reads), = [writes_during(lambda: claim(URL, make_user()))]

        assert outcome == "reuse", f"已有成品却没走复用，实得 {outcome}"
        assert reads >= 1, "前提不成立：一条都没读，测的不是复用路径"
        assert not writes, f"复用一条都写库了：{writes}，复用的成本必须是零"
        # 复用不交出所有权：那一行还在，内容还是社区里那一份。
        row = database.get_video_by_url(URL)
        assert row is not None, "复用之后那一行不见了"
        assert row["summary_md"] == "社区里那一份", "复用拿到的不是那一份结果"


# ── 竞态：探到与写入之间别人插进来 ──────────────────────────

class TestProbeThenWriteRace:
    def test_claimable_then_someone_else_wins_keeps_waiting(self, db, make_user, monkeypatch):
        """探到「可抢」之后、真去抢之前被别人抢走：不能误判成 owner。

        这一段是 `probe_video` 文档里明写的那道竞态。实现上它可能
        「碰巧」不发生（单线程测试里没人插进来），所以这里**主动制造**：
        把 reserve_video 换成「替我抢走位置」的桩，逼出那条分支。
        漏了它，一个把 probe 的 claimable 直接当成 owner 的实现会
        在并发下让两个人同时调模型、同时扣额度——而所有单线程用例照样全绿。
        """
        thief = make_user("thief@example.com")
        waiter = make_user("waiter@example.com")

        real_reserve = database.reserve_video
        stolen = {"done": False}

        def stealing_reserve(url, user_id, title="", cover=""):
            # 第一次被调用时：先让「别人」把位置抢走，再走真实路径
            if not stolen["done"]:
                stolen["done"] = True
                assert real_reserve(url, thief)[0] == "reserved", (
                    "前提不成立：替身没能抢到位置，这条测的就不是竞态那条路"
                )
            return real_reserve(url, user_id, title, cover)

        monkeypatch.setattr(video_claim, "reserve_video", stealing_reserve)

        outcome = claim(URL, waiter, timeout=0.2)

        assert outcome == "busy", (
            f"别人抢走位置后等待者实得 {outcome}，实得 busy 才对。"
            "当成 owner 会让两个人同时调模型、同时扣额度——"
            "而这个分支在单线程下不主动制造就永远走不到。"
        )
        assert stolen["done"], "前提不成立：替身没被调用，这条用例什么也没测"
        assert database.get_video_by_url(URL)["parsed_by"] == thief, "位置被偷走了"
