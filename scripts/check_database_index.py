#!/usr/bin/env python
"""`database.py` 概念索引必须与它的真实函数序列一致。

**为什么这一关存在**：`backend/database.py` 有 3700+ 行、113 个顶层函数、
63 个名字被外部导入。它是全仓改动最频繁的文件（近两百次提交里出现最多），
拆成多个文件会牵动 52 个测试文件的 import 面，所以它留在一个文件里，
靠 `docs/TECHNICAL.md` §3.4 的**概念索引**告诉读者「改这件事要看哪一段」。

问题是：索引表和源码是两处，源码加了函数、索引表不会自己跟着动。
本仓对这类问题已有两个前例——`check_refs.py` 只看「行号越界」，
`database.py:235` 指错了没人知道；`AGENTS.md` 曾把关卡数写少一关，
而指令文件里的过期数字会被**执行**，不是被阅读。
所以这里把「索引表漂移」变成门禁红。

**查五件事**：

1. 表里每个起止函数名都真实存在于 `backend/database.py` 的顶层。
2. 起点不晚于终点。
3. 相邻两行必须**首尾相接**：下一行的起点是本行终点的下一个顶层函数。
   这条同时挡住三个洞：两段之间漏登、两段重叠、顺序写反。
4. 第一行从第一个顶层函数开始，最后一行在最后一个顶层函数结束。
   合起来等价于「每个顶层函数恰好属于一行」——新增函数不登记就红。
5. 「领域词」列要么是 `—`，要么是 `GLOSSARY.md` 里真实存在的词条。
"""
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SOURCE = os.path.join(REPO, "backend", "database.py")
DOC = os.path.join(REPO, "docs", "TECHNICAL.md")
GLOSSARY = os.path.join(REPO, "GLOSSARY.md")

# | `连接与并发` | `get_connection_generation` → `get_db` | — | 说明 |
ROW_RE = re.compile(
    r"^\|\s*`[^`]+`\s*\|\s*`([A-Za-z_]\w*)`\s*→\s*`([A-Za-z_]\w*)`\s*"
    r"\|\s*([^|]+?)\s*\|"
)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def top_level_defs():
    tree = ast.parse(read(SOURCE))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def index_rows():
    rows = []
    for line in read(DOC).splitlines():
        match = ROW_RE.match(line)
        if match:
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def glossary_terms():
    terms = set()
    for match in re.finditer(r"^\*\*(.+?)（", read(GLOSSARY), re.M):
        terms.add(match.group(1))
    for match in re.finditer(r"^\*\*(.+?)\*\*", read(GLOSSARY), re.M):
        terms.add(match.group(1))
    return terms


def main():
    defs = top_level_defs()
    if not defs:
        sys.stderr.write("ERROR: 从 %s 里读不出任何顶层函数，判据本身坏了。\n" % SOURCE)
        return 2

    rows = index_rows()
    if not rows:
        sys.stderr.write(
            "ERROR: 在 docs/TECHNICAL.md 里找不到概念索引表，"
            "表头形如 `| 段 | `起点` → `终点` | 领域词 | 说明 |`。\n"
        )
        return 2

    problems = []
    position = {name: i for i, name in enumerate(defs)}
    terms = glossary_terms()

    for name, _end, _word in rows:
        if name not in position:
            problems.append("起止函数里的 `%s` 不是 backend/database.py 的顶层函数" % name)

    resolved = []
    for start, end, word in rows:
        if start not in position or end not in position:
            continue
        if position[start] > position[end]:
            problems.append("`%s` → `%s`：起点晚于终点" % (start, end))
            continue
        resolved.append((start, end, word))

    if resolved:
        if position[resolved[0][0]] != 0:
            problems.append(
                "第一行从 `%s` 开始，但它前面的 `%s` 等函数没有登记"
                % (resolved[0][0], defs[0])
            )
        if position[resolved[-1][1]] != len(defs) - 1:
            problems.append(
                "最后一行在 `%s` 结束，但它后面的顶层函数没有登记" % resolved[-1][1]
            )
        for prev, cur in zip(resolved, resolved[1:]):
            expected = prev[1]
            actual = cur[0]
            if position[actual] != position[expected] + 1:
                problems.append(
                    "`%s` → `%s` 之后应接 `%s`，实际从 `%s` 开始："
                    "两段之间漏登、重叠或顺序写反"
                    % (prev[0], expected, defs[position[expected] + 1], actual)
                )

    for _start, _end, word in rows:
        if word == "—":
            continue
        if word not in terms:
            problems.append(
                "领域词「%s」不在 GLOSSARY.md 里：索引表的概念必须用既有词汇，"
                "缺的先补词条" % word
            )

    if problems:
        sys.stderr.write("ERROR: database.py 概念索引与源码不一致：\n")
        for item in problems:
            sys.stderr.write("  - %s\n" % item)
        return 1

    print(
        "database index: OK（%d 段覆盖 %d 个顶层函数）" % (len(rows), len(defs))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
