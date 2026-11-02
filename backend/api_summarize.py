"""AI 视频总结相关 API 路由"""

import asyncio
import json
import logging

from collections.abc import AsyncIterable
from contextlib import AsyncExitStack

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import ServerSentEvent, EventSourceResponse
from pydantic import BaseModel, SecretStr, field_validator

import video_claim
from auth import get_optional_user
from credentials import CredentialError, UserCredential
from database import (
    append_chat_turn,
    check_quota_kind,
    complete_video,
    consume_quota,
    get_recent_chat_messages,
    get_video_by_url,
    QUOTA_KINDS,
    quota_limit,
    refund_quota,
    regenerate_video,
)
from prompt_template import default_template_id, is_known_template, list_templates
from tags import validate_tags

logger = logging.getLogger("api_summarize")

router = APIRouter(prefix="/api", tags=["AI 总结"])

#: 额度种类 → 中文名，只用于「今日XX次数已用完」这类提示文案。
#:
#: ⚠️ 这是**收口后仍需手工登记**的一处（工单 #38）：键集必须等于
#: `database.QUOTA_KINDS`，而中文名是循环产不出来的专有数据。
#: 漏登记的症状曾是响亮的 `KeyError`（`_check_quota_permission` 直接下标取），
#: 现在由 `tests/test_quota_kinds_single_source.py` 在门禁里指名缺哪一种。
_QUOTA_LABELS = {"parse": "解析", "chat": "追问"}

#: 未登录时的提示。提出来是因为这条拒绝要发生两次：
#: 一次在抢占位之前（未登录的人不该去占一个位置让别人干等），
#: 一次在额度检查里。两处文案必须一致，所以只能有一份来源。
_NOT_LOGGED_IN = "请先登录后使用 AI 功能"

#: 自带凭据的请求失败时回给用户的**固定**文案。
#: 绝不拼 str(e)：第三方 SDK 的鉴权异常可能带请求体片段（部分服务端会
#: 回显 key 的前若干位），拼进去就是把用户自己的凭据回显给他自己，
#: 同时也进了浏览器历史与代理日志。
_BYOK_FAILURE_MESSAGE = "凭据无效或调用失败，请检查后重试"

#: 送给模型的上下文里保留几轮对话，**含本轮**。
#:
#: 本轮问题自己占一轮，所以历史只取 CHAT_CONTEXT_TURNS - 1 轮——
#: 第 4 轮时第 1 轮就该掉出去了（票面 AC3）。「刚才那个」这类指代
#: 跨 3 轮以内还接得上；再多只是烧 token，还会把真正相关的那句挤出去。
CHAT_CONTEXT_TURNS = 3

#: 后来者想覆盖别人那一份时的拒绝文案（ADR 0007）。
#: 提出来是因为「重新解析」按钮对所有人可见，不给后来者一句明确的话，
#: 看到的就仍然是「点了没反应」——那正是本次要修的那个症状。
_NOT_OWNER = "这份总结是别人解析的，只有首次解析它的人才能重新解析"


@router.get("/quota")
async def get_quota(user: dict | None = Depends(get_optional_user)):
    """当前额度（只读，不消耗）。

    解析额度与对话额度是两个独立计数器，各自在次日重置。
    """
    if not user:
        # 每种额度都占一个键（值恒为 None），键集同样取自常量。
        # 匿名分支与登录分支的**键集必须一致**——前端 `quotaOf(q, kind)` 按
        # 键取槽位，少一个键它就少渲染一行，而 `describeQuota` 判的是
        # `!parse && !chat`（两个都缺才降级），所以单缺一个**不会**触发兜底，
        # 只会安静地少显示一种额度（工单 #38 实测，输出前后逐字相同）。
        return {"logged_in": False, "unlimited": False, "remaining": None,
                "limit": quota_limit("parse"),
                **{kind: None for kind in QUOTA_KINDS}}
    return {"logged_in": True, **_quota_payload(user["id"])}


def _quota_payload(user_id: int, primary: str = "parse") -> dict:
    """每种额度各自的 (allowed, remaining, limit)，供前端分别展示。

    种类取自 `database.QUOTA_KINDS`（工单 #38）——原来这里硬写
    `("parse", "chat")`，漏改不报错：`/api/quota` 少一个键，前端安静地
    少显示一种额度，而当时没有任何测试会红。

    顶层 remaining/limit/unlimited 是留给旧前端的兼容别名，必须跟随
    `primary`——即「这次事件刚动的是哪类额度」。若追问事件里还报 parse
    的数字，前端的降级分支会把刚扣掉的对话额度显示成解析的余量。

    limit 走 `quota_limit` 而不是模块常量：remaining 由数据层按调用时的
    模块值算，limit 若在 import 期冻结成另一份，同一份 payload 就会自相矛盾。
    同样必须带上 user_id（工单 #12）：上限可以按人覆盖，不带就会报出全局值。
    """
    payload = {}
    for kind in QUOTA_KINDS:
        allowed, remaining = check_quota_kind(user_id, kind)
        payload[kind] = {
            "allowed": allowed,
            "remaining": (0 if not allowed else remaining),
            "limit": quota_limit(kind, user_id),
        }
    head = payload[primary]
    return {
        **payload,
        "unlimited": head["remaining"] == -1,
        "remaining": head["remaining"],
        "limit": head["limit"],
    }


@router.get("/summary-templates")
async def list_summary_templates():
    """可用的总结提示词模板清单（工单 #105 / ADR 0026）。

    只读、匿名可读：清单是产品目录，不是用户数据，登录与否都该拿得到
    ——前端的下拉框要在用户点「开始解析」之前就把它填好。

    清单真值只有一份（``prompts/templates.json``），这里不复制第二份。
    响应**刻意不含**提示词文件名：那是仓库内部的事，暴露出去就等于让
    外部依赖「提示词从哪个文件读」这个本该自由改动的实现细节。
    """
    return {
        "default": default_template_id(),
        "templates": list_templates(),
    }


#: 端点覆盖为什么不是白名单，只写在下面第一处（两个模型字段完全相同，
#: 逐个抄一遍注释的代价是它们迟早会漂移——那正是本文件反复出问题的地方）。
#: 简述：用户自建服务无法被提前枚举，风险由「只出网到用户自己指定的
#: 那台机器」承担；协议与 userinfo 的硬校验在 credentials.validate_base_url。


class SummarizeRequest(BaseModel):
    url: str
    language: str = "zh"
    #: 重新解析：改写社区里已有的那一份（ADR 0007）。
    #: 只在 parsed_by 是本人时生效；否则服务端直接拒绝，不调模型不扣额度。
    #: 默认 False——绝大多数请求只是来看一眼或首次解析。
    overwrite: bool = False
    # 平台元数据（工单 #17 第 2 项）：前端在 /api/parse 之后就拿到了标题与缩略图，
    # 趁占位行还没建成就一起带过来。此前是靠前端事后打
    # POST /api/community/cards 回填，而那条路要求 status='ready' ——
    # 占位行在解析完成前一直是 pending，于是那次 UPDATE 永远匹配 0 行，
    # 首次解析者填的卡片标题/封面填不进去。
    # 都是可空字段，缺省不影响既有调用方。
    video_title: str = ""
    cover_url: str = ""
    # 自带凭据（BYOK）：用 SecretStr 让遮蔽从请求模型这一层就成立。
    # 裸 str 的模型 repr 与 model_dump() 都会带出真值，将来任何一句
    # logger.debug(f"{req}") 就会漏。进路由立刻包成 UserCredential，不落盘。
    user_api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model: str = ""
    # 提示词模板 id（工单 #105 / ADR 0026）。空串 = 用默认模板，
    # 于是「不传」与「传默认」走同一条路，行为与加这一列之前完全一致。
    #
    # 校验放在模型这一层：模板 id 会进提示词文件的名字，一个拼错的 id
    # 早该在入口就被挡住（422），而不是渲染出一个空提示词发给模型。
    summary_template: str = ""

    @field_validator("summary_template")
    @classmethod
    def _check_summary_template(cls, value: str) -> str:
        value = (value or "").strip()
        if value and not is_known_template(value):
            raise ValueError(f"未知的总结模板：{value!r}")
        return value


class ChatRequest(BaseModel):
    url: str
    question: str
    subtitle_text: str = ""
    # 与 SummarizeRequest 同名字段，同一含义、同一校验。逐字段重复而不是
    # 抽基类：两者的字段集合各自演化（summarize 有 overwrite，chat 没有），
    # 共用基类会逼着每加一个字段就回答「另一边要不要也加上」。
    user_api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model: str = ""


def _check_quota_permission(user: dict | None, kind: str):
    """检查某类额度权限（只判定，不扣）。"""
    label = _QUOTA_LABELS[kind]

    if not user:
        return False, 0, _NOT_LOGGED_IN

    # 带上 user_id（工单 #12）：上限可以按人覆盖，不带就拿到全局值，
    # 于是被限流的人会读到「每日 3 次」而真实上限是 1。
    # 放在登录判定之后，未登录那条路径因此不触库。
    limit = quota_limit(kind, user["id"])

    allowed, remaining = check_quota_kind(user["id"], kind)
    if not allowed:
        return False, 0, f"今日{label}次数已用完（每日 {limit} 次），明日 0 点重置"

    return True, remaining, None


def _get_summarizer():
    """延迟初始化 VideoSummarizer"""
    from summarizer import VideoSummarizer
    if not hasattr(_get_summarizer, "_instance"):
        try:
            _get_summarizer._instance = VideoSummarizer()
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
    return _get_summarizer._instance


def _build_user_summarizer(credential: UserCredential):
    """为**本次请求**造一个用用户凭据的 summarizer。

    刻意不走 _get_summarizer 那个模块级单例：单例活到进程结束，
    用户的凭据会跟着活到进程结束；更糟的是下一个不带凭据的请求会
    复用上一个人的 client，把这一次调用记到他自己账上（或者串号）。
    构造 OpenAI client 不发网络请求，逐请求构造的开销可以忽略。
    """
    from summarizer import VideoSummarizer
    return VideoSummarizer(credential=credential)


def _get_extractor():
    """延迟初始化 SubtitleExtractor"""
    from summarizer import SubtitleExtractor
    if not hasattr(_get_extractor, "_instance"):
        _get_extractor._instance = SubtitleExtractor()
    return _get_extractor._instance


def _subtitle_failure(subtitle_data: dict) -> tuple[str, str, str]:
    """把「为什么拿不到字幕」翻成一句用户看得懂的话。

    返回 (前半句, fail_reason, asr_fail_reason)。后半句留给调用方拼——同一个
    原因对「无法生成总结」和「无法回答问题」要接不同的尾巴。

    延迟导入 summarizer 与本文件其它地方一致（openai / yt_dlp 都很重），
    不为了几个常量在模块顶部把它拖进来。
    """
    from summarizer import (
        FAIL_ASR_FAILED,
        FAIL_ASR_NOT_CONFIGURED,
        FAIL_FETCH_FAILED,
        FAIL_NO_TRACK,
    )

    # .get() 不是防御性过度：这两个键是新加的，任何一条仍返回旧形状的
    # 路径（测试里的 StubExtractor、第三方补丁）都不该把这里变成 KeyError。
    reason = subtitle_data.get("fail_reason", "")
    asr_reason = subtitle_data.get("asr_fail_reason", "")

    if reason == FAIL_FETCH_FAILED:
        head = "平台提供了字幕但没能取下来"
    elif reason == FAIL_NO_TRACK:
        head = "该视频没有字幕"
    else:
        head = "该视频没有可用的字幕"

    if asr_reason == FAIL_ASR_NOT_CONFIGURED:
        head += "，且服务端未配置语音识别"
    elif asr_reason == FAIL_ASR_FAILED:
        head += "，服务端用语音识别补字幕也没成功"

    return head, reason, asr_reason


async def _run_in_thread(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


# ── 社区复用：抢解析权（工单 #6）───────────────────────────
#
# 只有一个链接该被解析一次。要做到这一点，光有「视频已存在就别调模型」
# 是不够的：两个用户同时进来时，两边都可能查到「还没有」，于是两边都
# 去调模型、都扣额度。唯一索引能挡住重复的**行**，挡不住重复的**调用**。
#
# 因此顺序被倒过来：先占位，只有占位成功的人才去调模型。占位失败的人
# 不是报错，而是等占位者出结果后复用同一份——这正是票面承诺的
# 「不管谁先解析，看到的总结都是同一份」。

# 谁能调模型、谁只能等着，由 video_claim 决定（ADR 0031）：
# 占位与覆盖闸门都在那个 module 里，退出上下文即释放——路由不再手写
# 轮询顺序、等待超时与闸门配对。


def _replay_events(user: dict, video: dict) -> list[ServerSentEvent]:
    """把社区里已有的那一份结果原样回放给后来的用户。

    事件名与首次解析完全一致，前端不必为「复用」再写一套分支。
    两处刻意的差异，都必须说清理由而不是留给下一个人猜：

    1. 总结整段一次下发，而不是逐 token 打字机。它不是正在生成的，
       逐字挤出来只会让用户白等。
    2. segments 为空。社区视频表存的是字幕全文（后续追问要拿它作上下文），
       没有分段信息；full_text 是完整的。
    额度事件报的是**这个用户自己**的余额：他没被扣，数字就不该动。

    ownership 事件是这个用户对这份内容的写权限（ADR 0007）。它必须在**回放
    之前**发：前端要靠它决定「重新解析」按钮是给一句能点的话、还是一句
    「这是别人的」——事后再补，用户已经看到一个点了没反应的按钮。
    """
    return [
        ServerSentEvent(
            raw_data=json.dumps({
                "can_regenerate": video.get("parsed_by") == user["id"],
            }, ensure_ascii=False),
            event="ownership",
        ),
        ServerSentEvent(
            raw_data=json.dumps({
                "has_subtitle": True,
                "full_text": video.get("subtitle_text") or "",
                "segments": [],
            }, ensure_ascii=False),
            event="subtitle",
        ),
        ServerSentEvent(
            raw_data=json.dumps(_quota_payload(user["id"], "parse"), ensure_ascii=False),
            event="quota",
        ),
        ServerSentEvent(
            raw_data=json.dumps(video.get("summary_md") or "", ensure_ascii=False),
            event="summary",
        ),
        ServerSentEvent(
            raw_data=json.dumps({"markdown": video.get("mindmap_md") or ""}, ensure_ascii=False),
            event="mindmap",
        ),
        ServerSentEvent(
            raw_data=json.dumps(video.get("tags") or [], ensure_ascii=False),
            event="tags",
        ),
        ServerSentEvent(raw_data="[DONE]", event="done"),
    ]


# ── 流式总结端点 ────────────────────────────────────────

@router.post("/summarize", response_class=EventSourceResponse)
async def summarize_video(
    req: SummarizeRequest,
    user: dict | None = Depends(get_optional_user),
) -> AsyncIterable[ServerSentEvent]:
    """
    AI 视频总结（SSE 流式）
    事件顺序：ownership? → subtitle → quota → summary(流式token) → mindmap → tags → done

    一次解析产出三件事（总结 / 思维导图 / 标签），共用同一份字幕上下文，
    也只调一次模型、只扣一次额度。

    同一个链接全站只解析一次（社区视频表，见 ADR 0001）：后来者直接拿到
    首次解析者的那一份结果，不调模型也不扣额度。两个用户同时进来时，
    只有抢到占位的那一个会调模型。

    ``overwrite=True`` 是唯一的例外（ADR 0007）：首次解析者本人可以改写
    自己那一份，走的是**不占位**的独立路径，不影响其他人的复用。
    """

    async def fail(message: str, need_vip: bool = False, need_login: bool = False):
        """回一条错误事件。抽出来是因为这个端点的拒绝有四五个出口，
        每次都手写一遍 json.dumps 只会让某一处漏掉某个字段。"""
        yield ServerSentEvent(
            raw_data=json.dumps({
                "message": message,
                "need_login": need_login,
                "need_vip": need_vip,
            }, ensure_ascii=False),
            event="error",
        )

    # 未登录先拒：占位期间别人只能干等，而这次请求注定要被拒，
    # 没有任何理由让它先去占一个位置。
    if not user:
        async for event in fail(_NOT_LOGGED_IN, need_login=True):
            yield event
        return

    # 自带凭据在这里就包好，之后全程只传封装对象。
    # 构造可能抛 CredentialError（端点不合法），那是**用户填错了**：
    # 必须在占位之前就拒绝，否则这个链接会先被占住再被还回来。
    try:
        credential = UserCredential.from_secret(
            req.user_api_key, req.base_url, req.model
        )
    except CredentialError as e:
        # 固定文案：CredentialError 的消息里没有用户原文（见 validate_base_url），
        # 这里也不追加任何上下文。
        async for event in fail(str(e)):
            yield event
        return
    # 自带凭据时不消耗平台额度——钱是用户自己出的（ADR 0004）。
    using_byok = credential is not None

    # 这次用哪份提示词（工单 #105 / ADR 0026）。空串 = 清单里的默认模板，
    # 于是「不传模板」与「加这个字段之前」是同一条路。放在占位之前算，
    # 是因为模板 id 后面要跟着写进社区视频表那一行。
    template_id = req.summary_template or default_template_id()

    # 额度是否已扣。扣了之后没走完流程就要还回去。
    quota_spent = False
    # 占位与覆盖闸门由 video_claim 决定（ADR 0031）。这里用 AsyncExitStack
    # 把两个上下文管起来：**退出即释放**——占位没落库就还位，闸门用完就放掉。
    # 路由不再手写轮询顺序、等待超时与闸门配对。
    stack = AsyncExitStack()
    try:
        # 覆盖自己的那一份（ADR 0007）。判定放在**抢占位之前**：
        # 覆盖路径根本没有占位可言，先抢位再判断会凭空造出一行 pending。
        regenerate = False
        if req.overwrite:
            reg = await stack.enter_async_context(
                video_claim.reclaim(req.url, user["id"])
            )
            if reg.outcome == video_claim.RECLAIM_FORBIDDEN:
                async for event in fail(_NOT_OWNER):
                    yield event
                return
            if reg.outcome == video_claim.RECLAIM_BUSY:
                async for event in fail("这份总结正在重新解析中，请稍后再试"):
                    yield event
                return
            # "skip"：社区里没有可覆盖的成品，按首次解析正常走
            regenerate = reg.outcome == video_claim.RECLAIM_REGENERATE

        # 社区里已有的结果：既不扣额度也不调模型，因此**不受额度限制**——
        # 复用的成本是零，额度不该拦住「看别人已经解析好的东西」。
        claim = None
        if not regenerate:
            claim = await stack.enter_async_context(
                video_claim.acquire_claim(
                    req.url, user["id"], req.video_title, req.cover_url
                )
            )
            if claim.outcome == video_claim.CLAIM_REUSE:
                existing = get_video_by_url(req.url)
                if existing is None:
                    # 探到「有成品」到读取之间那一行被删了（管理员删条目，
                    # ADR 0013）。重放需要那一行——没有就明确报错，
                    # 别拿 None 去重放。
                    async for event in fail("这份总结没能保存到社区，请稍后重试"):
                        yield event
                    return
                for event in _replay_events(user, existing):
                    yield event
                return
            if claim.outcome == video_claim.CLAIM_BUSY:
                async for event in fail("这个视频正在解析中，请稍后再试"):
                    yield event
                return

        # 到这里我们可以调模型了（占位者，或覆盖自己那一份的作者）。
        # 额度仍可能不够——占位必须还回去，否则这个链接会被一个注定失败的
        # 请求永久卡住。覆盖路径没有占位可还。
        #
        # 自带凭据时**不查额度**：花的是用户自己的钱，平台没有理由拦他。
        # 这条与「社区复用不查额度」是同一个道理——不是平台出的钱，就不是
        # 平台的额度。
        if not using_byok:
            allowed, remaining, message = _check_quota_permission(user, "parse")
            if not allowed:
                async for event in fail(message, need_vip=True):
                    yield event
                return

        extractor = _get_extractor()
        subtitle_data = await _run_in_thread(extractor.extract, req.url)

        yield ServerSentEvent(
            raw_data=json.dumps(subtitle_data, ensure_ascii=False),
            event="subtitle",
        )

        if not subtitle_data["has_subtitle"]:
            head, reason, asr_reason = _subtitle_failure(subtitle_data)
            yield ServerSentEvent(
                raw_data=json.dumps({
                    "message": f"{head}，无法生成总结",
                    # 机器可读的原因：前端将来要分开提示时不必再猜
                    "reason": reason,
                    "asr_reason": asr_reason,
                }, ensure_ascii=False),
                event="error",
            )
            return

        full_text = subtitle_data["full_text"]

        # 真正要调用 AI 了，此刻才扣额度（字幕提取失败不扣）。
        # 自带凭据时跳过扣减，也**没有余额可报**：凭空编一个数字报出去
        # 就是撒谎。只报「本次没消耗」，余额由前端原样留着——
        # 与 /api/chat 的 BYOK 分支同一个形状，前端一条 applyQuotaEvent 通吃。
        if not using_byok:
            # 扣减自带守卫（工单 #17）：上面的 `check_quota_kind` 与此刻之间
            # 隔着字幕提取（可能几十秒），并发请求会全部通过那次只读判定。
            # 现在判定与扣减在同一条 SQL 里完成，**没扣成时返回 None**。
            # **必须处理这个 None**：忽略它就等于「额度满了照样调模型」，
            # 而那正是白送平台付费资源（模型调用费）。
            # 0 **不**表示「没扣成」：额度恰好用完时 remaining 正好是 0，
            # 拿它当哨兵会把「刚好用完」误判成「额度已满」（理由见
            # consume_quota 的 docstring，工单 #17）。
            # **必须处理这个 0**：忽略它就等于「额度满了照样调模型」，
            # 而那正是白送平台付费资源（模型调用费）。
            remaining = consume_quota(user["id"], "parse")
            if remaining is None:
                yield ServerSentEvent(
                    raw_data=json.dumps(
                        {
                            "message": "今日次数已用完",
                            "reason": "quota_exhausted",
                        },
                        ensure_ascii=False,
                    ),
                    event="error",
                )
                return
            quota_spent = True
            # 额度尽早下发，前端在流式开始前就能显示剩余次数。
            # 顶层字段由 _quota_payload 统一产出——在这里手写会被末尾展开的
            # payload 覆盖掉，写了也不生效。
            yield ServerSentEvent(
                raw_data=json.dumps(_quota_payload(user["id"], "parse"), ensure_ascii=False),
                event="quota",
            )
        else:
            yield ServerSentEvent(
                raw_data=json.dumps(
                    {"byok": True, "consumed": False}, ensure_ascii=False
                ),
                event="quota",
            )

        # 一次模型调用产出三件事：总结逐 token 下发（打字机效果），
        # 哨兵之后的 JSON 在流末尾一次性解析。模型只被调一次，额度也只扣这一次。
        # 边下发给前端，边攒起来——流结束时要靠它们把结果写进社区视频表。
        summary_parts: list[str] = []
        mindmap_md = ""
        tags_payload: list[str] = []
        # 逐请求构造，绝不复用模块级单例：单例会活到进程结束，
        # 下一个不带凭据的请求会拿上一个人的 client 去调模型。
        summarizer = (
            _build_user_summarizer(credential) if using_byok else _get_summarizer()
        )
        for kind, payload in summarizer.summarize_full_stream(
            full_text, req.language, template_id
        ):
            if kind == "summary":
                summary_parts.append(payload)
                yield ServerSentEvent(
                    raw_data=json.dumps(payload, ensure_ascii=False),
                    event="summary",
                )
            elif kind == "mindmap":
                mindmap_md = payload
                yield ServerSentEvent(
                    raw_data=json.dumps({"markdown": payload}, ensure_ascii=False),
                    event="mindmap",
                )
            elif kind == "tags":
                # 词表是产品规则，模型说了不算：校验在这里做，
                # 线上只可能拿到词表内、去重、非空的数组。
                accepted, rejected = validate_tags(payload)
                if rejected:
                    logger.info("丢弃词表外的标签：%s", rejected)
                tags_payload = accepted
                yield ServerSentEvent(
                    raw_data=json.dumps(accepted, ensure_ascii=False),
                    event="tags",
                )
            else:
                logger.warning("未知的产出类型 %r，本次解析忽略它", kind)

        # 先落库，再结清扣费。顺序不能反：社区表里出现 ready 行与
        # 「此后不再回滚额度」必须是同一刻的两件事，否则两者之间断流
        # 会留下「有结果却退了款」的矛盾状态。
        # 存的是**校验后**的标签——落库内容必须与用户当时看到的完全一致。
        if regenerate:
            # 覆盖的 WHERE 里带 parsed_by：判定在 SQL 里，不在调用方。
            # 返回 0 说明这行此刻不属于他（被并发改过、或本来就是别人的）。
            # 直接报错收尾：闸门由上下文退出时放掉，quota_spent 保持真让
            # finally 把额度退回去——用户什么都没拿到，不该白付这一次。
            if regenerate_video(
                req.url,
                user["id"],
                summary_md="".join(summary_parts),
                mindmap_md=mindmap_md,
                tags=tags_payload,
                subtitle_text=full_text,
                template=template_id,
            ) == 0:
                logger.warning("社区视频 %r 已不属于 %r，未能覆盖", req.url, user["id"])
                async for event in fail("重新解析没能写回社区，这份总结已被别人改动"):
                    yield event
                return
            # 覆盖**不占位**：没有位置要还，闸门由上下文退出时放掉。
        else:
            if complete_video(
                req.url,
                summary_md="".join(summary_parts),
                mindmap_md=mindmap_md,
                tags=tags_payload,
                subtitle_text=full_text,
                template=template_id,
            ) == 0:
                # 落库失败与覆盖失败同权：不能装作成功。
                #
                # 返回 0 只有两种可能：这一行不存在（管理员删了 pending 行），
                # 或者它已经不是 pending（成了别人的成果）。两种情况下
                # **这个位置都已经结清了**——虽然不是我们结的。所以不回填，
                # 也不去 release：release 的 WHERE 只看 status='pending'，
                # 此刻对这两行都是空操作；而「在这之后有人重新占位」的那一瞬间，
                # 它就不再是空操作了。不拥有，就别去动它。
                claim.settle()
                logger.warning("社区视频 %r 的占位已不在 pending 状态，结果未落库", req.url)
                async for event in fail("这份总结没能保存到社区，请稍后重试"):
                    yield event
                # quota_spent 保持真 → finally 把这一次扣减退回去。
                # 用户看到的是一份完整总结，社区里却什么都没有，
                # 让他白付这一次不成立。
                return
            # 结果已落库：位置结清，退出上下文时不再还位。
            claim.settle()

        # 三项都已产出，扣费就此结清：此后即便客户端断流也不回滚。
        quota_spent = False
        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except HTTPException:
        # 交给下面的 finally 回滚，然后原样抛出——不能降级成 SSE 错误事件。
        raise
    except Exception as e:
        # 模型调用失败不该白扣额度。扣减与调用之间没有事务，
        # 这里是把已扣的那一次还回去——回滚本身不会把计数压到负数。
        #
        # 自带凭据时**不拼 str(e)**：第三方 SDK 的鉴权 / 连接异常可能带
        # 请求体片段（部分服务端会回显 key 的前若干位），拼进去就是把用户
        # 自己的凭据回显给他自己，同时进了浏览器历史与代理日志。
        # 与 /api/chat 共用同一句固定文案——两条路径的口径必须一致。
        if using_byok:
            yield ServerSentEvent(
                raw_data=json.dumps(
                    {"message": _BYOK_FAILURE_MESSAGE, "byok": True},
                    ensure_ascii=False,
                ),
                event="error",
            )
        else:
            yield ServerSentEvent(
                raw_data=json.dumps({"message": f"总结失败: {str(e)}"}, ensure_ascii=False),
                event="error",
            )
    finally:
        # 占位与覆盖闸门的释放（ADR 0031）：退出上下文即释放。
        # 覆盖路径**不会**走到还位——那个上下文只在抢到闸门时才放闸，
        # 那一行是别人的成果（或作者自己的旧成果），删掉它等于用一次
        # 失败的覆盖抹掉社区内容。
        await stack.aclose()
        # 唯一回滚点：except 分支和「客户端中途断开」共用它，不会重复退款。
        # 断流时抛的是 GeneratorExit / CancelledError，两者都继承 BaseException
        # 而非 Exception，上面的 except 抓不到——实测额度就停在扣减后的值。
        # 无论如何都原样抛出，KeyboardInterrupt / SystemExit 不会被吞掉。
        if quota_spent:
            refund_quota(user["id"], "parse")


# ── AI 问答端点 ──────────────────────────────────────────

@router.post("/chat", response_class=EventSourceResponse)
async def chat_with_video(
    req: ChatRequest,
    user: dict | None = Depends(get_optional_user),
) -> AsyncIterable[ServerSentEvent]:
    """AI 视频问答（SSE 流式）。消耗对话额度，与解析额度互不影响。

    带用户自带凭据的请求走**同一条**流式路径，但**不消耗额度**：
    用户自付费，不该白扣平台额度。此时额度三件套（check / consume /
    refund）一行都不碰，额度耗尽也照常放行。
    """
    # 进路由第一件事就是包成封装对象，且**不在路由里留裸串变量**——
    # from_secret 内部取一次值就交给 __init__，路由里的局部变量
    # 从头到尾只有封装对象。端点一并进去，校验也在那一步做完。
    try:
        credential = UserCredential.from_secret(
            req.user_api_key, req.base_url, req.model
        )
    except CredentialError as e:
        yield ServerSentEvent(
            raw_data=json.dumps({
                "message": str(e),
                "need_login": False,
                "need_vip": False,
            }, ensure_ascii=False),
            event="error",
        )
        return

    if credential is not None:
        # 仍要登录：追问会话按用户隔离，没登录就没有「他的会话」可言，
        # 也不能让未登录的人白用额度豁免。
        if not user:
            yield ServerSentEvent(
                raw_data=json.dumps({
                    "message": _NOT_LOGGED_IN,
                    "need_login": True,
                    "need_vip": False,
                }, ensure_ascii=False),
                event="error",
            )
            return
    else:
        allowed, _remaining, message = _check_quota_permission(user, "chat")
        if not allowed:
            yield ServerSentEvent(
                raw_data=json.dumps({
                    "message": message,
                    "need_login": user is None,
                    "need_vip": user is not None,
                }, ensure_ascii=False),
                event="error",
            )
            return

    quota_spent = False
    try:
        # 字幕来源顺序：社区视频表 → 入参 → 跑字幕提取。
        #
        # 社区表优先有两个理由：那省掉一次字幕提取（社区场景下字幕早就
        # 由首次解析者存好了），也不给前端篡改字幕的机会——入参那条路是
        # 老调用方的兼容路径，社区场景不依赖它。
        video = get_video_by_url(req.url)
        subtitle_text = ((video or {}).get("subtitle_text") or "").strip()
        if not subtitle_text:
            subtitle_text = (req.subtitle_text or "").strip()
        if not subtitle_text:
            extractor = _get_extractor()
            subtitle_data = await _run_in_thread(extractor.extract, req.url)
            if not subtitle_data["has_subtitle"]:
                head, reason, asr_reason = _subtitle_failure(subtitle_data)
                yield ServerSentEvent(
                    raw_data=json.dumps({
                        "message": f"{head}，无法回答问题",
                        "reason": reason,
                        "asr_reason": asr_reason,
                    }, ensure_ascii=False),
                    event="error",
                )
                return
            subtitle_text = subtitle_data["full_text"]

        # 即将调用 AI，此刻才扣额度（无字幕 / 未登录 / 超额都不扣）
        if credential is None:
            # 同上：扣减自带守卫，**没扣成（None）就是额度已满**，必须中止（工单 #17）。
            if consume_quota(user["id"], "chat") is None:
                yield ServerSentEvent(
                    raw_data=json.dumps({
                        "message": "今日追问次数已用完",
                        "reason": "quota_exhausted",
                    }, ensure_ascii=False),
                    event="error",
                )
                return
            quota_spent = True

        # 问答也显式回报额度，和总结走同一套展示。顶层数字跟 chat 走——
        # 旧前端只认顶层字段时，看到的必须是刚被扣掉的那个计数器，
        # 否则追问一次却显示解析余量没动。
        # 自带凭据时**没有额度可报**：上面刻意没查过库，凭空编一个数字
        # 报出去就是撒谎。只报「本次没消耗」，余额由前端原样留着。
        if credential is None:
            yield ServerSentEvent(
                raw_data=json.dumps(
                    _quota_payload(user["id"], "chat"), ensure_ascii=False
                ),
                event="quota",
            )
        else:
            yield ServerSentEvent(
                raw_data=json.dumps(
                    {"byok": True, "consumed": False}, ensure_ascii=False
                ),
                event="quota",
            )

        # 上下文取的是**这次追问之前**的会话，不含本轮 question——
        # 本轮问题由 chat_stream 单独追加在最后。取在前、拼在后，
        # 本轮问题就不可能被重复塞两遍。
        history = [
            (m["role"], m["content"])
            for m in get_recent_chat_messages(
                user["id"], req.url, CHAT_CONTEXT_TURNS - 1
            )
        ]

        summarizer = (
            _get_summarizer()
            if credential is None
            else _build_user_summarizer(credential)
        )
        answer_parts: list[str] = []
        for token in summarizer.chat_stream(subtitle_text, req.question, history):
            answer_parts.append(token)
            yield ServerSentEvent(
                raw_data=json.dumps(token, ensure_ascii=False),
                event="answer",
            )

        # 答案已完整产出，这一轮由此成为记录。落库在这里而不是让前端回调：
        # 追问记录是用户自己的数据，只能由服务端在真答出来时落。
        # 与扣费结清同一个位置——此后即便客户端断流，也不回滚也不重写，
        # 用户看到的半截答案不会被记成「他没问过」。
        append_chat_turn(user["id"], req.url, req.question, "".join(answer_parts))

        # 回答已完整产出，扣费就此结清：此后即便客户端断流也不回滚。
        quota_spent = False
        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except HTTPException:
        # 交给下面的 finally 回滚，然后原样抛出——不能降级成 SSE 错误事件。
        raise
    except Exception as e:
        if credential is not None:
            # 固定文案，不拼 str(e)，也不写日志——票面要求这条路径的
            # 日志记录列表、标准错误、标准输出里都搜不到凭据，而异常文本
            # 是唯一可能带着它回来的东西。诊断信息由用户重试一次拿到。
            yield ServerSentEvent(
                raw_data=json.dumps(
                    {"message": _BYOK_FAILURE_MESSAGE, "byok": True},
                    ensure_ascii=False,
                ),
                event="error",
            )
        else:
            # 不带凭据时维持原样：这里没有用户凭据要护，异常文本对用户
            # 排查问题有用。
            yield ServerSentEvent(
                raw_data=json.dumps(
                    {"message": f"回答失败: {str(e)}"}, ensure_ascii=False
                ),
                event="error",
            )
    finally:
        # 同 summarize_video：唯一回滚点，断流抛的 GeneratorExit /
        # CancelledError 不在 Exception 族里，但扣了的额度必须还回去。
        if quota_spent:
            refund_quota(user["id"], "chat")
