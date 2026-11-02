r"""SSE 事件名的前后端契约守卫（工单 #39）。

背景
----
后端的事件帧有两类来源：`ServerSentEvent(event=...)` 直接构造（其余事件），
以及 `sse_events.error_event(...)` 这个唯一构造点（`error`，ADR 0034）；
前端
`frontend/src/api/summarize.ts` 有两张路由表（`SUMMARY_ROUTES` /
`CHAT_ROUTES`）加一个 `streamSse` 里的 `done` 独立分支。**两边没有任何
机器可读的契约清单**，改一边忘了另一边没有任何东西会响。

失败形状是**静默丢弃**，这是本守卫存在的唯一理由：

    // frontend/src/api/summarize.ts:164 与 :192
    route: (event, data) => callbacks[SUMMARY_ROUTES[event]]?.(data)

`?.` 是**可选调用**：`SUMMARY_ROUTES[event]` 对未知事件名求值得到
`undefined`，`undefined?.(data)` 短路成 `undefined`——不抛异常、不进
catch、不写日志、UI 一点变化都没有。所以「后端加了事件忘了加路由」的
症状是**界面上的那个东西不动**，而前端日志里什么都没有，因为它压根没报错。
改名更隐蔽：diff 里只有「新增」没有「删除」。

⚠️ 本守卫**不改**那个可选调用。改成必调会让「后端先发、前端后加」的
滚动发布顺序直接崩（每一版发出去的事件，新版前端认得、旧版不认得）。
「未知事件该抛还是该记日志」是产品决定，属另一张单。

判据
----
按**端点**而不是按全局并集断言。实测两端发的事件**不是同一组**：

    summarize_video   done error mindmap quota subtitle summary tags   （7）
    chat_with_video   answer done error quota                          （4）
    _replay_events    done mindmap ownership quota subtitle summary tags（7）

在 `error` 收口到构造点之前，它由 `summarize_video` 里的闭包 `fail()` 发出，
扫描器把一个 `error` 归到 `fail` 名下；现在归到发起它的那个端点函数。

只比全局并集会漏掉「给追问端点发了一个 summary 事件」这类错误——它在
全局并集里完全合法，只在**逐端点**比对下才暴露。

四条：
1. `summarize_video` 发的事件 ⊆ `SUMMARY_ROUTES` ∪ {done}
2. `chat_with_video` 发的事件 ⊆ `CHAT_ROUTES` ∪ {done}
3. `_replay_events` 发的事件 ⊆ `SUMMARY_ROUTES` ∪ {done}
   （社区回放给前端的是同一个 `summarizeVideo` 流，前端不复用另一张表）
4. `done` **不在**任何路由表里，且必须由 `streamSse` 的独立分支接住

为什么 `done` 要单独登记而不是混进事件名集合
----------------------------------------
它在两端都不是同一类东西：后端是 `raw_data="[DONE]"` 的终止事件，
前端是 `streamSse:87` 的 `if (ev.event === 'done') finish()` 独立分支，
**不进路由表**。混进去会让「done 属于哪张表」这个问题没有答案，而
`createSseParser:36` 又会把**任何** `data: [DONE]` 映射成 done
（与 `event:` 行无关）——于是「后端发了个名字叫 done 的事件」和
「后端发了 [DONE]」在前端**完全同形**。这两条路径必须都被守住。

为什么用 AST 读后端，而不是文本扫
------------------------------
`ServerSentEvent(event=...)` 是关键字参数，文本扫会漏掉这些形状：
拼错的 `event =`（带空格）、变量传入的 `event=SOME_CONST`、以及
`ServerSentEvent` 被 import 别名后的调用。更要紧的是：**文本扫分不出
哪个事件属于哪个端点**，而判据 1/2/3 正是逐端点的。
（第一版探针扫的是 f-string 形态的 `event: x`，后端 0 命中——
**「0 命中」与「文件里没这段代码」在输出上完全一样**，先证伪工具再信结论。）

`error` 事件的构造点（ADR 0034）
------------------------------
`error` 不再手写 `ServerSentEvent`，而是 `sse_events.error_event(...)`。
扫描器因此认两种形状：`ServerSentEvent(event=<字面量>)`，以及模块限定的
`.error_event(...)` 调用（后者恒指 `error`）。只认前者会让扫描结果变成
「后端从不发 error」——那时前端那条 error 路由会被判成死路由，
守卫转而指着正确的代码说它错。
"""
import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
API_SUMMARIZE = BACKEND / "api_summarize.py"
FRONTEND_API = BACKEND.parent / "frontend" / "src" / "api" / "summarize.ts"

#: 后端逐端点该发的事件集合。**故意写死在这里**，不由扫描结果生成——
#: 让它从扫描结果派生的话，「后端发了什么」就永远等于真值，判据恒为真。
#: 多写一份的代价是改事件时要改两处，收益是漏改会红。
EXPECTED = {
    "summarize_video": frozenset({
        "done", "error", "mindmap", "quota", "subtitle", "summary", "tags"}),
    "chat_with_video": frozenset({"answer", "done", "error", "quota"}),
    "_replay_events": frozenset({
        "done", "mindmap", "ownership", "quota", "subtitle", "summary", "tags"}),
}

#: 每条 SSE 流都以 done 收尾。它由 streamSse 的独立分支接住，不进路由表。
DONE_EVENT = "done"

#: 端点 -> 该走哪张前端路由表。
ENDPOINT_ROUTE = {
    "summarize_video": "SUMMARY_ROUTES",
    "chat_with_video": "CHAT_ROUTES",
    "_replay_events": "SUMMARY_ROUTES",
}


def read_source(path: Path) -> str:
    """保行尾读。api_summarize.py 是 CRLF。"""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def backend_events_by_function(source: str) -> dict[str, set[str]]:
    """{函数名: {事件名}} —— 只认 `ServerSentEvent(event=<字面量>)`。

    按 AST 归属到**最近一层**函数，因此 `summarize_video` 里的闭包 `fail`
    会被归到 `fail` 而不是外层（见文件头「闭包 fail() 的归属」）。
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node, kinds):
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, kinds):
                return cur
            cur = parents.get(cur)
        return None

    def func_of(node):
        fn = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        return fn.name if fn is not None else "<module>"

    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `error` 事件由 `sse_events.error_event(...)` 构造（ADR 0034）：
        # 它不出现 `event=` 字面量，只认 `ServerSentEvent` 会把它整批漏掉，
        # 症状是「后端从不发 error」——比没有守卫更容易骗人。
        if isinstance(func, ast.Attribute) and func.attr == "error_event":
            found.setdefault(func_of(node), set()).add("error")
            continue
        if not (isinstance(func, ast.Name) and func.id == "ServerSentEvent"):
            continue
        for kw in node.keywords:
            if kw.arg == "event" and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                found.setdefault(func_of(node), set()).add(kw.value.value)
    return found


def frontend_route_tables(source: str) -> dict[str, set[str]]:
    """{表名: {事件名}} —— 从 `const X_ROUTES = { a: 'onA', ... }` 读。

    正则而非 AST：这是 JS，`ast.parse` 会把它当 Python 解析（实测报
    `SyntaxError: invalid character '。'`）。对象字面量的形状简单且
    本仓前端测试已普遍用正则读源码（见 `community-tags.test.mjs` 等）。
    """
    tables: dict[str, set[str]] = {}
    for m in re.finditer(r"const\s+(\w*_ROUTES)\s*=\s*\{(.*?)\n\}", source, re.S):
        tables[m.group(1)] = {ev for ev, _cb
                              in re.findall(r"(\w+)\s*:\s*'(\w+)'", m.group(2))}
    return tables


def describe_diff(actual: set, expected: set, label: str) -> str:
    """差集写成人能读的话：缺哪些（漏改）、多哪些（死路由）。"""
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    msg = (f"{label}\n"
           f"  真值（EXPECTED）  : {sorted(expected)}\n"
           f"  实际扫到的        : {sorted(actual)}\n")
    if missing:
        msg += f"  ❌ 后端发了但本守卫没登记: {missing}\n"
    if extra:
        msg += f"  ❌ 本守卫登记了但后端不发  : {extra}\n"
    return msg


class TestBackendSendsExactlyWhatIsRegistered:
    """逐端点比对：后端实际发的 == 守卫登记的。

    方向是**双向**的。只查「发了但没登记」会漏掉反向情形——
    把某个事件从后端删掉而守卫还留着它，守卫照样绿，而实际上线后
    前端那一行永远不会被触发。
    """

    def test_each_endpoint_matches_expected(self):
        found = backend_events_by_function(read_source(API_SUMMARIZE))
        offenders = []
        for func, expected in EXPECTED.items():
            actual = frozenset(found.get(func, set()))
            if actual != expected:
                offenders.append(describe_diff(set(actual), set(expected),
                                               f"backend::{func}"))
        assert not offenders, (
            "后端发出的 SSE 事件与守卫登记的不一致。\n"
            + "\n".join(offenders)
            + "\n\n修法：改后端或改 EXPECTED，但**两边必须同时改**——"
              "只改一边时另一边的断言会红。")

    def test_no_unregistered_function_emits_events(self):
        """不在 EXPECTED 里却发事件的函数也必须报出来。

        否则「新加一个发 SSE 的端点、忘了登记」是静默的——上面那条
        只遍历 EXPECTED 的键，多出来的函数压根不会被看见。
        """
        found = backend_events_by_function(read_source(API_SUMMARIZE))
        strays = sorted(set(found) - set(EXPECTED))
        assert not strays, (
            f"这些函数发了 SSE 事件但不在 EXPECTED 里：{strays}\n"
            f"  各自发的事件：{ {k: sorted(v) for k, v in found.items() if k in strays} }\n"
            "修法：登记进 EXPECTED，并给它指定 ENDPOINT_ROUTE 里的路由表。")

    def test_error_event_calls_count_as_the_error_event(self):
        """`sse_events.error_event(...)` 必须被认成发了一条 `error`（ADR 0034）。

        改造前 error 由 11 处 `ServerSentEvent(event="error")` 手写；收成唯一
        构造点之后，只认 `ServerSentEvent` 的扫描器会看到「后端从不发 error」，
        于是前端的 error 路由被误判成死路由——守卫反过来指着正确的代码说它错。
        """
        found = backend_events_by_function(read_source(API_SUMMARIZE))
        assert found.get("summarize_video", set()) >= {"error"}, (
            f"summarize_video 的 error 事件没被认出来：{sorted(found.get('summarize_video', set()))}")
        assert found.get("chat_with_video", set()) >= {"error"}, (
            f"chat_with_video 的 error 事件没被认出来：{sorted(found.get('chat_with_video', set()))}")

    def test_scanner_recognises_the_constructor_call(self):
        """扫描器正例：模块限定的 `x.error_event(...)` 也算一条 error。"""
        synthetic = (
            "import sse_events\n"
            "def g():\n"
            "    yield sse_events.error_event('boom', need_vip=True)\n"
        )
        assert backend_events_by_function(synthetic) == {"g": {"error"}}, \
            backend_events_by_function(synthetic)

    def test_the_ad_hoc_helper_is_gone(self):
        """`fail()` 那个闭包已删（ADR 0034）：它的存在意味着两条构造路径。"""
        assert "async def fail(" not in read_source(API_SUMMARIZE), (
            "api_summarize 里又出现了本地的 fail() 辅助函数——"
            "error 事件只能从 sse_events.error_event 出来")

    def test_scanner_finds_every_registration_form(self):
        """扫描器正例：关键字参数、空格变体都能认出。

        钉住「认的是 `event=` 关键字参数」这个决定：退回文本扫
        `event="x"` 时本条会红，而真实代码里两种写法都会出现。
        """
        synthetic = (
            "def f():\n"
            "    yield ServerSentEvent(raw_data='[DONE]', event='done')\n"
            "    yield ServerSentEvent(raw_data='{}', event = 'error')\n"
            "    yield ServerSentEvent(raw_data='{}', event='quota')\n"
        )
        assert backend_events_by_function(synthetic) == {"f": {"done", "error", "quota"}}, \
            backend_events_by_function(synthetic)

    def test_scanner_ignores_non_literal_event_argument(self):
        """`event=SOME_CONST` 不是字面量，扫描器不认——它报出来会变成噪音。

        噪音会让守卫被人加豁免，豁免一多守卫就废了（本仓已踩过）。
        """
        synthetic = (
            "NAME = 'progress'\n"
            "def f():\n"
            "    yield ServerSentEvent(raw_data='{}', event=NAME)\n"
        )
        assert backend_events_by_function(synthetic) == {}, \
            backend_events_by_function(synthetic)


class TestFrontendRoutesEveryEvent:
    """前端侧：每个端点发的事件都必须有路由，否则被 `?.` 静默丢弃。"""

    def test_each_endpoints_events_are_routed(self):
        found = backend_events_by_function(read_source(API_SUMMARIZE))
        tables = frontend_route_tables(read_source(FRONTEND_API))
        offenders = []
        for func, table_name in ENDPOINT_ROUTE.items():
            routed = tables.get(table_name, set())
            # done 不进路由表，由 streamSse 的独立分支接住（见下面那条）。
            sent = set(found.get(func, set())) - {DONE_EVENT}
            unrouted = sent - routed
            if unrouted:
                offenders.append(
                    f"backend::{func} 发了 {sorted(unrouted)}，"
                    f"而前端 {table_name} 没有它们\n"
                    f"  {table_name} = {sorted(routed)}\n"
                    "  症状：事件被 summarize.ts:164/:192 的 `?.` **静默丢弃**——"
                    "不抛错、不进 catch、不写日志")
        assert not offenders, (
            "SSE 事件没有前端路由：\n" + "\n".join(offenders)
            + "\n\n修法：在 summarize.ts 的对应路由表里补上并映射到回调。"
              "若该事件确实该被忽略，写明理由并登记进豁免，不要默默留着。")

    def test_no_dead_routes(self):
        """前端路由表里**不许有后端从不发的事件**（死路由）。

        这是上一条的反向，缺了它就会出现一个怪形状：**前端多加了一条
        路由，后端不发** —— 那条路由永远不会被触发，却看不出任何问题。
        实测（M4 变异）只加这条反向判据之前，`export: 'onExport'` 加进去
        全套守卫照样绿。

        它比「缺路由」隐蔽：缺路由的症状是「界面上的东西不动」（用户能
        察觉到），死路由的症状是**什么都没有**（没人会察觉）。
        """
        found = backend_events_by_function(read_source(API_SUMMARIZE))
        sent_anywhere = set().union(*found.values()) if found else set()
        tables = frontend_route_tables(read_source(FRONTEND_API))
        dead = {}
        for name, routed in tables.items():
            leftovers = routed - sent_anywhere
            if leftovers:
                dead[name] = sorted(leftovers)
        assert not dead, (
            f"前端路由表里有后端从不发的事件（死路由）：{dead}\n"
            f"  后端全部端点发出过的：{sorted(sent_anywhere)}\n"
            "  死路由永远不会被触发，却看不出任何问题。\n"
            "  若该事件是为「将来后端要发」预留的，写明理由并登记成豁免，"
            "不要默默留着。")

    def test_both_route_tables_still_exist(self):
        """两张表的名字是守卫的参照系：改名会让上面那条恒成立（扫到空表）。

        空表 → `sent - routed` 恒等于 sent → 上面那条**必定**红，
        所以这里报出来的是「参照系没了」而不是「缺路由」，形状不一样。
        """
        tables = frontend_route_tables(read_source(FRONTEND_API))
        missing = sorted(set(ENDPOINT_ROUTE.values()) - set(tables))
        assert not missing, (
            f"路由表不见了：{missing}\n"
            f"  现有：{sorted(tables)}\n"
            "守卫按名字找表；表改名了要么改这里，要么确认是有意为之。")

    def test_done_is_not_in_any_route_table(self):
        """`done` **不许**进路由表——它由 streamSse 的独立分支接住。

        它若混进某张表，`streamSse:87` 的 `if (ev.event === 'done')`
        就会先把它拦掉，那条路由永远不会被触发，成为死代码。
        """
        tables = frontend_route_tables(read_source(FRONTEND_API))
        holders = sorted(name for name, evs in tables.items()
                         if DONE_EVENT in evs)
        assert not holders, (
            f"done 出现在路由表 {holders} 里——它由 streamSse 的独立分支接住，"
            "进表就意味着那条路由是死代码。")

    def test_done_is_handled_by_a_dedicated_branch(self):
        """反向：`streamSse` 必须真的有一个 done 分支。

        与上一条成对：上一条防「done 混进表」，这一条防「done 从表里
        拿掉、分支也一起没了」——那时 done 事件走 route() → 查表未命中
        → `?.` 静默丢弃 → **流永远不会 finish**，UI 卡在 loading。
        """
        src = read_source(FRONTEND_API)
        assert re.search(r"ev\.event === '" + DONE_EVENT + r"'", src), (
            f"streamSse 里找不到 `ev.event === '{DONE_EVENT}'` 的独立分支。\n"
            "它去掉之后：done 事件走 route() → 路由表查不到 → 可选调用"
            "静默丢弃 → 流永远不 finish → UI 卡在 loading。")

    def test_unknown_events_are_still_silently_dropped(self):
        """**实测**未知事件名当前被静默丢弃——把现状钉住，别悄悄变。

        这是本守卫存在的前提。哪天有人把丢弃改成必调（正确但破坏
        滚动发布顺序），本条会红，那正是要重新评估契约清单的信号。

        2026-10-23 改过一次形状：前端把两处内联的可选调用
        `callbacks[SUMMARY_ROUTES[event]]?.(data)` 抽成了一个 dispatch
        辅助函数（frontend/src/api/summarize.ts 的 dispatch()）。**行为没变**
        ——事件名不在表里就早退、回调不在就跳过——但那两个字面量没了。
        所以本条不再数「可选调用出现几次」，改成两条语义约束：
          1. 不能出现**无保护**的调用（查表结果直接当函数用）——
             那会让未知事件名抛 TypeError，而不是被静默丢弃；
          2. 必须存在「查不到就早退」的守卫。
        """
        src = read_source(FRONTEND_API)
        unguarded = re.findall(r"callbacks\[\w*_ROUTES\[event\]\]\s*\(", src)
        assert not unguarded, (
            f"出现了无保护调用 {unguarded}：未知事件名会抛 TypeError，"
            "而不是被静默丢弃。")
        guarded = re.search(r"if \(!name\) return|ROUTES\[event\]\]\?\.\(", src)
        assert guarded, (
            "找不到「查不到就早退」的守卫：未知事件名要么被静默丢弃、要么抛错，"
            "本条要求前者。")