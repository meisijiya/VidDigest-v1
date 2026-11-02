r"""额度种类的**结构守卫**：一处枚举，其余派生（工单 #38 / ADR 0033）。

现在只有一处：`quota_kinds.QUOTA_KINDS`。一种额度 = 计数字段 + 日期字段
+ 上限常量名 + 覆盖列名 + 中文名，一条 `QuotaKind` 记录给全。列名用于拼
SQL，中文名进提示文案与协议（`/api/quota` 的 `labels`），字段名由
`admin_api.quota_field_name` 从 kind 派生，pydantic 请求体的字段由注册表
**生成**（`create_model`），前端按 `labels` 渲染。

这里守的是**结构**：
1. `backend/` 下（除注册表本身与 `tests/`）不许再出现枚举额度种类的字面量；
2. 注册表自身必须恰好是那一个 dict 字面量，别拆成两个、别改成 comprehension；
3. 注册表里的列名必须真的在 `users` 表上（拼错的表现是 `no such column`，
   而它只在真有人调用那类额度时才发生）；
4. `/api/quota` 的响应为每种额度带槽位键，并带 `labels`。

**曾经有两条现在恒真的守卫，已删**：「`_QUOTA_LABELS` 键集 == 注册表」
与「`QuotaUpdateRequest` 字段集 == 注册表」。它们守的是手工登记的两份
拷贝，而 ADR 0033 把这两份并进了注册表——构造即保证，留着只会给人
「有守卫」的错觉。真正还在起作用的是上面这场结构扫描。

为什么用 AST（本仓踩过的坑，不是假想）
--------------------------------------
天真的写法是搜文本：`re.findall(r'"(parse|chat)"', src)`。它有三个各自
独立的漏法：不认归属（`kind == "parse"` 这种用法也算命中）、数次数而非
集合（换个顺序就不变）、跨不过行尾（`database.py` 曾是 CRLF/LF 混合工作区，
多行 anchor 差一个 `\r` 就 0 次命中，而「0 次命中」看着像文件里没这段代码）。

AST 认的是**这个字面量是不是一份「种类集合」**：节点是
`Tuple/List/Set/Dict` 且元素/键是字符串常量，与排版、行尾、所在文件无关。
只扫 `For` 与 `Assign` 不够——第一版探针就漏了 `return {...}`（Dict）与
pydantic 的字段声明（AnnAssign），所以 `find_kind_enumerations` **遍历每一个节点**。
守卫最坏的失败模式是漏报。
"""
import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
AUTHORITY_PY = BACKEND / "quota_kinds.py"

#: 扫描范围：backend/ 下的生产代码。`tests/` 与 `.scratch/` 排除。
SCAN_SKIP_DIRS = {"venv", "__pycache__", ".pytest_cache", "node_modules", "tests",
                  ".scratch", "data"}


def read_source(path: Path) -> str:
    """保行尾读。本文件不做行尾断言（见文件头「跨不过行尾」那条坑）。"""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _fstring_literal_text(node: ast.JoinedStr) -> str:
    """把 f-string 里的字面量片段拼出来：`f"{kind}_limit"` → `_limit`。"""
    return "".join(v.value for v in node.values
                   if isinstance(v, ast.Constant) and isinstance(v.value, str))


# ── 真值：从生产代码导入，而不是在这里再抄一份 ──────────────────
import os  # noqa: E402

os.environ.setdefault(
    "JWT_SECRET", "test-only-jwt-secret-0123456789abcdef0123456789abcdef"
)
if str(BACKEND) not in os.sys.path:
    os.sys.path.insert(0, str(BACKEND))

import admin_api  # noqa: E402
import api_summarize  # noqa: E402
import database  # noqa: E402
import quota_kinds  # noqa: E402

KIND_NAMES = frozenset(quota_kinds.QUOTA_KINDS)


def find_kind_enumerations(source: str, kinds=KIND_NAMES) -> list:
    """返回 [Site, ...]，凡容器字面量里含额度名就算。

    `Site` = {lineno, names, node, func, target}，`func` / `target` 是最近一层
    的函数名与赋值目标——报错时要知道是**哪一处**，否则一条「有 3 处违规」
    的失败消息等于让人自己再扫一遍。

    认的是「容器字面量的元素/键里有额度名」，所以这些都抓得到：
        for kind in ("parse", "chat"):
        _REGISTRY = {"parse": ...}
        return {"parse": None, "chat": None}
        d = {f"{kind}_limit": 1}          # f-string 键也记一笔
    而 `kind == "parse"`（比较）与注释里的 "parse" 都不算。
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node, kinds_):
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, kinds_):
                return cur
            cur = parents.get(cur)
        return None

    def func_of(node):
        fn = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        return fn.name if fn is not None else "<module>"

    def target_of(node):
        owner = enclosing(node, (ast.Assign, ast.AnnAssign))
        if owner is None:
            return None
        if isinstance(owner, ast.AnnAssign):
            return getattr(owner.target, "id", None)
        names = [t.id for t in owner.targets if isinstance(t, ast.Name)]
        return names[0] if names else None

    def names_of(node):
        out = set()
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            for el in node.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    out.add(el.value)
                elif isinstance(el, ast.JoinedStr):
                    out.add(_fstring_literal_text(el))
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    continue
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    out.add(key.value)
                elif isinstance(key, ast.JoinedStr):
                    out.add(_fstring_literal_text(key))
        return out

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.List | ast.Set | ast.Dict):
            continue
        found = names_of(node)
        if found & kinds:
            hits.append({
                "lineno": node.lineno,
                "names": sorted(found & kinds),
                "node": type(node).__name__,
                "func": func_of(node),
                "target": target_of(node),
            })
    return hits


class TestNoThirdEnumerationExists:
    """核心结构守卫：注册表之外不许再枚举一遍额度种类。

    这条让「加第三种额度」只需要动注册表（加一条记录 + 迁移三列），
    也在有人重新硬写 `("parse", "chat")` 时立刻转红。
    """

    def _scan(self):
        found = []
        for path in sorted(BACKEND.rglob("*.py")):
            rel_parts = path.relative_to(BACKEND).parts
            if any(part in SCAN_SKIP_DIRS for part in rel_parts):
                continue
            if path == AUTHORITY_PY:
                continue  # 注册表自己，由 test_authority_constant_is_the_only_dict 单独把关
            rel = path.relative_to(BACKEND).as_posix()
            found.extend((rel, site) for site in find_kind_enumerations(read_source(path)))
        return found

    def test_only_the_registry_may_enumerate(self):
        offenders = self._scan()
        assert not offenders, (
            "额度种类在 quota_kinds.QUOTA_KINDS 之外又被枚举了一遍"
            "（加第三种额度时这里会静默漏改）：\n"
            + "\n".join(
                f"  {rel}:{s['lineno']}  {s['node']}  {s['names']}"
                f"  （函数 {s['func']}）" for rel, s in offenders)
            + "\n\n修法：改成遍历/查询 quota_kinds.QUOTA_KINDS。"
        )

    def test_authority_constant_is_the_only_dict(self):
        """注册表自身必须是那一个 dict 字面量。

        防止有人「顺手」把它拆成两个 dict、或改成 comprehension——那会让
        上面那条扫描的参照系悄悄换掉（扫描器就再也看不见注册表本身了）。
        """
        hits = find_kind_enumerations(read_source(AUTHORITY_PY))
        assert len(hits) == 1, (
            f"quota_kinds.py 里出现了 {len(hits)} 处额度种类枚举，应恰好 1 处（注册表）：\n"
            + "\n".join(f"  第 {h['lineno']} 行 {h['node']} {h['names']}" for h in hits)
        )
        assert hits[0]["node"] == "Dict", f"注册表必须是 dict 字面量，实得 {hits[0]['node']}"
        assert hits[0]["target"] == "QUOTA_KINDS", (
            f"注册表那个赋值的名字变了（实得 {hits[0]['target']!r}）——"
            "守卫的参照系换了，这条要重新评估"
        )

    def test_scanner_detects_a_hardcoded_loop(self):
        """守卫的正例：给它一份硬编码循环，它必须报出来。

        没有这条，把 `find_kind_enumerations` 改成恒返回 [] 就能让上面
        全部转绿，而全量测试照样通过。
        """
        synthetic = (
            "def admin_set_quota(kind):\n"
            "    for kind in ('parse', 'chat'):\n"
            "        pass\n"
        )
        hits = find_kind_enumerations(synthetic)
        assert hits and hits[0]["names"] == ["chat", "parse"], f"没认出硬编码循环：{hits}"
        assert hits[0]["lineno"] == 2, f"行号算错了：{hits}"
        assert hits[0]["func"] == "admin_set_quota", f"归属认错了：{hits}"

    def test_scanner_detects_the_return_literal_the_ticket_missed(self):
        """第一版探针漏掉的那处：`return {...}`，不是 For/Assign。

        这条钉住「遍历每一个节点」这个决定本身——退回只扫 For/Assign 时它会红。
        """
        synthetic = (
            "def get_quota(user):\n"
            "    if not user:\n"
            "        return {'logged_in': False, 'parse': None, 'chat': None}\n"
        )
        hits = find_kind_enumerations(synthetic)
        assert hits, "漏掉了 return {...} 字面量——扫描器退化成只扫 For/Assign 了？"
        assert hits[0]["lineno"] == 3, f"行号算错了：{hits}"
        assert hits[0]["func"] == "get_quota", f"归属认错了：{hits}"

    def test_scanner_ignores_a_comparison_against_one_kind(self):
        """`kind == "parse"` 是**用法**不是枚举，不该被算进来。

        少这一条，扫描器会因为误报而被人加豁免，豁免一多就等于没有守卫。
        """
        synthetic = (
            "def f(kind):\n"
            "    if kind == 'parse':\n"
            "        return 1\n"
            "    return 0\n"
        )
        assert find_kind_enumerations(synthetic) == [], (
            "把单个额度名的比较算成枚举了——会逼人加豁免，豁免一多守卫就废了"
        )

    def test_scanner_ignores_kind_names_inside_fstrings_and_calls(self):
        """`f"{kind}_limit"` 与 `check_quota_kind(u, "parse")` 都不是枚举。"""
        synthetic = (
            "def f(kind, u):\n"
            "    field = f'{kind}_limit'\n"
            "    return check_quota_kind(u, 'parse')\n"
        )
        assert find_kind_enumerations(synthetic) == [], (
            f"把用法算成枚举了：{find_kind_enumerations(synthetic)}"
        )


class TestRegistryRecordsAreUsable:
    """注册表里的每一行都要真的能拼进 SQL、真的能进文案。"""

    def test_labels_are_nonempty_strings(self):
        """中文名不许是空串或空白：空串在界面上与「没登记」表现相同。"""
        blank = sorted(k for k, v in quota_kinds.labels().items()
                       if not isinstance(v, str) or not v.strip())
        assert not blank, f"这些额度的中文名是空的：{blank}"

    def test_registry_columns_exist_in_the_users_table(self, db):
        """拼错的列名只在真有人调用那类额度时才报 `no such column`。

        注册表是 SQL 列名的唯一来源，所以拿它对着真库核一遍——这是唯一
        能在「没人调用」的路径上提前红的判据。
        """
        with database.get_db() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        wanted = {c for r in quota_kinds.QUOTA_KINDS.values()
                  for c in (r.count_col, r.date_col, r.override_col)}
        missing = sorted(wanted - cols)
        assert not missing, f"users 表上没有这些列：{missing}"

    def test_spec_rejects_an_unknown_kind(self):
        """拼错的 kind 立刻抛错，而不是静默回落全局。"""
        with pytest.raises(ValueError) as exc:
            quota_kinds.spec("no_such_kind")
        assert "no_such_kind" in str(exc.value)

    def test_missing_kind_fails_loudly_at_call_time(self):
        """实测「拼错的 kind」在**真调用时**是什么形状：ValueError。

        它响亮，但响亮的代价是 500 而非 400，且只在有人调用时才响——
        所以它不能替代注册表本身唯一这条结构约束。
        """
        with pytest.raises(ValueError) as exc:
            api_summarize._check_quota_permission(None, "no_such_kind")
        assert "no_such_kind" in str(exc.value)


class TestQuotaUpdateRequest:
    """请求体字段由注册表生成；未声明的键仍被静默丢弃（既有协议行为）。"""

    def test_undeclared_kind_is_silently_dropped(self, db, make_user):
        """传一个不存在的额度名：请求 200、什么都不改。

        这钉的是**协议行为**，不是实现细节——管理员拼错键名时没有报错，
        所以前端才要发它认识的那些键。判据与「字段怎么生成」无关。
        """
        uid = make_user()
        payload = admin_api.QuotaUpdateRequest(**{
            "parse_limit": 5, "undeclared_limit": 7,
        })
        dumped = payload.model_dump(exclude_unset=True)
        assert "undeclared_limit" not in dumped, (
            "pydantic 不再静默丢弃未声明的键了——协议行为变了，"
            "前端与文档都要跟着评估（也许是该加 extra='forbid' 让它响亮地报错）"
        )
        assert dumped == {"parse_limit": 5}


class TestPayloadCarriesEveryKind:
    """`/api/quota` 的响应必须为每种额度带一个槽位键，并带中文名。

    这条不依赖任何 AST 判据，直接打真实函数——它守的是外部可观察的形状。
    """

    def test_anonymous_payload_has_a_slot_per_kind(self, db):
        import asyncio

        body = asyncio.run(api_summarize.get_quota(user=None))
        missing = sorted(k for k in quota_kinds.QUOTA_KINDS if k not in body)
        assert not missing, (
            f"匿名 /api/quota 少了这些额度的槽位键: {missing}\n"
            f"  实际键: {sorted(body)}"
        )
        assert body["labels"] == quota_kinds.labels()

    def test_logged_in_payload_has_a_slot_per_kind(self, db, make_user):
        import asyncio

        uid = make_user()
        body = asyncio.run(
            api_summarize.get_quota(user={"id": uid, "email": "a@example.com"})
        )
        missing = sorted(k for k in quota_kinds.QUOTA_KINDS if k not in body)
        assert not missing, (
            f"登录 /api/quota 少了这些额度的槽位键: {missing}\n"
            f"  实际键: {sorted(body)}"
        )

    def test_payload_labels_match_the_registry(self, db, make_user):
        """中文名随额度下发，且**就是注册表那一份**（ADR 0033）。

        前端按这个键序渲染额度行，所以顺序也算契约：dict 保插入序，
        `labels()` 按注册表顺序产出。
        """
        import asyncio

        uid = make_user()
        body = asyncio.run(
            api_summarize.get_quota(user={"id": uid, "email": "a@example.com"})
        )
        assert body["labels"] == quota_kinds.labels(), (
            f"payload 的 labels 与注册表不一致：{body.get('labels')!r}"
            f" ≠ {quota_kinds.labels()!r}"
        )
        assert list(body["labels"]) == list(quota_kinds.QUOTA_KINDS), (
            "labels 的键序必须是注册表顺序——前端按它渲染行"
        )

    def test_admin_user_item_has_a_block_per_kind(self, db, make_user):
        """后台用户行也必须为每种额度给出 4 个派生字段。"""
        uid = make_user()
        row = database.admin_user_detail(uid)
        suffixes = ("_used", "_limit", "_limit_override", "_limit_source")
        missing = [f"{k}{s}" for k in quota_kinds.QUOTA_KINDS for s in suffixes
                   if f"{k}{s}" not in row]
        assert not missing, f"后台用户行缺这些字段: {missing}"
