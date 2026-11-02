"""路由级：SSE 事件顺序与配额扣减时机（bug 1 + bug 2）。
用桩替掉字幕提取和 LLM —— 不联网、不花钱、可重复。

工单 #4 之后额度拆成两个计数器（解析 / 追问）。本文件原先锁定
「总结与问答共用一个额度」，那些用例已按新语义重写：
每条仍然守它最初要守的那件事（扣减时机、扣几个、拒绝时是否调模型），
只是额度种类换成了对应的那个。
断言一律读 daily_parse_count / daily_chat_count——旧的
daily_summary_count 在工单 #4 之后已无生产写入者，拿它断言等于恒真。

工单 #5 之后一次解析产出三项（总结 / 思维导图 / 标签），事件序列尾部
多了 tags，模型从两次调用变成一次。下面既有用例守的扣减时机没变。
"""
import asyncio
import json
import re

import pytest

import api_summarize
import database
import summarizer
import tags
from seams import StubSummarizer as SeamStubSummarizer

FAR_FUTURE = "2099-01-01T00:00:00+00:00"
SENTINEL = summarizer.MINDMAP_TAGS_SENTINEL


def parse_count_of(uid):
    with database.get_db() as c:
        row = c.execute("SELECT daily_parse_count FROM users WHERE id=?", (uid,)).fetchone()
    return row["daily_parse_count"]


def chat_count_of(uid):
    with database.get_db() as c:
        row = c.execute("SELECT daily_chat_count FROM users WHERE id=?", (uid,)).fetchone()
    return row["daily_chat_count"]


def heading_levels_of(md):
    """一段 Markdown 里的标题层级（列表叶子不占层级）。"""
    levels = []
    for line in md.splitlines():
        m = re.match(r"^(#{1,6})\s+\S", line.strip())
        if m:
            levels.append(len(m.group(1)))
    return levels


def leaf_count_of(md):
    """一段 Markdown 里的列表叶子（`- xxx`）数量。"""
    return sum(
        1 for line in md.splitlines() if re.match(r"^[-*+•]\s+\S", line.strip())
    )


class StubExtractor:
    def __init__(self, has_subtitle):
        self.has = has_subtitle

    def extract(self, url):
        return {"has_subtitle": self.has, "full_text": "transcript" if self.has else "", "segments": []}


class StubSummarizer:
    """本文件既有的桩：只数总调用次数，够守「扣几个 / 调没调」。

    新方法的产出形状对齐生产协议（summary token → mindmap → tags）。
    「模型只被调一次」这类逐方法断言用 seams.StubSummarizer，不靠它。
    """

    def __init__(self):
        self.calls = 0

    def summarize_full_stream(self, text, language, template=None):
        self.calls += 1
        yield ("summary", "tok-a")
        yield ("summary", "tok-b")
        yield ("mindmap", "# mindmap")
        yield ("tags", ["编程"])

    def chat_stream(self, text, question, history=()):
        self.calls += 1
        yield "answer-1"


@pytest.fixture()
def stub(monkeypatch):
    """返回 set_subtitles(has_subtitle) → StubSummarizer"""
    holder = {}

    def set_subtitles(has_subtitle):
        s = StubSummarizer()
        monkeypatch.setattr(api_summarize, "_get_extractor", lambda: StubExtractor(has_subtitle))
        monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: s)
        holder["s"] = s
        return s

    return set_subtitles


def collect(gen):
    """跑完一个 SSE 异步生成器，返回 [(event, payload)]。
    注意 payload 在 raw_data 上 —— ServerSentEvent 是 Pydantic 模型，data 与 raw_data 互斥。"""
    async def _run():
        out = []
        async for e in gen:
            raw = e.raw_data
            out.append((e.event, raw if raw in (None, "", "[DONE]") else json.loads(raw)))
        return out

    return asyncio.run(_run())


def summarize_req(url="https://example.com/v"):
    return api_summarize.SummarizeRequest(url=url, language="zh")


def chat_req(subtitle="s"):
    return api_summarize.ChatRequest(url="u", question="q", subtitle_text=subtitle)


class ReasonStubExtractor:
    """按给定字典原样返回的提取器——用来喂各种 fail_reason 组合。"""

    def __init__(self, payload):
        self.payload = payload

    def extract(self, url):
        return dict(self.payload)


class TestSubtitleFailureReason:
    """字幕拿不到时，错误事件要说清「为什么」，而不是笼统一句「没有字幕」。

    这里守的是两件事：
    1. 两种结局（「平台没给字幕轨道」与「给了但取不下来」）在用户看得见的
       文案里必须不同——它们对应完全不同的排查方向。
    2. 新键是**可选**的。StubExtractor 至今仍返回旧形状，任何一条没跟上
       新返回结构的路径都不该在这里变成 KeyError。
    """

    def _run(self, monkeypatch, db, make_user, payload, email="u@example.com"):
        monkeypatch.setattr(
            api_summarize, "_get_extractor", lambda: ReasonStubExtractor(payload)
        )
        monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: StubSummarizer())
        uid = make_user(email)
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
        return events, uid

    def test_no_track_and_fetch_failure_read_differently(
        self, db, make_user, monkeypatch
    ):
        from summarizer import FAIL_FETCH_FAILED, FAIL_NO_TRACK

        no_track, _ = self._run(monkeypatch, db, make_user, {
            "has_subtitle": False, "full_text": "", "segments": [],
            "fail_reason": FAIL_NO_TRACK,
            "asr_fail_reason": "asr_not_configured",
        }, email="no-track@example.com")
        fetch_failed, _ = self._run(monkeypatch, db, make_user, {
            "has_subtitle": False, "full_text": "", "segments": [],
            "fail_reason": FAIL_FETCH_FAILED,
            "asr_fail_reason": "asr_failed",
        }, email="fetch-failed@example.com")

        no_msg = no_track[1][1]["message"]
        fetch_msg = fetch_failed[1][1]["message"]
        assert no_msg != fetch_msg, "两种落穿不该给出同一句提示"
        assert "没有字幕" in no_msg, no_msg
        assert "没能取下来" in fetch_msg, fetch_msg
        # 运维视角也要能看到「服务端没配语音识别」这条
        assert "未配置语音识别" in no_msg, no_msg
        assert "也没成功" in fetch_msg, fetch_msg

        # 机器可读的原因一并下发，前端将来要分开提示时不必再猜
        assert no_track[1][1]["reason"] == FAIL_NO_TRACK
        assert fetch_failed[1][1]["reason"] == FAIL_FETCH_FAILED
        assert no_track[1][1]["asr_reason"] == "asr_not_configured"

    def test_old_shaped_payload_does_not_raise_key_error(
        self, db, make_user, monkeypatch
    ):
        """提取器仍返回旧形状（无新键）时退回笼统文案，而不是 KeyError。"""
        events, uid = self._run(monkeypatch, db, make_user, {
            "has_subtitle": False, "full_text": "", "segments": [],
        })
        assert [e[0] for e in events] == ["subtitle", "error"], events
        payload = events[1][1]
        assert payload["reason"] == "" and payload["asr_reason"] == ""
        assert "没有可用的字幕" in payload["message"], payload
        assert parse_count_of(uid) == 0, "无字幕却扣了额度"

    def test_chat_stream_reports_reason_too(
        self, db, make_user, monkeypatch
    ):
        from summarizer import FAIL_FETCH_FAILED

        monkeypatch.setattr(api_summarize, "_get_extractor", lambda: ReasonStubExtractor({
            "has_subtitle": False, "full_text": "", "segments": [],
            "fail_reason": FAIL_FETCH_FAILED, "asr_fail_reason": "asr_failed",
        }))
        monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: StubSummarizer())
        uid = make_user()
        events = collect(
            api_summarize.chat_with_video(chat_req(subtitle=""), user={"id": uid})
        )
        payload = next(e[1] for e in events if e[0] == "error")
        assert payload["reason"] == FAIL_FETCH_FAILED, payload
        assert "无法回答问题" in payload["message"], payload
        assert chat_count_of(uid) == 0, "无字幕却扣了对话额度"


class TestSummarizeQuotaTiming:
    def test_no_subtitle_does_not_consume(self, db, make_user, stub):
        s = stub(has_subtitle=False)
        uid = make_user()
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
        assert [e[0] for e in events] == ["subtitle", "error"], events
        assert "没有可用的字幕" in events[1][1]["message"], events
        assert parse_count_of(uid) == 0, "无字幕却扣了额度"
        assert s.calls == 0, "无字幕却调了 LLM"
        assert db.check_quota_kind(uid, "parse") == (True, db.DAILY_PARSE_LIMIT), (
            "无字幕时解析额度应原封不动"
        )

    def test_real_summary_consumes_exactly_one(self, db, make_user, stub):
        s = stub(has_subtitle=True)
        uid = make_user()
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
        assert [e[0] for e in events] == [
            "subtitle", "quota", "summary", "summary", "mindmap", "tags", "done",
        ]
        assert parse_count_of(uid) == 1
        quota = next(e[1] for e in events if e[0] == "quota")
        assert quota["remaining"] == database.DAILY_PARSE_LIMIT - 1
        assert quota["limit"] == database.DAILY_PARSE_LIMIT
        assert quota["unlimited"] is False
        assert s.calls == 1

    def test_guard_refusal_costs_zero_even_when_check_passed(self, db, make_user, stub, monkeypatch):
        """扣减阶段的守卫说满了，就**不许调模型**——工单 #17。

        与 `test_fourth_use_refused_without_llm` 守的是**不同的东西**：
        那条是「只读判定 `check_quota_kind` 判死」，本条是
        「只读判定放行、扣减处的守卫拒绝」。

        真实漏洞窗口正在两者之间那几十秒（字幕提取 + 模型调用准备）：
        并发请求全部通过只读判定，然后全部挤到扣减处。

        造法：在 `_get_extractor`（**只读判定之后、扣减之前**的窗口）里
        偷跑一次 consume 把额度用满。

        **判据必须盯住模型调用次数**——这是「白送平台付费资源」的直接后果。
        只断「最后一个事件是 error」不够：守卫失效时后面仍有别的失败路径
        会产出 error 事件（本例实测：守卫失效该用例仍绿），
        真正区分两种实现的是「模型有没有被调」。
        """
        s = stub(has_subtitle=True)
        uid = make_user()
        # 压到只剩 1 次：只读判定会放行
        with database.get_db() as c:
            today = database.datetime.now(database.timezone.utc).strftime("%Y-%m-%d")
            c.execute(
                "UPDATE users SET daily_parse_count = ?, last_parse_date = ? WHERE id = ?",
                (database.DAILY_PARSE_LIMIT - 1, today, uid),
            )

        real_get_extractor = api_summarize._get_extractor
        state = {"stolen": False}

        def stealing_get_extractor():
            if not state["stolen"]:
                state["stolen"] = True
                database.consume_quota(uid, "parse")
            return real_get_extractor()

        monkeypatch.setattr(api_summarize, "_get_extractor", stealing_get_extractor)

        collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))

        assert state["stolen"], "前提没成立：提取器没被取过，窗口没造出来"
        assert parse_count_of(uid) == database.DAILY_PARSE_LIMIT, (
            f"窗口里偷的那次额度没生效（计数 {parse_count_of(uid)}），用例前提不成立"
        )
        assert s.calls == 0, (
            f"额度已满却调了 {s.calls} 次模型 —— 白送平台付费资源。"
            "扣减处的守卫必须在这一步中止请求。"
        )

    def test_quota_event_arrives_before_any_token(self, db, make_user, stub):
        """额度必须早于正文下发，否则前端拿不到。"""
        stub(has_subtitle=True)
        uid = make_user()
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
        kinds = [e[0] for e in events]
        assert kinds.index("quota") < kinds.index("summary")

    def test_fourth_use_refused_without_llm(self, db, make_user, stub):
        """解析额度用满后拒绝，且**不调模型**——这条守的是「拒绝时零成本」。

        每次必须用**不同**的链接：工单 #6 之后同一链接第二次起是免费复用，
        拿同一个 URL 连打并不会耗额度，用例会失去它要测的前提。
        """
        s = stub(has_subtitle=True)
        uid = make_user()
        for i in range(database.DAILY_PARSE_LIMIT):
            collect(
                api_summarize.summarize_video(
                    summarize_req(f"https://example.com/v/{i}"), user={"id": uid}
                )
            )
        events = collect(
            api_summarize.summarize_video(
                summarize_req("https://example.com/v/overflow"), user={"id": uid}
            )
        )
        assert [e[0] for e in events] == ["error"]
        assert "解析次数已用完" in events[0][1]["message"], events[0][1]["message"]
        assert parse_count_of(uid) == database.DAILY_PARSE_LIMIT
        assert s.calls == database.DAILY_PARSE_LIMIT

    def test_anonymous_refused(self, db, stub):
        stub(has_subtitle=True)
        events = collect(api_summarize.summarize_video(summarize_req(), user=None))
        assert [e[0] for e in events] == ["error"]
        assert events[0][1]["need_login"] is True
        assert events[0][1]["need_vip"] is False


class TestSingleCallThreeArtifacts:
    """工单 #5 的验收：一次解析产出总结 + 思维导图 + 标签，模型只被调一次。

    这里用 seams.StubSummarizer（别名 SeamStubSummarizer）而不是本文件那个
    总计数桩：核心 AC 是「模型只被调一次」，而总计数证明不了「调的是哪个
    方法」——接缝建立的起因正是思维导图方法不计数，导致「未调用模型」
    这类断言假通过。
    """

    def _run(self, monkeypatch, make_user, summarizer, email):
        """按给定桩跑一次解析，返回 (事件列表, 用户 id)。"""
        monkeypatch.setattr(
            api_summarize, "_get_extractor", lambda: StubExtractor(has_subtitle=True)
        )
        monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: summarizer)
        uid = make_user(email)
        return collect(api_summarize.summarize_video(
            summarize_req(), user={"id": uid}
        )), uid

    @staticmethod
    def _payloads(events, name):
        return [payload for kind, payload in events if kind == name]

    def test_three_artifacts_come_out_of_one_request(self, db, make_user, monkeypatch):
        s = SeamStubSummarizer(
            summary_tokens=("甲", "乙"), mindmap="# 主题\n## 章节", tags=("编程", "读书"),
        )
        events, _ = self._run(monkeypatch, make_user, s, "three@example.com")

        assert self._payloads(events, "summary") == ["甲", "乙"], "总结应逐 token 原样下发"
        assert self._payloads(events, "mindmap") == [{"markdown": "# 主题\n## 章节"}]
        assert self._payloads(events, "tags") == [["编程", "读书"]]
        assert [e[0] for e in events][-1] == "done"

    def test_model_is_called_exactly_once(self, db, make_user, monkeypatch):
        """核心 AC：逐方法断言——三个产出同属这一次调用。"""
        s = SeamStubSummarizer()
        self._run(monkeypatch, make_user, s, "once@example.com")

        assert s.calls_of("summarize_full_stream") == 1
        # 「只有这一个方法被调过」比「某个方法没被调」更强：
        # 任何第二处模型调用（哪怕是重名的旧方法）都会让它变红
        assert s.called_methods() == {"summarize_full_stream"}
        assert s.calls_of("generate_mindmap") == 0, "思维导图不该再单独调一次模型"
        assert s.calls_of("summarize_stream") == 0, "总结不该再走那条旧路径"
        assert s.calls == 1, "一次解析只该调一次模型"

    def test_three_artifacts_cost_one_quota(self, db, make_user, monkeypatch):
        """三项产出只扣 1 次：额度条不是每样减 1。"""
        s = SeamStubSummarizer()
        events, uid = self._run(monkeypatch, make_user, s, "quota1@example.com")

        assert parse_count_of(uid) == 1
        assert [k for k, _ in events].count("tags") == 1
        assert [k for k, _ in events].count("mindmap") == 1
        quota = next(p for k, p in events if k == "quota")
        assert quota["remaining"] == database.DAILY_PARSE_LIMIT - 1
        assert db.check_quota_kind(uid, "parse") == (True, database.DAILY_PARSE_LIMIT - 1)

    def test_out_of_vocabulary_tags_never_reach_the_client(self, db, make_user, monkeypatch):
        """模型自创的词一个都不能下发——ADR 0005 说的服务端兜底。"""
        s = SeamStubSummarizer(tags=("AI 编程", "编程", "随便编的"))
        events, _ = self._run(monkeypatch, make_user, s, "vocab@example.com")

        (payload,) = self._payloads(events, "tags")
        assert payload == ["编程"], payload

    def test_empty_tags_fall_back_to_other(self, db, make_user, monkeypatch):
        """模型一个都没给、或给的全是词表外时下发 ["其他"]，数组永不为空。"""
        for tags in ((), ("不在词表里", "也不在")):
            s = SeamStubSummarizer(tags=tags)
            events, _ = self._run(
                monkeypatch, make_user, s, f"empty{len(tags)}@example.com"
            )
            (payload,) = self._payloads(events, "tags")
            assert payload == ["其他"], (tags, payload)

    def test_model_giving_five_tags_is_capped_at_three(self, db, make_user, monkeypatch):
        s = SeamStubSummarizer(tags=("读书", "健身", "旅行", "摄影", "影视"))
        events, _ = self._run(monkeypatch, make_user, s, "cap@example.com")

        (payload,) = self._payloads(events, "tags")
        assert len(payload) == 3, payload
        assert set(payload) <= {"读书", "健身", "旅行", "摄影", "影视"}

    def test_failed_single_call_refunds_the_quota(self, db, make_user, monkeypatch):
        """模型失败不白扣：工单 #4 的唯一 finally 回滚点仍然生效。"""

        class Exploding(SeamStubSummarizer):
            def summarize_full_stream(self, text, language, template=None):
                self._calls["summarize_full_stream"] += 1
                yield ("summary", "半个总结")
                raise RuntimeError("模型服务不可用")

        events, uid = self._run(monkeypatch, make_user, Exploding(), "boom@example.com")

        assert [e[0] for e in events][-1] == "error", events
        assert parse_count_of(uid) == 0, "模型失败不该白扣额度"
        assert db.check_quota_kind(uid, "parse") == (True, database.DAILY_PARSE_LIMIT)

    def test_no_subtitle_produces_none_of_the_three(self, db, make_user, monkeypatch):
        """字幕拿不到：三项一个都不产出，模型一次都不调，额度不动。"""
        s = SeamStubSummarizer()
        monkeypatch.setattr(
            api_summarize, "_get_extractor", lambda: StubExtractor(has_subtitle=False)
        )
        monkeypatch.setattr(api_summarize, "_get_summarizer", lambda: s)
        uid = make_user("nosub@example.com")
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))

        kinds = [e[0] for e in events]
        assert kinds == ["subtitle", "error"], kinds
        assert not {"summary", "mindmap", "tags"} & set(kinds)
        assert s.calls == 0, "无字幕却调了模型"
        assert parse_count_of(uid) == 0, "无字幕却扣了额度"


class TestDualOutputProtocol:
    """模型侧的分段协议（summarizer.parse_dual_output）。

    协议长这样：

        <总结正文，逐字流式>
        <<<VIDDIGEST_MINDMAP_TAGS>>>
        {"mindmap": "# ...", "tags": ["编程"]}

    协议级测试住在路由测试文件里：工单 #5 的可写范围里没有单独的
    summarizer 测试文件，而这份协议是「一次调用产出三项」的前半段——
    路由拿到的每一项都由它决定。
    """

    @staticmethod
    def _summary_of(events):
        return [p for k, p in events if k == "summary"]

    def test_summary_is_yielded_before_the_stream_ends(self):
        """核心约束：不能先把整个响应读完再吐——那就没有打字机效果了。

        观察点是**上游消费进度**，不是最终事件列表：等流结束再比内容，
        「边收边吐」和「读完再吐」会给出完全相同的结果，断言会恒真。
        """
        consumed = []

        def _upstream():
            for part in ("甲", "乙", SENTINEL, '{"mindmap": "# x", "tags": []}'):
                consumed.append(part)
                yield part

        stream = summarizer.parse_dual_output(_upstream())

        first = next(stream)
        assert first == ("summary", "甲"), first
        assert consumed == ["甲"], (
            f"第一个总结事件之前就把整段流读完了：{consumed}"
        )

    def test_sentinel_cut_across_chunks_is_not_leaked_into_the_summary(self):
        cut = len(SENTINEL) // 2
        events = list(summarizer.parse_dual_output([
            "总结正文", SENTINEL[:cut],
            SENTINEL[cut:] + '{"mindmap": "# x", "tags": ["编程"]}',
        ]))

        assert self._summary_of(events) == ["总结正文"], events
        assert ("mindmap", "# x") in events, events
        assert ("tags", ["编程"]) in events, events

    def test_newline_before_the_sentinel_belongs_to_the_sentinel(self):
        events = list(summarizer.parse_dual_output(
            ["正文\n", SENTINEL, '{"mindmap": "# x", "tags": []}']
        ))

        assert self._summary_of(events) == ["正文"], events

    def test_missing_sentinel_still_streams_the_whole_summary(self):
        """模型不听话时总结不能丢；两条尾事件恒定发出，前端才不用写两套分支。"""
        events = list(summarizer.parse_dual_output(["只有总结", "没有哨兵"]))

        assert self._summary_of(events) == ["只有总结", "没有哨兵"], events
        assert ("mindmap", "") in events, events
        assert ("tags", []) in events, events

    def test_broken_tail_json_degrades_instead_of_raising(self):
        events = list(summarizer.parse_dual_output(["正文", SENTINEL, "{不是 JSON"]))

        assert self._summary_of(events) == ["正文"], events
        assert ("mindmap", "") in events, events
        assert ("tags", []) in events, events

    def test_fenced_json_tail_is_still_parsed(self):
        events = list(summarizer.parse_dual_output([
            "正文", SENTINEL,
            '```json\n{"mindmap": "# x", "tags": ["编程"]}\n```',
        ]))

        assert ("mindmap", "# x") in events, events
        assert ("tags", ["编程"]) in events, events

    def test_wrong_shaped_fields_do_not_leak_types(self):
        """模型输出是不可信输入：类型不对就当没有，不许变成下游 TypeError。"""
        events = list(summarizer.parse_dual_output([
            "正文", SENTINEL, '{"mindmap": {"不是": "字符串"}, "tags": "编程"}',
        ]))

        assert ("mindmap", "") in events, events
        assert ("tags", []) in events, events

    def test_non_object_json_degrades(self):
        events = list(summarizer.parse_dual_output(["正文", SENTINEL, "[1, 2, 3]"]))

        assert ("mindmap", "") in events, events
        assert ("tags", []) in events, events

    def test_prompt_carries_the_vocabulary_and_the_sentinel(self):
        """prompt 少一样东西，模型就少一样产出——而这种失败是沉默的。

        解析器再正确，模型没被告知哨兵行就只会吐出总结、连思维导图和标签
        都没有；词表不进 prompt，模型就只能自创标签。
        """
        prompt = summarizer.VideoSummarizer._build_full_prompt("字幕正文", "zh")

        assert SENTINEL in prompt, "prompt 没告诉模型哨兵行，尾部 JSON 不会被产出"
        assert tags.vocabulary_prompt_text() in prompt, "词表没进 prompt，模型只能自创标签"
        assert "字幕正文" in prompt, "字幕正文没进 prompt"


class TestSummaryPromptContentDemand:
    """总结正文为什么短：提示词只教了格式，没教内容。

    实测（改写前）：5323 字字幕 → 370 字总结，压缩比 14:1，且每条要点都
    退化成「这一节讲什么」的目录标签，一条具体数字都没有。根因有三条，
    每条都对应本类里的一条用例：
      1. 占位示例「（总结正文……）」本身是三行骨架，few-shot 的形状权重
         远高于散文式规则；
      2. 第一部分一条内容要求都没有，也没有篇幅下限；
      3. 导图的「节点 ≤15 字 / 节点总数 ≤30」没有作用域隔离，被模型泛化
         成全文基调。

    每条都按「把这条要求整个删掉，它还会绿吗」自查过。
    """

    @staticmethod
    def prompt():
        return summarizer.VideoSummarizer._build_full_prompt("字幕正文", "zh")

    def test_example_is_no_longer_a_placeholder_skeleton(self):
        """根因 #1：占位示例在教模型写短。"""
        p = self.prompt()

        assert "（总结正文……）" not in p, (
            "完整示例退回了「（总结正文……）」占位骨架 —— 模型对齐的是示例的"
            "形状，两行标题加省略号就是在说「写到这儿就够了」"
        )

    def test_example_demonstrates_density_not_just_headings(self):
        """示例除了不是占位，还得真的展示出「多要点」的形状。"""
        p = self.prompt()
        example = p.split("完整示例（")[-1].split("视频字幕内容：")[0]
        lines = example.splitlines()

        h2 = sum(1 for l in lines if l.startswith("## "))
        bullets = sum(1 for l in lines if l.startswith("- "))

        assert h2 >= 3, f"示例只有 {h2} 个二级章节，演示不出「覆盖完整」的形状"
        assert bullets >= 10, f"示例只有 {bullets} 条要点，模型会照着这个密度写"

    def test_summary_section_demands_content_not_just_format(self):
        """根因 #2：内容要求存在，且落在第一部分里面。"""
        p = self.prompt()
        i_mindmap = p.find("【第二部分：思维导图】")

        assert "不是目录" in p, "没有禁止把总结写成目录"
        assert "反例" in p and "正例" in p, (
            "没有给正反例对照 —— 只说「要有内容」不够，模型需要看到"
            "「什么样算有内容」的样本"
        )
        assert "覆盖优先" in p, "没有覆盖优先的要求，模型会挑着讲"
        assert "不要编造" in p, "没有禁止编造，补充篇幅时最容易编"

        for marker in ("不是目录", "覆盖优先", "不要编造"):
            i = p.find(marker)
            assert 0 <= i < i_mindmap, (
                f"「{marker}」跑到了第一部分外面（{i} >= {i_mindmap}），"
                "模型读起来会以为那是导图或标签的要求"
            )

    def test_summary_section_states_a_length_floor(self):
        """篇幅下限必须写死。只说「详细一点」= 没有任何约束。"""
        p = self.prompt()
        i_mindmap = p.find("【第二部分：思维导图】")

        i = p.find("不少于 1500 字")
        assert i >= 0, "提示词里没有篇幅下限 —— 这就是模型自己决定写多短的全部原因"
        assert i < i_mindmap, "篇幅下限写到了第二部分之后，作用域错了"

    def test_mindmap_limits_are_scoped_so_they_do_not_leak(self):
        """根因 #3：导图的字数限制必须被明确圈在自己的段落里。

        断言的是「那句作用域说明落在第二部分**内部**」——挪到提示词开头
        也能让模型看见，但那时它是在跟总结正文平级竞争，约束力大幅削弱。
        """
        p = self.prompt()
        seg = p.split("【第二部分：思维导图】")[-1].split("【第三部分：标签】")[0]

        assert "只适用于思维导图" in seg, (
            "导图的字数限制没有作用域隔离，模型会把「节点 ≤15 字」"
            "泛化成全文基调，于是总结也跟着变短"
        )
        assert "不要套用到第一部分的总结正文" in seg, "没有说清导图限制不作用于总结"

    def test_tag_limit_is_scoped_too(self):
        p = self.prompt()
        seg = p.split("【第三部分：标签】")[-1].split("【输出格式")[0]

        assert "只约束标签本身" in seg, "标签数量限制的作用域没隔离"

    def test_scope_notes_did_not_cost_the_original_three(self):
        """加内容要求不能挤掉协议本身：哨兵、词表、字幕正文。"""
        p = self.prompt()

        assert summarizer.MINDMAP_TAGS_SENTINEL in p, "哨兵行没了 —— 导图和标签会整个消失"
        assert tags.vocabulary_prompt_text() in p, "词表没了 —— 模型只能自创标签"
        assert "字幕正文" in p, "字幕正文没进 prompt"

    def test_the_mindmap_examples_are_really_multi_level(self):
        """工单 #106：few-shot 的**形状**权重远高于散文规则。

        样例只有一根分支时，模型被教出来的导图就只有一层。所以判据是
        **解析**样例里的 JSON——提示词怎么改写都行，只要样例本身还是
        一棵多层的树（≥2 根分支、有第三层、节点数在上限内）。
        """
        p = self.prompt()
        samples = re.findall(r'\{"mindmap":[^}]*\}', p)
        assert samples, "提示词里没有可解析的 mindmap 样例"

        for sample in samples:
            md = json.loads(sample)["mindmap"]
            levels = heading_levels_of(md)
            assert levels, f"样例不是标题结构：{md!r}"
            assert levels.count(1) == 1, f"样例的根标题不唯一：{md!r}"
            assert max(levels) >= 3, f"样例最深只有 {max(levels)} 级：{md!r}"
            assert levels.count(2) >= 2, f"样例只有一根分支：{md!r}"
            nodes = len(levels) + leaf_count_of(md)
            assert nodes <= 30, f"样例有 {nodes} 个节点，超出上限：{md!r}"

    def test_the_mindmap_section_states_the_structure_limits(self):
        """导图段必须写明层级与节点数上限（工单 #106）。

        上限没人写，模型就不知道能有多深、能挂多少节点；上限没人执行，
        清洗时又只能靠截断。阿拉伯数字与中文数字都认。
        """
        p = self.prompt()
        seg = p.split("【第二部分：思维导图】")[-1].split("【第三部分：标签】")[0]

        assert re.search(r"3\s*级|三级", seg), "导图段没写明层级上限（3 级）"
        assert re.search(r"7\s*个|七个", seg), "导图段没写明每支子节点上限（7 个）"
        assert re.search(r"30\s*个|三十个", seg), "导图段没写明总节点上限（30 个）"


class TestDegenerateMindmapIsRebuiltFromTheSummary:
    """工单 #106：模型给的导图退化时，用总结正文重建（工单口径 4）。

    ## 判据读的是落库的内容

    退化发生在**写库那一刻**：只看 SSE 证明不了写进去的是什么。所以这里
    断言 `videos` 那行的 `mindmap_md`——它必须有多于一层。

    ## 真的走生产那条路

    这里的桩只换掉**模型那一段**（`_stream_chunks`），解析、清洗、兜底
    重建、SSE、落库全是生产代码。把 `summarize_full_stream` 整个 stub
    掉的话，被验的就不是那条路了。
    """

    SUMMARY = (
        "# 视频主题\n"
        "## 视频概述\n"
        "- 本期实测三款扫地机器人\n"
        "## 吸力表现\n"
        "### 甲机地毯残留最少\n"
    )

    class CannedModel(summarizer.VideoSummarizer):
        """真的走 summarize_full_stream，只把模型那一段换成罐头。

        不调 `super().__init__()`：那一步要 API Key，而这里根本不联网。
        """

        def __init__(self, chunks):
            self.chunks = chunks

        def _stream_chunks(self, prompt):
            return iter(self.chunks)

    def _run(self, monkeypatch, make_user, chunks):
        monkeypatch.setattr(
            api_summarize, "_get_extractor", lambda: StubExtractor(has_subtitle=True)
        )
        monkeypatch.setattr(
            api_summarize, "_get_summarizer", lambda: self.CannedModel(chunks)
        )
        uid = make_user("rebuild@example.com")
        return collect(api_summarize.summarize_video(
            summarize_req(), user={"id": uid}
        )), uid

    @staticmethod
    def _stored_mindmap(url="https://example.com/v"):
        with database.get_db() as c:
            row = c.execute(
                "SELECT mindmap_md FROM videos WHERE video_url = ?", (url,)
            ).fetchone()
        return row["mindmap_md"] if row else None

    def test_a_single_level_mindmap_is_rebuilt_before_it_is_stored(
        self, db, make_user, monkeypatch
    ):
        chunks = [
            self.SUMMARY,
            SENTINEL,
            json.dumps({"mindmap": "# 视频思维导图", "tags": ["编程"]}),
        ]
        events, _ = self._run(monkeypatch, make_user, chunks)

        stored = self._stored_mindmap()
        assert stored is not None, "这次解析没能写库"
        assert max(heading_levels_of(stored)) >= 2, (
            f"落库的导图只有 {heading_levels_of(stored)} 级：{stored!r}"
        )
        assert "## 视频概述" in stored, stored

    def test_an_empty_mindmap_is_rebuilt_before_it_is_stored(
        self, db, make_user, monkeypatch
    ):
        """模型连导图都没给（JSON 里没有 mindmap）：也一样重建。"""
        chunks = [
            self.SUMMARY,
            SENTINEL,
            json.dumps({"tags": ["编程"]}),
        ]
        events, _ = self._run(monkeypatch, make_user, chunks)

        stored = self._stored_mindmap()
        assert stored is not None, "这次解析没能写库"
        assert max(heading_levels_of(stored)) >= 2, stored

    def test_the_client_gets_the_same_rebuilt_map(self, db, make_user, monkeypatch):
        """下发与落库是同一份：SSE 里的导图必须也是重建过的那份。"""
        chunks = [
            self.SUMMARY,
            SENTINEL,
            json.dumps({"mindmap": "# 视频思维导图", "tags": ["编程"]}),
        ]
        events, _ = self._run(monkeypatch, make_user, chunks)

        (payload,) = [p for kind, p in events if kind == "mindmap"]
        assert payload["markdown"] == self._stored_mindmap()
        assert max(heading_levels_of(payload["markdown"])) >= 2


class TestChatQuota:
    def test_chat_was_open_to_everyone(self, db, stub):
        """回归：修复前 /api/chat 没有任何配额检查。"""
        s = stub(has_subtitle=True)
        events = collect(api_summarize.chat_with_video(chat_req(), user=None))
        assert [e[0] for e in events] == ["error"]
        assert events[0][1]["need_login"] is True
        assert s.calls == 0

    def test_chat_consumes_its_own_budget(self, db, make_user, stub):
        """追问扣对话额度；事件顺序仍是 quota → answer → done。"""
        s = stub(has_subtitle=True)
        uid = make_user()
        events = collect(api_summarize.chat_with_video(chat_req(), user={"id": uid}))
        assert [e[0] for e in events] == ["quota", "answer", "done"]
        assert chat_count_of(uid) == 1
        assert database.check_quota_kind(uid, "chat") == (
            True, database.DAILY_CHAT_LIMIT - 1
        )
        assert s.calls == 1

    def test_chat_blocked_when_budget_spent(self, db, make_user, stub):
        """对话额度用满后拒绝追问。"""
        stub(has_subtitle=True)
        uid = make_user()
        for _ in range(database.DAILY_CHAT_LIMIT):
            collect(api_summarize.chat_with_video(chat_req(), user={"id": uid}))
        events = collect(api_summarize.chat_with_video(chat_req(), user={"id": uid}))
        assert [e[0] for e in events] == ["error"]
        assert events[0][1]["need_vip"] is True
        assert chat_count_of(uid) == database.DAILY_CHAT_LIMIT

    def test_chat_no_subtitle_does_not_consume(self, db, make_user, stub):
        stub(has_subtitle=False)
        uid = make_user()
        events = collect(api_summarize.chat_with_video(chat_req(subtitle=""), user={"id": uid}))
        assert [e[0] for e in events] == ["error"]
        assert chat_count_of(uid) == 0

    def test_chat_and_summary_use_separate_budgets(self, db, make_user, stub):
        """修订自原 test_chat_and_summary_share_one_budget（工单 #4 AC）。

        原用例守的是「问答不能变成绕过总结限额的免费通道」；
        额度拆开后那条担心换了个形式——两个计数器各自独立，
        任一用满都不该影响另一个。

        两个方向必须用两个用户：先把对话额度用满之后，
        同一个用户的对话额度本来就没了，再验证「解析用满不影响追问」
        只会得到一个必然的 error，测不出任何东西。

        耗额度时每次必须用**不同**的链接：工单 #6 之后同一链接第二次起是
        免费复用，拿同一个 URL 连打不会耗额度。
        """
        stub(has_subtitle=True)

        # 方向一：对话用满，解析照常
        a = make_user("a@example.com")
        for _ in range(database.DAILY_CHAT_LIMIT):
            collect(api_summarize.chat_with_video(chat_req(), user={"id": a}))
        events = collect(
            api_summarize.summarize_video(
                summarize_req("https://example.com/chat-drained"), user={"id": a}
            )
        )
        assert "error" not in [e[0] for e in events], "对话额度用满不该挡住解析"
        assert parse_count_of(a) == 1

        # 方向二：解析用满，追问照常
        b = make_user("b@example.com")
        for i in range(database.DAILY_PARSE_LIMIT):
            collect(
                api_summarize.summarize_video(
                    summarize_req(f"https://example.com/parse/{i}"), user={"id": b}
                )
            )
        blocked = collect(
            api_summarize.summarize_video(
                summarize_req("https://example.com/parse/overflow"), user={"id": b}
            )
        )
        assert [e[0] for e in blocked] == ["error"], "前提：解析额度此时应已用满"

        events = collect(api_summarize.chat_with_video(chat_req(), user={"id": b}))
        assert "error" not in [e[0] for e in events], "解析额度用满不该挡住追问"


class TestVipBypassesBudget:
    def test_vip_unlimited_on_both_endpoints(self, db, make_user, stub):
        s = stub(has_subtitle=True)
        uid = make_user(is_vip=True, vip_expire_at=FAR_FUTURE)
        for _ in range(5):
            collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
            collect(api_summarize.chat_with_video(chat_req(), user={"id": uid}))
        assert parse_count_of(uid) == 0, "VIP 总结不该记进解析计数"
        assert chat_count_of(uid) == 0, "VIP 追问不该记进对话计数"
        assert db.check_quota(uid) == {"parse": (True, -1), "chat": (True, -1)}

    def test_vip_quota_event_marks_unlimited(self, db, make_user, stub):
        stub(has_subtitle=True)
        uid = make_user(is_vip=True, vip_expire_at=FAR_FUTURE)
        events = collect(api_summarize.summarize_video(summarize_req(), user={"id": uid}))
        quota = next(e[1] for e in events if e[0] == "quota")
        assert quota["remaining"] == -1 and quota["unlimited"] is True


class TestQuotaEndpoint:
    """GET /api/quota —— 只读，前端用来在点击前就显示剩余次数。"""

    async def _call(self, user):
        return await api_summarize.get_quota(user=user)

    def test_anonymous(self, db):
        r = asyncio.run(self._call(None))
        assert r["logged_in"] is False
        assert r["parse"] is None and r["chat"] is None

    def test_free_user_untouched(self, db, make_user):
        uid = make_user()
        r = asyncio.run(self._call({"id": uid}))
        assert r["logged_in"] is True and r["unlimited"] is False
        assert r["parse"]["remaining"] == database.DAILY_PARSE_LIMIT
        assert r["chat"]["remaining"] == database.DAILY_CHAT_LIMIT
        assert parse_count_of(uid) == 0 and chat_count_of(uid) == 0, "只读端点不该写库"

    def test_free_user_partially_spent(self, db, make_user):
        uid = make_user()
        database.consume_quota(uid, "parse")
        r = asyncio.run(self._call({"id": uid}))
        assert r["parse"]["remaining"] == database.DAILY_PARSE_LIMIT - 1
        assert r["chat"]["remaining"] == database.DAILY_CHAT_LIMIT

    def test_over_limit_reports_zero(self, db, make_user):
        uid = make_user()
        for _ in range(database.DAILY_PARSE_LIMIT):
            database.consume_quota(uid, "parse")
        r = asyncio.run(self._call({"id": uid}))
        assert r["parse"]["remaining"] == 0 and r["unlimited"] is False

    def test_vip_reports_unlimited(self, db, make_user):
        uid = make_user(is_vip=True, vip_expire_at=FAR_FUTURE)
        r = asyncio.run(self._call({"id": uid}))
        assert r["unlimited"] is True and r["remaining"] == -1


class TestClientDisconnectReleasesTheClaim:
    """客户端中途断流：占位必须还回去。

    不还回去的代价是永久性的——那一行永远停在 pending，这个链接从此
    再也解析不了，后来的每个人都只能干等到超时。

    这条守的是「生成器被**关闭**」这条路径：正常跑完时位置是落库结清的，
    断流时什么都没落库，必须靠还位把位置交回去（ADR 0031）。
    """

    def test_placeholder_is_given_back_when_the_stream_is_cut(self, db, make_user, stub):
        stub(has_subtitle=True)
        uid = make_user()
        url = "https://example.com/disconnect"

        async def scenario():
            gen = api_summarize.summarize_video(summarize_req(url), user={"id": uid})
            await gen.__anext__()   # 第一块是 subtitle —— 此刻占位已经拿到
            await gen.aclose()      # 客户端断流：生成器被关闭

        asyncio.run(scenario())

        assert database.get_video_by_url(url) is None, (
            "断流后占位没有还回去——这一行会永远停在 pending，"
            "这个链接从此解析不了"
        )
