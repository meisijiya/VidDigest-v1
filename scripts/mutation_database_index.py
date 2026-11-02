"""变异确认：`database.py` 概念索引这一关必须真的有牙。

**为什么要有这个脚本**：`scripts/check_database_index.py` 的判据是
「相邻两行首尾相接」，一个写错的实现很容易变成**永远为真**——
比如只检查「起点名存在」而不检查顺序，那么新增函数、漏登一段、
顺序写反三种情况都会放行。这个脚本按注入形态逐条确认它会红，
并确认无关改动（加注释、改别的文档）保持绿，即关卡没有越权。

**判据**：每个「该红」的形态都让关卡非零退出；每个「该绿」的形态都让它退出 0；
每次注入后把目标文件按字节还原并核对一致。
"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from venv_python import venv_python

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = venv_python()
CHECKER = os.path.join(REPO, "scripts", "check_database_index.py")

SOURCE = "backend/database.py"
DOC = "docs/TECHNICAL.md"
OTHER_DOC = "docs/OPERATIONS.md"

MIGRATE_ROW = "`_migrate_invite_codes_table` → `_migrate_history_favorite_column`"
QUOTA_ROW = "`get_user_by_email` → `refund_quota`"

# (用例名, 目标文件, 注入方式, 注入内容, 期望关卡失败)
CASES = [
    (
        "D1 源码新增一个顶层函数而索引表没跟着登记",
        SOURCE,
        "append",
        "\n\ndef _mutation_probe_helper():\n    return None\n",
        True,
    ),
    (
        "D2 某段的起点后移（两段之间漏登）",
        DOC,
        "replace",
        (MIGRATE_ROW, "`_migrate_quota_columns` → `_migrate_history_favorite_column`"),
        True,
    ),
    (
        "D3 起点晚于终点",
        DOC,
        "replace",
        (QUOTA_ROW, "`refund_quota` → `get_user_by_email`"),
        True,
    ),
    (
        "D4 领域词不在 GLOSSARY 里",
        DOC,
        "replace",
        ("| 额度种类 |", "| 额度种类X |"),
        True,
    ),
    (
        "D5 反向对照：给已有函数加注释行（不新增顶层函数）必须放行",
        SOURCE,
        "replace",
        (
            "def init_db():",
            "# 建表函数：全部 CREATE TABLE / CREATE INDEX 只在这里出现。\ndef init_db():",
        ),
        False,
    ),
    (
        "D6 反向对照：改别的文档必须放行",
        OTHER_DOC,
        "append",
        "\n<!-- 与 database.py 概念索引无关的一行 -->\n",
        False,
    ),
]


def run_checker():
    proc = subprocess.run([PY, CHECKER], capture_output=True, cwd=REPO)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode(
        "utf-8", "replace"
    )


def inject(rel, mode, payload):
    path = os.path.join(REPO, rel)
    with open(path, "rb") as fh:
        original = fh.read()
    if mode == "append":
        with open(path, "ab") as fh:
            fh.write(payload.encode("utf-8"))
        return path, original
    old, new = payload
    text = original.decode("utf-8")
    if text.count(old) != 1:
        raise RuntimeError("注入点不唯一或不存在：%r（出现 %d 次）" % (old, text.count(old)))
    with open(path, "wb") as fh:
        fh.write(text.replace(old, new, 1).encode("utf-8"))
    return path, original


def restore(path, original):
    with open(path, "wb") as fh:
        fh.write(original)
    with open(path, "rb") as fh:
        return fh.read() == original


print("=" * 70)
print("变异确认：database.py 概念索引关卡是否有牙")
print("=" * 70)

fails = []

code, out = run_checker()
if code != 0:
    print("基线: 关卡自己就是红的，先修关卡")
    print(out)
    sys.exit(2)
print("基线: 绿（%s）" % out.strip().splitlines()[-1])

for name, rel, mode, payload, expect_fail in CASES:
    path, original = inject(rel, mode, payload)
    try:
        code, out = run_checker()
    finally:
        same = restore(path, original)
    verdict = "红" if code != 0 else "绿"
    want = "红" if expect_fail else "绿"
    ok = verdict == want and same
    print("")
    print("%s %s" % ("OK  " if ok else "FAIL", name))
    print("     期望 %s / 实际 %s / 还原 %s" % (want, verdict, "一致" if same else "不一致"))
    if not ok:
        fails.append(name)
        for line in out.strip().splitlines()[:6]:
            print("     " + line.strip())

code, out = run_checker()
print("")
print("收尾: %s（%s）" % ("绿" if code == 0 else "红", out.strip().splitlines()[-1]))

print("")
print("=" * 70)
if fails or code != 0:
    print("RESULT: 关卡不可信，共 %d 项不合格" % (len(fails) + (0 if code == 0 else 1)))
    sys.exit(1)
print("RESULT: 每个注入形态都按预期转红，反向对照保持绿，还原后逐字节一致")
sys.exit(0)
