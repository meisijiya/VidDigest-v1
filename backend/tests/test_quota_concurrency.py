"""额度扣减的并发安全（工单 #17 第 1 项）。

**为什么这一组必须存在**：判定与扣减在真实链路里隔着字幕提取与整个模型调用
（几十秒）。2026-10-06 实测：8 线程 barrier 同时起跑、上限 3、预置已用 2，
`daily_parse_count` 最终到 **5** —— 超限 2 次。白送的是平台付费资源。

原来的扣减是**读-改-写**：先 SELECT 拿 `current`，再无条件 `count + 1`。
单线程顺序调用下它是对的（已有 test_quota.py 守着），所以全部现有断言都绿 ——
**绿灯不构成「并发下正确」的证据**，这就是本文件存在的理由。

## 判据不是「返回了什么」，而是「库里的计数是多少」

本文件所有断言都重新读库，不信任 `consume_quota` 的返回值。
理由：返回值是函数的自述，库才是被观测对象。工单 #17 第 3 项
（`complete_video` 返回 0 被忽略）就是这个错误的现实版。

## 不要用 `db` 夹具的直觉来想这里

`db` 夹具每次 `init_db()` 出一个全新库，适合单线程路径。
并发测试需要**同一个库**被多线程同时打开——本文件用 `db` 夹具建库，
但自己开线程（每个线程经 `database.get_db()` 各自取连接，
`get_db()` 是按线程隔离连接的，SQLite 的写锁在文件级生效）。
"""
import threading

import pytest

import database
import quota_kinds


def _count(uid, kind="parse"):
    """一次读出某个计数器的当前值——判据落在库上，不落在返回值上。"""
    col = quota_kinds.spec(kind).count_col
    with database.get_db() as c:
        row = c.execute(f"SELECT {col} FROM users WHERE id=?", (uid,)).fetchone()
    return row[col] or 0


def _race(fn, n_threads):
    """n 个线程同时起跑同一个函数，返回各自的返回值。

    用 `barrier` 而不是「依次调用」——依次调用测的是幂等性，不是并发安全性。
    这两者必须分开测：顺序调用下读-改-写完全正确（所以旧测试全绿），
    只有真正同时起跑才会暴露交叉写。
    """
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def worker(i):
        barrier.wait()          # 所有线程都到这里才放行
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for t in threads:
        assert not t.is_alive(), "有线程没结束 —— 并发测试自身有问题，不是被测行为"
    return results


class TestConcurrentConsumeNeverExceedsLimit:
    """核心不变式：并发扣减成功的次数**永远不超过上限**。"""

    def test_mid_day_race_does_not_exceed_limit(self, db, make_user):
        """2026-10-06 的原始复现：8 线程同时起跑，上限 3，预置已用 2。

        旧实现实测 `daily_parse_count = 5`（超限 2 次）。
        """
        uid = make_user()
        # 预置「今天已用 2 次」，把上限 3 的额度压到只剩 1 次——
        # 这样任何超限都会立刻体现为计数 > limit。
        for _ in range(2):
            database.consume_quota(uid, "parse")
        assert _count(uid) == 2, "前提：已用 2 次"
        limit = database.quota_limit("parse", uid)
        assert limit == 3, "前提：上限是 3"

        results = _race(lambda _i: database.consume_quota(uid, "parse"), 8)

        final = _count(uid)
        assert final <= limit, (
            f"并发 8 次后计数 {final} > 上限 {limit} —— 超限 "
            f"{final - limit} 次。扣减与判定之间有可被穿插的窗口。"
        )
        # 且确实用满了（否则这条测试会因为「什么都没扣」而假绿）
        assert final == limit, f"并发后计数 {final}，期望正好用满 {limit}"
        # 没扣成的次数 == 8 - (limit - 2)。**没扣成返回 None，不返回 0**——
        # 0 是「扣成功且刚好用完」的 remaining，两种含义撞车会让
        # 「上限 3 只调 2 次模型」（2026-10-06 实测踩过）。
        assert results.count(None) == 8 - (limit - 2), (
            f"None（没扣成）的次数与实际不符：{results}"
        )

    def test_race_from_zero_still_stops_at_limit(self, db, make_user):
        """从 0 起跑，8 线程抢 3 次额度——最多成功 3 次。"""
        uid = make_user()
        limit = database.quota_limit("parse", uid)
        assert limit == 3, "前提：默认上限是 3"

        results = _race(lambda _i: database.consume_quota(uid, "parse"), 8)

        assert _count(uid) == limit, f"计数 {_count(uid)}，期望 {limit}"
        # 成功的那几次 remaining 必须各不相同（2/1/0）——
        # 全部返回同一个数就说明 remaining 是算错的。
        # 判据是 `is not None` 而**不是** `> 0`：恰好用完那次的 remaining
        # **就是 0**，它是成功不是失败。
        succeeded = sorted(r for r in results if r is not None)
        assert succeeded == [0, 1, 2], f"成功扣减的返回值应各不相同：{succeeded}"
        assert results.count(None) == 5, f"没扣成应有 5 次：{results}"

    def test_crossday_branch_does_not_swallow_concurrent_consumes(self, db, make_user):
        """跨天支必须原子：**N 次并发扣减要扣 N 次，不能都设成 1。**

        这是与 `test_race_from_zero` 不同的一个 bug。跨天支原本是
        `SET count = 1`（绝对值赋值）——8 个线程同时判定「日期不是今天」，
        于是都把计数设成 1，**7 次扣减被静默吞掉**。
        症状是「额度只扣 1 次，用户却发了 8 个请求」= 丢额度。

        为什么 `test_race_from_zero` 抓不到：SQLite 写锁把 8 个线程串行化，
        第一个改完 `last_parse_date` 之后，其余 7 个进的是同一天分支。
        所以这条必须**在事务外先造好状态、再让所有线程一起冲进跨天支**——
        靠真实线程时序去撞那个窗口是不可靠的。
        """
        uid = make_user()
        limit = database.quota_limit("parse", uid)
        assert limit == 3, "前提：默认上限是 3"

        # 造出「今天第一次用」的状态：计数 0 + 日期不是今天
        with database.get_db() as c:
            c.execute(
                "UPDATE users SET daily_parse_count = 0, last_parse_date = '1999-01-01' "
                "WHERE id = ?", (uid,)
            )

        results = _race(lambda _i: database.consume_quota(uid, "parse"), 4)

        # 4 次并发，上限 3 → 恰好 3 次成功、1 次拒。
        # 若跨天支是 `SET count = 1`，计数会是 1（其余被吞），successes 也会是 4
        successes = [r for r in results if r is not None]
        assert len(successes) == limit, (
            f"4 次并发扣减只成功 {len(successes)} 次，期望 {limit} —— "
            f"跨天支把其中几次的扣减吞掉了（丢额度）。results={results}"
        )
        assert _count(uid) == limit, (
            f"计数 {_count(uid)}，期望 {limit} —— "
            f"`SET count = 1` 那种绝对值赋值会把并发扣减互相覆盖"
        )
        assert results.count(None) == 4 - limit

    def test_chat_counter_has_the_same_guarantee(self, db, make_user):
        """parse 与 chat 是两个独立计数器，但守卫生效范围必须一样。

        只给 parse 加守卫、chat 漏掉，是这类修复最常见的半途形态。
        """
        uid = make_user()
        limit = database.quota_limit("chat", uid)
        assert limit == 10, "前提：追问默认上限是 10"

        _race(lambda _i: database.consume_quota(uid, "chat"), 8)

        # 8 个线程抢 10 次额度 → 8 次全扣成，计数就是 8（不是 10）
        assert _count(uid, "chat") <= limit
        assert _count(uid, "chat") == 8, (
            f"8 次并发扣减应当全部成功（上限 10），计数却是 {_count(uid, 'chat')}"
        )
        # parse 那个计数器一个都不该动
        assert _count(uid, "parse") == 0, "扣 chat 不该动 parse"

    def test_repeated_rounds_do_not_creep(self, db, make_user):
        """连打三轮，每轮都回满额度——计数不能一轮轮往上爬。

        单轮通过也可能是因为「并发只赢了一个」；多轮才能看出
        是不是每轮都稳定停在上限上。
        """
        uid = make_user()
        limit = database.quota_limit("parse", uid)
        for round_no in range(3):
            _race(lambda _i: database.consume_quota(uid, "parse"), 6)
            assert _count(uid) == limit, (
                f"第 {round_no + 1} 轮后计数 {_count(uid)}，期望恒为 {limit}"
            )
            # refund_quota 一次只退 1 次（它退的是「一次调用」），
            # 所以要退够次数才回得到 0。
            for _ in range(limit):
                database.refund_quota(uid, "parse")
            assert _count(uid) == 0, (
                f"第 {round_no + 1} 轮退款后计数 {_count(uid)}，期望回到 0"
            )


class TestGuardDoesNotBreakTheExistingContract:
    """守卫不能顺手改掉既有语义。这一组是回归护栏。"""

    def test_exhausted_consume_returns_none_and_writes_nothing(self, db, make_user):
        """额度已满时扣减返回 **None**，且**不再写库**。

        「不再写库」是关键：满额后反复调 consume 会把计数越推越高的话，
        后台看到的今日用量就是假的。

        这里是 `None` 而不是 0：上限 3 时「第 3 次扣成功」返回的 remaining
        **正好是 0**，拿 0 当「没扣成」会把「刚好用完」误判成「额度已满」
        ——2026-10-06 实测症状是「上限 3 只调了 2 次模型」。
        """
        uid = make_user()
        for _ in range(database.DAILY_PARSE_LIMIT):
            database.consume_quota(uid, "parse")
        before = _count(uid)

        for _ in range(5):
            assert database.consume_quota(uid, "parse") is None
        assert _count(uid) == before, "满额后的扣减不该继续推高计数"

    def test_exact_last_use_returns_zero_not_none(self, db, make_user):
        """恰好用完那一次返回 **0**，绝不是 None。

        这一条是上面那条的镜像：0 与 None 必须能区分开，否则调用方
        判 `== 0` 就把「用完了」当成「超了」。
        """
        uid = make_user()
        limit = database.quota_limit("parse", uid)
        for i in range(limit):
            remaining = database.consume_quota(uid, "parse")
            expected = limit - i - 1
            assert remaining == expected, f"第 {i + 1} 次 remaining={remaining}，期望 {expected}"
            assert remaining is not None, "扣成功了却返回 None"
        # 恰好用完：remaining == 0，且**不是** None
        assert remaining == 0
        assert remaining is not None
        # 再来一次才是 None
        assert database.consume_quota(uid, "parse") is None

    def test_unlimited_user_still_gets_minus_one(self, db, make_user):
        """无限额度返回 -1，且计数照记（后台要看今日用量）。"""
        uid = make_user()
        # 直接落库设覆盖值，不依赖可能改名的高层接口
        with database.get_db() as c:
            c.execute(
                "UPDATE users SET parse_limit_override = -1 WHERE id = ?", (uid,)
            )

        for _ in range(5):
            assert database.consume_quota(uid, "parse") == database.QUOTA_UNLIMITED
        # 计数照记，但不参与判定
        assert _count(uid) == 5, "无限额度也要记今日用量"
        assert database.check_quota_kind(uid, "parse") == (True, -1)

    def test_zero_limit_user_is_refused_not_gifted_one(self, db, make_user):
        """上限 0 = 一条都不能用。跨天那支不能白送第 1 次。"""
        uid = make_user()
        with database.get_db() as c:
            c.execute("UPDATE users SET parse_limit_override = 0 WHERE id = ?", (uid,))

        assert database.consume_quota(uid, "parse") is None
        assert _count(uid) == 0, "上限 0 的用户不该被记一次用量"

    def test_vip_short_circuits_before_any_write(self, db, make_user):
        """有效 VIP 短路返回 -1，计数不参与判定。"""
        uid = make_user(is_vip=True, vip_expire_at="2099-01-01T00:00:00+00:00")
        assert database.consume_quota(uid, "parse") == database.QUOTA_UNLIMITED
        assert _count(uid) == 0, "VIP 不该被计入免费额度计数"

    def test_unknown_user_returns_none(self, db):
        assert database.consume_quota(9999, "parse") is None


class TestCheckThenConsumeWindowIsClosed:
    """守卫生效的判据：把「只读判定」换成任何返回值都不影响结果。

    旧实现的漏洞正是「check 通过之后、consume 之前那一段」，
    所以这里连 check 都不调——纯粹的 N 次并发扣减必须自己守住上限。
    全部并发测试都不调 `check_quota_kind`，就是为了让这一条成立。
    """

    def test_no_check_needed_to_be_correct(self, db, make_user):
        uid = make_user()
        limit = database.quota_limit("parse", uid)
        for _ in range(4):
            _race(lambda _i: database.consume_quota(uid, "parse"), 5)
            assert _count(uid) <= limit
        assert _count(uid) == limit
