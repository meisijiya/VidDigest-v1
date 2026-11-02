"""作者本人可以重新解析并覆盖自己那一份（ADR 0007）。

ADR 0006 定了「首份为准，后来者不得覆盖」。0007 在其中开一个口子：
**改写自己写的东西**是允许的，改写别人写的不允许。这两者必须分开测，
因为它们共用一个按钮、一个端点里的两个分支——只测一个的话，
另一个分支整段删掉测试照样全绿。

本文件要守住的四件事，每一条都对应「把这个功能删掉会不会红」：

1. owner 覆盖真的**改写内容**（不是又回放一遍旧内容——那正是原 bug）。
2. 外人覆盖被**拒绝**，且社区内容与他的额度毫发无损。
3. 覆盖**失败时不破坏已有内容**，且闸门必须放掉（否则这个链接此后再也覆盖不了）。
4. 覆盖**不占位**：重新解析的那几十秒里，复用者拿到的仍是完整旧内容。

第 4 条只能靠真线程 + 强制交错测出来：单线程顺序跑两次，
「覆盖不占位」和「覆盖顺便清了空」的表现完全一样。
"""
import asyncio
import json
import threading

import api_summarize
import database
from seams import StubExtractor
from seams import StubSummarizer as SeamStubSummarizer

URL = "https://example.com/regen"


# ── 小工具 ─────────────────────────────────────────────────

def collect(gen):
    """跑完一个 SSE 异步生成器，返回 [(event, payload)]。"""
    async def _run():
        out = []
        async for e in gen:
            raw = e.raw_data
            out.append((e.event, raw if raw in (None, "", "[DONE]") else json.loads(raw)))
        return out

    return asyncio.run(_run())


def summarize(url=URL, uid=None, overwrite=False):
    return api_summarize.summarize_video(
        api_summarize.SummarizeRequest(url=url, language="zh", overwrite=overwrite),
        user=None if uid is None else {"id": uid},
    )


def wire(monkeypatch, summarizer=None, extractor=None):
    summarizer = summarizer if summarizer is not None else SeamStubSummarizer()
    extractor = extractor if extractor is not None else StubExtractor()
    monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: summarizer)
    monkeypatch.setattr(api_summarize, "_get_extractor", lambda: extractor)
    return summarizer, extractor


def kinds_of(events):
    return [kind for kind, _ in events]


def payload_of(events, name):
    return [payload for kind, payload in events if kind == name]


def parse_count_of(db, uid):
    with db.get_db() as c:
        return c.execute(
            "SELECT daily_parse_count FROM users WHERE id = ?", (uid,)
        ).fetchone()[0]


def seed(db, owner, summary="首份总结"):
    """造一份「已 ready、属于 owner」的社区内容。"""
    db.reserve_video(URL, owner)
    db.complete_video(URL, summary_md=summary, mindmap_md="# 首", tags=["编程"],
                      subtitle_text="字幕")
    return db.get_video_by_url(URL)


# ── 1. owner 覆盖真的改写内容 ─────────────────────────────

class TestOwnerCanRegenerate:
    def test_summary_is_actually_replaced_not_replayed(self, db, make_user, monkeypatch):
        """这是原 bug 的反面：点「重新解析」必须产出**新内容**。

        断言落在落库后的 summary_md 上，而不是「模型被调了几次」——
        后者在「调了但结果没落库」时也会绿，而那正是用户看到的失效。
        """
        owner = make_user()
        seed(db, owner, summary="首份总结")
        s, _ = wire(monkeypatch, SeamStubSummarizer(
            summary_tokens=("全新", "的", "总结"), mindmap="# 新", tags=("读书",),
        ))

        events = collect(summarize(uid=owner, overwrite=True))

        assert kinds_of(events)[0] == "subtitle"
        assert kinds_of(events)[-1] == "done", events
        assert "error" not in kinds_of(events), events
        assert "ownership" not in kinds_of(events), (
            "覆盖是作者自己发起的，不该走回放路径——带上它说明覆盖被当成了复用"
        )
        # 总结是逐 token 下发的，所以按内容断言而不是按事件个数
        assert payload_of(events, "summary") == ["全新", "的", "总结"]
        row = db.get_video_by_url(URL)
        assert row["summary_md"] == "全新的总结", "覆盖没有改写社区里的内容"
        assert row["mindmap_md"] == "# 新"
        assert row["tags"] == ["读书"], "覆盖时落库的仍是旧标签"
        assert s.calls_of("summarize_full_stream") == 1, "覆盖没有真的调模型"

    def test_owner_keeps_ownership_after_overwriting(self, db, make_user, monkeypatch):
        """覆盖不转移所有权——覆盖者本来就是主人，改写后还是。

        顺带锁住「只更新内容列」：parsed_by / created_at 不该被这次覆盖动过。
        """
        owner = make_user()
        before = seed(db, owner)
        wire(monkeypatch, SeamStubSummarizer(summary_tokens=("新的",)))

        collect(summarize(uid=owner, overwrite=True))

        after = db.get_video_by_url(URL)
        assert after["parsed_by"] == owner
        assert after["created_at"] == before["created_at"]
        assert after["status"] == "ready", "覆盖后行不能退回 pending"

    def test_overwrite_costs_a_quota_unit_like_a_fresh_parse(self, db, make_user, monkeypatch):
        """覆盖调了模型，代价必须与首次解析一样。

        少扣一次就等于社区内容可以靠反复覆盖免费重写。
        """
        owner = make_user()
        before = parse_count_of(db, owner)
        wire(monkeypatch)

        events = collect(summarize(uid=owner, overwrite=True))

        assert parse_count_of(db, owner) == before + 1
        quota = payload_of(events, "quota")[0]
        assert quota["remaining"] == quota["limit"] - before - 1, quota

    def test_overwrite_on_a_link_with_no_result_parses_it_normally(
        self, db, make_user, monkeypatch
    ):
        """社区里压根没有成品时，「重新解析」不能报错，要当首次解析做。

        这条护住一个具体的坑：判定写成「没有 owner 就不许覆盖」，
        而刚建好还没人解析的链接 parsed_by 是 NULL，会被误判成别人的。
        """
        uid = make_user()
        s, _ = wire(monkeypatch, SeamStubSummarizer(summary_tokens=("首份",)))

        events = collect(summarize(uid=uid, overwrite=True))

        assert kinds_of(events)[-1] == "done", events
        assert db.get_video_by_url(URL)["summary_md"] == "首份"
        assert s.calls_of("summarize_full_stream") == 1


# ── 2. 外人不得改写别人的内容 ─────────────────────────────

class TestStrangerIsRefused:
    def test_stranger_gets_an_error_and_the_content_is_untouched(
        self, db, make_user, monkeypatch
    ):
        owner = make_user()
        stranger = make_user("other@example.com")
        seed(db, owner, summary="作者认真写的总结")
        s, ex = wire(monkeypatch, SeamStubSummarizer(summary_tokens=("篡改",)))

        events = collect(summarize(uid=stranger, overwrite=True))

        assert kinds_of(events) == ["error"], events
        assert "首次解析" in payload_of(events, "error")[0]["message"], events
        assert db.get_video_by_url(URL)["summary_md"] == "作者认真写的总结", (
            "外人的请求改写了社区内容"
        )
        assert s.calls == 0, "被拒之前就调了模型：白烧一次额度"
        assert ex.calls == 0, "被拒之前就重跑了字幕提取"
        assert parse_count_of(db, stranger) == 0, "被拒却扣了额度"

    def test_refusal_leaves_the_video_still_reusable_by_anyone(self, db, make_user, monkeypatch):
        """被拒的那次请求不能留下任何占位或状态变化。

        留下 pending 的话，这个链接会从此对所有人卡到超时——
        一个人点错按钮，代价由全站承担。
        """
        owner = make_user()
        stranger = make_user("other@example.com")
        seed(db, owner)
        wire(monkeypatch)

        collect(summarize(uid=stranger, overwrite=True))

        row = db.get_video_by_url(URL)
        assert row["status"] == "ready"
        events = collect(summarize(uid=stranger))
        assert kinds_of(events)[-1] == "done", "被拒之后这个链接不再可用了"

    def test_ownership_event_tells_the_ui_who_may_press_the_button(
        self, db, make_user, monkeypatch
    ):
        """复用回放必须带上写权限，否则前端对 owner 和外人显示同一个按钮。

        断言两侧都验：只验「owner 得到 true」的话，一个恒返回 true 的实现
        也能让这条变绿。
        """
        owner = make_user()
        stranger = make_user("other@example.com")
        seed(db, owner)
        wire(monkeypatch)

        mine = payload_of(collect(summarize(uid=owner)), "ownership")
        theirs = payload_of(collect(summarize(uid=stranger)), "ownership")

        assert mine == [{"can_regenerate": True}], mine
        assert theirs == [{"can_regenerate": False}], theirs

    def test_owner_flag_is_the_first_event_of_a_replay(self, db, make_user, monkeypatch):
        """ownership 必须在内容之前。

        放在后面的话，用户会先看到一个点了没反应的按钮，
        然后才收到「这是别人的」——正是这次要修的症状。
        """
        owner = make_user()
        seed(db, owner)
        wire(monkeypatch)

        events = collect(summarize(uid=owner))

        assert kinds_of(events)[0] == "ownership", events
        assert kinds_of(events)[-1] == "done"


# ── 3. 覆盖失败时旧内容与额度都保住 ───────────────────────

class _ExplodingSummarizer(SeamStubSummarizer):
    """模型桩：第 ``fail_at`` 次调用时抛错。"""

    def __init__(self, fail_at=1, **kw):
        super().__init__(**kw)
        self._fail_at = fail_at

    def summarize_full_stream(self, text, language, template=None):
        if self.calls_of("summarize_full_stream") + 1 == self._fail_at:
            self._calls["summarize_full_stream"] += 1
            raise RuntimeError("模型挂了")
        yield from super().summarize_full_stream(text, language)


class TestFailedOverwriteKeepsTheOldContent:
    def test_old_summary_survives_a_failed_regeneration(self, db, make_user, monkeypatch):
        """失败的覆盖绝不能把已有内容清空。

        覆盖不占位正是为了这件事：行在整个过程中始终是 ready，
        落库那一步才一次性换掉内容，中间不存在「空窗」。
        """
        owner = make_user()
        seed(db, owner, summary="完好无损的总结")
        wire(monkeypatch, _ExplodingSummarizer(fail_at=1))

        events = collect(summarize(uid=owner, overwrite=True))

        assert kinds_of(events)[-1] == "error", events
        assert db.get_video_by_url(URL)["summary_md"] == "完好无损的总结", (
            "一次失败的覆盖抹掉了社区里已有的总结"
        )
        assert db.get_video_by_url(URL)["status"] == "ready"

    def test_failed_regeneration_refunds_the_quota(self, db, make_user, monkeypatch):
        owner = make_user()
        before = parse_count_of(db, owner)
        seed(db, owner)
        wire(monkeypatch, _ExplodingSummarizer(fail_at=1))

        collect(summarize(uid=owner, overwrite=True))

        assert parse_count_of(db, owner) == before, "失败的覆盖没有退回额度"

    def test_gate_is_released_so_the_next_attempt_still_works(
        self, db, make_user, monkeypatch
    ):
        """闸门漏放一次，这个链接就**永远**覆盖不了了——而内容明明还在。

        这是 finally 里那一次放闸的唯一见证：
        删掉它，第一条（成功路径）照样绿，只有这条会红。
        """
        owner = make_user()
        seed(db, owner)
        wire(monkeypatch, _ExplodingSummarizer(fail_at=1))
        collect(summarize(uid=owner, overwrite=True))

        wire(monkeypatch, SeamStubSummarizer(summary_tokens=("终于成功",)))
        events = collect(summarize(uid=owner, overwrite=True))

        assert kinds_of(events)[-1] == "done", "一次失败之后这个链接再也覆盖不了"
        assert db.get_video_by_url(URL)["summary_md"] == "终于成功"

    def test_gate_is_released_after_a_successful_run_too(
        self, db, make_user, monkeypatch
    ):
        """成功路径也必须放掉，否则第二次覆盖会被自己挡住。"""
        owner = make_user()
        seed(db, owner)
        wire(monkeypatch, SeamStubSummarizer(summary_tokens=("一",)))
        collect(summarize(uid=owner, overwrite=True))

        wire(monkeypatch, SeamStubSummarizer(summary_tokens=("二",)))
        events = collect(summarize(uid=owner, overwrite=True))

        assert kinds_of(events)[-1] == "done", "第二次覆盖被上一次留下的闸门挡住了"
        assert db.get_video_by_url(URL)["summary_md"] == "二"

    def test_a_second_concurrent_overwrite_is_refused_not_run_twice(
        self, db, make_user, monkeypatch
    ):
        """同一个人点两次按钮：第二次必须被挡住，不能再烧一次额度。"""
        owner = make_user()
        seed(db, owner)
        entered, release = threading.Event(), threading.Event()

        class _Gated(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                entered.set()
                assert release.wait(timeout=10), "门没被打开，测试会挂死"
                yield from super().summarize_full_stream(text, language)

        wire(monkeypatch, _Gated(summary_tokens=("慢的",)))

        out = {}

        def _run():
            out["first"] = collect(summarize(uid=owner, overwrite=True))

        t = threading.Thread(target=_run, name="regen-first")
        t.start()
        assert entered.wait(timeout=10), "覆盖根本没开始"

        second = collect(summarize(uid=owner, overwrite=True))
        release.set()
        t.join(timeout=15)
        assert not t.is_alive()

        assert kinds_of(second) == ["error"], second
        assert "正在重新解析" in payload_of(second, "error")[0]["message"], second
        assert kinds_of(out["first"])[-1] == "done"


# ── 4. 覆盖不占位：并发复用者看到的仍是完整旧内容 ───────────

class TestRegenerationDoesNotBlankTheVideo:
    def test_a_visitor_during_regeneration_still_gets_the_full_old_summary(
        self, db, make_user, monkeypatch
    ):
        """这是 ADR 0007 最核心的承诺，也是「不占位」唯一的理由。

        若覆盖期间把行改回 pending，复用者会看到「正在解析中」甚至空白；
        顺序执行测不出这个差别——必须有另一个请求**真的落在覆盖进行当中**。
        同步点放在模型桩里：覆盖在落库之前一定会经过它，
        而正确实现下复用者根本不会进模型，所以这里不会互相等待。
        """
        owner = make_user()
        visitor = make_user("visitor@example.com")
        seed(db, owner, summary="覆盖期间的旧总结")
        entered, release = threading.Event(), threading.Event()

        class _Gated(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                entered.set()
                assert release.wait(timeout=10), "门没被打开，测试会挂死"
                yield from super().summarize_full_stream(text, language)

        wire(monkeypatch, _Gated(summary_tokens=("新总结",)))

        out = {}

        def _run():
            out["owner"] = collect(summarize(uid=owner, overwrite=True))

        t = threading.Thread(target=_run, name="regen-owner")
        t.start()
        assert entered.wait(timeout=10), "覆盖根本没开始"

        visitor_events = collect(summarize(uid=visitor))
        release.set()
        t.join(timeout=15)
        assert not t.is_alive()

        assert payload_of(visitor_events, "summary") == ["覆盖期间的旧总结"], (
            "覆盖进行中，复用者没拿到完整旧内容——说明期间行被清空了"
        )
        assert payload_of(visitor_events, "subtitle")[0]["full_text"] == "字幕"
        assert kinds_of(out["owner"])[-1] == "done"
        assert db.get_video_by_url(URL)["summary_md"] == "新总结"

    def test_regeneration_leaves_exactly_one_row(self, db, make_user, monkeypatch):
        """覆盖走的是 UPDATE，不是 INSERT——行数不变。"""
        owner = make_user()
        seed(db, owner)
        wire(monkeypatch, SeamStubSummarizer(summary_tokens=("新的",)))

        collect(summarize(uid=owner, overwrite=True))

        with db.get_db() as c:
            assert c.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


# ── 5. 额度不足时不许开始覆盖 ─────────────────────────────

class TestQuotaBlocksRegeneration:
    def test_exhausted_quota_stops_the_overwrite_before_the_model(
        self, db, make_user, monkeypatch
    ):
        """覆盖要调模型，所以它吃额度；额度用满时不能先扣了再想。

        造法：额度靠**不同**链接耗尽——同一个链接第二次起就是复用/覆盖，
        根本不会走到额度判定。
        """
        owner = make_user()
        s, _ = wire(monkeypatch)
        for i in range(database.DAILY_PARSE_LIMIT - 1):
            collect(summarize(url=f"{URL}/warm{i}", uid=owner))
        seed(db, owner, summary="原总结")
        assert db.check_quota_kind(owner, "parse")[0] is True, "前提：还剩最后一次"

        collect(summarize(url=f"{URL}/last", uid=owner))  # 用掉最后一次
        assert db.check_quota_kind(owner, "parse")[0] is False, "前提：额度已用满"
        before = parse_count_of(db, owner)
        calls_before = s.calls_of("summarize_full_stream")

        events = collect(summarize(uid=owner, overwrite=True))

        assert kinds_of(events) == ["error"], events
        assert s.calls_of("summarize_full_stream") == calls_before, "额度用满仍调了模型"
        assert db.get_video_by_url(URL)["summary_md"] == "原总结"
        assert parse_count_of(db, owner) == before


# ── 6. regenerate_video 本身的 WHERE 语义 ──────────────────

class TestRegenerateSqlSemantics:
    def test_null_owner_matches_a_null_caller(self, db, make_user):
        """`IS` 与 `=` 的唯一可观察差别就在 NULL 上。

        建表早期有一批 parsed_by 为 NULL 的行。判成「谁都不能覆盖」
        保守但无害；判成「谁都能覆盖」是数据被公开改写。
        写错成 `=` 时这条会红（结果是 0），写成 `IS` 才会过。
        """
        now = "2026-01-01T00:00:00+00:00"
        with db.get_db() as c:
            c.execute(
                """INSERT INTO videos (video_url, status, parsed_by, created_at, updated_at)
                   VALUES (?, 'ready', NULL, ?, ?)""",
                (URL, now, now),
            )

        touched = db.regenerate_video(
            URL, None, subtitle_text="匿名者的字幕", summary_md="匿名者写的")

        assert touched == 1, "NULL 属主行不该被 NULL 调用者改写"
        assert db.get_video_by_url(URL)["summary_md"] == "匿名者写的"
        # 成功写进来的字幕必须是调用方给的那份。这一列在此之前是
        # 「调用方忘了传就静默清空」，而本文件四条断言没有一条看它——
        # subtitle_text 提成必填就是要让「忘了传」变成 TypeError（工单 #21）。
        assert db.get_video_by_url(URL)["subtitle_text"] == "匿名者的字幕"

    def test_a_logged_in_user_cannot_touch_a_null_owned_row(self, db, make_user):
        """反向：NULL 属主的行不能被任何登录用户顺手改掉。"""
        now = "2026-01-01T00:00:00+00:00"
        with db.get_db() as c:
            c.execute(
                """INSERT INTO videos (video_url, status, parsed_by, created_at, updated_at)
                   VALUES (?, 'ready', NULL, ?, ?)""",
                (URL, now, now),
            )

        touched = db.regenerate_video(
            URL, make_user(), subtitle_text="不该写进去", summary_md="顺手改的")

        assert touched == 0, "登录用户改写了无主的内容"
        assert db.get_video_by_url(URL)["summary_md"] == ""

    def test_a_pending_row_is_not_regeneratable(self, db, make_user):
        """别人正在解析的占位不能被覆盖——那是他的位置。"""
        owner = make_user()
        db.reserve_video(URL, owner)

        assert db.regenerate_video(
            URL, owner, subtitle_text="抢跑的字幕", summary_md="抢跑") == 0
        assert db.get_video_by_url(URL)["status"] == "pending"

    def test_a_missing_row_is_a_zero_not_a_crash(self, db, make_user):
        assert db.regenerate_video(
            URL, make_user(), subtitle_text="x 的字幕", summary_md="x") == 0
