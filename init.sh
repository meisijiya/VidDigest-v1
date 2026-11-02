#!/bin/bash
# VidDigest 验证门禁。退出码 0 之前，任何工单都不得标记为完成。
#
# 跑法（Windows + Git Bash）：
#   cd 'D:\26code\新简历项目\universal-video-downloader'
#   & 'C:\Program Files\Git\bin\bash.exe' ./init.sh
#
# 第一次跑之前先装环境：bash scripts/bootstrap.sh
# （backend/venv 与 frontend/node_modules 都不在版本库里，门禁依赖它们。）
#
# 解释器路径是硬约束，不是风格问题：系统 Python 版本不对，
# 必须用 backend 自己的 venv。AGENTS.md「环境事实」一节记的是同一条。

set -e

echo "=== VidDigest Verification ==="

explain_failure() {
  echo ""
  echo "=== Verification FAILED ==="
  echo "要么基线已经坏了，要么上面的检查真的失败了。"
  echo "先修基线，再重跑 ./init.sh。"
  echo "在 ./init.sh 退出 0 之前，不要把任何工单标记为完成。"
}
trap explain_failure ERR

# 真正跑起来的检查数。计数为 0 时门禁必须失败：
# 一个跑不了任何检查却打印 "Verification Complete" 并退出 0 的门禁不是门禁。
RAN=0

# 解释器路径：Windows 与 Linux/macOS 各一条路，按存在性挑。
#
# 为什么必须挑、而不是写死一条：写死 Windows 那条，Linux/macOS 上 clone
# 下来的仓库连门禁都跑不起来，CI 也就无从谈起——**一个只有作者能跑的
# 门禁，等于没有门禁**。
#
# 为什么挑不到就退出、而不是回退到系统 Python：系统 Python 的版本不对
# （AGENTS.md「环境事实」一节记的是同一条），拿它跑测试，测的是另一个
# 解释器下的行为。
if [ -x "backend/venv/Scripts/python.exe" ]; then
  PY="backend/venv/Scripts/python.exe"
  PY_IN_BACKEND="./venv/Scripts/python.exe"
elif [ -x "backend/venv/bin/python" ]; then
  PY="backend/venv/bin/python"
  PY_IN_BACKEND="./venv/bin/python"
else
  echo "ERROR: 找不到 backend/venv 里的解释器 —— 后端虚拟环境没装好，门禁拒绝通过。"
  echo "先跑 scripts/bootstrap.sh（或在 backend/ 下重建 venv）再重跑。"
  exit 1
fi

echo ""
echo "=== backend: pytest ==="
( cd backend && "$PY_IN_BACKEND" -m pytest tests -q )
RAN=$((RAN + 1))

# 静态检查：编译全部后端源码，跳过 venv 与缓存目录。
# 用的是同一个 venv 解释器，因此不依赖系统 PATH 上的 python 版本。
echo ""
echo "=== backend: compileall ==="
( cd backend && "$PY_IN_BACKEND" -m compileall -q \
    -x '(^|[\\/])(\.?venv|env|node_modules|build|dist|__pycache__)([\\/]|$)' \
    -x 'venv[\\/]' . )
RAN=$((RAN + 1))

# 敏感内容扫描（第 3 关）。
#
# 为什么它必须在门禁里，而不是只写成 .gitignore 里的一条注释：
# .gitignore 是**文件名级**的。它挡得住 .env，挡不住「把 .env 里那条口令
# 抄进 docs/OPERATIONS.md」。本仓库真的发生过一次——TEST_ACCOUNTS.md 按
# 规矩移出了版本库，可同一批明文口令与本机用户名留在了运维手册和一个
# agent 日志目录里，直到推上 PUBLIC 远端。护栏必须在**内容**上。
#
# 这条检查自身有变异测试兜着：每种要抓的形态都故意放回去确认它转红，
# 否则「跑完是绿的」证明不了关卡有效。见 scripts/mutation_secrets_gate.py。
#
# 用 $PY（仓库根相对）而不是 venv 里的相对路径：上面两关跑在 ( cd backend && … )
# 子 shell 里，cwd 改动不外泄，所以这一关仍在仓库根 —— 在仓库根写 ./venv/...
# 会得到 "No such file or directory"，退出码 127。检查脚本自己按 __file__
# 推算仓库根，与 cwd 无关。
echo ""
echo "=== secrets: content-level scan of tracked files ==="
"$PY" scripts/check_secrets.py
RAN=$((RAN + 1))
# ── 前端：测试 ──────────────────────────────────────────────
if [ -f frontend/package.json ]; then
  echo ""
  echo "=== frontend: npm test (静态 / 契约断言) ==="
  ( cd frontend && npm test )
  RAN=$((RAN + 1))

  # 挂载层（工单 #18）。**单独一关**，不是上面那条的一部分：
  # 两套 runner 守的是两类判据，混在一处就看不出是哪一类红了——
  # 而「绿色的那一层」是哪一层，正是工单 #18 要回答的问题。
  #
  # 只收 tests/*.spec.mjs（vitest.config.js 的 include 写死了）。默认
  # include 会顺手吃掉 node:test 的 *.test.mjs，而 node:test 的
  # describe 体抛异常时 vitest 仍退出 0 —— 那会让这一关**恒绿**。
  # 「一条都没跑却报绿」不是可能性，是已实测过的形态。
  echo ""
  echo "=== frontend: npm run test:mount (真挂载) ==="
  ( cd frontend && npm run test:mount )
  RAN=$((RAN + 1))

  # 类型检查（工单 #63 之后接上的三关）。
  #
  # 为什么把类型检查、静态检查、格式检查放进门禁：它们不是"锦上添花的
  # 质量指标"，是**换一种实现就会红**的判据。`.js` 迁到 `.ts` 之后，
  # 一次类型检查能挡住的东西比人眼审 53 个组件快得多。
  #
  # 三关各自独立：typecheck 看类型，lint 看写法与未使用的变量，
  # format:check 看格式。混在一起就看不出是哪一类红了——
  # 而"红色的那一类是哪一类"，正是要回答的问题。
  echo ""
  echo "=== frontend: npm run typecheck (vue-tsc --noEmit) ==="
  ( cd frontend && npm run typecheck )
  RAN=$((RAN + 1))

  echo ""
  echo "=== frontend: npm run lint (eslint) ==="
  ( cd frontend && npm run lint )
  RAN=$((RAN + 1))

  echo ""
  echo "=== frontend: npm run format:check (prettier) ==="
  ( cd frontend && npm run format:check )
  RAN=$((RAN + 1))
else
  echo "ERROR: 找不到 frontend/package.json，门禁拒绝通过。"
  exit 1
fi

# ── 门禁自身：引用与口径 ─────────────────────────────────────
# 下面两关守的是「指令文件与注释本身有没有烂掉」。
#
# 为什么要有它们：本仓库的规约（AGENTS.md）、设计决策（docs/adr/）、
# 测试策略（docs/TESTING-STRATEGY.md）与源码注释之间**互相指着**。
# 指着的那一头被改写或删掉时，**没有任何一条既有用例会红**——
# 用例断言的是行为，不是「你写的这句话还指得对」。
# 这不是假设：frontend/src/App.vue 里那句「不引 vue-router（ADR 0010）」
# 就指着错的那一篇（ADR 0010 记的是管理后台范围与鉴权）。
# 文档与注释腐烂之后**仍在产生约束力**，而它们自己不会报警。
# 这两关把「腐烂」变成「门禁红」。
echo ""
echo "=== refs: ADR 与路径引用一致性 ==="
"$PY" scripts/check_refs.py
RAN=$((RAN + 1))

# 第 10 关：门禁有几关，只有 init.sh 知道。文档里写的数字必须跟着它走。
# 真值从 init.sh 里数出来，不在脚本里硬编码。
echo ""
echo "=== gate: 关卡数口径一致性 ==="
"$PY" scripts/check_gate_consistency.py
RAN=$((RAN + 1))

# 第 11 关：社区检索质量（结构轨 + 质量轨）。
#
# 为什么单独一关，而不是塞进后端 pytest：这一关回答的是
# **「社区搜索还能不能把视频找出来」**。它退化时的症状不是报错，
# 而是「接口照常返 200，只是搜不出东西了」——索引少一列、正文没进索引、
# 触发器漏写一列，三者的症状都是这个。这类退化不会被任何
# 「功能正常」的用例发现：搜索还是能搜，只是搜不全。
#
# 判据在两处，都可复算：语料与阈值同源于 scripts/eval/corpus.json，
# 命令是本地一条 python，无网络、无 key、秒级跑完。
echo ""
echo "=== eval: 社区检索质量（结构 + 召回） ==="
"$PY" scripts/eval/check_search_quality.py
RAN=$((RAN + 1))

# 第 12 关：database.py 的概念索引不许漂。
#
# 为什么这一关存在：database.py 一个文件里 113 个顶层函数、63 个名字被外部导入，
# 它不按表拆开（拆的代价落在 52 个测试文件的 import 面上），导航靠
# docs/TECHNICAL.md §3.4 那张「概念 → 起止函数」的表。表和源码在两处，
# 加了函数表不会自己动；本仓已有前例——check_refs.py 只看行号越界，
# 指错了没人知道。这一关把「索引表漂移」变成门禁红：相邻两行必须首尾相接，
# 于是每个顶层函数恰好属于一行，漏登一个新函数就报错。
echo ""
echo "=== index: database.py 概念索引与源码一致 ==="
"$PY" scripts/check_database_index.py
RAN=$((RAN + 1))

if [ "$RAN" -eq 0 ]; then
  echo ""
  echo "ERROR: 一条检查都没真跑过。"
  echo "这个门禁不能失败，所以它不算门禁。修好上面的分支再重跑。"
  exit 1
fi

echo ""
echo "=== Verification Complete ($RAN checks) ==="
echo ""
echo "下一步："
echo "1. 读 AGENTS.md —— 启动路径、工作规则、完成定义、状态在哪"
echo "2. gh issue list --state open —— 挑一张阻塞边清楚的工单"
echo "3. 只实现那一张，不要越界"
echo "4. 收尾前重跑本脚本；退出 0 才能在工单里留证据"
echo "5. 状态与阻塞边归 to-tickets，会话交接归 handoff，本文件不记录任何进度状态"
