"""额度种类的注册表：一处枚举，其余派生（ADR 0033）。

「有哪几种额度」原本散在四处：列名在 `database.QUOTA_KINDS`、中文名在
`api_summarize._QUOTA_LABELS`、对外字段名在 `admin_api.quota_field_name`，
前端还有一份自己的种类清单加一份重复的中文名。工单 #38 收掉了列名，但
中文名与 pydantic 字段声明仍是两处**手工登记**，靠一条结构守卫指名守着。

本 module 把它们并成一条记录：一种额度 = 计数字段 + 日期字段 + 上限常量名
+ 覆盖列名 + 中文名。四列要拼进 SQL，第五项要进提示文案与协议，所以它们
必须由同一个对象给出——「加了一种额度」在任一分叉上漏掉的表现都是**静默**的：
漏列名 = `no such column`；漏中文名 = 少显示一格或 `KeyError`；漏字段名 =
管理员传了改不动却返回 200。

中文名放在这里而不是界面层：它是这个概念的**名字**，不是文案样式。
前端拿到 payload 里的 `labels`，不再自己抄一份。

查找 `spec` 也待在这里：能判定「拼错的 kind」的只有表本身，报错文本
留在数据层会与表分家。
"""
from typing import NamedTuple


class QuotaKind(NamedTuple):
    """一种额度的全部专有数据。前四项拼进 SQL，`label` 进文案与协议。"""

    count_col: str
    date_col: str
    limit_const: str
    override_col: str
    label: str


QUOTA_KINDS: dict[str, QuotaKind] = {
    "parse": QuotaKind(
        "daily_parse_count", "last_parse_date", "DAILY_PARSE_LIMIT",
        "parse_limit_override", "解析",
    ),
    "chat": QuotaKind(
        "daily_chat_count", "last_chat_date", "DAILY_CHAT_LIMIT",
        "chat_limit_override", "追问",
    ),
}


def spec(kind: str) -> QuotaKind:
    """取一种额度的记录。拼错的 kind 立刻抛错。

    静默 fallback 会让「扣了对话额度却记到解析头上」这类错误一路走到
    用户面前才发现。
    """
    try:
        return QUOTA_KINDS[kind]
    except KeyError:
        raise ValueError(
            f"未知的额度类型 {kind!r}；可用：{sorted(QUOTA_KINDS)}"
        ) from None


def labels() -> dict[str, str]:
    """种类 → 中文名，顺序即注册表顺序（前端按它渲染额度行）。"""
    return {kind: record.label for kind, record in QUOTA_KINDS.items()}
