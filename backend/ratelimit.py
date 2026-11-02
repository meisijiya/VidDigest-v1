"""按调用方限流：给**不挂鉴权**的公开端点补一层护栏。

为什么要有这层：`/api/parse`、`/api/download`、`/api/direct-url`、
`/api/proxy/thumbnail` 与登录注册端点都不挂鉴权，谁都能打；而它们的
成本全在服务器这一侧（出网抓取、落盘、bcrypt 计算）。额度护栏只挂在
模型调用那边（每日解析 / 追问两个计数器），管不到这些端点——没有这一层，
一个脚本就能把带宽与 CPU 打满。

怎么算「同一个人」：未登录请求按来源 IP 分桶。来源 IP 取自**直接对端**；
只有当直接对端是回环或私网地址（说明这台机器前面确实有个反向代理，
本仓的 nginx 就是这种）才采信 `X-Forwarded-For`——取它的**最后一段**。
为什么是末位：边缘代理（nginx）追加这个头时把自己看到的直接对端地址
（`$remote_addr`）放在**末尾**，那才是权威地址；客户端自带的那些段可以
随便伪造。取末位，前缀就失效了（ADR 0022）。
公网直连时**不采信**该头：否则任何人改一个头就能绕开限流，
限流器就成了摆设。

为什么用固定窗口而不是滑动窗口或令牌桶：滑动窗口要维护时间序列，
令牌桶要处理突发与补充速率——这两样都要写一堆难解释的规则。固定窗口
的语义一句话说得清（「一分钟内最多 N 次」），`Retry-After` 也能直接
给出窗口剩余秒数。够用，且口径好解释。

计数键是**「来源 + 桶」**：同一个来源在每个端点各有一个计数器，六个桶
互不相干。为什么必须是两段：键里**只有来源**的话，六个桶会共用同一个
计数器——判红时用的是**当前桶**的上限，累加的却是**所有桶**的总次数，
于是打满解析桶（20 次）之后，**第一次**登录就会被拒（工单 #102）。
两段用 `:` 隔开：来源那段自带 `ip:` 前缀，桶名以 `/` 开头，两者不撞。

范围：这是**单机**限流。进程重启计数就清零，多实例之间各数各的——
现在后端是单进程单 worker（`docs/TECHNICAL.md` §1.3），多实例部署那天
再换成共享存储。

这里有**两道闸门**，管的是两件事：

- **速率闸门**（`rate_limit`）：管「一段时间内来了多少次」——超了说明来得频繁。
- **并发闸门**（`concurrency_limit`）：管「此刻有几个还在跑」——超了说明同时在跑太多。

同一个来源一分钟只发一次、但那个任务跑十分钟，速率闸门一次都不会拦；
反过来一秒发二十次、但每个都秒回，速率闸门会拦、并发闸门不会。两道闸门
各调各的，互不干扰（ADR 0021）。
"""

import math
import os
import threading
import time
from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request

#: 可信对端的网段（回环、私网、链路本地）。
#:
#: 逐条列出来而不是用 `ipaddress.is_private`：Python 把文档保留段
#: （192.0.2.0/24 等）也算私有，那不是「部署里会出现的对端」。
_TRUSTED_NETWORKS = tuple(
    ip_network(net) for net in (
        "127.0.0.0/8", "::1/128",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "fe80::/10", "fc00::/7",
    )
)

#: 计数表最多记多少个来源。
#:
#: 为什么要有上限：限流器自己不能成为新的故障源——攻击者换着 IP 打，
#: 每一分钟塞进来一批新键，内存就被这些键吃掉了。过期条目会被清理，
#: 但清理之前上限先兜住。
MAX_TRACKED_KEYS = 10000

#: 每个桶：(环境变量名, 默认上限)。上限 0 表示这个桶不限流。
#:
#: 默认值的取法：按「这个端点一次要花多少服务器资源」排——
#: 下载要落盘（最重）→ 10 次/分钟；解析要出网抓取 → 20 次/分钟；
#: 直链与缩略图是轻量出网 → 30 / 60 次/分钟；登录要算 bcrypt → 10 次/分钟；
#: 注册要写库 → 5 次/分钟。都可用环境变量改，改不动就说明该换部署形态了。
BUCKETS = {
    "/api/parse": ("VIDDIGEST_RATE_LIMIT_PARSE", 20),
    "/api/download": ("VIDDIGEST_RATE_LIMIT_DOWNLOAD", 10),
    "/api/direct-url": ("VIDDIGEST_RATE_LIMIT_DIRECT_URL", 30),
    "/api/proxy/thumbnail": ("VIDDIGEST_RATE_LIMIT_THUMBNAIL", 60),
    "/api/login": ("VIDDIGEST_RATE_LIMIT_LOGIN", 10),
    "/api/register": ("VIDDIGEST_RATE_LIMIT_REGISTER", 5),
}

#: 并发闸门的每个桶：(环境变量名, 默认并发上限)。上限 0 表示这个桶不限并发。
#:
#: 只挂解析与下载两个端点：它们是仅有的两个「一次请求要在服务器这边占资源
#: 占很久」的公开端点（出网抓取、落盘）。直链、缩略图、登录、注册都是秒级
#: 往返，速率闸门对它们已经够了，再挂一道并发闸门只会多一处要维护的口径。
#:
#: 默认 2 的取法：一个来源同时跑两个重活就已经把带宽与 CPU 占满了，第三个
#: 进来只会让三个都变慢。宁可让用户等一下，也不要让整台机器被一个来源拖垮。
#:
#: 桶名沿用「被限的那个端点路径」，与速率闸门同一约定（ADR 0021）。为什么
#: 不写 `parse` 那种短名：额度种类在 `quota_kinds.QUOTA_KINDS` 里只有一处枚举，
#: 别处再写一遍会破坏那条结构守卫。
CONCURRENCY_BUCKETS = {
    "/api/parse": ("VIDDIGEST_CONCURRENCY_PARSE", 2),
    "/api/download": ("VIDDIGEST_CONCURRENCY_DOWNLOAD", 2),
}

#: 并发闸门的 `Retry-After` 固定值（秒）。
#:
#: 为什么是固定值而不是「前一个任务还剩多久」：那一刻算不出来——闸门只知道
#: 有几个在跑，不知道它们什么时候结束。真正的建议在错误文案里（「等前面的
#: 任务结束再试」）。
#:
#: 为什么是 30：占着名额的任务可能跑几分钟（下载要出网 + 落盘）。给 5 秒
#: 的话，客户端按这个头回来重试，多半还是超限，只是白等一轮。这里给的是
#: **保底下限**——真的等待取决于前面的任务什么时候结束，错误文案说的
#: 才是准的。
CONCURRENCY_RETRY_AFTER_SECONDS = 30

#: 429 响应体里的**机器可读**错误码。
#:
#: 为什么要有它：前端过去靠中文文案（「服务器限流」「同时进行的任务太多」）
#: 认出「这是我们自己的闸门，不是视频站在封人」。文案一改前端就跟着坏，
#: 两边分头改就会把自家闸门误报成平台风控——用户会去怪视频站，等错方向。
#: 有了 `code`，协议落在字段上，文案退化成只给人看。
#:
#: 值域纪律：这里的取值不能和别的错误体里的 `code` 撞车（历史记录那边用的是
#: "favorited"，见 `backend/api_history.py`）。撞车的症状是不报错、只是慢慢错，
#: 所以取值列在这一处，改之前先对一眼。
RATE_LIMIT_CODE = "rate_limit"
CONCURRENCY_LIMIT_CODE = "concurrency_limit"


class FixedWindowCounter:
    """固定窗口计数器：同一个键在一个窗口内最多记 limit 次。

    线程安全靠一把锁——FastAPI 的同步依赖跑在线程池里，
    `dict` 的「读-改-写」不是原子的，不加锁会漏计。
    """

    def __init__(self, clock=time.monotonic, max_keys: int = MAX_TRACKED_KEYS):
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        # 键 → [窗口起点, 次数]。用 list 而不是 tuple 是因为要就地改。
        self._entries: dict[str, list[float]] = {}

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        """记一次。

        返回 `(是否放行, 剩余次数, Retry-After 秒数)`。
        """
        now = self._clock()
        with self._lock:
            self._prune(now, window)
            entry = self._entries.get(key)
            if entry is None or now - entry[0] >= window:
                if len(self._entries) >= self._max_keys:
                    self._evict_oldest()
                self._entries[key] = [now, 1]
                return True, max(limit - 1, 0), 0
            entry[1] += 1
            if entry[1] > limit:
                retry_after = max(int(math.ceil(window - (now - entry[0]))), 1)
                return False, 0, retry_after
            return True, limit - entry[1], 0

    def tracked_keys(self) -> int:
        """当前记着多少个来源（测试与巡检用）。"""
        with self._lock:
            return len(self._entries)

    def reset(self) -> None:
        """清空计数（测试用；也留给将来加「运维手动清一次」的入口）。"""
        with self._lock:
            self._entries.clear()

    def _prune(self, now: float, window: int) -> None:
        """删掉窗口已过的条目——不删的话计数表只增不减。"""
        stale = [k for k, v in self._entries.items() if now - v[0] >= window]
        for k in stale:
            del self._entries[k]

    def _evict_oldest(self) -> None:
        """超上限时踢掉窗口起点最早的那一条。

        踢最旧而不是踢新的：踢新的等于「新来的不计数」，
        那等于给攻击者发了一张免死金牌。
        """
        oldest_key = None
        oldest_start = None
        for k, v in self._entries.items():
            if oldest_start is None or v[0] < oldest_start:
                oldest_key = k
                oldest_start = v[0]
        if oldest_key is not None:
            del self._entries[oldest_key]


class ConcurrencyCounter:
    """「此刻还有几个没结束」的计数器。

    与 `FixedWindowCounter` 是两回事：那个记「一段时间内来过几次」，
    窗口一到就归零；这个记**当下**还在跑的数量，所以它的值会升也会降——
    任务结束时必须显式还回来，否则名额会被已经结束的幽灵任务永久占住。

    键是 (来源, 端点桶)：同一个来源在解析与下载上的占用互不相干，
    否则一个慢解析会把下载也堵死。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], int] = {}

    def acquire(self, key: tuple[str, str], limit: int) -> bool:
        """占一个名额；名额满了就一个也不占，返回 False。"""
        with self._lock:
            current = self._entries.get(key, 0)
            if current >= limit:
                return False
            self._entries[key] = current + 1
            return True

    def release(self, key: tuple[str, str]) -> None:
        """还回一个名额。

        多还一次也不会把计数弄成负数：键不存在就当还了 0 个。
        宁可少算也不能让计数变负——负数会让后面的请求永远放行。
        """
        with self._lock:
            current = self._entries.get(key, 0)
            if current <= 1:
                self._entries.pop(key, None)
                return
            self._entries[key] = current - 1

    def reset(self) -> None:
        """清空计数（测试与将来的人工清理都用它）。"""
        with self._lock:
            self._entries.clear()


class ConcurrencySlot:
    """一次并发占用的凭据：调 `release()` 把名额还回去。

    **为什么要多这一层，而不是端点直接调计数器**：名额有两条释放路径
    （端点显式释放、依赖收尾兜底），两条都走这里，所以「还过了没有」
    得记在这个凭据上——多还一次会把别人的名额也减掉，计数一旦偏小，
    上限就形同虚设。

    **为什么要端点显式释放**：FastAPI 的依赖收尾发生在响应发出**之后**
    （`fastapi/routing.py` 里 `await response(...)` 在退出栈之前），
    只靠收尾还名额的话，客户端收到响应后立刻再发一次请求，
    仍会被自己上一个任务挡住。
    """

    def __init__(self, counter: ConcurrencyCounter, key: tuple[str, str] | None = None) -> None:
        self._counter = counter
        # key 为 None = 不限并发，本来就没有名额可还。
        self._key = key
        self._released = False

    def release(self) -> None:
        """把名额还回去。重复调用是空操作。"""
        if self._released:
            return
        self._released = True
        if self._key is not None:
            self._counter.release(self._key)


#: 进程内唯一的计数器。
_counter = FixedWindowCounter()

#: 进程内唯一的并发计数器。
_concurrency_counter = ConcurrencyCounter()


def reset_counters() -> None:
    """清空**所有**计数（速率窗口 + 并发在跑数；测试与将来的人工清理都用它）。

    两个都清：它们是同一层护栏上的两道闸门，「手动清一次」不该只清一道。
    """
    _counter.reset()
    _concurrency_counter.reset()


def use_counter(counter: FixedWindowCounter) -> None:
    """换一个计数器（测试用；也给将来「把计数存到 Redis」的改造留了位置）。"""
    global _counter
    _counter = counter


def tracked_keys() -> int:
    """当前记着多少个来源（测试与巡检用）。"""
    return _counter.tracked_keys()


def _env_int(name: str, default: int) -> int:
    """读一个整数环境变量，取不到或不是数字时用默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _enabled() -> bool:
    """总开关。关掉之后所有桶都不限流（留给本地调试与压测）。"""
    return _env_int("VIDDIGEST_RATE_LIMIT_ENABLED", 1) != 0


def _window_seconds() -> int:
    """窗口长度（秒）。"""
    return max(1, _env_int("VIDDIGEST_RATE_LIMIT_WINDOW", 60))


def _limit_for(bucket: str) -> int:
    """某个桶的上限；0 表示不限流。"""
    env_name, default = BUCKETS[bucket]
    return _env_int(env_name, default)


def _is_trusted_peer(ip_text: str) -> bool:
    """直接对端是不是「可信代理」。

    回环与私网段算可信：本仓的 nginx 与后端在同一台机器/同一张 docker
    网络上，公网直连时对端是公网地址，不在可信段里——**不能**把
    `X-Forwarded-For` 无条件采信，否则限流形同虚设。

    为什么逐条列出网段，而不是用 `ipaddress` 的 `is_private`：
    Python 的 `is_private` 把**文档保留段**（192.0.2.0/24、198.51.100.0/24、
    203.0.113.0/24）也算成私有——那是给写文档用的地址段，真实部署里
    永远不会出现对端在那儿。用一个「看起来像私网、其实不是」的判断，
    等于把判据寄托在库实现的细节上。
    """
    try:
        ip = ip_address(ip_text)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_NETWORKS)


def _peer_ip(request: Request) -> str:
    """直接对端地址。"""
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


def client_identity(request: Request) -> str:
    """这个请求算「谁」发出来的。

    直接对端可信（回环/私网，前面确实有反向代理）且请求带
    `X-Forwarded-For` 时，身份取这个头的**最后一段**。

    **为什么是末位而不是首位**：标准的边缘代理（nginx）用的是**追加**
    语义（`$proxy_add_x_forwarded_for`）——客户端带来的值排在前面，
    代理自己看到的直接对端地址 `$remote_addr` 追加在**最后**。于是末位
    才是权威地址，客户端自报的那些段（可以随便伪造）失效。
    取首位等于让攻击者自己挑限流桶的键：改一个头就换一个桶，
    限流器就成了摆设（工单 #94、ADR 0022）。

    配套的另一半在边缘代理那一侧：`X-Forwarded-For` 必须被**覆盖**
    （`proxy_set_header X-Forwarded-For $remote_addr;`），不能追加。
    两层都做，后端这边才拿得到「权威的那一段」。

    没有这个头时用直接对端地址；直接对端是公网时**完全忽略**这个头——
    宁可把同一 NAT 后面的人算成一个人，也不能让任何人改一个头就绕开限流。
    """
    peer = _peer_ip(request)
    if _is_trusted_peer(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            segments = [part.strip() for part in forwarded.split(",") if part.strip()]
            candidate = segments[-1] if segments else ""
            try:
                ip_address(candidate)
            except ValueError:
                candidate = ""
            if candidate:
                return f"ip:{candidate}"
    return f"ip:{peer}"


def enforce(request: Request, bucket: str) -> None:
    """超了就抛 429；放行就什么都不做。

    **计数键是「来源 + 桶」**：同一个来源在每个端点各有一个计数器，
    桶与桶互不影响（工单 #102）。键里少了桶名的话，六个桶会共用一个
    计数器——判红时用的是当前桶的上限，累加的却是所有桶的总次数。
    """
    if not _enabled():
        return
    limit = _limit_for(bucket)
    if limit <= 0:
        return
    allowed, _remaining, retry_after = _counter.hit(
        f"{client_identity(request)}:{bucket}", limit, _window_seconds()
    )
    if allowed:
        return
    raise HTTPException(
        status_code=429,
        detail={
            "success": False,
            # 机器可读的错误码：前端按它分类，不再认中文文案。
            "code": RATE_LIMIT_CODE,
            "error": f"请求过于频繁：服务器限流，请 {retry_after} 秒后再试",
        },
        headers={"Retry-After": str(retry_after)},
    )


def rate_limit(bucket: str):
    """返回一个 FastAPI 依赖：挂在端点上，超限就 429。

    桶名写错要在**装配时**就炸（写错桶名等于端点没挂上限，还不报错）。
    """
    if bucket not in BUCKETS:
        raise KeyError(f"未知的限流桶: {bucket}")

    def dependency(request: Request) -> None:
        enforce(request, bucket)

    return dependency


def _concurrency_limit_for(bucket: str) -> int:
    """某个桶的并发上限；0 表示不限并发。"""
    env_name, default = CONCURRENCY_BUCKETS[bucket]
    return _env_int(env_name, default)


def concurrency_limit(bucket: str):
    """返回一个 FastAPI 依赖：同一来源同时在跑的任务超过上限就 429。

    与速率闸门是**两件事**，谁也替代不了谁：

    - 速率闸门管「一段时间内来了多少次」——超了说明**来得频繁**；
    - 并发闸门管「此刻有几个还在跑」——超了说明**同时在跑太多**。

    同一个来源一分钟只发一次、但那个任务跑十分钟，速率闸门一次都不会拦；
    反过来一秒发二十次、但每个都秒回，速率闸门会拦、并发闸门不会。
    两道闸门的消息也不一样：前端得分开告诉用户「等一分钟」还是
    「等前一个任务结束」（ADR 0021）。

    桶名写错要在**装配时**就炸（写错桶名等于端点没挂上限，还不报错）。

    **释放为什么由端点来做**：FastAPI 的依赖收尾在响应发出**之后**
    （`fastapi/routing.py` 里 `await response(...)` 排在退出栈之前），
    只靠收尾还名额的话，客户端收到响应后立刻再发一次请求，
    仍会被自己上一个任务挡住。所以这里 yield 出去一个凭据，
    端点在 `finally` 里显式释放；这里的收尾只兜底「端点根本没跑」
    的那种情况（请求体校验不通过时端点根本不会被调用）。
    """
    if bucket not in CONCURRENCY_BUCKETS:
        raise KeyError(f"未知的并发桶: {bucket}")

    async def dependency(request: Request):
        limit = _concurrency_limit_for(bucket)
        if limit <= 0:
            yield ConcurrencySlot(_concurrency_counter)
            return
        key = (client_identity(request), bucket)
        if not _concurrency_counter.acquire(key, limit):
            raise HTTPException(
                status_code=429,
                detail={
                    "success": False,
                    # 机器可读的错误码：前端按它分类（`frontend/src/lib/errors.ts`
                    # 的 `looksLikeServerConcurrencyLimit`）。下面的文案只给人看，
                    # 改文案不会再让前端把自家闸门误报成「平台风控」。
                    "code": CONCURRENCY_LIMIT_CODE,
                    "error": (
                        f"同时进行的任务太多：这个来源同时在跑的任务已超过 {limit} 个，"
                        "请等前面的任务结束再试"
                    ),
                },
                headers={"Retry-After": str(CONCURRENCY_RETRY_AFTER_SECONDS)},
            )
        slot = ConcurrencySlot(_concurrency_counter, key)
        try:
            yield slot
        finally:
            # 端点抛异常时也得还回去——漏了这一步，名额会被幽灵任务占死，
            # 那个来源之后的所有请求都会被拒。
            slot.release()

    return dependency
