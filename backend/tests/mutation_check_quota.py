"""变异核验：额度拆分与失败回滚的守护网（工单 #4）。

复用 #2 建立的按字节变异框架，聚焦本工单引入的新机制：
双计数器隔离、跨天各自重置、环境变量上限、失败回滚。

绿灯不算证据——每个变异都必须让测试转红。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from venv_python import venv_python

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
PY = venv_python()
DB_FILE = BACKEND / "database.py"
ROUTES_FILE = BACKEND / "api_summarize.py"
KINDS_FILE = BACKEND / "quota_kinds.py"

DATA = ["tests/test_quota_split.py"]
ROUTES = ["tests/test_quota_routes.py", "tests/test_summarize_routes.py"]
#: 额度种类注册表的守卫（结构扫描 + payload 的 labels）住在这里。
GUARD = ["tests/test_quota_kinds_single_source.py"]


def _loose_find(text: str, pattern: str):
    """在 text 里找 pattern，忽略空白差异，返回 (start, end) 或 None。

    非空白字符仍要逐字相同、顺序不变，只允许它们之间多出空白（换行、缩进、
    逗号后加空格……）。这些是格式化工具造成的样子，不是代码变了。

    刻意不用正则：把 \s* 挂在每个字符后面会让回溯指数爆炸，源文件一大
    就能把进程卡死。这里是双指针扫描，复杂度 O(n*m)，没有回溯。
    """
    chars = [c for c in pattern if not c.isspace()]
    if not chars:
        return (0, 0) if not text.strip() else None
    n = len(text)
    for start in range(n):
        if text[start] != chars[0]:
            continue
        ti = start
        ok = True
        for c in chars:
            while ti < n and text[ti].isspace():
                ti += 1
            if ti >= n or text[ti] != c:
                ok = False
                break
            ti += 1
        if ok:
            return (start, ti)
    return None


def mutate_bytes(data: bytes, pattern: str, replacement: str = "") -> bytes:
    """按行尾符不敏感的方式替换；未命中时原样返回（不抛不猜）。"""
    crlf = b"\r\n" in data
    text = data.decode("utf-8")
    if crlf:
        text = text.replace("\r\n", "\n")
    if pattern not in text:
        # 精确匹配落空时再试一次「空白宽松」：重排版不算代码变了。
        span = _loose_find(text, pattern)
        if span is None:
            return data  # 未命中：原样返回，让调用方识别
        text = text[: span[0]] + replacement + text[span[1] :]
    else:
        text = text.replace(pattern, replacement)
    if crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


MUTATIONS = [
    # ── 双计数器隔离 ──
    ("Q1 parse 与 chat 指向同一个计数字段", KINDS_FILE,
     '"parse": QuotaKind(\n        "daily_parse_count", "last_parse_date", "DAILY_PARSE_LIMIT",',
     '"parse": QuotaKind(\n        "daily_chat_count", "last_parse_date", "DAILY_PARSE_LIMIT",',
     DATA),
    ("Q2 扣减写错字段：parse 写到 chat 列", DB_FILE,
     "            f\"UPDATE users SET {count_col} = 1, {date_col} = ? \"\n"
     "            f\"WHERE id = ? AND {date_col} IS NOT ?\",",
     "            \"UPDATE users SET daily_chat_count = 1, \" + date_col + \" = ? WHERE id = ?\",",
     DATA),
    ("Q3 两个上限用同一个值", DB_FILE,
     'DAILY_CHAT_LIMIT = _env_int("VIDDIGEST_DAILY_CHAT_LIMIT", 10)',
     'DAILY_CHAT_LIMIT = _env_int("VIDDIGEST_DAILY_CHAT_LIMIT", 3)',
     DATA),

    # ── 跨天重置隔离 ──
    ("Q4 判定不看日期字段，永远读当前计数", DB_FILE,
     "        if user[date_col] != today:\n            return True, limit\n",
     "",
     DATA),
    ("Q5 两个计数器共用同一个日期字段", KINDS_FILE,
     '"chat": QuotaKind(\n        "daily_chat_count", "last_chat_date", "DAILY_CHAT_LIMIT",',
     '"chat": QuotaKind(\n        "daily_chat_count", "last_parse_date", "DAILY_CHAT_LIMIT",',
     DATA),

    # ── 环境变量上限 ──
    ("Q6 上限读成导入时冻结的值（改 env 不再生效）", DB_FILE,
     "    return globals()[quota_kinds.spec(kind).limit_const], \"global\"",
     "    return globals().get('_frozen_limits', {}).get(kind, 3)",
     DATA),
    ("Q7 环境变量写错时直接崩而不是退回默认", DB_FILE,
     "    try:\n        return int(raw)\n    except ValueError:",
     "    if True:\n        return int(raw)\n    if False:",

     DATA),

    # ── 失败回滚 ──
    # 回滚点已从 except 分支收进 finally（单一回滚点，断流不白扣）。
    # 因此这两条变异指向 finally 里的那个 if，而不是旧的 except 内联块——
    # 模式串照着旧结构写，命中数会是 0，脚本还会报「未命中」而不是「存活」。
    ("Q8 模型失败不回滚（白扣）", ROUTES_FILE,
     '        if quota_spent:\n            refund_quota(user["id"], "parse")',
     '        if False:\n            refund_quota(user["id"], "parse")',
     ROUTES),
    ("Q9 追问失败不回滚（白扣）", ROUTES_FILE,
     '        if quota_spent:\n            refund_quota(user["id"], "chat")',
     '        if False:\n            refund_quota(user["id"], "chat")',
     ROUTES),
    ("Q10 回滚扣错计数器", ROUTES_FILE,
     "        if quota_spent:\n            refund_quota(user[\"id\"], \"parse\")",
     "        if quota_spent:\n            refund_quota(user[\"id\"], \"chat\")",
     ROUTES),
    ("Q11 回滚反了向：把额度越还越多", DB_FILE,
     '            f"UPDATE users SET {count_col} = MAX(COALESCE({count_col}, 0) - 1, 0) "\n'
     '            f"WHERE id = ?",',
     '            f"UPDATE users SET {count_col} = COALESCE({count_col}, 0) + 1 "\n'
     '            f"WHERE id = ?",',
     DATA),
    ("Q12 跨天也回滚（给今天白送额度）", DB_FILE,
     "        if user[date_col] != today:\n            return QUOTA_UNLIMITED if limit < 0 else limit",
     "        if False:\n            return limit",
     DATA),

    # ── 列迁移 ──
    ("Q13 迁移不执行（老库缺列直接崩）", DB_FILE,
     "        _migrate_quota_columns(conn)",
     "        pass",
     DATA),
    ("Q14 迁移不判列是否存在（重复 init 必报 duplicate column）", DB_FILE,
     "        if column not in existing:\n            conn.execute(ddl)",
     "        conn.execute(ddl)",
     DATA),
    ("Q15 迁移原地删除旧列（违背 expand 原则）", DB_FILE,
     '        ("daily_chat_count", "ALTER TABLE users ADD COLUMN daily_chat_count INTEGER DEFAULT 0"),',
     '        ("daily_chat_count", "ALTER TABLE users DROP COLUMN daily_summary_count"),',
     DATA),

    # ── 以下 6 条来自独立复审：它们在修复前全部存活 ──
    ("Q16 parse 上限的环境变量名拼错", DB_FILE,
     'DAILY_PARSE_LIMIT = _env_int("VIDDIGEST_DAILY_PARSE_LIMIT", 3)',
     'DAILY_PARSE_LIMIT = _env_int("VIDDIGEST_PARSE_LIMIT", 3)',
     DATA),
    ("Q17 parse 上限被改回硬编码（环境变量彻底失效）", DB_FILE,
     'DAILY_PARSE_LIMIT = _env_int("VIDDIGEST_DAILY_PARSE_LIMIT", 3)',
     "DAILY_PARSE_LIMIT = 3",
     DATA),
    ("Q18 chat 上限被改回硬编码（环境变量彻底失效）", DB_FILE,
     'DAILY_CHAT_LIMIT = _env_int("VIDDIGEST_DAILY_CHAT_LIMIT", 10)',
     "DAILY_CHAT_LIMIT = 10",
     DATA),
    ("Q19 追问事件的顶层数字误取 parse 计数器", ROUTES_FILE,
     '_quota_payload(user["id"], "chat")',
     '_quota_payload(user["id"], "parse")',
     ROUTES),
    ("Q20 追问事件不再携带 parse / chat 两个对象", ROUTES_FILE,
     'json.dumps(_quota_payload(user["id"], "chat"), ensure_ascii=False)',
     'json.dumps({k: v for k, v in _quota_payload(user["id"], "chat").items()\n'
     '                    if k not in ("parse", "chat")}, ensure_ascii=False)',
     ROUTES),
    ("Q21 回滚退回读-改-写（与并发扣减交叉会丢更新）", DB_FILE,
     '            f"UPDATE users SET {count_col} = MAX(COALESCE({count_col}, 0) - 1, 0) "\n'
     '            f"WHERE id = ?",\n'
     "            (user_id,),\n"
     "        )\n"
     '        row = conn.execute(\n'
     '            f"SELECT {count_col} FROM users WHERE id = ?", (user_id,)\n'
     "        ).fetchone()\n"
      "        return QUOTA_UNLIMITED if limit < 0 else limit - (row[count_col] or 0)",
     '            f"UPDATE users SET {count_col} = ? WHERE id = ?",\n'
     "            (max(0, (user[count_col] or 0) - 1), user_id),\n"
     "        )\n"
     "        return limit - max(0, (user[count_col] or 0) - 1)",
     DATA),
    # 错误文案改成了按 fail_reason 分派（工单 #3），锚点跟着挪到新形状上。
    ("Q22 无字幕也扣额度（旧断言读废弃列时恒真）", ROUTES_FILE,
     '        if not subtitle_data["has_subtitle"]:\n'
     '            head, reason, asr_reason = _subtitle_failure(subtitle_data)\n',
     '        if not subtitle_data["has_subtitle"]:\n'
     '            consume_quota(user["id"], "parse")\n'
     '            head, reason, asr_reason = _subtitle_failure(subtitle_data)\n',
     ROUTES),

    # ── 额度种类的唯一出处（ADR 0033）──
    # 中文名若在 payload 里另抄一份，注册表就不再是唯一出处：前端按它渲染，
    # 于是「加了一种额度」在界面上仍会静默少一行。
    # 判据在 GUARD 里（payload 的 labels == 注册表），不在路由测试里——
    # 第一次跑这条变异时把测试集写成 ROUTES，结果**存活**：路由测试根本不看 labels。
    ("Q23 labels 不再取自注册表（协议里另抄一份中文名）", ROUTES_FILE,
     '"labels": labels(),',
     '"labels": {"parse": "解析"},',
     GUARD),
]

_COUNT_RE = re.compile(r"(\d+) (passed|failed|error)", re.IGNORECASE)


def run(args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [str(PY), "-m", "pytest", *args, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return r.returncode, r.stdout


def counts(out):
    lines = out.strip().splitlines()
    summary = lines[-1] if lines else out
    p = sum(int(m.group(1)) for m in _COUNT_RE.finditer(summary) if m.group(2) == "passed")
    f = sum(int(m.group(1)) for m in _COUNT_RE.finditer(summary) if m.group(2) in ("failed", "error"))
    return p, f, summary.strip()


def main():
    code, out = run(["tests"])
    if code != 0:
        print("FAIL: 基线不绿，无法评估变异")
        print(out[-2000:])
        return 9
    print(f"基线全量: {counts(out)[2]}\n")

    targets = sorted({tuple(m[4]) for m in MUTATIONS}, key=str)
    for t in targets:
        _, out = run(list(t))
        p, f, s = counts(out)
        if p + f == 0:
            print(f"FAIL: 目标 {t} 收集到 0 个测试，变异评估无意义")
            return 9
        print(f"目标 {' '.join(t)}: {p + f} 条可收集")
    print()

    originals = {f: f.read_bytes() for f in {m[1] for m in MUTATIONS}}
    survived, not_run = [], []

    try:
        for name, target_file, pattern, repl, pytest_args in MUTATIONS:
            source = originals[target_file]
            mutated = mutate_bytes(source, pattern, repl)
            if mutated == source:
                print(f"  [未命中] {name} —— 模式没匹配上，变异无效")
                not_run.append(name)
                continue
            target_file.write_bytes(mutated)
            try:
                _, out = run(pytest_args)
            finally:
                target_file.write_bytes(source)

            p, f, _ = counts(out)
            if p + f == 0:
                print(f"  [没跑] {name} —— 收集到 0 个测试，结果无效")
                not_run.append(name)
            elif f > 0:
                print(f"  [杀死] {name}  ({f} 条转红)")
            else:
                print(f"  [存活] {name}  <-- 测试守不住")
                survived.append(name)
    finally:
        for f, content in originals.items():
            f.write_bytes(content)

    dirty = [f.name for f, c in originals.items() if f.read_bytes() != c]
    if dirty:
        print(f"\nFAIL: 还原后文件与原始不一致：{dirty}")
        return 9
    print(f"\n还原自检: {len(originals)} 个文件均逐字节一致"
          f"（{', '.join(sorted(f.name for f in originals))}）")

    print()
    if survived or not_run:
        print(f"结论: {len(survived)} 个存活 / {len(not_run)} 个无效 —— 额度守护网有缺口")
        for s in survived:
            print(f"  - 存活: {s}")
        for s in not_run:
            print(f"  - 无效: {s}")
        return 1
    print(f"结论: {len(MUTATIONS)} 个变异全部被杀死，额度拆分守得住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
