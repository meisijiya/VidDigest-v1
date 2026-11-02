"""抢占状态机的守卫（工单 #40）。

## 这个状态机是什么

「一个链接只该被解析一次」这条承诺落在三条边上，三条边各自带一套
**互不相同**的返回集：

| 边 | 函数 | 返回集 |
|---|---|---|
| 写事务里的正式抢占 | ``database.reserve_video`` | ``reserved`` / ``ready`` / ``pending`` |
| 只读探测（等待者轮询） | ``database.probe_video`` | ``ready`` / ``claimable`` / ``waiting`` |
| 覆盖闸门 | ``video_claim._decide_reclaim`` | ``regenerate`` / ``forbidden`` / ``busy`` / ``skip`` |

外加 ``video_claim._wait_for_claim``——它把 reserve 的三态**重新编码**成
自己的 ``owner`` / ``reuse`` / ``busy``，是这套状态机对外的唯一出口
（``video_claim.acquire_claim`` / ``video_claim.reclaim`` 是它的两个
interface，ADR 0031）。

只有拿到 ``reserved``（或 ``owner``）的人才允许去调模型。

## 两套返回集不是一一对应（这是本文件最要紧的一条）

``probe_video`` 的 ``claimable`` 与 ``reserve_video`` 的 ``reserved``
**语义不同**：

- ``claimable`` = 「这一刻**值得**花一次写事务去抢」，对应「空表**或**老占位」
- ``reserved``  = 「你**抢到**了」

两者既不等价，也不是函数关系：``claimable`` 之后仍可能抢不到（探到与写入
之间被别人抢走），而 ``reserved`` 只在真抢到时才出现。所以本文件把它
写成**行为断言**（探到 claimable 之后真去抢，抢到了就是 reserved；被别人
抢走就是 pending），**不是**一张 ``claimable ↔ reserved`` 的字面映射表。
写成映射表会让「两个集合字面相同」这件事看起来像契约，而它不是。

## 为什么结构层用 AST，不用正则

结构层判据扫函数体里所有 ``return`` 的 outcome 槽位。正则只对**恰好一种
写法**有效——本仓已经为此吃过亏（``test_tags_decode_single_source.py``
记着 ``json\\.loads\\([^)]*tags`` 的教训），而守卫最坏的失败模式是漏报，
不是误报。AST 认的是表达式本身，与怎么写、跨不跨行、有没有嵌套无关。

三处写法都要覆盖，它们在真实文件里都存在：

- ``return "reserved", None``            —— 元组，outcome 在**第一格**
- ``return "reserved"``                  —— 裸字面量（probe_video）
- ``return "a" if cond else "b"``        —— 三元，两支都要收

## 判据的方向性（写之前先自问）

拿每条断言去测另一种实现，问它会不会红：

- 集合判定用**相等**而非子集：少了（被改名）与多了（新增）都红。
  把 ``reserve_video`` 整体改成恒返回 ``"pending"``，相等判定立刻报 missing。
- 「返回集恰好是这三态」**挡不住语义颠倒**——把
  ``"claimable" if stale else "waiting"`` 两个分支对调，字面量集合一点没变。
  所以每条边都必须配一组行为断言（``TestProbeBehaviour``），
  这也是结构层与行为层都存在的原因，不是重复。
- 行为层的四态**一条都不能省**：少测一态，就有一整条分支没被量到，
  而「该等的时候抢」与「该抢的时候等」两种错误都不抛异常。
- 所有扫源码的测试都在**模块顶层**读一次源码，不在 ``describe``/``test``
  体里读：本仓实测过 ``test`` 体抛异常是响的、模块顶层抛是响的，
  而某些 runner 的 ``describe`` 体抛异常是**哑的**。
"""
import ast
import asyncio
import os
from datetime import datetime, timedelta, timezone

import api_summarize
import database
import video_claim

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_PY = os.path.join(BACKEND, "database.py")
API_SUMMARIZE_PY = os.path.join(BACKEND, "api_summarize.py")
VIDEO_CLAIM_PY = os.path.join(BACKEND, "video_claim.py")

URL = "https://example.com/claim"

#: reserve_video 的 outcome 集合。**恰好**这三个，一个不多一个不少。
RESERVE_OUTCOMES = frozenset({"reserved", "ready", "pending"})

#: probe_video 的返回集合。与 RESERVE_OUTCOMES 语义不同，**故意不统一**：
#: 合并会抹掉「值不值得花一次写事务」与「你抢到了没有」这条设计意图。
PROBE_STATES = frozenset({"ready", "claimable", "waiting"})

#: _begin_regenerate 的 outcome 集合（覆盖闸门，与上面两条边无关）。
REGENERATE_OUTCOMES = frozenset({"regenerate", "forbidden", "busy", "skip"})

#: _claim_video 对外的 outcome 集合：reserve 三态的**重新编码**，
#: 不是同一套字面量。只调模型的那一支叫 owner，不叫 reserved。
CLAIM_OUTCOMES = frozenset({"owner", "reuse", "busy"})


# ── 扫描器 ──────────────────────────────────────────────────

def read_source(path: str) -> str:
    """保行尾读源码。

    database.py 与 api_summarize.py 都是 CRLF 工作区。本文件不做任何行尾
    断言，但读的时候必须原样读进来——换了行尾再 parse 不是错，可它会让
    「同一个 anchor 在两种行尾下命中数不同」这类问题排查起来变得莫名其妙。
    """
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def module_constants(source: str) -> dict:
    """模块级常量表：``NAME = "字面量"``。

    为什么需要它：占位权 module 的 outcome 用具名常量表达（词汇只有一处
    出处），而守卫要看见**常量真正的值**——否则改常量名下的字面量时，
    集合判定只会说「读不出来」，等于没有守卫。

    具名常量不是计算值：它在 import 时就定死了，读它等于读字面量。所以
    「outcome 必须是字面量」这条判据对它是成立的。
    """
    tree = ast.parse(source)
    consts: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def outcome_literals(node, consts: dict):
    """从一个 Return 的值表达式里取出 outcome 字面量。

    返回 ``(literals, opaque)``：

    - ``literals``：能静态读出来的字符串字面量
    - ``opaque``   ：**读不出来**的行号（返回的是变量、函数调用等）

    为什么元组只跟第一格：``return "pending", row`` 里第二格是**载荷**，
    不是 outcome。把它也算进去的话，每一条 return 都会被标成 opaque，
    判据立刻变成天天误报的狼来（守卫一旦变成噪声，人就会去放宽它，
    而放宽的守卫和没有守卫等价）。
    """
    if node is None:                       # bare `return`
        return [], False
    if isinstance(node, ast.Constant):
        # 任何常量都是静态可判定的：字符串进 literals，
        # None / 数字 / 布尔不是 outcome，但也不该算「读不出来」。
        return ([node.value] if isinstance(node.value, str) else []), False
    if isinstance(node, ast.Name):
        # 具名常量：读它的值。读不出来才算 opaque（那是局部变量或别的）。
        if node.id in consts:
            return [consts[node.id]], False
        return [], True
    if isinstance(node, ast.IfExp):
        # 三元两支都要收：probe_video 的最后一分支就是这个形状，
        # 只收 body 会把 "waiting" 漏掉——而漏报是守卫最坏的失败模式。
        body, opaque_body = outcome_literals(node.body, consts)
        orelse, opaque_orelse = outcome_literals(node.orelse, consts)
        return body + orelse, opaque_body or opaque_orelse
    if isinstance(node, (ast.Tuple, ast.List)):
        if not node.elts:
            return [], False
        return outcome_literals(node.elts[0], consts)
    return [], True                         # Name / Call / Subscript / ...


def find_outcomes(source: str, func_name: str):
    """返回 ``(字面量集合, 读不出来的行号列表)``；函数不存在时抛 AssertionError。

    函数不存在必须炸而不是返回空集合：空集合会让「相等」判据报出
    「三个全都不见了」，把改名说成集体失踪，指错方向。
    """
    tree = ast.parse(source)
    nodes = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == func_name
    ]
    assert nodes, f"{func_name} 不在源码里了，守卫需要更新（是它被改名了吗？）"
    assert len(nodes) == 1, (
        f"{func_name} 在同一个文件里出现了 {len(nodes)} 次，"
        "按名字取第一个会扫错函数"
    )

    literals: set = set()
    opaque: list = []
    consts = module_constants(source)
    for sub in ast.walk(nodes[0]):
        if not isinstance(sub, ast.Return):
            continue
        got, is_opaque = outcome_literals(sub.value, consts)
        literals.update(got)
        if is_opaque:
            opaque.append(sub.lineno)
    return literals, sorted(opaque)


def find_sql_literals(source: str, func_name: str) -> list:
    """返回该函数体内所有 ``execute(<字符串字面量>)`` 的 SQL 原文。"""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) \
                        and getattr(sub.func, "attr", None) == "execute" \
                        and sub.args \
                        and isinstance(sub.args[0], ast.Constant) \
                        and isinstance(sub.args[0].value, str):
                    out.append(sub.args[0].value)
    return out


# ── 行为层的小工具 ────────────────────────────────────────────

def age_pending(seconds: int, url: str = None) -> None:
    """把占位行的 updated_at 往前推，模拟「占位者已经死了这么久」。"""
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()
    with database.get_db() as conn:
        if url is None:
            conn.execute("UPDATE videos SET updated_at = ?", (old,))
        else:
            conn.execute(
                "UPDATE videos SET updated_at = ? WHERE canonical_url = ?",
                (old, database.canonical_video_url(url)),
            )


def begin_regenerate(url: str, user_id: int):
    """跑一次 reclaim，拿到 outcome。

    闸门在**退出上下文时**放掉（ADR 0031），所以这个 helper 只能问
    「判定成什么」，问不了「闸门还在不在我手上」。后者要在上下文里问——
    见 ``test_own_row_twice_is_regenerate_then_busy``。
    """
    async def scenario():
        async with video_claim.reclaim(url, user_id) as reg:
            return reg.outcome
    return asyncio.run(scenario())


# ── 结构层：三条边的返回集 ────────────────────────────────────

class TestReturnSetsAreExactlyThese:
    """每条边的 outcome 集合必须**恰好**等于它那张表。

    用相等而不是子集：相等同时抓住两个方向——多出来的（新增一个第四态）
    与少掉的（被改名）。工单 #3 的教训是「票面说的顺序早就对了，
    真正的缺口在别处」；这里守的是「字面量集合不许悄悄变」。
    """

    def test_reserve_returns_exactly_the_three_outcomes(self):
        literals, _ = find_outcomes(read_source(DATABASE_PY), "reserve_video")
        assert literals == RESERVE_OUTCOMES, (
            "reserve_video 的 outcome 集合变了。\n"
            f"  约定: {sorted(RESERVE_OUTCOMES)}\n"
            f"  实际: {sorted(literals)}\n"
            + (f"  多出来: {sorted(literals - RESERVE_OUTCOMES)}\n"
               if literals - RESERVE_OUTCOMES else "")
            + (f"  不见了: {sorted(RESERVE_OUTCOMES - literals)}\n"
               if RESERVE_OUTCOMES - literals else "")
            + "\n「只有拿到 reserved 的人可以调模型」是这套设计唯一的闸门，\n"
              "多一个态就等于多一条没人审过的调模型路径。"
        )

    def test_probe_returns_exactly_the_three_states(self):
        literals, _ = find_outcomes(read_source(DATABASE_PY), "probe_video")
        assert literals == PROBE_STATES, (
            "probe_video 的返回集合变了。\n"
            f"  约定: {sorted(PROBE_STATES)}\n"
            f"  实际: {sorted(literals)}\n"
            "注意 claimable 与 reserved 语义不同，本就不该统一成一个字面量。"
        )

    def test_reclaim_returns_exactly_the_four_outcomes(self):
        literals, _ = find_outcomes(read_source(VIDEO_CLAIM_PY), "_decide_reclaim")
        assert literals == REGENERATE_OUTCOMES, (
            "覆盖闸门的 outcome 集合变了。\n"
            f"  约定: {sorted(REGENERATE_OUTCOMES)}\n"
            f"  实际: {sorted(literals)}\n"
            "四个态各有各的后果（放行覆盖 / 拒绝 / 拒绝且扣额度 / 改走首次解析）。"
        )

    def test_claim_re_encodes_into_its_own_three_outcomes(self):
        """`acquire_claim` 是这套状态机对外的唯一出口，它也必须自带契约。

        它返回的**不是** reserve 的三个字面量：抢到叫 ``owner`` 不叫
        ``reserved``，抢不到等超时叫 ``busy`` 不叫 ``pending``。
        这层重新编码是调用方唯一能看到的东西，钉住它才叫把状态机钉住。
        """
        literals, _ = find_outcomes(read_source(VIDEO_CLAIM_PY), "_wait_for_claim")
        assert literals == CLAIM_OUTCOMES, (
            "acquire_claim 的 outcome 集合变了。\n"
            f"  约定: {sorted(CLAIM_OUTCOMES)}\n"
            f"  实际: {sorted(literals)}\n"
            "注意它与 RESERVE_OUTCOMES 故意不同名，别顺手改名去「统一」。"
        )

    def test_no_outcome_slot_is_a_computed_value(self):
        """每条 return 的 outcome 槽位都必须是**字面量**。

        这一条是上面四条的前提：``return outcome``（把 outcome 存进局部变量
        再返回）在结构层是**完全不可见**的——扫出来一个字符串都没有，
        「相等」判定只会说「三个态都不见了」，说错了真正发生的事。
        """
        for path, func_name in (
            (DATABASE_PY, "reserve_video"),
            (DATABASE_PY, "probe_video"),
            (VIDEO_CLAIM_PY, "_decide_reclaim"),
            (VIDEO_CLAIM_PY, "_wait_for_claim"),
        ):
            _literals, opaque = find_outcomes(read_source(path), func_name)
            assert opaque == [], (
                f"{func_name} 有 {len(opaque)} 处 return 的 outcome 读不出来"
                f"（行号 {opaque}）——返回的是变量或调用结果，不是字面量。\n"
                "守卫只能看见写在 return 上的字面量；outcome 藏进局部变量，\n"
                "这套契约就再也钉不住了。请改成直接 return 字面量。"
            )


class TestTakeoverKeepsItsOptimisticLock:
    """接管那条 UPDATE 的 WHERE 必须带 ``updated_at = ?``。

    这是**结构层**的乐观锁守卫，与 ``TestOptimisticLock`` 里那几条行为断言
    互补：那几条从外面看「抢输的人没抢走位置」，这一条直接看条件还在不在。
    条件被删掉时行为断言也会红，但那时它报的是「位置被抢走了」，
    指不到「WHERE 少了一个条件」。
    """

    def test_takeover_update_is_guarded_by_updated_at(self):
        statements = find_sql_literals(read_source(DATABASE_PY), "reserve_video")
        guarded = [
            sql for sql in statements
            if "UPDATE videos" in sql and "AND updated_at = ?" in sql
        ]
        assert guarded, (
            "reserve_video 里找不到一条「WHERE 带 updated_at = ?」的 UPDATE ——\n"
            f"  实际找到的写语句: {[s.strip()[:60] for s in statements]}\n"
            "那条条件是接管的乐观锁：拿读出来的旧值去比，\n"
            "只有行没被别人动过才抢得到（rowcount == 1）。"
        )

    def test_takeover_update_is_also_guarded_by_status(self):
        """同一句还必须带 ``status = ?``——不能接管一个已 ready 的行。

        少这个条件的话，一个已经完成的解析会被后来的调用方抢走并重写，
        而 #7 的承诺是「ready 行谁都不能改写」。
        """
        statements = find_sql_literals(read_source(DATABASE_PY), "reserve_video")
        guarded = [
            sql for sql in statements
            if "UPDATE videos" in sql
            and "AND updated_at = ?" in sql
            and "AND status = ?" in sql
        ]
        assert guarded, (
            "接管那条 UPDATE 少了 status 条件 —— 已 ready 的行可能被抢走重写"
        )


# ── 行为层：reserve_video ─────────────────────────────────────

class TestReserveBehaviour:
    """四种库现状各返回什么。一态都不能省——每态走的是不同分支。"""

    def test_empty_slot_is_reserved_with_no_row(self, db, make_user):
        owner = make_user("owner@example.com")
        outcome, row = database.reserve_video(URL + "/empty", owner)
        assert outcome == "reserved", (
            f"空表抢占应返回 reserved，实际 {outcome!r}。"
            "返回别的会让第一次解析的人不敢去调模型"
        )
        assert row is None, f"reserved 不该带行，实际带了 {row!r}"

    def test_fresh_placeholder_is_pending_with_that_row(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/fresh"
        database.reserve_video(url, owner)
        outcome, row = database.reserve_video(url, owner)
        assert outcome == "pending", (
            f"新鲜占位被说成可抢：等待者会去偷一个正在干活的位置，"
            f"两个人同时调模型、同时扣额度，实际 {outcome!r}"
        )
        assert row is not None and row["canonical_url"] == database.canonical_video_url(url), (
            f"pending 必须把占位行带回去，调用方要靠它判断等的是谁，实际 {row!r}"
        )

    def test_ready_slot_is_ready_with_the_finished_row(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/done"
        database.reserve_video(url, owner)
        database.complete_video(url, summary_md="好了")
        outcome, row = database.reserve_video(url, owner)
        assert outcome == "ready", (
            f"已有结果应返回 ready 走复用，实际 {outcome!r}——"
            "会让后来的人重新解析一遍"
        )
        assert row is not None and row["status"] == database.VIDEO_STATUS_READY, (
            f"ready 必须带那一行结果去复用，实际 {row!r}"
        )

    def test_stale_placeholder_is_taken_over(self, db, make_user):
        """老占位可接管：这是「占位者进程被杀了」的恢复路径。

        挡掉它的话，那个链接从此永久不可解析。
        """
        dead = make_user("dead@example.com")
        taker = make_user("taker@example.com")
        url = URL + "/stale"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)

        outcome, row = database.reserve_video(url, taker)
        assert outcome == "reserved", (
            f"老到 TTL 的占位没被接管，实际 {outcome!r}——"
            "占位者进程被杀时它跑到不了 finally 还位，这个链接会永久卡死"
        )
        assert row is None, f"接管不该带行，实际 {row!r}"
        after = database.get_video_by_url(url)
        assert after["parsed_by"] == taker, (
            f"接管后位置没归接管者，实际 parsed_by={after['parsed_by']!r}，"
            f"期望 {taker!r}"
        )


# ── 行为层：probe_video ───────────────────────────────────────

class TestProbeBehaviour:
    """probe 只回答「值不值得花一次写事务」，四个库现状必须分别答对。

    结构层守不住这一组：把三元的两支对调，字面量集合一模一样，
    而语义整个颠倒（该抢的时候说在等，该等的时候说能抢）。
    """

    def test_empty_slot_is_claimable(self, db):
        assert database.probe_video(URL + "/none") == "claimable", (
            "没人占位时该说值得抢"
        )

    def test_fresh_placeholder_is_waiting(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/busy"
        database.reserve_video(url, owner)
        assert database.probe_video(url) == "waiting", (
            "新鲜占位被说成可抢：等待者会去偷一个正在干活的位置"
        )

    def test_ready_slot_is_ready(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/ready"
        database.reserve_video(url, owner)
        database.complete_video(url, summary_md="好了")
        assert database.probe_video(url) == "ready", (
            "成品没被认出来：等待者会一直等一份已经存在的结果"
        )

    def test_stale_placeholder_is_claimable(self, db, make_user):
        dead = make_user("dead@example.com")
        url = URL + "/dead"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)
        assert database.probe_video(url) == "claimable", (
            "老占位没被判成可抢：这个链接会永久卡死"
        )


# ── 两套返回集的对应关系 ──────────────────────────────────────

class TestClaimableIsNotReserved:
    """claimable 与 reserved 不是一回事，这里用行为把它们钉住。

    写成「claimable ↔ reserved」的映射表是错的：那个形状会让两套字面量
    看起来像同一件事，而它们差着一次**竞态**——probe 读到「没人占」到
    INSERT 之间仍可能被别人抢走。那一段由 reserve 的唯一索引兜住，
    抢输了必须报 pending。
    """

    def test_claimable_then_reserve_yields_reserved_on_an_empty_slot(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/corr-empty"
        assert database.probe_video(url) == "claimable"
        outcome, row = database.reserve_video(url, owner)
        assert outcome == "reserved", (
            f"探到 claimable 之后真去抢，空槽应当抢到，实际 {outcome!r}"
        )
        assert row is None

    def test_claimable_then_reserve_yields_reserved_on_a_stale_slot(self, db, make_user):
        dead = make_user("dead@example.com")
        taker = make_user("taker@example.com")
        url = URL + "/corr-stale"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)
        assert database.probe_video(url) == "claimable"
        outcome, row = database.reserve_video(url, taker)
        assert outcome == "reserved", (
            f"探到 claimable 之后真去抢，老占位应当抢到，实际 {outcome!r}"
        )
        assert row is None

    def test_waiting_then_reserve_yields_pending(self, db, make_user):
        """反方向：waiting 之后去抢只能拿到 pending，**不可能**是 reserved。"""
        owner = make_user("owner@example.com")
        url = URL + "/corr-wait"
        database.reserve_video(url, owner)
        assert database.probe_video(url) == "waiting"
        outcome, _row = database.reserve_video(url, owner)
        assert outcome == "pending", (
            f"waiting 之后抢到了 {outcome!r}：有人从别人正在干活的位置上偷走了闸门"
        )

    def test_claimable_then_someone_else_wins_yields_pending(
        self, db, make_user, monkeypatch
    ):
        """claimable 之后也可能抢不到——所以两张表之间**没有映射**。

        模拟竞态：探到 claimable 之后、reserve 动手之前，别人抢到并占了
        一个**新鲜**位置。reserve 重读到的行不再是老的，于是压根不尝试接管，
        直接报 pending。

        与 ``TestOptimisticLock`` 那两条的差别：那里 reserve 手里握着**旧**
        updated_at 而库里的行已经变了（乐观锁失败）；这里 reserve 读到的是
        **新鲜**行（压根没进接管分支）。两条都是 pending，坏掉的地方不同，
        所以要分开守。
        """
        owner = make_user("owner@example.com")
        url = URL + "/race"
        database.reserve_video(url, owner)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)
        assert database.probe_video(url) == "claimable"

        real_get = database.get_video_by_url
        fired = []

        def raced(target):
            row = real_get(target)
            if row is not None and not fired:
                fired.append(True)
                now = datetime.now(timezone.utc).isoformat()
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE videos SET updated_at = ? WHERE canonical_url = ?",
                        (now, database.canonical_video_url(target)),
                    )
                return real_get(target)
            return row

        monkeypatch.setattr(database, "get_video_by_url", raced)
        taker = make_user("taker@example.com")
        outcome, row = database.reserve_video(url, taker)

        assert fired, "前置失效：竞态没注入，这条用例什么都证明不了"
        assert outcome == "pending", (
            f"被别人抢走之后仍报 {outcome!r}：抢输的人会以为自己是首次解析者，"
            "于是调两次模型、扣两次额度"
        )
        assert row is not None


# ── 乐观锁 ────────────────────────────────────────────────────

class TestOptimisticLock:
    """接管靠「拿读出来的旧 updated_at 去比」当锁，比不上就是抢输了。"""

    def _race_after_read(self, db, url, bump_seconds, monkeypatch):
        """让库里的行在 reserve 读到它之后、UPDATE 之前被改掉。

        reserve 手里握着的是**旧** updated_at，而库里已经是新的，
        于是 ``WHERE updated_at = ?`` 匹配 0 行 → 抢输。
        """
        real_get = database.get_video_by_url
        fired = []

        def moved(target):
            row = real_get(target)
            if row is not None and not fired:
                fired.append(True)
                bumped = (
                    datetime.now(timezone.utc) - timedelta(seconds=bump_seconds)
                ).isoformat()
                with database.get_db() as conn:
                    conn.execute(
                        "UPDATE videos SET updated_at = ? WHERE canonical_url = ?",
                        (bumped, database.canonical_video_url(target)),
                    )
            return row                       # 交出去的是**旧**值

        monkeypatch.setattr(database, "get_video_by_url", moved)
        return fired

    def test_takeover_fails_when_the_row_moves_under_its_own_read(
        self, db, make_user, monkeypatch
    ):
        dead = make_user("dead@example.com")
        taker = make_user("taker@example.com")
        url = URL + "/lock"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)
        assert database.probe_video(url) == "claimable", "前置不成立：这一行还是老占位"

        fired = self._race_after_read(
            db, url, database.VIDEO_PENDING_TTL_SECONDS + 30, monkeypatch
        )
        outcome, row = database.reserve_video(url, taker)

        assert fired, "前置失效：竞态没注入，这条用例什么都证明不了"
        assert outcome == "pending", (
            f"行被改过之后接管仍成功（{outcome!r}）——乐观锁失效了，"
            "两个人会同时接管同一个链接、同时调模型"
        )
        assert row is not None

    def test_second_takeover_of_the_same_stale_row_reports_pending(
        self, db, make_user
    ):
        """连抢两次：第一次抢到，第二次报 pending 且**位置没被抢走**。

        只断言返回 pending 是不够的——把 ``updated_at`` 条件删掉之后，
        第二次仍可能因为行已经变新鲜而报 pending，这条照样绿。
        所以额外断言 ``parsed_by`` 仍属于第一个抢到的人：
        乐观锁真正保证的是「抢输的人没抢走位置」。
        """
        dead = make_user("dead@example.com")
        first = make_user("first@example.com")
        second = make_user("second@example.com")
        url = URL + "/twice"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)

        assert database.reserve_video(url, first)[0] == "reserved", "前置不成立"
        assert database.reserve_video(url, second)[0] == "pending", (
            "第一个抢到之后，第二个不该再抢到"
        )
        after = database.get_video_by_url(url)
        assert after["parsed_by"] == first, (
            f"抢输的人把位置抢走了：parsed_by={after['parsed_by']!r}，"
            f"期望仍是第一个抢到者的 {first!r}"
        )

    def test_takeover_moves_the_timestamp_forward(self, db, make_user):
        """接管成功会把 updated_at 推到当下——这是它给后来者续命的机制。

        少了这一下，被接管的行仍旧「老」，下一轮又会被当成可接管，
        每 50ms 抢一轮。
        """
        dead = make_user("dead@example.com")
        taker = make_user("taker@example.com")
        url = URL + "/forward"
        database.reserve_video(url, dead)
        age_pending(database.VIDEO_PENDING_TTL_SECONDS + 60, url)
        age_row_before = database.get_video_by_url(url)["updated_at"]

        assert database.reserve_video(url, taker)[0] == "reserved"
        after = database.get_video_by_url(url)["updated_at"]
        assert after != age_row_before, (
            "接管后 updated_at 没动：这一行仍旧被判成老占位，"
            "等待者会一轮一轮地反复抢它"
        )
        assert database.probe_video(url) == "waiting", (
            "刚接管完的占位不该立刻又被说成可抢"
        )


# ── TTL 的保守分支 ────────────────────────────────────────────

class TestStalenessIsConservative:
    """**解析不出时间就当它还活着** —— 判 stale 的那一步不许猜。

    `_pending_is_stale`（`database.py`）里有三条保守分支：`updated_at` 为空、
    解析抛 `TypeError`/`ValueError`、以及 naive 时间戳按 UTC 处理。三条都返回
    False，也就是「不接管、等原占位者」。

    为什么这条要守：**反过来**（解析失败就当它老了、可以接管）会把一个**活着的**
    占位者当场顶掉，于是两个请求同时去调模型 —— 而「只调一次模型」正是这套
    状态机存在的全部理由。所以这是「拿不准时不动」而不是「拿不准时乐观」。

    ⚠️ 这三条分支在本单开工时**全仓零覆盖**：所有 TTL 相关用例
    （`test_release_video_ownership` / `test_waiter_poll_writes` /
    `test_community_videos` / `test_community_card_metadata`）都用合法 ISO
    时间戳，唯一的 `not-a-date` 在 `test_quota.py:171` 而那是 **VIP 过期**，
    与本函数无关。
    """

    def _make_row_with_raw_timestamp(self, url, raw):
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO videos (video_url, canonical_url, status,"
                " parsed_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (url, database.canonical_video_url(url), "pending", 1,
                 raw, raw))

    def test_unparseable_timestamp_is_treated_as_alive(self, db):
        url = URL + "/bad-ts"
        raw = "not-a-date-at-all"
        self._make_row_with_raw_timestamp(url, raw)
        assert database._pending_is_stale({"updated_at": raw}) is False, (
            "解析不出时间就该当它还活着（保守），而不是当它老了")
        assert database.probe_video(url) == "waiting", (
            "探到一个时间坏掉的占位时必须继续等，不该说可抢")

    def test_empty_timestamp_is_treated_as_alive(self, db):
        url = URL + "/empty-ts"
        self._make_row_with_raw_timestamp(url, "")
        assert database._pending_is_stale({"updated_at": ""}) is False
        assert database._pending_is_stale({"updated_at": None}) is False
        assert database.probe_video(url) == "waiting", (
            "updated_at 为空说明没法判断年龄，不该接管")

    def test_naive_timestamp_is_read_as_utc(self, db):
        """naive 时间戳按 UTC 补上，而不是按本地时区猜。

        这一条与前两条方向相反：它**要**得出一个年龄。若实现漏了
        `replace(tzinfo=...)`，`now - updated` 会抛 TypeError（naive 与 aware
        不能相减）落进 except，于是被判成「还活着」——**每一行**都接管不了，
        这个链接永久不可解析。
        """
        url = URL + "/naive-ts"
        naive = (
            datetime.now(timezone.utc)
            - timedelta(seconds=database.VIDEO_PENDING_TTL_SECONDS + 60)
        ).replace(tzinfo=None)
        iso = naive.isoformat()
        self._make_row_with_raw_timestamp(url, iso)
        assert database._pending_is_stale({"updated_at": iso}) is True, (
            "naive 时间戳（按 UTC 理解）超过 TTL 却被判成还活着——"
            "多半是少了 replace(tzinfo=timezone.utc)，"
            "相减抛 TypeError 落进 except，于是**每一行**都接管不了")
        assert database.probe_video(url) == "claimable", (
            "老到 TTL 之外的占位必须可被接管")

    def test_empty_row_does_not_crash(self, db):
        """`_pending_is_stale` 读的是 `row.get(...)`，传空 dict 不能炸。"""
        assert database._pending_is_stale({}) is False

    def test_the_except_branch_is_actually_reached(self, db):
        """**结构层**：except 分支必须真的能被走到，不能是死代码。

        为什么要这条：前三条断言的都是**结果**，而结果可以被「换个写法绕过」。
        实测变异 M4 把 `datetime.fromisoformat(raw)` 改成
        `datetime.fromisoformat(str(raw))` —— except 分支从此成死代码，
        而 `str()` 让 `not-a-date` 照样抛 ValueError，于是
        **前三条全绿**（结果确实仍是 False）。

        也就是说：只断言结果时，「让那条分支不再存在」是隐形的。
        本条直接读源码里的异常元组与被调用的函数名：
        - 必须是 `datetime.fromisoformat`（不是 `str(...)` 包装过的），
        - 异常元组必须同时含 `TypeError` 与 `ValueError`
          （`fromisoformat` 对 None 抛 TypeError、对坏串抛 ValueError，
            少写一个就有一种输入直接冒出去）。
        """
        import inspect  # noqa: PLC0415
        import re  # noqa: PLC0415
        src = inspect.getsource(database._pending_is_stale)
        code = src.split('"""')[-1]
        assert "datetime.fromisoformat(" in code, (
            "_pending_is_stale 里的解析调用不见了：\n" + code)
        assert "fromisoformat(str(" not in code, (
            "解析前多包了一层 str()——except 分支从此成死代码，"
            "而结果仍然是 False，于是**只断言结果的守卫全都发现不了**")
        handler = re.search(r"except\s*\(([^)]*)\)\s*:", code)
        assert handler, f"找不到 except 子句：\n{code}"
        caught = {n.strip() for n in handler.group(1).split(",")}
        assert {"TypeError", "ValueError"} <= caught, (
            f"except 只捕 {sorted(caught)}——"
            "fromisoformat(None) 抛 TypeError、fromisoformat('坏串') 抛 ValueError，"
            "少捕一种就有一种输入直接穿过 except 冒出去，"
            f"症状是这一行被当成 stale。\n{code}")


# ── 覆盖闸门 ──────────────────────────────────────────────────

class TestReclaimBehaviour:
    """四个 outcome 各对应一条真实路径，一个都不能只靠结构层。"""

    def test_absent_row_is_skip(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/regen-none"
        outcome = begin_regenerate(url, owner)
        assert outcome == "skip", f"没有行时该改走首次解析，实际 {outcome!r}"
        assert database.get_video_by_url(url) is None

    def test_pending_row_is_skip(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/regen-pending"
        database.reserve_video(url, owner)
        outcome = begin_regenerate(url, owner)
        assert outcome == "skip", (
            f"还没 ready 的行压根没有可覆盖的东西，实际 {outcome!r}"
        )

    def test_someone_elses_row_is_forbidden(self, db, make_user):
        owner = make_user("owner@example.com")
        other = make_user("other@example.com")
        url = URL + "/regen-foreign"
        database.reserve_video(url, owner)
        database.complete_video(url, summary_md="好了")
        outcome = begin_regenerate(url, other)
        assert outcome == "forbidden", (
            f"覆盖别人那一份必须直接拒绝，实际 {outcome!r}"
        )
        assert database.get_video_by_url(url) is not None

    def test_own_row_twice_is_regenerate_then_busy(self, db, make_user):
        owner = make_user("owner@example.com")
        url = URL + "/regen-own"
        database.reserve_video(url, owner)
        database.complete_video(url, summary_md="好了")

        async def scenario():
            # 第一次：抢到闸门。**上下文不退出**，闸门就还在我手上。
            async with video_claim.reclaim(url, owner) as first:
                assert first.outcome == "regenerate", (
                    f"覆盖自己已完成的总结应放行，实际 {first.outcome!r}"
                )
                row = database.get_video_by_url(url)
                assert row is not None and row["status"] == database.VIDEO_STATUS_READY
                # 第二次：闸门在我手上 → busy
                async with video_claim.reclaim(url, owner) as second:
                    assert second.outcome == "busy", (
                        f"同一个链接已有一次覆盖在跑，第二次应报 busy，"
                        f"实际 {second.outcome!r}——放行意味着同一个作者能并发"
                        "发起两次覆盖、两次调模型、两次扣额度"
                    )

        asyncio.run(scenario())


# ── 守卫自查：扫描器本身有没有判别力 ──────────────────────────

class TestScannerDetectsViolations:
    """把扫描器改成恒返回空，上面所有结构层断言都会绿。

    没有这一组的话，「守卫失效」与「守卫通过」无法区分——这与
    ``test_db_fixture_guard.py`` / ``test_tags_decode_single_source.py``
    守的是同一件事。
    """

    def test_detects_a_new_literal_outcome(self):
        source = (
            "def probe_video(url):\n"
            "    if row is None:\n"
            "        return 'claimable'\n"
            "    return 'stolen'\n"
        )
        literals, _ = find_outcomes(source, "probe_video")
        assert "stolen" in literals, f"新加的字面量没被认出来：{sorted(literals)}"
        assert sorted(literals - PROBE_STATES) == ["stolen"]

    def test_detects_a_renamed_outcome(self):
        """改名要报「不见了」，而不是含糊地说集合变了。"""
        source = (
            "def probe_video(url):\n"
            "    return 'claimable'\n"
            "    return 'ready'\n"
        )
        literals, _ = find_outcomes(source, "probe_video")
        assert sorted(PROBE_STATES - literals) == ["waiting"], (
            "把 waiting 改名后，缺的那个没被认出来"
        )

    def test_detects_both_arms_of_a_ternary(self):
        """三元两支都要收。只收 body 的话 waiting 会被漏掉——漏报最坏。"""
        source = (
            "def probe_video(url, row):\n"
            "    return 'claimable' if stale(row) else 'waiting'\n"
        )
        literals, opaque = find_outcomes(source, "probe_video")
        assert literals == {"claimable", "waiting"}, f"三元只收了一支：{literals}"
        assert opaque == []

    def test_reports_an_outcome_that_is_a_variable(self):
        """outcome 藏进局部变量时必须被点名，而不是安静地当成空集合。"""
        source = (
            "def probe_video(url, row):\n"
            "    outcome = 'claimable'\n"
            "    return outcome\n"
        )
        literals, opaque = find_outcomes(source, "probe_video")
        assert literals == set(), f"变量不该被当成字面量：{literals}"
        assert opaque == [3], f"第 3 行的 return 没被点名：{opaque}"

    def test_payload_slot_is_not_an_opaque_outcome(self):
        """``return "pending", row`` 的第二格是载荷，不该被判成「读不出来」。

        这条是防**误报**的：每条 return 都被标 opaque 的守卫用不了两天
        就会被当成噪声放宽，而放宽的守卫等于没有守卫。
        """
        source = (
            "def reserve_video(url):\n"
            "    return 'reserved', None\n"
            "    return 'ready', get_row(url)\n"
            "    return 'pending', row\n"
        )
        literals, opaque = find_outcomes(source, "reserve_video")
        assert literals == {"reserved", "ready", "pending"}
        assert opaque == [], f"载荷被误判成 opaque：{opaque}"

    def test_bare_return_is_not_an_opaque_outcome(self):
        source = (
            "def probe_video(url):\n"
            "    return\n"
        )
        literals, opaque = find_outcomes(source, "probe_video")
        assert (literals, opaque) == (set(), []), (
            f"bare return 被误判：{(literals, opaque)}"
        )

    def test_missing_function_raises_instead_of_reporting_an_empty_set(self):
        """函数被改名/删掉必须炸。返回空集合会把「改名」说成「三个态全没了」。"""
        source = "def other_name(url):\n    return 'ready'\n"
        try:
            find_outcomes(source, "probe_video")
        except AssertionError as exc:
            assert "probe_video" in str(exc), f"报错信息没指名函数：{exc}"
        else:
            raise AssertionError("函数不存在时应当抛 AssertionError")

    def test_sql_scan_sees_multiline_statements(self):
        """SQL 是跨行的三引号串，靠它才能钉住「WHERE 还带不带条件」。"""
        source = (
            "def reserve_video(url):\n"
            "    cursor = conn.execute(\n"
            "        '''UPDATE videos\n"
            "              SET updated_at = ?\n"
            "           WHERE canonical_url = ? AND updated_at = ?'''\n"
            "    )\n"
        )
        statements = find_sql_literals(source, "reserve_video")
        assert len(statements) == 1, f"跨行 SQL 没被认出来：{statements}"
        assert "AND updated_at = ?" in statements[0]