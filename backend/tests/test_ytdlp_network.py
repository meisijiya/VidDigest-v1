"""yt-dlp 出网开关：``YTDL_PROXY`` / ``YTDL_COOKIES_FILE`` 必须真的落到选项上。

线上解析整条走不通的原因就是出口 IP 被平台风控。开关写了却没接进 yt-dlp，
在这个仓库里看不出任何差别——所以断言的是**构造 yt-dlp 客户端时那份选项**。

第二条是结构守卫：任何新的 ``YoutubeDL`` 构造点如果绕过 ``with_network``，
代理与 cookies 就只对老路径生效；这种漏点只会表现为「有些链接还是 412」。
"""
import ast
from pathlib import Path

import ytdlp_net

BACKEND = Path(__file__).resolve().parents[1]


def test_proxy_and_cookie_file_reach_the_options(monkeypatch):
    monkeypatch.setenv("YTDL_PROXY", " http://127.0.0.1:7890 ")
    monkeypatch.setenv("YTDL_COOKIES_FILE", "/etc/viddigest/cookies.txt")

    opts = ytdlp_net.with_network({"quiet": True})

    assert opts["proxy"] == "http://127.0.0.1:7890"
    assert opts["cookiefile"] == "/etc/viddigest/cookies.txt"


def test_blank_values_leave_yt_dlp_untouched(monkeypatch):
    monkeypatch.setenv("YTDL_PROXY", "   ")
    monkeypatch.delenv("YTDL_COOKIES_FILE", raising=False)

    assert ytdlp_net.with_network({"quiet": True}) == {"quiet": True}


def _names_wrapped_by_helper(tree: ast.Module) -> set:
    """``X = with_network(...)`` 里的 X。"""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        callee = value.func if isinstance(value, ast.Call) else None
        if callee is not None and getattr(callee, "id", None) == "with_network":
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def test_every_ytdlp_construction_goes_through_the_network_helper():
    unwrapped = []
    for module in ("downloader.py", "summarizer.py"):
        tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
        wrapped = _names_wrapped_by_helper(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "YoutubeDL"):
                continue
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Name) and arg.id in wrapped):
                unwrapped.append(f"{module}:{node.lineno}")

    assert unwrapped == [], f"这些 YoutubeDL 构造绕过了 with_network：{unwrapped}"
