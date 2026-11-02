"""管理后台 API（工单 #12 / #13 / ADR 0010）。

端点全表如下，**形状以这张表为准**：

| 端点 | 鉴权 | 写操作 |
|---|---|---|
| `GET /api/models` | **公开** | 只读 |
| `GET /api/admin/models` | require_admin | 只读 |
| `PATCH /api/admin/models/{id}` | require_admin | **写**（改已有行，不新增） |
| `GET /api/admin/users` | require_admin | 只读 |
| `POST /api/admin/users` | require_admin | **写**（建号） |
| `PATCH /api/admin/users/{id}` | require_admin | **写**（管理员标记，ADR 0012） |
| `DELETE /api/admin/users/{id}` | require_admin | **写**（有内容则 409） |
| `POST /api/admin/users/{id}/quota` | require_admin | **写** |
| `GET /api/admin/community` | require_admin | 只读（每项带 status） |
| `PATCH /api/admin/community/{video_id}` | require_admin | **写**（只改标签） |
| `DELETE /api/admin/community/{video_id}` | require_admin | **写**（只删 videos 一行） |
| `GET /api/admin/tags/vocabulary` | require_admin | 只读 |
| `GET /api/admin/invite-codes` | require_admin | 只读（码已脱敏；可按 status 筛选） |
| `POST /api/admin/invite-codes` | require_admin | **写**（发码，可批量；明文只给一次） |
| `POST /api/admin/invite-codes/{id}/revoke` | require_admin | **写**（作废） |

## 为什么 `/api/models` 是公开的

工单 #13 的原话：普通用户的 BYOK 面板要靠这份清单渲染厂商下拉，
挂 `require_admin` 会让「消除前后端漂移」变成「普通用户的下拉没了」。
公开端点只吐 `enabled=1` 的行与公共字段，与管理端点同源不同投影。

## 鉴权只写在依赖上

路由体内**没有一处** `if not user`：身份判定全部由 `require_admin`
在依赖层完成（ADR 0010 的约定）。少写一次判断就少一个「这条忘了挂依赖」
的位置，而那种遗漏在测试里表现为「全员 403」——照样绿。

## 响应体里没有凭据，也不该有

平台 Key 在 .env、用户 BYOK 不落盘（ADR 0004 / 0011）。本文件不做
「记得删掉 key 字段」这种逐个剔除，字段形状由数据层的白名单投影决定
（`model_catalog.PUBLIC_FIELDS` / `ADMIN_FIELDS`，以及各 `_admin_*_item`）。
将来给表加一列 `api_key`，白名单不会因为那列存在就把它吐出去。
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, create_model

import model_catalog
from auth import hash_password, require_admin, validate_email, validate_password
from database import (
    ADMIN_PAGE_SIZE_DEFAULT,
    QUOTA_UNLIMITED,
    UserConflict,
    admin_user_detail,
    create_admin_user,
    create_invite_code,
    create_invite_codes,
    delete_user,
    delete_video_record,
    get_invite_code_by_id,
    get_user_by_email,
    invite_code_expiry,
    INVITE_CODE_BATCH_MAX,
    INVITE_CODE_STATUSES,
    is_vip_active,
    list_admin_community,
    list_admin_users,
    list_invite_codes,
    revoke_invite_code,
    set_user_admin,
    set_user_quota_override,
    update_video_tags,
)
from quota_kinds import QUOTA_KINDS
from tags import MAX_TAGS, VOCABULARY_GROUPS, validate_tags

logger = logging.getLogger("admin_api")

router = APIRouter(prefix="/api", tags=["管理后台"])


# ── 额度值域（唯一值得单测的部分）──────────────────────────
#
# 值域规则（工单 #12）：
#   null  → 清除覆盖，回落全局
#   0     → 一条都不能用
#   -1    → 无限（与数据层 remaining == -1 同一个约定）
#   其它负数 / 非整数 / 超大值 → 400
#
# 纯函数，不触库也不依赖 FastAPI，所以能直接单测。

#: 单人上限的可取上界。一年 10 万次解析远超任何真实用法，
#: 而没有上界的话一个手滑的 10**18 会变成「这列 int 存不下」的运行期错误。
QUOTA_OVERRIDE_MAX = 100_000


class QuotaValueError(ValueError):
    """额度值域非法。路由层把它翻译成 400。"""


#: 额度种类 → 请求体 / 响应体里那个字段的名字（工单 #38）。
#:
#: **这是一条命名约定，不是常量里的数据**：`QUOTA_KINDS` 只给列名，给不出
#: 「对外叫什么」。提成具名函数是为了让它**有唯一的定义处**——请求体侧
#: （`QuotaUpdateRequest` 的字段、`admin_set_quota` 读）与响应体侧
#: （`database._admin_user_item` 写）都从这一个名字派生。
def quota_field_name(kind: str) -> str:
    """额度种类 → 线上协议里的字段名，如 ``parse`` → ``parse_limit``。"""
    return f"{kind}_limit"


def normalize_quota_value(raw: Any, field: str) -> int | None:
    """把请求里的一个额度值收敛成 int | None，非法就抛 QuotaValueError。

    刻意**只收 int**，float 一律拒绝：``1.0`` 与 ``1`` 在 JSON 里是同一个
    语义，但「收下 1.0」就得额外决定它存 1 还是 1.0，而那条规则没有消费者。
    前端也不会产生 ``5.0``——``JSON.stringify(5)`` 给的是 ``5``。

    bool 显式拒绝：Python 里 ``True == 1``，不拦的话 ``{"parse_limit": true}``
    会静静地变成「每天 1 次」。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise QuotaValueError(f"{field} 必须是整数或 null，不能是布尔值")
    if not isinstance(raw, int):
        raise QuotaValueError(f"{field} 必须是整数或 null，实得 {raw!r}")
    if raw < QUOTA_UNLIMITED:
        raise QuotaValueError(
            f"{field} 只能取 null（回落全局）、0（不可用）或 -1（无限），"
            f"负数 {raw} 不在约定内"
        )
    if raw > QUOTA_OVERRIDE_MAX:
        raise QuotaValueError(
            f"{field} 上限是 {QUOTA_OVERRIDE_MAX}，实得 {raw}"
        )
    return raw


# ── 请求体 ──────────────────────────────────────────────────

#: 改单人额度的请求体。字段由注册表生成（ADR 0033），一种额度一个字段。
#:
#: 每个字段都声明成 ``Any`` 且缺省 ``None``：声明成 ``int | None`` 的话，
#: 非法值会由 pydantic 变成 **422**，而契约要求的是 **400**——「参数不合法」
#: 与「请求体结构不对」是两种不同的客户端错误，不该混成一个码。值域判定
#: 交给 `normalize_quota_value`，那才是可单测的纯函数。
#:
#: **字段缺省 ≠ 传 null**：
#:   - 缺省（这个 key 压根没出现）→ 不动这一项，只改传了的那项
#:   - 显式 null → 清除覆盖，回落全局
#: 两者混为一谈的话，一次只想改对话额度的请求会把解析额度静默清掉。
#:
#: 未声明的键仍被**静默丢弃**（pydantic 默认 ``extra="ignore"``）：管理员
#: 传了一个不存在的额度名，请求照样 200 而什么都不改。这是既有协议行为，
#: 由 `tests/test_quota_kinds_single_source.py` 钉着。
QuotaUpdateRequest = create_model(
    "QuotaUpdateRequest",
    __doc__="改单人额度的请求体。字段由 `quota_kinds.QUOTA_KINDS` 生成。",
    **{quota_field_name(kind): (Any, None) for kind in QUOTA_KINDS},
)


# ── 模型清单（工单 #13）─────────────────────────────────────

@router.get("/models")
async def public_model_providers():
    """可用厂商清单。**任何人可访问，含未登录访客。**

    刻意不挂鉴权依赖：厂商、端点、模型这些是**产品配置**，
    本来就该公开（byok.js 的注释第 4 条：配置可以公开，key 不行）。
    挂上 get_optional_user 再按登录状态分支，等于给「将来多给点字段」
    留一个位置；不挂则那个分支在结构上就不存在。
    """
    return {"items": model_catalog.list_model_providers(only_enabled=True)}


@router.get("/admin/models")
async def admin_model_providers(_: dict = Depends(require_admin)):
    """厂商清单（管理视图）。全部行，含已下架的，并带上架状态与排序。"""
    return {
        "items": model_catalog.list_model_providers(
            only_enabled=False, admin_view=True
        )
    }


class ModelUpdateRequest(BaseModel):
    """改一个厂商行。字段全是 ``Any``，值域判定交给数据层的纯函数。

    声明成具体类型的话非法值会由 pydantic 变成 422，而契约要的是 400 ——
    「参数不合法」与「请求体结构不对」是两种客户端错误，不该混成一个码
    （与 QuotaUpdateRequest 同一理由）。

    **没出现的键 = 不改这一项**，所以改默认模型不必把整行重发一遍。

    ``extra="forbid"`` 不是洁癖：pydantic 默认会**悄悄丢掉**未声明的键，
    于是 ``{"api_key": "...", "label": "新名"}`` 会只剩 label 生效并返回
    200 —— 调用方以为凭据也改了。数据层的 ``EDITABLE_FIELDS`` 拦得住这个，
    但它在 HTTP 边界根本走不到（键先被 pydantic 丢了），白写了。

    用 422 而不是 400 正是本类自己的口径：「参数不合法」(400) 与
    「请求体结构不对」(422) 是两种客户端错误，不该混成一个码。
    """

    model_config = ConfigDict(extra="forbid")

    label: Any = None
    hint: Any = None
    base_url: Any = None
    models: Any = None
    default_model: Any = None
    enabled: Any = None
    sort_order: Any = None


@router.patch("/admin/models/{provider_id}")
async def admin_update_model(
    provider_id: str,
    payload: ModelUpdateRequest,
    _: dict = Depends(require_admin),
):
    """改厂商行。**可改**：显示名、提示、端点、模型列表、平台默认、上下架、排序。

    明确不提供**新增**端点：新增平台厂商还得在 .env 里配凭据并重启
    （ADR 0011 记录的代价）。后台建出来的那一行会立刻不可用 ——
    那比「不能建」更糟。改已有行是纯配置操作，效果当场可见。

    改完**回读**而不是回显：返回的是「库里现在是什么」。
    """
    # exclude_unset=True → 只处理请求里真的出现的键
    patch = payload.model_dump(exclude_unset=True)
    try:
        updated = model_catalog.update_model_provider(provider_id, patch)
    except model_catalog.ModelValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if updated is None:
        # 厂商 id 不存在：不能静默成功——那会让调用方以为改到了
        raise HTTPException(status_code=404, detail=f"厂商 {provider_id} 不存在")

    # 下架的即时可见性：平台默认模型读的就是 enabled=1 的行
    return {"item": updated, "platform_default": model_catalog.platform_default_model(provider_id)}


# ── 用户记录（工单 #12）─────────────────────────────────────

@router.get("/admin/users")
async def admin_users(
    limit: int = Query(ADMIN_PAGE_SIZE_DEFAULT, description="每页条数，有上界"),
    offset: int = Query(0, description="跳过的条数"),
    q: str = Query("", description="按邮箱模糊匹配"),
    _: dict = Depends(require_admin),
):
    """用户记录列表（只读）。分页上界由数据层统一收敛。"""
    return list_admin_users(limit=limit, offset=offset, q=q)


class QuotaUpdateResult(BaseModel):
    user: dict
    note: str | None = None
    message: str


@router.post("/admin/users/{user_id}/quota")
async def admin_set_quota(
    user_id: int,
    payload: QuotaUpdateRequest,
    _: dict = Depends(require_admin),
):
    """改某个用户的解析/对话额度。

    明确不做（项目范围边界）：封禁/解封、下架社区视频、改 VIP。
    账号的增删与管理员标记在下面另外三个端点（ADR 0012）；
    这里只动两列额度，且随时可以用 null 改回去。
    """
    # 404 先判：用户不存在时**不能**静默成功。
    before = admin_user_detail(user_id)
    if before is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # exclude_unset=True → 只处理请求里真的出现了的 key（见 QuotaUpdateRequest）。
    #
    # 字段名（parse_limit）在这里映射成额度种类（parse）：数据层那套
    # QUOTA_KINDS 按种类索引，两种命名混过一次，症状是数据层抛
    # 「未知的额度类型 'parse_limit'」——只有真跑起来才看得见的错。
    # 名字由 quota_field_name 派生，请求体的字段也由注册表生成（ADR 0033），
    # 所以「声明了却没遍历到」这种错位在结构上不存在了。
    supplied = payload.model_dump(exclude_unset=True)
    try:
        overrides = {}
        for kind in QUOTA_KINDS:
            field = quota_field_name(kind)
            if field in supplied:
                overrides[kind] = normalize_quota_value(supplied[field], field)
    except QuotaValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if overrides:
        set_user_quota_override(user_id, overrides)

    # 回读而不是回显：返回的是「库里现在是什么」，
    # 而不是「我们请求写进去的是什么」。
    user = admin_user_detail(user_id)
    if user is None:
        # 刚写完就没了：只有并发删除能做到。不假装成功。
        raise HTTPException(status_code=404, detail="用户不存在")

    if is_vip_active(user):
        # 覆盖值**照写不误**（VIP 到期后它就该生效），但必须明说此刻不生效：
        # check_quota_kind / consume_quota / refund_quota 三处都在解析上限
        # 之前就为 VIP 短路返回 -1。静默成功会让管理员以为改成了。
        return QuotaUpdateResult(
            user=user,
            note="vip_not_effective",
            message="给有效 VIP 改额度不会生效",
        )
    return QuotaUpdateResult(user=user, note=None, message="额度已更新")


# ── 社区记录（工单 #12）─────────────────────────────────────

@router.get("/admin/community")
async def admin_community(
    limit: int = Query(ADMIN_PAGE_SIZE_DEFAULT, description="每页条数，有上界"),
    offset: int = Query(0, description="跳过的条数"),
    _: dict = Depends(require_admin),
):
    """社区记录列表。**不过滤 status**——占位行也是记录，理由见
    :func:`database.list_admin_community`。

    每项都带 status（ready / pending），前端按它区别渲染。
    """
    return list_admin_community(limit=limit, offset=offset)


class CommunityUpdateRequest(BaseModel):
    """后台改社区标签的请求体。**只认 tags 一个键。**

    声明成 ``list | None`` 而不是 ``Any``：结构错（tags 不是数组）由 pydantic
    变成 422，值域错（空、超上限、词表外）由本文件给 400——与文件头
    「值域 400 / 结构 422」的口径一致，两种客户端错误不该混成一个码。

    刻意**不**用 ``Any``：``UserAdminUpdateRequest`` 用 Any 是因为它的值域只能
    靠一个额外校验器兜住，代价是这个字段彻底没有类型保证（见那里的注释）。
    这里不需要付那份代价——「是不是数组」交给 pydantic 是最省事也最不会漏的
    做法，不给它开口子的理由没有。

    ``extra="forbid"`` 不是洁癖：pydantic 默认**悄悄丢掉**未声明的键，于是
    ``{"tags": [...], "status": "pending"}`` 会只改标签然后返回 200，调用方却
    以为下架成功了——收到 200 的前端不会再去确认一遍。
    """

    model_config = ConfigDict(extra="forbid")

    tags: list | None = None


@router.patch("/admin/community/{video_id}")
async def admin_update_community(
    video_id: int,
    payload: CommunityUpdateRequest,
    _: dict = Depends(require_admin),
):
    """改一条社区视频的标签。**只改 tags 与 updated_at**（数据层有论证）。

    四个 400 分支，按判定的先后排：

    - 缺 tags（这个键压根没出现）→ 「没有要改的字段」：与 is_admin 的 null
      同理，null 不是「改成空」，是「没说要改」。
    - 空数组 → 「每条视频至少要有 1 个标签」。这是**系统不变式**：模型路径
      永不产出空（validate_tags 会回落到「其他」），所以空数组只可能来自
      一个手滑或一个坏调用方。
    - 超过 ``MAX_TAGS`` 个 → 400。validate_tags 对超额是**截断**不是报错，
      4 个词表内的标签会被悄悄砍成 3 个然后返回 200——那是容错，不是请求的
      意思。管理员明确给了 4 个，我们要的是「多了 1 个」这个信息。
    - 词表外值 → 400，detail 里**列出被拒掉的值**。

    顺序由词表说了算（accepted 原样透传，不按请求的书写顺序）：前端展示要
    稳定，同样的标签集合换个顺序提交，展示就会抖一下。

    **为什么管理员这条路 fail-fast，而模型那路容错**：模型路径在 rejected
    非空时回落到「其他」，因为「模型选不出来」是**模型的问题**——它只用于排查
    日志，静默兜底正好。而管理员提交词表外值是**请求错误**：把「AI编程」静默
    变成「其他」，等于把打错字藏起来——管理员以为自己改好了，分类页上却多出
    一条「其他」，而真正想要的分类没设上。模型路径容错，管理员路径 fail-fast。
    """
    if payload.tags is None:
        raise HTTPException(status_code=400, detail="没有要改的字段")
    if not payload.tags:
        raise HTTPException(
            status_code=400, detail="标签不能为空：每条视频至少要有 1 个标签"
        )
    if len(payload.tags) > MAX_TAGS:
        raise HTTPException(
            status_code=400,
            detail=f"最多 {MAX_TAGS} 个标签，实得 {len(payload.tags)} 个",
        )

    accepted, rejected = validate_tags(payload.tags)
    if rejected:
        # 被拒掉的值必须出现在 detail 里：只说「不在词表内」的话，管理员
        # 不知道自己错的是哪一个，只能一个个试。
        raise HTTPException(
            status_code=400,
            detail=(
                f"这些标签不在固定词表内：{'、'.join(str(x) for x in rejected)}。"
                "完整词表见 GET /api/admin/tags/vocabulary"
            ),
        )

    # 回读而不是回显：返回的是「库里现在是什么」，形状与列表项逐字段一致。
    item = update_video_tags(video_id, accepted)
    if item is None:
        # 不能静默成功——那会让管理员以为改到了某个其实不存在的地方
        raise HTTPException(status_code=404, detail=f"视频 {video_id} 不存在")
    return {"item": item}


@router.delete("/admin/community/{video_id}")
async def admin_delete_community(
    video_id: int,
    _: dict = Depends(require_admin),
):
    """删掉一条社区视频。**只删 videos 一行，用户的解析历史一行不动。**

    这是用户明确要求的语义：视频从社区消失，解析过它的用户在自己的历史里
    仍看得到自己那条记录。前提（schema 里没有任何外键指向 videos）与允许删
    pending 占位行的代价，都写在 :func:`database.delete_video_record` 里——
    那里有依据，这里不重复。

    幂等性刻意不做：0 有两种可能（本来就不存在 / 刚被别人删了），报 404 而不是
    静默成功，两种都不该被当成「删过了」。
    """
    if not delete_video_record(video_id):
        raise HTTPException(status_code=404, detail=f"视频 {video_id} 不存在")
    return {"deleted": video_id}


# ── 标签词表（只读）─────────────────────────────────────────
#
# 为什么要有这个出口：前端不能自己抄一份词表。CommunityPage.vue 早就为社区页
# 做过同一条反漂移决策（「标签选项由当前页的卡片汇总而来，不额外维护一份标签
# 词表」），而后台要渲染的是**全量**勾选框——卡片汇总只给得出此刻有人在用的
# 那几个，于是管理员没法给新视频选一个「还没人用过」的分类，词表被现有的数据
# 悄悄反向限死。抄一份的后果更具体：后台能选出一个模型永远不会产生的标签。
#
# 口径与 /api/models 那个公开端点一致：公开端点解决「普通用户也要读」，
# 这里解决「只有后台要读」，两者同源（都从 tags 拿），不同投影。


@router.get("/admin/tags/vocabulary")
async def admin_tag_vocabulary(_: dict = Depends(require_admin)):
    """固定标签词表（ADR 0005）。**只读**：词表是产品规则，管理员不造词，
    只从现有词里挑——想加词得改 ``tags.py`` 并发版。

    ``maxTags`` 取 :data:`tags.MAX_TAGS` 而不是写死 3：写死就是第二个真值
    来源，词表上限改了这里会悄悄变成错的，而前端是照着它禁用的。

    ``groups`` 保留 ``VOCABULARY_GROUPS`` 的原始分组与顺序：``tags.py`` 的
    注释写明分组「顺带说明标签的适用语境，能少一些误选」，拍平成一坨就把这个
    信息丢了，前端只能渲染一条没有上下文的词列表。顺序本身也有语义——展平后
    严格等于 ``TAG_VOCABULARY``，那是分类页与筛选器的稳定输出顺序，破坏它会让
    展示抖动。
    """
    return {
        "maxTags": MAX_TAGS,
        "groups": [
            {"name": name, "tags": list(group)}
            for name, group in VOCABULARY_GROUPS
        ],
    }


# ── 账号生命周期（ADR 0012）─────────────────────────────────
#
# ADR 0010 原本把「删账号」列进「明确不做」，理由是 videos.parsed_by
# 是弱引用、后台不该靠删人来顺带清理社区数据。这条理由**仍然成立**——
# 下面 delete_user 确实一行 videos 都不碰。变的是另一件事：管理员需要一个
# 处理测试账号、误注册账号与离职账号的出口，而这个出口不必、也不该以
# 「顺带清掉别人看得见的社区内容」为代价。


class UserCreateRequest(BaseModel):
    """后台建号。email / password 就是 str，错类型让 pydantic 直接 422，
    比收下再转成 400 报错省事。"""

    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    is_admin: bool = False


class UserAdminUpdateRequest(BaseModel):
    """改管理员标记。**只认 is_admin 一个键**——VIP 不在这里
    （项目范围边界：会员判定留在代码里不动，新增测试不得锁会员行为）。

    ``is_admin`` 声明成 ``Any`` 是本文件的既有约定：声明成 ``bool`` 的话非法值
    会由 pydantic 变成 422，而额度与厂商两个端点的契约要的是 400。代价是这个
    字段不再有任何类型保证，所以**必须**由下面的 _as_admin_flag 把关——
    少了它，``{"is_admin": []}`` 会走 ``bool([])`` 静默变成「撤权」，
    ``{"is_admin": {}}`` 同理（bool({}) 也是 False），而调用方一个错都收不到。
    """

    model_config = ConfigDict(extra="forbid")

    is_admin: Any = None


def _as_admin_flag(raw: Any) -> bool:
    """把请求里的 is_admin 收敛成 bool，非法值抛 400。

    刻意**不**收字符串：``bool("false")`` 是 True，一个想撤权的调用方传
    "false" 会拿到提权，且 200。这种输入只有打错字才会出现，但打错字的
    后果不该是「静默反向」。
    """
    if not isinstance(raw, bool):
        raise HTTPException(status_code=400, detail="is_admin 必须是 true 或 false")
    return raw


def _conflict(exc: UserConflict) -> JSONResponse:
    """把 blockers 挂到 409 的响应体上。

    FastAPI 的 HTTPException 没有自定义 body 的口子，所以走 JSONResponse
    手工拼——这比把行数编码进 detail 字符串强：前端要的是数字，
    不是从中文里正则抠数字。blockers 为空时就是普通的 409。
    """
    return JSONResponse(
        status_code=409,
        content={"detail": exc.detail, "blockers": exc.blockers},
    )


@router.post("/admin/users")
async def admin_create_user(
    payload: UserCreateRequest,
    _: dict = Depends(require_admin),
):
    """建号。初始密码由管理员指定，创建后由管理员自行转交。"""
    # 与公开注册**同一套**校验：各写各的会出现「同一个邮箱后台能建、
    # 公开注册说格式不对」，而这种分歧只会在用户投诉时才暴露。
    if not validate_email(payload.email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    err = validate_password(payload.password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if get_user_by_email(payload.email):
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    created = create_admin_user(
        payload.email, hash_password(payload.password), bool(payload.is_admin))

    # 回读：前端要显示的是库里那一行（含额度回落后的真实上限），
    # 不是我们打算写进去的东西。
    return {"user": admin_user_detail(created["id"])}


@router.patch("/admin/users/{user_id}")
async def admin_set_user_admin(
    user_id: int,
    payload: UserAdminUpdateRequest,
    admin: dict = Depends(require_admin),
):
    """改管理员标记。提权与撤权下一次请求即生效（不塞进 JWT，ADR 0010）。"""
    if payload.is_admin is None:
        raise HTTPException(status_code=400, detail="没有要改的字段")
    flag = _as_admin_flag(payload.is_admin)
    try:
        changed = set_user_admin(user_id, flag, acting_id=admin["id"])
    except UserConflict as exc:
        return _conflict(exc)
    if not changed:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = admin_user_detail(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"user": user}


@router.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    admin: dict = Depends(require_admin),
):
    """删号。**名下有订单或解析历史就 409**，不做静默级联（ADR 0012）。"""
    try:
        delete_user(user_id, acting_id=admin["id"])
    except UserConflict as exc:
        # return 不是 raise：_conflict 返回的是 JSONResponse，不是异常。
        return _conflict(exc)
    return {"deleted": user_id}


# ── 邀请码（工单 #99 / #103）────────────────────────────────
#
# 注册闸门的钥匙由管理员发（ADR 0023）。限流按来源 IP 计，换个 IP 就能
# 重来，所以它挡不住批量注册；邀请码能，因为码是有限的几枚、且有名字
# （issued to whom）——它把「谁能进来」从公网匿名变成一个人的决定。
#
# 工单 #103 之后**库里只存哈希**：明文码只在创建那一次响应里给，之后
# 任何出口都拿不回完整码——所以管理员要自己复制走。


class InviteCodeCreateRequest(BaseModel):
    """发一枚邀请码的请求体。

    三个字段都**可省**：省掉就用默认（一次、不过期、无备注）。

    ``max_uses`` / ``expires_in_hours`` 声明成 ``Any``：值域错（0、负数、
    字符串）要由本文件转成 **400**，而不是让 pydantic 回 422——与本文件
    既有的「值域 400 / 结构 422」口径一致。收下再判的写法见
    ``normalize_quota_value``。

    ``extra="forbid"``：传了一个没声明的键时**报错**，而不是被 pydantic
    静默丢掉。丢掉的话管理员会以为「可用次数」生效了，其实发出去的是
    默认的一次性码。
    """

    model_config = ConfigDict(extra="forbid")

    max_uses: Any = None
    expires_in_hours: Any = None
    note: str | None = None
    count: Any = None


def _normalize_invite_count(raw: Any) -> int:
    """一次发几枚（工单 #104 的批量发码）。默认 1。

    区间是 1..INVITE_CODE_BATCH_MAX：上界存在的理由是**明文只给一次**，
    一次发几百枚意味着管理员要手动复制几百行，而绝大多数码根本没人要。

    bool 显式拒绝（``True == 1``），与 max_uses 同一口径。
    """
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(status_code=400, detail="count 必须是正整数")
    if raw < 1:
        raise HTTPException(status_code=400, detail="count 至少是 1")
    if raw > INVITE_CODE_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多发 {INVITE_CODE_BATCH_MAX} 枚邀请码",
        )
    return raw


def _normalize_max_uses(raw: Any) -> int:
    """可用次数。默认 1；至少 1。

    bool 显式拒绝：``True == 1``，收下就意味着「可用无数次」或
    「一次」取决于调用方想的是什么——而这个字段决定的是**名额**。
    """
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(status_code=400, detail="max_uses 必须是正整数")
    if raw < 1:
        raise HTTPException(status_code=400, detail="max_uses 至少是 1")
    return raw


def _normalize_expires_in_hours(raw: Any) -> int | None:
    """有效期（小时）。None = 永不过期。

    0 与负数都拒：「0 小时后过期」在字面上成立，但它发出去就是一枚
    立刻作废的码，管理员会以为自己发错了——而真正的错误是传了 0。
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(status_code=400, detail="expires_in_hours 必须是正整数")
    if raw < 1:
        raise HTTPException(status_code=400, detail="expires_in_hours 至少是 1")
    return raw


@router.post("/admin/invite-codes")
async def admin_create_invite_code(
    payload: InviteCodeCreateRequest,
    admin: dict = Depends(require_admin),
):
    """发一枚邀请码。**明文码只在这一次响应里给。**

    为什么明文只给一次：库里存的是 sha256 摘要（工单 #103），**明文取不回来**
    ——所以明文只在这一刻出现一次，之后列表里只有脱敏提示（前 4 后 4）。
    但明文再出现在列表里，就等于后台页面成了一张「所有人都有注册钥匙」的
    表。创建完，管理员手上那一份就是全部——所以前端在创建成功后要**立刻
    显示并提示复制**，之后只剩脱敏码。

    回读而不是回显：返回的是「库里现在是什么」（含真实 id、真实计数）。
    """
    max_uses = _normalize_max_uses(payload.max_uses)
    hours = _normalize_expires_in_hours(payload.expires_in_hours)
    count = _normalize_invite_count(payload.count)
    created = create_invite_codes(
        count,
        created_by=admin["id"],
        max_uses=max_uses,
        expires_at=invite_code_expiry(hours),
        note=payload.note,
    )
    # `item` 是单枚出口（老前端读它），`items` 是批量出口。两者都在：
    # 一次发一枚时 item 就是 items[0]，前端不用为「发几枚」写两套分支。
    return {"item": created[0], "items": created}


@router.get("/admin/invite-codes")
async def admin_list_invite_codes(
    limit: int = Query(ADMIN_PAGE_SIZE_DEFAULT, description="每页条数，有上界"),
    offset: int = Query(0, description="跳过的条数"),
    status: str | None = Query(None, description="按状态筛选：active/exhausted/expired/revoked"),
    _: dict = Depends(require_admin),
):
    """邀请码列表。**不回显完整码**，只有脱敏提示 ``code_masked``（前 4 后 4）。

    库里存的是 sha256 摘要，**明文取不回来**（工单 #103）：所以列表里的
    ``code_masked`` 是建码那一刻存下的脱敏提示（``code_hint`` 列），
    管理员靠它认出「这枚是我发给张三的那枚」——但它**不是明文，不能
    用来注册**。

    每项带用量（used_count / max_uses）、是否过期、是否作废——这三样
    是管理员唯一需要的信息：还能不能用、还能用几次、要不要作废。

    ## 状态筛选（工单 #104）

    ``status`` 取 active / exhausted / expired / revoked 之一；给别的值
    回 **400** 而不是「当成没有筛选」——静默忽略会让管理员以为筛过了，
    而列表其实没筛。
    """
    if status is not None and status not in INVITE_CODE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"未知的邀请码状态：{status}（可选 {'/'.join(INVITE_CODE_STATUSES)}）",
        )
    return list_invite_codes(limit=limit, offset=offset, status=status)


@router.post("/admin/invite-codes/{invite_id}/revoke")
async def admin_revoke_invite_code(
    invite_id: int,
    _: dict = Depends(require_admin),
):
    """作废一枚邀请码。

    **作废与用尽是两件事**：作废是「这枚码从此作废」（可能还没用），
    用尽是「名额用完了」。所以库里只存 revoked 一个标志，用尽是
    ``used_count >= max_uses`` 这个**算出来的**状态——不存成两列，
    否则两个真相会互相打架。

    **幂等**：已作废的码再作废一次仍然成功。已经作废过的码再点一次
    报 409 是误报——管理员点重了不是错误。
    """
    if not revoke_invite_code(invite_id):
        raise HTTPException(status_code=404, detail="邀请码不存在")
    item = get_invite_code_by_id(invite_id)
    if item is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    return {"item": item}
