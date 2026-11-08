"""yt-dlp 的出网口径：代理与 cookies 从环境变量注入。

机房 IP 段会被平台风控（腾讯云广州三台机器实测：B 站首页 200，视频页与
view API 一律 412，换 UA、带 Referer 都不变），只有换出口 IP 才解得开；
而 AI 字幕轨匿名请求只回 ``subtitles: []``，要登录态才有。

两项都是 yt-dlp 原生选项（``proxy`` / ``cookiefile``），所以这里不改任何
取数逻辑，只在每个 ``YoutubeDL`` 的选项字典上补这两个键。留空即不加。
"""

import os

PROXY_ENV = "YTDL_PROXY"
COOKIES_ENV = "YTDL_COOKIES_FILE"


def with_network(opts: dict) -> dict:
    """把 ``YTDL_PROXY`` / ``YTDL_COOKIES_FILE`` 并入给定选项，返回同一个字典。"""
    proxy = os.getenv(PROXY_ENV, "").strip()
    cookies = os.getenv(COOKIES_ENV, "").strip()
    if proxy:
        opts["proxy"] = proxy
    if cookies:
        opts["cookiefile"] = cookies
    return opts
