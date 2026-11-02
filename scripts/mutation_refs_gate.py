"""变异确认：引用一致性这两道关卡必须真的有牙。

按纪律，「跑完是绿的」不能证明关卡有效——必须把每一种要抓的形态故意放回去，
确认它转红，再确认原样恢复。只验当前状态 = 没验。

每个用例都断言两件事：
  1. 注入后关卡退出码为 1（不是 review，不是 0）
  2. 恢复后文件逐字节等于注入前，且关卡回到 0

第 2 条同样重要：注入动作本身可能把文件写坏，而「恢复失败」在只查退出码时看不见。

**每个用例都配一条反向对照**：不该管的改动必须保持绿。
守卫越权（该绿的红了）比变异存活更严重。
"""
import os
import subprocess
import sys

from venv_python import venv_python

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = venv_python()
CHECKERS = {
    "refs": os.path.join(REPO, "scripts", "check_refs.py"),
    "gate": os.path.join(REPO, "scripts", "check_gate_consistency.py"),
}

MAIN = "backend/main.py"
AGENTS = "AGENTS.md"
INIT = "init.sh"

#: `init.sh` 现在真跑几关——**从 init.sh 数**，不写死数字。
#:
#: 写死过一次：门禁从 11 关变 12 关（ADR 0035 加了概念索引那一关），
#: 而这里 G3 注入的还是「跑11件」→ 关卡一致性判它与真实关卡数不符 → 转红，
#: G3 这条「写对必须放行」的反向对照自己先死了。数字会变，所以从源头数。
GATE_COUNT = 0
with open(os.path.join(REPO, INIT), "r", encoding="utf-8") as fh:
    GATE_COUNT = fh.read().count("RAN=$((RAN + 1))")

# (用例名, 检查器, 目标文件, 注入方式, 注入内容, 期望关卡失败)
CASES = [
    ("R1 注释里引用不存在的 ADR 编号",
     "refs", MAIN, "append", "\n# 参见 ADR 9999\n", True),
    ("R2 注释里引用不存在的文件路径",
     "refs", MAIN, "append", "\n# 参见 backend/definitely_missing.py:12\n", True),
    ("R3 反向对照：合法引用必须放行",
     "refs", MAIN, "append",
     "\n# 参见 ADR 0006 与 docs/adr/0006-first-parse-wins.md\n", False),
    ("R4 反向对照：字符串字面量里的路径不查",
     "refs", MAIN, "append",
     "\n_NOT_A_DOC_REF = 'backend/definitely_missing.py'\n", False),
    ("R5 中文文件名：指向不存在的文件必须转红",
     "refs", MAIN, "append",
     "\n# 参见 docs/AI功能核心技术说明X.md\n", True),
    ("R6 反向对照：真实存在的中文文件名必须放行",
     "refs", MAIN, "append",
     "\n# 参见 docs/AI功能核心技术说明.md\n", False),
    ("R7 反向对照：运行期数据目录不查",
     "refs", MAIN, "append",
     "\n# 运行期产物：backend/data/definitely_missing.py\n", False),
    ("R8 反向对照：被 .gitignore 排除的运行期文件不查",
     "refs", MAIN, "append",
     "\n# 本机环境文件：frontend/.env\n", False),
    ("R9 .env 豁免不许放得过宽：模板仍要查",
     "refs", MAIN, "append",
     "\n# 环境模板：frontend/.env.example\n", True),
    ("G1 文档把关卡数写少",
     "gate", AGENTS, "append", "\n它依次跑六件真会失败的事。\n", True),
    ("G2 关卡块自报的序号错位",
     "gate", INIT, "before", "# 第 9 关\n", True),
    ("G3 反向对照：关卡数写对必须放行",
     "gate", AGENTS, "append",
     "\n它依次跑%d件真会失败的事。\n" % GATE_COUNT, False),
    ("G4 反向对照：自报序号正确必须放行",
     "gate", INIT, "before", "# 第 1 关\n", False),
]

# G2 / G4 的注入点：第 1 关的 echo 行
INIT_MARKER = 'echo "=== backend: pytest ==="'


def run_checker(which):
    p = subprocess.run([PY, CHECKERS[which]], capture_output=True, cwd=REPO)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def inject(rel, mode, text):
    path = os.path.join(REPO, rel)
    if mode == "append":
        with open(path, "ab") as fh:      # 字节级追加，不改原内容
            fh.write(text.encode("utf-8"))
        return
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    if INIT_MARKER not in content:
        raise RuntimeError("注入点不存在：" + INIT_MARKER)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content.replace(INIT_MARKER, text + INIT_MARKER, 1))


print("=" * 70)
print("变异确认：引用一致性两道关卡是否有牙")
print("=" * 70)

fails = []

for which in ("refs", "gate"):
    rc, out = run_checker(which)
    print("\n[基线] %s 退出码=%d  期望=0  -> %s"
          % (which, rc, "OK" if rc == 0 else "BAD"))
    if rc != 0:
        fails.append("%s 基线就已经是红的" % which)
        for ln in out.splitlines():
            print("     %s" % ln.strip())

for name, which, rel, mode, text, expect_fail in CASES:
    path = os.path.join(REPO, rel)
    with open(path, "rb") as fh:
        before = fh.read()
    try:
        inject(rel, mode, text)
        rc, out = run_checker(which)
        verdict = "OK" if ((rc == 1) == expect_fail) else "BAD"
        if verdict == "BAD":
            fails.append("%s（期望 fail=%s，实得 rc=%d）" % (name, expect_fail, rc))
        print("\n[%s] %s" % (verdict, name))
        print("     期望 fail=%s，实得 rc=%d" % (expect_fail, rc))
        if expect_fail:
            for ln in out.splitlines()[1:]:
                print("     关卡指认: %s" % ln.strip())
    finally:
        with open(path, "wb") as fh:
            fh.write(before)
        with open(path, "rb") as fh:
            restored = fh.read()
        same = restored == before
        if not same:
            fails.append("%s：恢复后文件与注入前不一致（注入动作写坏了文件）" % name)
        print("     恢复: %s" % ("逐字节一致" if same else "!!! 不一致 !!!"))

for which in ("refs", "gate"):
    rc, out = run_checker(which)
    print("\n[收尾] %s 退出码=%d  期望=0  -> %s"
          % (which, rc, "OK" if rc == 0 else "BAD"))
    if rc != 0:
        fails.append("%s 收尾不是绿的，说明有残留注入" % which)
        for ln in out.splitlines():
            print("     %s" % ln.strip())

print("")
print("=" * 70)
if fails:
    print("RESULT: 关卡不可信，共 %d 项不合格" % len(fails))
    for f in fails:
        print("   - " + f)
    sys.exit(1)
print("RESULT: 每个注入形态都按预期转红，恢复后逐字节一致，基线与收尾均为绿")
sys.exit(0)
