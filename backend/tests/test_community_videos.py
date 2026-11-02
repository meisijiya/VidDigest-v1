"""社区视频表 + 首次解析者为准（工单 #6，ADR 0001）。

三条 AC 是本文件存在的理由，其余用例都在守它们的边界：

1. 解析成功后社区视频表有一行，含总结、思维导图、标签、字幕。
2. 第二次解析同一链接：未调模型、未扣额度、返回已有结果。
3. 两个用户同时解析同一链接：只有一次模型调用、一次额度扣减、一行数据。

第 3 条是本工单最险的地方，所以并发用例都是**真线程 + 强制交错**：
单线程接缝测不出竞态，两次调用若不落在同一时间窗，测试就会假通过。
同步点的位置在文件里逐条说明了理由——它**不能**放在模型调用里，
因为实现正确时第二个请求根本不会进模型调用。

断言一律读外部可观察的结果：桩的逐方法计数、行数、额度计数、事件序列。
不测私有函数，不断言内部调用顺序。
"""
import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import api_summarize
import database
import video_claim
from seams import StubExtractor
from seams import StubSummarizer as SeamStubSummarizer

#: 多数用例都用这个链接：同一链接只能有一行，第二条 AC 才有意义。
URL = "https://example.com/v"


# ── 小工具 ─────────────────────────────────────────────────

def collect(gen):
    """跑完一个 SSE 异步生成器，返回 [(event, payload)]。

    与 test_summarize_routes 同形：payload 在 raw_data 上，done 的负载是
    字符串 "[DONE]"，所以按事件名取，不要用负下标。
    """
    async def _run():
        out = []
        async for e in gen:
            raw = e.raw_data
            out.append((e.event, raw if raw in (None, "", "[DONE]") else json.loads(raw)))
        return out

    return asyncio.run(_run())


def summarize(url=URL, uid=None):
    return api_summarize.summarize_video(
        api_summarize.SummarizeRequest(url=url, language="zh"),
        user=None if uid is None else {"id": uid},
    )


def wire(monkeypatch, summarizer=None, extractor=None):
    """把模型与字幕提取换成桩。不联网、不花钱、可重复。"""
    summarizer = summarizer if summarizer is not None else SeamStubSummarizer()
    extractor = extractor if extractor is not None else StubExtractor()
    monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: summarizer)
    monkeypatch.setattr(api_summarize, "_get_extractor", lambda: extractor)
    return summarizer, extractor


def kinds_of(events):
    return [kind for kind, _ in events]


def payload_of(events, name):
    return [payload for kind, payload in events if kind == name]


def video_count(db):
    with db.get_db() as c:
        return c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def history_count(db, uid):
    with db.get_db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM parse_history WHERE user_id = ?", (uid,)
        ).fetchone()[0]


def parse_count_of(db, uid):
    with db.get_db() as c:
        return c.execute(
            "SELECT daily_parse_count FROM users WHERE id = ?", (uid,)
        ).fetchone()[0]


# ── AC1 解析成功后社区视频表有一行，含四项产出 ───────────────

class TestCommunityRowIsWritten:
    def test_one_row_holds_all_four_artifacts(self, db, make_user, monkeypatch):
        """AC1：总结 / 思维导图 / 标签 / 字幕全文，四项都得在这一行里。

        字幕全文是刻意存下来的：后续追问要拿它作上下文，而社区视频是
        全站共享的——不存就意味着每个追问者都得重新下载一次。
        """
        s, ex = wire(monkeypatch, SeamStubSummarizer(
            summary_tokens=("甲", "乙"), mindmap="# 主题\n## 章节",
            tags=("编程", "读书"),
        ), StubExtractor(full_text="字幕全文在这里"))
        uid = make_user()

        collect(summarize(uid=uid))

        row = db.get_video_by_url(URL)
        assert row is not None, "解析成功后社区视频表没有这一行"
        assert video_count(db) == 1, "同一个链接不该出现第二行"
        assert row["status"] == "ready"
        assert row["summary_md"] == "甲乙"
        assert row["mindmap_md"] == "# 主题\n## 章节"
        assert row["tags"] == ["编程", "读书"]
        assert row["subtitle_text"] == "字幕全文在这里"
        assert row["parsed_by"] == uid, "行该记下是谁首次解析的"
        assert s.calls_of("summarize_full_stream") == 1

    def test_stored_tags_are_the_validated_ones(self, db, make_user, monkeypatch):
        """落库的必须是**校验后**的标签，与用户当时看到的完全一致。

        存模型原话的话，社区里会出现词表外的标签，而用户当时看到的
        并没有——同一份内容两种口径。
        """
        s, _ = wire(monkeypatch, SeamStubSummarizer(tags=("AI 编程", "编程", "随便编的")))
        uid = make_user()

        events = collect(summarize(uid=uid))

        assert payload_of(events, "tags") == [["编程"]]
        assert db.get_video_by_url(URL)["tags"] == ["编程"], (
            "落库的是模型原话，不是词表校验后的结果"
        )

    def test_anonymous_request_creates_no_row(self, db, monkeypatch):
        """未登录先拒，且不许占位——否则别人只能对着一个空占位干等。"""
        wire(monkeypatch)
        events = collect(summarize(uid=None))

        assert kinds_of(events) == ["error"]
        assert video_count(db) == 0, "未登录的请求占了位"


# ── AC2 第二次解析：未调模型、未扣额度、返回已有结果 ─────────

class TestSecondParseReusesInsteadOfParsing:
    def test_model_is_not_called_again_per_method(self, db, make_user, monkeypatch):
        """AC2 的核心：逐方法断言「没有调过模型」。

        逐方法而不是总数：总数为 0 也可能是「调了另一个方法」，
        而接缝建立的起因正是思维导图方法曾经不计数。
        """
        s, ex = wire(monkeypatch)
        uid = make_user()

        collect(summarize(uid=uid))
        assert s.calls_of("summarize_full_stream") == 1, "前提：第一次该真调模型"

        collect(summarize(uid=uid))

        assert s.calls_of("summarize_full_stream") == 1, "第二次又调了一次模型"
        assert s.called_methods() == {"summarize_full_stream"}, "出现了第二处模型调用"
        assert s.calls == 1
        assert ex.calls == 1, "第二次连字幕都重跑了一遍"

    def test_quota_is_not_consumed_twice(self, db, make_user, monkeypatch):
        s, _ = wire(monkeypatch)
        uid = make_user()

        collect(summarize(uid=uid))
        assert parse_count_of(db, uid) == 1

        collect(summarize(uid=uid))

        assert parse_count_of(db, uid) == 1, "复用扣了第二次额度"
        assert db.check_quota_kind(uid, "parse") == (
            True, database.DAILY_PARSE_LIMIT - 1
        )

    def test_second_parse_returns_the_stored_result(self, db, make_user, monkeypatch):
        s, _ = wire(monkeypatch, SeamStubSummarizer(
            summary_tokens=("甲", "乙"), mindmap="# 主题", tags=("编程", "读书"),
        ))
        uid = make_user()
        collect(summarize(uid=uid))

        events = collect(summarize(uid=uid))

        row = db.get_video_by_url(URL)
        assert payload_of(events, "summary") == [row["summary_md"]], (
            "复用返回的不是社区里那一份总结"
        )
        assert payload_of(events, "mindmap") == [{"markdown": row["mindmap_md"]}]
        assert payload_of(events, "tags") == [row["tags"]]
        # 字幕全文随行，复用时也一并给回——不重跑提取器
        assert payload_of(events, "subtitle")[0]["full_text"] == row["subtitle_text"]

    def test_event_sequence_is_identical_to_a_fresh_parse(self, db, make_user, monkeypatch):
        """复用走同一套事件名，前端不必为「复用」写第二套分支。

        两处刻意的不一致，理由都写在 _replay_events 的 docstring 里：
        多了 ownership（ADR 0007，回答「这份是不是你自己写的」），
        以及 summary 整段一次下发而不是逐 token——它不是正在生成的。
        除这两条之外事件名一致。
        """
        s, _ = wire(monkeypatch)
        uid = make_user()

        first = collect(summarize(uid=uid))
        second = collect(summarize(uid=uid))

        assert kinds_of(first) == ["subtitle", "quota", "summary", "summary",
                                   "mindmap", "tags", "done"]
        assert kinds_of(second) == ["ownership", "subtitle", "quota", "summary",
                                    "mindmap", "tags", "done"]
        # 去掉 ownership 之后，事件名集合（不是序列：summary 的条数本就不同）
        # 必须与首次解析完全相同，多出或少掉任何一个都是前端要补的分支
        assert set(kinds_of(second)[1:]) == set(kinds_of(first)), (
            kinds_of(second), kinds_of(first)
        )

    def test_quota_event_reports_my_own_unchanged_balance(self, db, make_user, monkeypatch):
        """复用者的额度事件必须是他自己的余额——他没被扣，数字就不该动。

        判据是「两次调用之间数字没有变化」，不是「等于上限」：
        第一次解析确实扣过一次，所以这里比的是前后两次事件本身。
        """
        s, _ = wire(monkeypatch)
        uid = make_user()
        first = collect(summarize(uid=uid))
        before = parse_count_of(db, uid)

        second = collect(summarize(uid=uid))

        (quota,) = payload_of(second, "quota")
        assert quota == payload_of(first, "quota")[0], "复用者的额度数字动了"
        assert parse_count_of(db, uid) == before == 1
        assert quota["remaining"] == database.DAILY_PARSE_LIMIT - 1
        assert quota["limit"] == database.DAILY_PARSE_LIMIT

    def test_a_fresh_user_reusing_sees_a_full_balance(self, db, make_user, monkeypatch):
        """从没花过额度的用户复用时，看到的必须是满额。"""
        s, _ = wire(monkeypatch)
        a = make_user("a@example.com")
        b = make_user("b@example.com")
        collect(summarize(uid=a))

        (quota,) = payload_of(collect(summarize(uid=b)), "quota")

        assert quota["remaining"] == database.DAILY_PARSE_LIMIT
        assert parse_count_of(db, b) == 0

    def test_reuse_does_not_need_the_model_at_all(self, db, make_user, monkeypatch):
        """第二遍把模型换成会炸的桩，照样返回社区那份结果。

        计数断言的独立佐证：只要有一处碰了模型，这里就会转成 error 事件。
        """
        wire(monkeypatch)
        uid = make_user()
        collect(summarize(uid=uid))

        class Exploding(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                raise AssertionError("复用路径碰了模型")

        wire(monkeypatch, Exploding())
        events = collect(summarize(uid=uid))

        assert "error" not in kinds_of(events), events
        assert kinds_of(events)[-1] == "done"

    def test_reuse_is_not_gated_by_quota(self, db, make_user, monkeypatch):
        """额度用满的用户仍然能拿到社区里已有的结果。

        复用的成本是零；额度该管的是「让服务器替你算一遍」，
        不是「看别人已经算好的东西」。
        """
        s, _ = wire(monkeypatch)
        uid = make_user()
        collect(summarize(uid=uid))
        database.refund_quota(uid, "parse")
        for _ in range(database.DAILY_PARSE_LIMIT):
            database.consume_quota(uid, "parse")
        assert db.check_quota_kind(uid, "parse") == (False, 0), "前提：额度已用满"

        events = collect(summarize(uid=uid))

        assert "error" not in kinds_of(events), events
        assert s.calls_of("summarize_full_stream") == 1


# ── AC7 跨用户复用：B 解析 A 已解析的视频，B 的额度不减少 ─────

class TestCrossUserReuse:
    def test_b_gets_a_s_result_without_paying(self, db, make_user, monkeypatch):
        """AC7：这是社区功能存在的理由。"""
        s, ex = wire(monkeypatch, SeamStubSummarizer(
            summary_tokens=("甲", "乙"), mindmap="# 主题", tags=("编程",),
        ))
        a = make_user("a@example.com")
        b = make_user("b@example.com")

        collect(summarize(uid=a))
        events = collect(summarize(uid=b))

        assert s.calls_of("summarize_full_stream") == 1, "B 触发了一次新的模型调用"
        assert s.called_methods() == {"summarize_full_stream"}
        assert ex.calls == 1, "B 重跑了一遍字幕提取"
        assert parse_count_of(db, a) == 1
        assert parse_count_of(db, b) == 0, "B 被扣了额度"
        assert video_count(db) == 1
        assert payload_of(events, "summary") == ["甲乙"]
        assert payload_of(events, "mindmap") == [{"markdown": "# 主题"}]
        assert payload_of(events, "tags") == [["编程"]]

    def test_b_sees_the_same_summary_whichever_order(self, db, make_user, monkeypatch):
        """「不管谁先解析，看到的都是同一份」——反向也要成立。"""
        s, _ = wire(monkeypatch, SeamStubSummarizer(summary_tokens=("只有这一份",)))
        a = make_user("first@example.com")
        b = make_user("second@example.com")

        first = payload_of(collect(summarize(uid=a)), "summary")
        second = payload_of(collect(summarize(uid=b)), "summary")

        assert first == second == ["只有这一份"]
        assert s.calls_of("summarize_full_stream") == 1

    def test_existence_is_decided_by_the_community_table_not_parse_history(
        self, db, make_user, monkeypatch
    ):
        """ADR 0001：parse_history 是个人记录，不得再当「已存在」的判据。

        造一个「历史里有、社区里没有」的局面：命中个人历史就跳过解析的话，
        这次解析会被静默吞掉。
        """
        s, _ = wire(monkeypatch, SeamStubSummarizer(summary_tokens=("新算的",)))
        uid = make_user()
        db.upsert_parse_history(uid, URL, summary_md="历史里的旧总结")

        events = collect(summarize(uid=uid))

        assert s.calls_of("summarize_full_stream") == 1, "命中了 parse_history 的旧缓存"
        assert parse_count_of(db, uid) == 1
        assert db.get_video_by_url(URL)["summary_md"] == "新算的"


# ── AC6 链接在社区视频表上全局唯一 ──────────────────────────

class TestVideoUrlIsGloballyUnique:
    def test_second_reservation_for_the_same_url_is_refused(self, db, make_user):
        outcome, row = db.reserve_video(URL, make_user("a@example.com"))
        assert outcome == "reserved"
        assert row is None

        again, existing = db.reserve_video(URL, make_user("b@example.com"))

        assert again == "pending", "第二个用户抢到了同一个链接的解析权"
        assert existing["status"] == "pending"
        assert video_count(db) == 1

    def test_insert_outside_the_reservation_path_also_hits_the_constraint(
        self, db
    ):
        """唯一性不是 reserve 的内部约定，是表上的约束。

        绕过 reserve 直接插一行也必须被拒——否则将来任何一条新写入路径
        都能塞进第二行。
        """
        db.reserve_video(URL, 1)
        with pytest.raises(sqlite3.IntegrityError):
            with db.get_db() as c:
                c.execute(
                    "INSERT INTO videos (video_url, status) VALUES (?, 'pending')",
                    (URL,),
                )

    def test_the_url_index_is_a_named_global_unique_index(self, db):
        """ADR 0001 点名要一条新的全局唯一索引。

        parse_history 那条 idx_history_user_url 含 user_id，跨用户不管用，
        复用不得——所以这条索引必须是 videos 表自己的、且带名字的。
        """
        with db.get_db() as c:
            rows = c.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'videos'"
            ).fetchall()

        unique_on_url = [
            r for r in rows
            if "UNIQUE" in (r["sql"] or "").upper() and "video_url" in (r["sql"] or "")
        ]
        assert len(unique_on_url) == 1, f"videos 表上的唯一索引：{[r['name'] for r in rows]}"
        assert unique_on_url[0]["name"] == "idx_videos_url"


# ── 社区内容不可改写 ────────────────────────────────────────

class TestCommunityContentIsImmutable:
    def test_completed_row_cannot_be_overwritten(self, db, make_user):
        """「社区内容不会被任何人改写」——包括后来的解析者。

        complete_video 只更新 status='pending' 的行，所以这里连改都改不进去。
        """
        db.reserve_video(URL, 1)
        db.complete_video(URL, summary_md="原来的总结", tags=["编程"])

        touched = db.complete_video(URL, summary_md="被篡改的总结", tags=["健身"])

        assert touched == 0, "已完成的行被覆盖了"
        assert db.get_video_by_url(URL)["summary_md"] == "原来的总结"
        assert db.get_video_by_url(URL)["tags"] == ["编程"]

    def test_releasing_does_not_delete_a_finished_row(self, db, make_user):
        """占位者失败后的还位动作，绝不能误删已经 ready 的社区内容。"""
        owner = make_user()
        db.reserve_video(URL, owner)
        db.complete_video(URL, summary_md="社区内容")

        assert db.release_video(URL, owner) == 0

        assert db.get_video_by_url(URL) is not None, "已完成的行被还位动作删掉了"


# ── AC4 / AC5 两张表各自的裁剪规则 ─────────────────────────

class TestTrimRules:
    def test_community_table_is_never_trimmed(self, db, make_user, monkeypatch):
        """AC4：社区视频表不做条数裁剪。

        判据是「条数超过个人历史上限之后仍然全在」——只要还按那条上限
        裁，社区价值就没了：A 解析的视频被裁掉，B 就再也复用不到。

        这里把上限**钉回 30**：这条要证明的是「社区表不受个人历史上限影响」，
        而不是「上限现在是 1000」（那是下一条独立断言的事）。跟着真实
        上限走的话，1005 次 collect 会先撞上 DAILY_PARSE_LIMIT，被额度
        挡下来后 video_count 停在 40——症状和「社区表被裁剪」一模一样，
        实测就是这么误判过一次。额度也一并抬到 total 之上。
        """
        monkeypatch.setattr(db, "MAX_PARSE_HISTORY_PER_USER", 30)
        wire(monkeypatch)
        uid = make_user()
        total = 35
        monkeypatch.setattr(db, "DAILY_PARSE_LIMIT", total + 10)

        for i in range(total):
            collect(summarize(url=f"{URL}/{i}", uid=uid))

        assert video_count(db) == total, (
            f"社区视频表只剩 {video_count(db)} 条，应为 {total} 条——被裁剪了"
        )

    def test_the_cap_is_a_thousand(self, db):
        """真实上限就是 1000，且 30 那个数不是被人忘了改回去。

        单独一条，而不是在别处顺带断言：上面两条把上限 monkeypatch 掉了，
        它们对真实取值**完全没有发言权**。
        """
        assert db.MAX_PARSE_HISTORY_PER_USER == 1000

    def test_parse_history_is_still_trimmed(self, db, make_user, monkeypatch):
        """AC5：解析历史的滚动删除**原样保留**，没有被这次改动带偏。

        滚动删除本身是承重的：没有它，个人历史会无限增长。
        """
        monkeypatch.setattr(db, "MAX_PARSE_HISTORY_PER_USER", 30)
        uid = make_user()
        for i in range(35):
            db.upsert_parse_history(uid, f"{URL}/h{i}")

        assert history_count(db, uid) == 30, "解析历史的滚动删除被改动了"

    def test_favourites_are_never_trimmed_away(self, db, make_user,
                                               monkeypatch):
        """收藏不参与裁剪 —— 这条是收藏功能存在的全部理由。

        越线那一刻被删的永远是最旧的那条，而那恰好可能是用户特意标星
        的。界面上没有任何一处提示过「收藏也会被滚掉」，所以那是一次
        静默的数据丢失。

        判据形状：把**最早**的几条标星（它们正是最先被裁的），再插到
        越线。若实现改成「先裁后看收藏」，这里会直接掉到 30。
        """
        monkeypatch.setattr(db, "MAX_PARSE_HISTORY_PER_USER", 30)
        uid = make_user()

        # 先插 10 条并给最早的 5 条打星。**必须先打星再插满**：
        # upsert_parse_history 自己就会调 _trim_parse_history，一次性插满
        # 35 条的话，最早那几条在测试还没收藏它们之前就已经被裁掉了 ——
        # 实测过一次，set_parse_history_favorite 对着不存在的行返回 False，
        # 症状看着像收藏功能坏了，其实是测试在给尸体发请求。
        early = [db.upsert_parse_history(uid, f"{URL}/h{i}") for i in range(10)]
        starred = early[:5]
        for hid in starred:
            assert db.set_parse_history_favorite(uid, hid, True) is True

        # 再插到越线，让裁剪真的对着已收藏的行跑一遍
        for i in range(10, 40):
            db.upsert_parse_history(uid, f"{URL}/h{i}")

        # 再收藏一条**最近**的。子查询里那层 is_favorite = 0 只有在
        # 收藏项落进「最新 N 条」时才看得出来：留着它，保留的是 30 条
        # 未收藏 + 6 条收藏；去掉它，被子查询选中的 30 条里已经占掉 1 个
        # 收藏名额，未收藏就只剩 29 条。只收藏最早的 5 条看不出差别 ——
        # 那时最新 30 条本来就没收藏项，两种写法结果一样。
        recent = db.upsert_parse_history(uid, f"{URL}/recent")
        assert db.set_parse_history_favorite(uid, recent, True) is True
        starred = starred + [recent]

        # 再插一条把裁剪**重新**触发一次。这一步不是多余的：打星本身不
        # 触库（服务端刻意不改 updated_at），所以标完星那一刻根本没有
        # 新的裁剪跑过 —— 那条 recent 用的还是它没被收藏时就分到的名额。
        # 不补这一插，测试量的只是「收藏不占名额」，不是「上限只算未收藏」。
        db.upsert_parse_history(uid, f"{URL}/trigger")

        with db.get_db() as conn:
            kept = {r["id"] for r in conn.execute(
                "SELECT id FROM parse_history WHERE user_id = ?", (uid,)
            ).fetchall()}

        for hid in starred:
            assert hid in kept, f"收藏记录 {hid} 被滚动删除了"
        # 精确值：6 条收藏（永不裁剪）+ 30 条未收藏（裁到上限）= 36。
        # 写成 >= 的话，裁剪整个停摆也照样绿；写成 35 的话，子查询里
        # 那层收藏过滤被删掉也照样绿。
        assert history_count(db, uid) == 36, (
            f"应为 6 条收藏 + 30 条未收藏 = 36，实际 {history_count(db, uid)}"
        )

    def test_the_two_tables_keep_independent_rows(self, db, make_user, monkeypatch):
        """职责分离：社区表按链接唯一，历史表按 (user_id, video_url) 去重。"""
        wire(monkeypatch)
        a = make_user("a@example.com")
        b = make_user("b@example.com")

        collect(summarize(uid=a))
        collect(summarize(uid=b))
        db.upsert_parse_history(a, URL)
        db.upsert_parse_history(b, URL)

        assert video_count(db) == 1, "社区表里同一个链接出现了多行"
        assert history_count(db, a) == 1
        assert history_count(db, b) == 1, "个人历史是按用户各自的记录"


# ── 占位失败时必须还位，否则链接被永久卡死 ──────────────────

class TestPlaceholderIsAlwaysReleased:
    def test_failed_model_call_releases_the_placeholder(self, db, make_user, monkeypatch):
        """模型失败：退款（工单 #4）之外，还必须把解析权还回去。

        不还的话那一行永远停在 pending，后来的人只能等到超时，
        这个链接从此再也没人能解析。
        """
        class Exploding(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                self._calls["summarize_full_stream"] += 1
                yield ("summary", "半个总结")
                raise RuntimeError("模型服务不可用")

        s, _ = wire(monkeypatch, Exploding())
        uid = make_user()

        events = collect(summarize(uid=uid))

        assert kinds_of(events)[-1] == "error"
        assert parse_count_of(db, uid) == 0, "模型失败不该白扣额度"
        assert db.get_video_by_url(URL) is None, (
            "占位没还回去：这个链接会永久停在「正在解析」"
        )
        assert video_count(db) == 0

    def test_next_user_can_parse_after_the_previous_one_failed(
        self, db, make_user, monkeypatch
    ):
        """前一任失败后，换个人必须能立刻解析同一个链接。"""
        class Exploding(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                raise RuntimeError("模型服务不可用")

        s, _ = wire(monkeypatch, Exploding())
        failed = make_user("failed@example.com")
        collect(summarize(uid=failed))

        s, _ = wire(monkeypatch, SeamStubSummarizer(summary_tokens=("这次成功了",)))
        next_user = make_user("next@example.com")
        events = collect(summarize(uid=next_user))

        assert kinds_of(events)[-1] == "done", events
        assert s.calls_of("summarize_full_stream") == 1
        assert db.get_video_by_url(URL)["summary_md"] == "这次成功了"

    def test_quota_refusal_also_releases_the_placeholder(self, db, make_user, monkeypatch):
        """额度不够的请求已经占了位——它必须还回去。

        造法说明：额度要靠**不同**链接耗尽。同一个链接第二次开始就是复用，
        根本不会扣额度（工单 #6 的语义）。
        """
        s, _ = wire(monkeypatch)
        uid = make_user()
        for i in range(database.DAILY_PARSE_LIMIT):
            collect(summarize(url=f"{URL}/warm{i}", uid=uid))
        assert db.check_quota_kind(uid, "parse")[0] is False, "前提：额度已用满"

        events = collect(summarize(url=f"{URL}/blocked", uid=uid))

        assert kinds_of(events) == ["error"]
        assert s.calls_of("summarize_full_stream") == database.DAILY_PARSE_LIMIT, (
            "额度用满后不该再调模型"
        )
        assert db.get_video_by_url(f"{URL}/blocked") is None, (
            "被拒绝的请求留下了占位，这个链接会永久卡住"
        )

    def test_waiter_times_out_instead_of_hanging_forever(self, db, make_user, monkeypatch):
        """占位者迟迟不出现：明确告诉用户「正在解析」，而不是无限期挂着。

        造法：先手工占一个位（模拟一个卡住的占位者），再把等待上限调到很小。
        """
        s, _ = wire(monkeypatch)
        monkeypatch.setattr(video_claim, "VIDEO_WAIT_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(video_claim, "VIDEO_POLL_INTERVAL_SECONDS", 0.01)
        uid = make_user()
        db.reserve_video(URL, make_user("stuck@example.com"))

        events = collect(summarize(uid=uid))

        assert kinds_of(events) == ["error"]
        assert "正在解析" in events[0][1]["message"], events[0][1]
        assert s.calls_of("summarize_full_stream") == 0, "没抢到位置却调了模型"
        assert parse_count_of(db, uid) == 0
        # 等待者不得顺手把别人的占位删掉
        assert db.get_video_by_url(URL) is not None, "等待者删掉了别人的占位"


# ── AC3 两个用户同时解析同一链接 ────────────────────────────
#
# 这里必须真并发 + 强制交错：单线程接缝永远测不出竞态，两次调用若不落在
# 同一时间窗，测试就会假通过。两个设计决定必须写下来，否则下一个人会
# 「优化」掉它们：
#
# 1. 同步点**不能**用「模型调用里的 Barrier(2)」：实现正确时第二个请求
#    根本不会进模型调用，屏障永远等不到第二个人，测试会挂死。
#    真正危险的交错是「占位者正卡在模型里，后来者在占位期间抵达」，
#    所以同步点由两个 Event 组成——entered（模型桩一被调用就按下）与
#    released（测试放行）。第二个请求只在 entered 之后才允许启动。
# 2. 「谁是占位者」是确定的：先到的那个。把它断言住，同步点一旦被拿掉，
#    这条就会红（后到的会抢到先机，事件序列整个对调）。

#: 让第二个请求确实走到占位判定上所需的等待。它只影响「B 走等待分支
#: 还是走 ready 分支」，不影响任何一条不变式断言，所以取值保守也不会
#: 让测试变脆。
_ARRIVE_SETTLE_SECONDS = 0.3


class _GatedSummarizer(SeamStubSummarizer):
    """把占位者按在模型调用里，直到测试放行。

    计数与产出协议仍由接缝桩负责，这里只做「拦住」这一件事：
    自己重新实现一遍 yield 协议的话，接缝改了协议这条测试会跟着一起变绿。
    """

    def __init__(self, entered, released, **kwargs):
        super().__init__(**kwargs)
        self.entered = entered
        self.released = released

    def summarize_full_stream(self, text, language, template=None):
        # 注意这个方法**不是**生成器（里面没有 yield）：它只拦住
        # 「进入模型调用」这一刻，然后把接缝桩自己的生成器原样交回去。
        self.entered.set()
        if not self.released.wait(10.0):
            raise RuntimeError("模型桩没等到放行——测试的同步点失效了")
        return super().summarize_full_stream(text, language)


class _TwoRequests:
    """两个用户各跑一次解析，各自一条真线程。

    线程里抛出的异常会被原样带回主线程：线程里的异常默认只会打印一行
    警告，测试照样绿——那正是本文件最不能出现的假通过。
    """

    def __init__(self, url):
        self.url = url
        self.results = {}
        self.done = {"a": threading.Event(), "b": threading.Event()}
        self.threads = {}

    def start(self, key, uid, gate=None):
        def _body():
            try:
                if gate is not None:
                    gate()
                self.results[key] = collect(summarize(self.url, uid=uid))
            except BaseException as exc:  # noqa: BLE001 —— 要原样带回主线程
                self.results[key] = exc
            finally:
                self.done[key].set()

        thread = threading.Thread(target=_body, name=f"community-{key}")
        self.threads[key] = thread
        thread.start()
        return thread

    def join(self, timeout=15.0):
        for thread in self.threads.values():
            thread.join(timeout)
        stuck = [k for k, t in self.threads.items() if t.is_alive()]
        assert not stuck, f"线程没退出：{stuck}"

    def events(self, key):
        out = self.results[key]
        if isinstance(out, BaseException):
            raise out
        return out


class TestConcurrentSameUrl:
    def test_late_arriver_waits_then_reuses_the_finished_result(
        self, db, make_user, monkeypatch
    ):
        """AC3 的完整版：先到的解析，后到的复用。

        三条不变式一起断言——只有一次模型调用、一次额度扣减、一行数据——
        少任何一条都说明并发保护漏了一半（只挡重复行、没挡重复调用，
        正是唯一约束单独使用时做不到的事）。
        """
        entered, released = threading.Event(), threading.Event()
        s = _GatedSummarizer(
            entered, released,
            summary_tokens=("甲", "乙"), mindmap="# 主题", tags=("编程",),
        )
        ex = StubExtractor(full_text="字幕全文")
        wire(monkeypatch, s, ex)
        monkeypatch.setattr(video_claim, "VIDEO_WAIT_TIMEOUT_SECONDS", 20.0)
        monkeypatch.setattr(video_claim, "VIDEO_POLL_INTERVAL_SECONDS", 0.01)

        a = make_user("a@example.com")
        b = make_user("b@example.com")
        pair = _TwoRequests(URL)

        pair.start("a", a)
        assert entered.wait(10.0), "第一个请求没有进入模型调用"
        # 此刻社区表里那行是 pending——位置已经被唯一索引占住了
        assert db.get_video_by_url(URL)["status"] == "pending", "前提：占位还没完成"

        # 第二个请求在占位者卡在模型里的时候抵达——这就是那个窗口
        pair.start("b", b)
        time.sleep(_ARRIVE_SETTLE_SECONDS)
        assert not pair.done["b"].is_set(), (
            "占位者还卡在模型里，第二个请求就返回了——它拿到的绝不该是结果"
        )

        released.set()
        pair.join()

        assert s.calls_of("summarize_full_stream") == 1, "两次请求各调了一次模型"
        assert s.called_methods() == {"summarize_full_stream"}, "出现了第二处模型调用"
        assert s.calls == 1
        assert ex.calls == 1, "第二个请求重跑了字幕提取"
        assert parse_count_of(db, a) + parse_count_of(db, b) == 1, "额度被扣了两次"
        assert video_count(db) == 1, "同一个链接落了两行"

        first, second = pair.events("a"), pair.events("b")
        row = db.get_video_by_url(URL)

        # 先到的是占位者：完整流式路径
        assert kinds_of(first) == ["subtitle", "quota", "summary", "summary",
                                   "mindmap", "tags", "done"], first
        # 后到的是复用者：拿到完成后的那一份，不是半成品
        # （前置 ownership：告诉前端这个人能不能覆盖它，ADR 0007）
        assert kinds_of(second) == ["ownership", "subtitle", "quota", "summary",
                                    "mindmap", "tags", "done"], second
        assert payload_of(second, "summary") == [row["summary_md"]], second
        assert payload_of(second, "mindmap") == [{"markdown": row["mindmap_md"]}]
        assert payload_of(second, "tags") == [row["tags"]]
        assert payload_of(second, "subtitle")[0]["full_text"] == "字幕全文"
        assert parse_count_of(db, a) == 1, "额度该记在首次解析者头上"
        assert parse_count_of(db, b) == 0, "复用者被扣了额度"

    def test_two_requests_arriving_together_still_cost_one_call(
        self, db, make_user, monkeypatch
    ):
        """屏障放在**路由调用之前**：两个线程在同一瞬间一起进场。

        不预设谁抢到占位，断言全部写成顺序无关的不变式。
        """
        entered, released = threading.Event(), threading.Event()
        s = _GatedSummarizer(
            entered, released,
            summary_tokens=("唯一一份",), mindmap="# 唯一", tags=("编程",),
        )
        ex = StubExtractor(full_text="字幕全文")
        wire(monkeypatch, s, ex)
        monkeypatch.setattr(video_claim, "VIDEO_WAIT_TIMEOUT_SECONDS", 20.0)
        monkeypatch.setattr(video_claim, "VIDEO_POLL_INTERVAL_SECONDS", 0.01)

        a = make_user("a@example.com")
        b = make_user("b@example.com")
        start = threading.Barrier(2, timeout=10)
        pair = _TwoRequests(URL)

        pair.start("a", a, gate=start.wait)
        pair.start("b", b, gate=start.wait)
        assert entered.wait(10.0), "两个请求都没进入模型调用"
        time.sleep(_ARRIVE_SETTLE_SECONDS)
        released.set()
        pair.join()

        assert s.calls_of("summarize_full_stream") == 1, "两次请求各调了一次模型"
        assert s.called_methods() == {"summarize_full_stream"}
        assert parse_count_of(db, a) + parse_count_of(db, b) == 1
        assert video_count(db) == 1
        assert db.get_video_by_url(URL)["summary_md"] == "唯一一份"

        # 两边看到的内容必须一致：一份是流式出来的，一份是复用的
        seen = [
            payload_of(pair.events(key), "summary") for key in ("a", "b")
        ]
        assert seen[0] == ["唯一一份"], seen
        assert seen[1] == ["唯一一份"], seen



# ── 陈旧占位回收（独立复审 MEDIUM-1）─────────────────────────

class TestStalePlaceholderReclamation:
    """陈旧占位必须能被接管——否则进程一死，那个链接就永久不可解析。

    独立复审抓到的洞：`release_video` 只在请求的 `finally` 里跑。进程被杀、
    机器断电、模型无限挂起时它跑不到那里，那一行就永远停在 `pending`，
    之后**任何**人都拿不到「首次解析者」身份，只能一直等到超时。
    """

    @staticmethod
    def _age_row(seconds: int) -> None:
        """把占位行的 updated_at 往前推，模拟「占位者已经死了这么久」。"""
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds)
        ).isoformat()
        with database.get_db() as conn:
            conn.execute("UPDATE videos SET updated_at = ?", (old,))

    def test_stale_pending_is_taken_over(self, db, make_user):
        first = make_user("first@example.com")
        second = make_user("second@example.com")
        assert db.reserve_video(URL, first)[0] == "reserved"

        self._age_row(db.VIDEO_PENDING_TTL_SECONDS + 60)

        outcome, _row = db.reserve_video(URL, second)
        assert outcome == "reserved", "陈旧占位没被接管，这个链接会永久卡死"

    def test_fresh_pending_is_not_stolen(self, db, make_user):
        """反向检查：还在正常工作的占位**不能**被偷走。

        偷走 = 两个人同时调模型、同时扣额度——正是本设计要防的那件事。
        所以 TTL 必须大于一次正常解析的最长耗时，这条测试守着这个前提。
        """
        first = make_user("first@example.com")
        second = make_user("second@example.com")
        assert db.reserve_video(URL, first)[0] == "reserved"

        outcome, _row = db.reserve_video(URL, second)
        assert outcome == "pending", "新鲜占位被偷走了：会出现重复调模型 + 重复扣额度"

    def test_ready_row_is_never_taken_over(self, db, make_user):
        """已完成的结果谁都改不了——哪怕它已经很「旧」。"""
        first = make_user("first@example.com")
        second = make_user("second@example.com")
        db.reserve_video(URL, first)
        db.complete_video(URL, summary_md="社区里那一份")
        self._age_row(db.VIDEO_PENDING_TTL_SECONDS * 10)

        outcome, row = db.reserve_video(URL, second)
        assert outcome == "ready", "已完成的内容被当成可抢占的占位了"
        assert row["summary_md"] == "社区里那一份"
