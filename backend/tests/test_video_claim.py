"""解析占位权 module 的行为守卫（ADR 0031）。

**这个接缝在哪**：``video_claim`` 的两个 interface——``acquire_claim`` 与
``reclaim``。它们是「谁能调模型、谁只能等着」的唯一决定处，断言都从这层
出发，不碰内部私有函数。

**为什么用真 SQLite**：占位权的行为有一半在事务边界上（唯一索引裁决、
条件 UPDATE 抢闸门、DELETE 的 WHERE 只删 pending 行）。替身会把这些语义
复制一份，于是替身与真库漂移时没有任何东西会红。这里直接用 ``db`` 夹具
（tmp_path 里的独立临时库）。

**为什么有一半断言写在 with 块里面**：占位是真的被占着的——退出上下文就
还回去了。要观察「占位期间那一行的状态」，就得在占位期间看。
"""

import asyncio

import pytest

import database
import video_claim

URL = "https://example.com/claim-module"


def run(coro):
    return asyncio.run(coro)


# ── acquire_claim ────────────────────────────────────────────

class TestAcquireClaim:
    def test_first_caller_owns_a_fresh_link(self, db, make_user):
        """没人占位时，第一个进来的人拿到 owner，且那一行归他。"""
        user = make_user()

        async def scenario():
            async with video_claim.acquire_claim(URL, user) as claim:
                assert claim.outcome == video_claim.CLAIM_OWNER
                row = database.get_video_by_url(URL)
                assert row is not None, "占位者手里没有那一行"
                assert row["status"] == "pending"
                assert row["parsed_by"] == user

        run(scenario())

    def test_ready_link_is_reused_and_left_untouched(self, db, make_user):
        """社区里已有成品时走复用：不改那一行，也不占位。"""
        owner = make_user("owner@example.com")
        assert database.reserve_video(URL, owner)[0] == "reserved"
        assert database.complete_video(URL, summary_md="社区里那一份") == 1
        reader = make_user("reader@example.com")

        async def scenario():
            async with video_claim.acquire_claim(
                URL, reader, timeout=0.05, poll_interval=0.01
            ) as claim:
                assert claim.outcome == video_claim.CLAIM_REUSE
                row = database.get_video_by_url(URL)
                assert row["summary_md"] == "社区里那一份", "复用拿到的是别的东西"
                assert row["parsed_by"] == owner, "复用改写了那一行的归属"

        run(scenario())

    def test_holder_that_never_finishes_makes_the_waiter_busy(self, db, make_user):
        """占位者一直不出现：等待者超时退回去，且不碰别人的位置。"""
        stuck = make_user("stuck@example.com")
        assert database.reserve_video(URL, stuck)[0] == "reserved"
        waiter = make_user("waiter@example.com")

        async def scenario():
            async with video_claim.acquire_claim(
                URL, waiter, timeout=0.05, poll_interval=0.01
            ) as claim:
                assert claim.outcome == video_claim.CLAIM_BUSY
                row = database.get_video_by_url(URL)
                assert row["parsed_by"] == stuck, "等待者动了别人的占位"

        run(scenario())

    def test_unsettled_claim_gives_the_placeholder_back(self, db, make_user):
        """没 settle 就退出：位置必须还回去。

        不还回去的代价是永久性的——那一行永远停在 pending，这个链接从此
        再也解析不了。
        """
        user = make_user()

        async def scenario():
            async with video_claim.acquire_claim(URL, user) as claim:
                assert claim.outcome == video_claim.CLAIM_OWNER
            assert database.get_video_by_url(URL) is None, "退出后位置没有还回去"

        run(scenario())

    def test_settled_claim_keeps_the_row(self, db, make_user):
        """settle 之后退出：那一行留着。

        这是「结果已落库」与「位置已结清」的信号。还位会删掉 pending 行，
        而刚写进去的社区视频会跟着一起没了。
        """
        user = make_user()

        async def scenario():
            async with video_claim.acquire_claim(URL, user) as claim:
                assert claim.outcome == video_claim.CLAIM_OWNER
                assert database.complete_video(URL, summary_md="好了") == 1
                claim.settle()
            row = database.get_video_by_url(URL)
            assert row is not None, "settle 之后那一行被删了——刚落库的内容没了"
            assert row["status"] == "ready"

        run(scenario())

    def test_placeholder_is_released_when_the_body_raises(self, db, make_user):
        """块里抛异常时位置照样还回去。

        模型调用失败、客户端断流都会走到这里；不还回去就是把链接作废。
        """
        user = make_user()

        async def scenario():
            with pytest.raises(RuntimeError):
                async with video_claim.acquire_claim(URL, user):
                    raise RuntimeError("模型调用炸了")

        run(scenario())
        assert database.get_video_by_url(URL) is None, "异常路径没有还位"


# ── reclaim ─────────────────────────────────────────────────

class TestReclaim:
    def test_missing_community_video_is_skipped(self, db, make_user):
        """社区里没有成品：按首次解析走（skip）。"""
        user = make_user()

        async def scenario():
            async with video_claim.reclaim(URL, user) as reg:
                assert reg.outcome == video_claim.RECLAIM_SKIP

        run(scenario())

    def test_other_peoples_row_is_forbidden(self, db, make_user):
        """别人的成果不许改写（ADR 0007），且那一行不能被动过。"""
        other = make_user("other@example.com")
        assert database.reserve_video(URL, other)[0] == "reserved"
        assert database.complete_video(URL, summary_md="别人的") == 1
        user = make_user("me@example.com")

        async def scenario():
            async with video_claim.reclaim(URL, user) as reg:
                assert reg.outcome == video_claim.RECLAIM_FORBIDDEN
                row = database.get_video_by_url(URL)
                assert row["summary_md"] == "别人的", "被拒绝的那次覆盖改了内容"

        run(scenario())

    def test_owner_gets_the_gate_and_releases_it_on_exit(self, db, make_user):
        """本人可以覆盖：闸门真被抢到，退出时放掉，旧内容全程还在。

        覆盖**不占位**（ADR 0007）：重新解析的那几十秒里旧内容仍然对
        所有人可见，所以那一行既不能被删，也不能变成 pending。
        """
        user = make_user()
        assert database.reserve_video(URL, user)[0] == "reserved"
        assert database.complete_video(URL, summary_md="我自己的") == 1

        async def scenario():
            async with video_claim.reclaim(URL, user) as reg:
                assert reg.outcome == video_claim.RECLAIM_REGENERATE
                row = database.get_video_by_url(URL)
                assert row["regenerating_by"] == user, "闸门没有真的被抢到"
                assert row["status"] == "ready", "覆盖期间那一行不能变成 pending"
            row = database.get_video_by_url(URL)
            assert row["regenerating_by"] is None, "退出后闸门没有放掉"
            assert row["summary_md"] == "我自己的", "覆盖不占位：旧内容必须还在"

        run(scenario())

    def test_second_reclaim_by_the_same_user_is_busy(self, db, make_user):
        """同一个人已经在覆盖了：再点一次只会白烧一次额度。"""
        user = make_user()
        assert database.reserve_video(URL, user)[0] == "reserved"
        assert database.complete_video(URL, summary_md="我自己的") == 1
        assert database.acquire_regenerate_gate(URL, user)

        async def scenario():
            async with video_claim.reclaim(URL, user) as reg:
                assert reg.outcome == video_claim.RECLAIM_BUSY

        run(scenario())

    def test_gate_is_released_when_the_body_raises(self, db, make_user):
        """块里抛异常时闸门照样放掉。

        漏放一次的后果是永久性的：这个链接此后再也覆盖不了了，
        而内容明明还在（ADR 0015）。
        """
        user = make_user()
        assert database.reserve_video(URL, user)[0] == "reserved"
        assert database.complete_video(URL, summary_md="我自己的") == 1

        async def scenario():
            with pytest.raises(RuntimeError):
                async with video_claim.reclaim(URL, user) as reg:
                    assert reg.outcome == video_claim.RECLAIM_REGENERATE
                    raise RuntimeError("模型调用炸了")

        run(scenario())
        row = database.get_video_by_url(URL)
        assert row["regenerating_by"] is None, "异常路径没有放闸"
