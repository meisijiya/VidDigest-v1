"""公开端点的并发闸门（工单 #89）。

## 为什么要有这层

速率闸门（工单 #85）管的是「一段时间内来了多少次」——一个来源一分钟只发
一次、但那个任务跑十分钟，速率闸门一次都不会拦。反过来一秒发二十次、但
每个都秒回，速率闸门会拦、并发闸门不会。这两件事谁也替代不了谁，所以
速率与并发是**两道闸门**，不是一个阈值（ADR 0021）。

## 判据读的是外部可观察的东西

- 超并发上限的响应码是 **429**；
- 响应带 `Retry-After` 头；
- 响应体里的 `detail.error` 说清是**同时在跑太多**（不是速率闸门那句
  「来得频繁」）；
- 响应体里的 `detail.code` 是**机器可读**的错误码——前端按它分类，
  不再靠中文文案认自家闸门；
- 两个端点桶互不牵连（解析占满了不代表下载也满）；
- 任务结束（含抛异常）后名额**立刻**还回来；
- 超限的请求**立刻**被拒，不排队等；
- 上限可用环境变量调，写 0 就是不限并发；
- 计数是**进程内**的——换个进程就重新算。

## 三个容易做错的地方，这里都有用例钉着

1. **名额必须还回来**。漏还的后果不是「偶尔多拒一次」，而是那个来源之后
   的所有请求都被拒——一次崩溃就把自己锁在门外了。用例 6/7 钉住它。
2. **不能排队**。排队的话用户等的是一个他不知道还要多久的队列，服务器端
   的内存还被请求堆着——那正是并发闸门要防的事。用例 9 钉住它。
3. **两道闸门对「谁发来的」必须是同一个判断**。判断口径分了叉的话，用户
   会看到「速率说没问题、并发说太多了」这种自相矛盾的拒绝理由。用例
   15/16/17 钉住它。

## 外网绝不碰

所有会真出网的调用都被换成必定失败的替身（`DEAD_URL` 指向 127.0.0.1:9，
连接被拒是立刻发生的），断言只看 HTTP 层。
"""
import ast
import itertools
import subprocess
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import main as main_module  # noqa: E402
import ratelimit  # noqa: E402
from seams import make_client  # noqa: E402

#: 一个必定连不上的地址：连接被拒是**立刻**发生的，不会去碰外网。
DEAD_URL = "http://127.0.0.1:9/nothing"

#: 公网直接对端（203.0.113.0/24 是文档保留段，真实部署里不会真从这儿来）。
#: 对端在公网 ⇒ `X-Forwarded-For` **不被采信**。
PUBLIC_PEER = ("203.0.113.5", 50000)

#: 回环直接对端：说明这台机器前面确实有个反向代理 ⇒ `X-Forwarded-For`
#: 会被采信。
PROXY_PEER = ("127.0.0.1", 50000)

#: 经代理时用的来源地址（文档保留段）。
PROXY_CLIENT_IP = "198.51.100.7"

#: 另一个经代理的来源地址，用来造「换了一个人」。
OTHER_PROXY_CLIENT_IP = "198.51.100.9"


# ── 夹具 ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def concurrency_on(monkeypatch):
    """每个用例都从「并发闸门开着、速率闸门关着、计数清零」开始。

    为什么关掉速率闸门：两道闸门挂在同一个端点上，速率闸门先判。开着
    它的话，「并发超限」这条判据会被速率那条先拦掉——测的就不是并发闸门
    了。两道闸门各自的判据另有专门的用例。
    """
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_ENABLED", "0")
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "1")
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_DOWNLOAD", "1")
    ratelimit.reset_counters()
    yield
    ratelimit.reset_counters()


@pytest.fixture()
def client():
    return _client()


def _client(peer=PUBLIC_PEER):
    """一个「直接对端」可指定的测试客户端。

    为什么要指定对端：并发闸门按来源分桶，「谁发来的」算得对不对，
    取决于直接对端是不是可信代理。不指定的话测试客户端的默认对端是
    `"testclient"`——不是合法 IP，判断逻辑会走另一条分支。

    为什么关掉 raise_server_exceptions：服务端炸了应该表现为一个 500
    响应，而不是把异常从测试里抛出来。判据看的是 HTTP 码，炸了就是
    码不对——这样「实现坏掉」和「判据不成立」在报告里长得一样。
    """
    return make_client(main_module.app, client=peer, raise_server_exceptions=False)


# ── 造出「真的还在跑」的请求 ──────────────────────────────

class _InFlight:
    """把处理函数卡住，好让请求真的处在「还在跑」的状态。

    为什么非要真的卡住：并发闸门数的是「此刻还有几个没结束」。模拟一个
    计数是没意义的——那样测的是模拟器，不是闸门。
    """

    def __init__(self, target: int = 1) -> None:
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._released = threading.Event()
        self._target = target
        self._count = 0

    def _enter(self) -> None:
        with self._lock:
            self._count += 1
            if self._count >= self._target:
                self._arrived.set()
        # 超时是兜底：万一某个用例忘了放行，也不能让整个套件挂死。
        if not self._released.wait(30):
            raise RuntimeError("probe: 卡住的处理函数被强制放行")

    def install_parse(self, monkeypatch) -> "_InFlight":
        def _parse(url):
            self._enter()
            return {"title": "probe"}
        monkeypatch.setattr(main_module.downloader, "parse_video", _parse)
        return self

    def install_download(self, monkeypatch, filepath: str) -> "_InFlight":
        def _download(url, format_id):
            self._enter()
            return {"filepath": filepath, "filename": "probe.mp4"}
        monkeypatch.setattr(main_module.downloader, "download_video", _download)
        return self

    def wait_arrived(self, timeout: float = 15.0) -> None:
        assert self._arrived.wait(timeout), "处理函数根本没被调到，前置条件不成立"

    def arrivals(self, timeout: float = 5.0) -> bool:
        """等目标数量的请求进到处理函数，返回等没等到。

        为什么要一个「不抛异常」的版本：有的用例要区分「没等到」与「到了但
        状态不对」——前者是前置条件不成立，后者是实现坏了。
        """
        return self._arrived.wait(timeout)

    def release(self) -> None:
        self._released.set()


def _post_async(client, path, body, results, headers=None):
    """在后台线程里发一个请求，返回那个线程。

    为什么放后台：请求会卡在处理函数里直到放行，放前面就等于把主线程
    堵死。
    """

    def _run():
        try:
            results.append(client.post(path, json=body, headers=headers))
        except Exception as exc:  # pragma: no cover - 失败时让下面的断言说话
            results.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _probe_while_in_flight(inflight, client, path, body, headers=None):
    """在一个请求还卡在处理函数里的时候发第二个请求，返回它的响应。

    第二个请求要么被闸门拒掉（很快回来），要么也进到处理函数里卡住。
    所以拿完结果必须放行，否则整个套件会挂住。
    """
    results = []
    thread = _post_async(client, path, body, results, headers=headers)
    thread.join(timeout=10)
    inflight.release()
    thread.join(timeout=30)
    assert results, "第二个请求既没返回也没进到处理函数 —— 前置条件不成立"
    response = results[0]
    if isinstance(response, Exception):
        raise response
    return response


def _parse_body():
    return {"url": DEAD_URL}


def _download_body():
    return {"url": DEAD_URL, "format_id": "1"}


# ── 用户故事 1/2/5：超并发上限要被拒 ──────────────────────

def test_the_parse_endpoint_rejects_over_the_concurrency_limit(client, monkeypatch):
    """解析端点上，同时在跑的任务超了就拒。"""
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    first.join(timeout=30)

    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["success"] is False
    assert detail.get("code") == "concurrency_limit", (
        "429 的错误体里没有机器可读的 code —— 前端只能去认中文文案，"
        "两边分头改就会把自家并发闸门误报成平台风控"
    )
    assert "retry-after" in {k.lower() for k in second.headers}
    assert int(second.headers["retry-after"]) > 0


def test_the_download_endpoint_rejects_over_the_concurrency_limit(client, monkeypatch, tmp_path):
    """下载端点也有并发上限——它比解析更重（出网 + 落盘）。"""
    video = tmp_path / "probe.mp4"
    video.write_bytes(b"probe")
    inflight = _InFlight().install_download(monkeypatch, str(video))
    first = _post_async(client, "/api/download", _download_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(inflight, client, "/api/download", _download_body())
    first.join(timeout=30)

    assert second.status_code == 429
    assert second.json()["detail"]["success"] is False


def test_the_rejection_message_says_too_many_at_the_same_time(client, monkeypatch):
    """拒的话要说清是「同时在跑太多」，不是「来得频繁」。

    为什么：两件事的处置建议完全不一样——速率那道等一分钟就行，这道等
    前面的任务结束就行。混成一句话，用户就会白等一分钟。
    """
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    first.join(timeout=30)

    error = second.json()["detail"]["error"]
    assert "同时" in error
    # 速率闸门那两句不能出现——出现了用户就分不清该等一分钟还是等任务结束
    assert "服务器限流" not in error
    assert "请求过于频繁" not in error


def _seconds_in(text):
    """文案里「N 秒」这种写法的 N（只认紧挨着「秒」的数字）。"""
    found = []
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
            continue
        if char == "秒" and digits:
            found.append(int(digits))
        digits = ""
    return found


def test_the_retry_after_header_and_the_wording_say_the_same_thing(
    client, monkeypatch
):
    """429 的 Retry-After 头、文案与常量说的是同一件事。

    为什么：头里的秒数是客户端**机器**读的那一份，文案是人读的那一份。
    两边对不上，客户端按头等几秒就回来重试（多半还是超限），用户按文案
    等前面的任务结束。秒数本身也要是个明确的数：改它的人得知道自己在
    改什么。
    """
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    first.join(timeout=30)

    assert second.status_code == 429
    header = int(second.headers["retry-after"])
    assert header == ratelimit.CONCURRENCY_RETRY_AFTER_SECONDS, (
        "响应头里的秒数与常量对不上 —— 头不能另写一个数"
    )
    assert header == 30, (
        f"并发闸门的 Retry-After 变成了 {header} 秒 —— 那个值是个决定，"
        "改它之前先想清楚占着名额的任务要跑多久"
    )

    error = second.json()["detail"]["error"]
    assert "等前面的任务结束" in error, (
        "文案没说清要等前面的任务结束 —— 用户不知道该等多久"
    )
    assert all(n == header for n in _seconds_in(error)), (
        f"文案里的秒数与 Retry-After 头对不上（头是 {header}）"
    )


# ── 用户故事 3：两个桶互不牵连 ────────────────────────────
def test_the_parse_bucket_does_not_block_the_download_bucket(client, monkeypatch, tmp_path):
    """解析占满了名额，下载照跑。

    为什么：两道闸门各按「来源 × 端点桶」计数。一个慢解析把下载也堵死，
    等于把重活全赶到同一条路上。
    """
    video = tmp_path / "probe.mp4"
    video.write_bytes(b"probe")
    monkeypatch.setattr(
        main_module.downloader,
        "download_video",
        lambda url, format_id: {"filepath": str(video), "filename": "probe.mp4"},
    )
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    download = _probe_while_in_flight(inflight, client, "/api/download", _download_body())
    first.join(timeout=30)

    assert download.status_code != 429


# ── 用户故事 6/7：名额必须还回来 ──────────────────────────

def test_a_failed_task_does_not_hold_a_slot_forever(client, monkeypatch):
    """处理函数抛异常时，名额必须还回去。

    漏还的后果不是「偶尔多拒一次」，而是那个来源之后的所有请求都被拒——
    一次崩溃就把自己锁在门外了。
    """

    def _boom(url):
        raise RuntimeError("probe")

    monkeypatch.setattr(main_module.downloader, "parse_video", _boom)

    first = client.post("/api/parse", json=_parse_body())
    assert first.status_code == 400

    second = client.post("/api/parse", json=_parse_body())
    assert second.status_code != 429


def test_the_slot_is_released_as_soon_as_the_task_finishes(client, monkeypatch):
    """任务正常结束，名额立刻回到池子里。"""
    monkeypatch.setattr(main_module.downloader, "parse_video", lambda url: {"title": "probe"})

    first = client.post("/api/parse", json=_parse_body())
    assert first.status_code == 200

    second = client.post("/api/parse", json=_parse_body())
    assert second.status_code != 429


class _ProbeDuringResponse:
    """在响应体开始发出的那一刻，从后台线程发第二个请求。

    为什么挑这一刻：名额什么时候还回来，就是这件事的全部。响应还没发完
    就还回来的话，客户端在下载途中再点一次不会被自己上一个任务挡住；
    等响应发完才还，用户在收到响应之前那段时间里再点一次就会被 429。

    为什么在包装器里等第二个请求回来：不等的话，两个请求谁先谁后就是
    运气——运气好的时候，第二个请求赶在第一个任务收尾之后就到了，判据
    就测不出「名额在响应发出之前有没有还回来」。
    """

    def __init__(self, app, fire):
        self.app = app
        self._fire = fire
        self.fired = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.body" and not self.fired:
                self.fired = True
                self._fire().join(timeout=30)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def test_the_slot_is_released_before_the_response_body_starts(
    client, monkeypatch, tmp_path
):
    """名额在响应体开始发出之前就还回来了。

    为什么这条要紧：产物用完即删让下载端点的响应变长了——文件是从响应
    里流出去的。客户端拿到响应之前那段时间，这个任务其实已经不占资源
    了。这时候还占着名额，用户在下载途中再点一次就会被自己上一个任务
    挡住（429）。
    """
    counter = itertools.count()

    def fake_download(url, format_id):
        # 每次下载给一个自己的文件：第二个请求不许动第一个请求正在发的那个。
        path = tmp_path / f"probe-{next(counter)}.mp4"
        path.write_bytes(b"probe-bytes")
        return {"filepath": str(path), "filename": path.name}

    monkeypatch.setattr(main_module.downloader, "download_video", fake_download)

    results = []
    probe = _ProbeDuringResponse(
        main_module.app,
        lambda: _post_async(client, "/api/download", _download_body(), results),
    )
    watching = make_client(
        probe, client=PUBLIC_PEER, raise_server_exceptions=False
    )

    first = watching.post("/api/download", json=_download_body())

    assert first.status_code == 200
    assert probe.fired, "第二个请求根本没发出去 —— 前置条件不成立"
    assert results, "第二个请求没回来 —— 前置条件不成立"
    assert results[0].status_code != 429, (
        "响应体都开始发了，名额还没还回来 —— 客户端在这个窗口里再点一次"
        "会被自己上一个任务挡住"
    )

    # 响应发完之后，名额当然也得是还回来的。
    after = client.post("/api/download", json=_download_body())
    assert after.status_code != 429


# ── 用户故事 8：两道闸门各管各的 ──────────────────────────

def test_the_rate_gate_and_the_concurrency_gate_are_independent(client, monkeypatch):
    """速率闸门管「来得频不频繁」，并发闸门管「同时在跑太多」。

    两个方向都要验：并发闸门拒的时候，那句话得是并发那条；速率闸门先到
    的时候，那句话得是速率那条。
    """
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_WINDOW", "60")
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_PARSE", "100")
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "1")

    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    first.join(timeout=30)
    assert second.status_code == 429
    assert "同时" in second.json()["detail"]["error"]

    # 反过来：速率闸门先到，那句话是速率的
    ratelimit.reset_counters()
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_PARSE", "1")
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "99")
    monkeypatch.setattr(main_module.downloader, "parse_video", lambda url: {"title": "probe"})

    client.post("/api/parse", json=_parse_body())
    limited = client.post("/api/parse", json=_parse_body())
    assert limited.status_code == 429
    assert "服务器限流" in limited.json()["detail"]["error"]


# ── 用户故事 9：不排队 ────────────────────────────────────

def test_an_over_limit_request_is_not_queued(client, monkeypatch):
    """超并发上限的请求要立刻被拒，不许排队等。

    排队的代价：用户等的是一个他不知道还要多久的队列，服务器端的内存
    还被请求堆着——那正是并发闸门要防的事。
    """
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    results = []
    second = _post_async(client, "/api/parse", _parse_body(), results)
    second.join(timeout=10)
    still_waiting = second.is_alive()

    inflight.release()
    first.join(timeout=30)
    second.join(timeout=30)

    assert not still_waiting, "超限的请求被排队了 —— 它一直不返回，直到前面的任务结束"
    assert results[0].status_code == 429


# ── 用户故事 11/12/默认上限 ───────────────────────────────

def test_the_limit_is_configurable_by_env(client, monkeypatch):
    """上限可以用环境变量调。

    为什么钉住「三个都能进来」：只断言「第四个被拒」的话，上限写死成 2
    也能过——那这条用例就钉不住「环境变量到底生效了没有」。
    """
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "3")
    inflight = _InFlight(target=3).install_parse(monkeypatch)
    threads = [_post_async(client, "/api/parse", _parse_body(), []) for _ in range(3)]
    reached = inflight.arrivals(timeout=5)
    extra = (
        _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
        if reached
        else None
    )
    inflight.release()
    for thread in threads:
        thread.join(timeout=30)

    assert reached, "上限调成 3 之后应该有三个请求同时在跑"
    assert extra is not None and extra.status_code == 429


def test_zero_turns_the_gate_off(client, monkeypatch):
    """上限写 0 就是不限并发。"""
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "0")
    inflight = _InFlight(target=3).install_parse(monkeypatch)
    threads = [_post_async(client, "/api/parse", _parse_body(), []) for _ in range(3)]
    inflight.wait_arrived()

    extra = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    for thread in threads:
        thread.join(timeout=30)

    assert extra.status_code != 429


def test_the_default_limit_is_two(client, monkeypatch):
    """不配环境变量时，每个来源每个桶同时只让跑 2 个。

    为什么钉死 2：默认值得有一个明确的数，否则「默认到底几个」就成了
    没人知道的事——改代码的人无从判断自己动没动到它。
    """
    monkeypatch.delenv("VIDDIGEST_CONCURRENCY_PARSE", raising=False)
    inflight = _InFlight(target=2).install_parse(monkeypatch)
    threads = [_post_async(client, "/api/parse", _parse_body(), []) for _ in range(2)]
    inflight.wait_arrived()

    extra = _probe_while_in_flight(inflight, client, "/api/parse", _parse_body())
    for thread in threads:
        thread.join(timeout=30)

    assert extra.status_code == 429


# ── 用户故事 14：计数是进程内的 ───────────────────────────

def test_the_count_does_not_survive_a_process_restart(client, monkeypatch, tmp_path):
    """另一个进程（= 重启后的世界）看不到本进程占着的名额。

    为什么用子进程而不是 mock 计数器：并发计数是**进程内**的，这是它
    最重要的边界。用子进程量，才是真的量到了「另一个进程」。
    """
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(client, "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    probe = tmp_path / "probe_restart.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from fastapi.testclient import TestClient\n"
        "import main\n"
        f"client = TestClient(main.app, client={PUBLIC_PEER!r})\n"
        "resp = client.post('/api/parse', json={'url': 'http://127.0.0.1:9/nothing'})\n"
        "print('STATUS', resp.status_code)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    inflight.release()
    first.join(timeout=30)

    assert proc.returncode == 0, f"探针子进程挂了：{proc.stderr[-2000:]}"
    assert "STATUS" in proc.stdout, f"探针子进程没跑到判据那一步：{proc.stdout[-2000:]}"
    assert "STATUS 429" not in proc.stdout, (
        "另一个进程竟然看到了本进程占着的名额 —— 并发计数不是进程内的"
    )


# ── 用户故事 15/16/17：两道闸门对「谁发来的」判断一致 ─────

#: (用例名, 第一个请求的对端, 第一个请求的 XFF, 第二个请求的对端, 第二个请求的 XFF, 是不是同一个人)
SOURCE_CASES = [
    ("公网直连：改 XFF 也还是同一个人", PUBLIC_PEER, PROXY_CLIENT_IP, PUBLIC_PEER, OTHER_PROXY_CLIENT_IP, True),
    ("经代理：XFF 相同就是同一个人", PROXY_PEER, PROXY_CLIENT_IP, PROXY_PEER, PROXY_CLIENT_IP, True),
    ("经代理：XFF 不同就是不同的人", PROXY_PEER, PROXY_CLIENT_IP, PROXY_PEER, OTHER_PROXY_CLIENT_IP, False),
]


@pytest.mark.parametrize("case", SOURCE_CASES, ids=[c[0] for c in SOURCE_CASES])
def test_the_two_gates_agree_on_who_the_caller_is(monkeypatch, case):
    """两道闸门对「谁发来的」必须是同一个判断。

    为什么这条要紧：两道闸门数的是同一件事（同一个来源）。判断口径分了
    叉的话，用户会看到「速率说没问题、并发说太多了」这种自相矛盾的拒绝
    理由。
    """
    _name, peer1, xff1, peer2, xff2, same_person = case

    # ── 并发闸门的判断 ──
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(
        _client(peer1), "/api/parse", _parse_body(), [],
        headers={"X-Forwarded-For": xff1},
    )
    inflight.wait_arrived()

    second = _probe_while_in_flight(
        inflight, _client(peer2), "/api/parse", _parse_body(),
        headers={"X-Forwarded-For": xff2},
    )
    first.join(timeout=30)
    concurrency_rejected = second.status_code == 429

    # ── 速率闸门的判断：同样的两次请求、同样的来源 ──
    ratelimit.reset_counters()
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_WINDOW", "60")
    monkeypatch.setenv("VIDDIGEST_RATE_LIMIT_PARSE", "1")
    monkeypatch.setenv("VIDDIGEST_CONCURRENCY_PARSE", "99")
    monkeypatch.setattr(main_module.downloader, "parse_video", lambda url: {"title": "probe"})

    rate_first = _client(peer1).post(
        "/api/parse", json=_parse_body(), headers={"X-Forwarded-For": xff1}
    )
    rate_second = _client(peer2).post(
        "/api/parse", json=_parse_body(), headers={"X-Forwarded-For": xff2}
    )
    rate_rejected = rate_second.status_code == 429

    assert rate_first.status_code != 429
    assert rate_rejected is same_person, "速率闸门对「谁发来的」判断错了"
    assert concurrency_rejected is same_person, "并发闸门对「谁发来的」判断错了"
    assert concurrency_rejected == rate_rejected, "两道闸门对同一个来源的判断分叉了"


def test_a_direct_caller_and_the_same_caller_behind_a_proxy_are_the_same_source(monkeypatch):
    """同一个 IP，直连来和经代理来，必须算同一个人。

    为什么这条要紧：部署形态有直连和挂 nginx 两种。识别规则不一样的话，
    换个部署方式就等于换了一批人——护栏在一种部署下管用、另一种下形同
    虚设。
    """
    inflight = _InFlight().install_parse(monkeypatch)
    first = _post_async(_client(PUBLIC_PEER), "/api/parse", _parse_body(), [])
    inflight.wait_arrived()

    second = _probe_while_in_flight(
        inflight, _client(PROXY_PEER), "/api/parse", _parse_body(),
        headers={"X-Forwarded-For": PUBLIC_PEER[0]},
    )
    first.join(timeout=30)

    assert second.status_code == 429


# ── 用户故事 18/19/20/21/22：结构性约束 ───────────────────

def _module_tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _concurrency_buckets_in(path: Path):
    """一个文件里被挂上并发闸门的端点桶。"""
    found = set()
    for node in ast.walk(_module_tree(path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "concurrency_limit"
        ):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


def test_the_concurrency_gate_shares_the_module_with_the_rate_gate():
    """两道闸门必须住在同一个模块里。

    为什么：它们数的是同一件事（同一个来源在同一个端点上的占用）。拆成
    两个模块就迟早会出现两套「怎么算同一个人」——那时候用户会看到互相
    矛盾的拒绝理由。
    """
    tree = _module_tree(BACKEND / "ratelimit.py")
    functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "rate_limit" in functions
    assert "concurrency_limit" in functions
    assert {"FixedWindowCounter", "ConcurrencyCounter"} <= classes

    imported = set()
    for node in ast.walk(_module_tree(BACKEND / "main.py")):
        if isinstance(node, ast.ImportFrom) and node.module == "ratelimit":
            imported.update(alias.name for alias in node.names)
    assert {"rate_limit", "concurrency_limit"} <= imported, (
        "两道闸门不是从同一个模块引进来的"
    )


def test_an_unknown_bucket_blows_up_at_assembly_time():
    """桶名写错要在装配时就炸。

    写错桶名等于端点没挂上限，还不报错——那种错要等到被攻击时才暴露。
    """
    with pytest.raises(KeyError):
        ratelimit.concurrency_limit("/api/does-not-exist")


def test_the_concurrency_buckets_are_named_after_the_endpoint_paths():
    """桶名沿用「被限的那个端点路径」。

    为什么：不写 `parse` 那种短名——额度种类在 `quota_kinds.QUOTA_KINDS`
    里只有一处枚举，别处再写一遍会破坏那条结构守卫。
    """
    import quota_kinds

    for bucket in ratelimit.CONCURRENCY_BUCKETS:
        assert bucket.startswith("/api/"), f"桶名 {bucket} 不是端点路径"
    assert not (set(ratelimit.CONCURRENCY_BUCKETS) & set(quota_kinds.QUOTA_KINDS))


def test_the_concurrency_gate_adds_no_new_dependency():
    """并发闸门不许引入新依赖。

    为什么：这层护栏跑在所有公开端点的最前面，它自己挂了就等于给整个
    后端的启动加了一条新的失败可能。标准库够用。
    """
    modules = set()
    for node in ast.walk(_module_tree(BACKEND / "ratelimit.py")):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert modules <= {"math", "os", "threading", "time", "ipaddress", "fastapi"}, (
        f"引入了新依赖：{sorted(modules)}"
    )


def test_only_the_two_heavy_endpoints_carry_the_concurrency_gate():
    """并发闸门只挂在解析与下载上。

    为什么：其余四个公开端点（直链、缩略图、登录、注册）都是秒级往返，
    挂上只会多一处要维护的口径。

    要给第三个端点挂闸门，是一次有意的决定——那时这条用例要一起改，
    免得它变成一句没人看懂的「不许变」。
    """
    used = _concurrency_buckets_in(BACKEND / "main.py") | _concurrency_buckets_in(
        BACKEND / "api_auth.py"
    )
    assert used == {"/api/parse", "/api/download"}


# ── 用户故事 27：变异装置得进 CI 矩阵 ─────────────────────

def test_the_mutation_script_is_in_the_ci_matrix():
    """变异装置必须进 CI 矩阵。

    为什么：变异装置不跑在 init.sh 里（init.sh 只跑那 12 件「真会失败」的
    事），它只跑在 CI 的矩阵里。忘了加进矩阵，这个装置就永远不会执行，
    而它绿着不绿着都没人知道。
    """
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "tests/mutation_concurrency_limit.py" in ci, (
        "变异脚本没进 CI 矩阵 —— 它不会被执行"
    )
