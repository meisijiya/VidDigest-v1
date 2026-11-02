# AGENTS.md

VidDigest —— 通用视频解析 / 下载 + AI 字幕总结工具。
`backend/` FastAPI（Python 3.11）+ `frontend/` Vue 3 + Vite。两个进程独立启动，没有 monorepo 编排。

## 环境事实（实测，勿凭猜测推翻）

- 后端解释器**必须**用 `backend\venv\Scripts\python.exe`（3.11.9），系统 Python 不对。
- `backend/main.py` 用 `uvicorn.run(app, ...)`，**没有 `--reload`**——改 Python 代码必须重启进程。
- 前端 Vite 只监听 IPv6 回环：一律用 `http://localhost:5173`，`127.0.0.1:5173` 连不上。
- 前端 Node 必须是 `^20.19.0 || >=22.12.0`（Vite 8 的 `engines` 硬要求，本机实测 24.4.0 / npm 11.6.0）。**Node 18 装不上**——2026-10-06 实测发现 README 与 OPERATIONS 都写的「Node 18+」是错的，已改。
- 真实凭据只在 `backend/.env`（已 gitignore）。**任何时候不要 cat 出来贴进文档、提交或日志。**
- Windows 上 PowerShell 会静默改写内联脚本里的中文 / 引号 / JSON。含中文、多行、引号的一次性脚本一律先 `write` 成 `.py` / `.cjs` 再执行，不要用 `python -c` / `node -e` 内联。

## 启动工作流

1. 读下面「环境事实」与「工作规则」
2. `bash ./init.sh` 确认基线是绿的——**基线红着就不要开工**，先修基线
3. `gh issue list --state open` 挑**一张**阻塞边清楚的工单
4. 只实现那一张（`/implement`）。别在同一轮里开第二张
5. 收尾前重跑 `bash ./init.sh`；退出 0 才能在工单里留证据

## 验证命令

**唯一门禁入口是 `init.sh`**，不是本节，不是 `CONSTRAINTS.md`，也不是 CI 之外任何东西。

```bash
& 'C:\Program Files\Git\bin\bash.exe' ./init.sh
```

它依次跑 12 件**真会失败**的事：后端 pytest（用 `backend\venv` 里的解释器，Windows 与 Linux 各一条路径，按存在性挑）、后端 compileall、**敏感内容扫描**（`scripts/check_secrets.py`，扫工作树 + 全部可达历史）、前端 `npm test`、前端**真挂载**测试（`npm run test:mount`）、前端**类型检查**（`npm run typecheck`）、前端**静态检查**（`npm run lint`）、前端**格式检查**（`npm run format:check`）、**引用一致性**（`scripts/check_refs.py`：源码注释与文档里的 ADR 编号、路径锚点必须指向真实存在的东西）、**门禁口径一致性**（`scripts/check_gate_consistency.py`：文档里写的关卡数必须等于 `init.sh` 真跑的关卡数）、**概念索引一致性**（`scripts/check_database_index.py`：`docs/TECHNICAL.md` §3.4 里 `database.py` 的每一段都接着下一段，每个顶层函数恰好属于一行，领域词都是 `GLOSSARY.md` 的词条）、**社区检索质量**（`scripts/eval/check_search_quality.py`：索引列集合与同步触发器的结构判定，加上固定语料上的 top-1 准确率、正文词召回、2 字词召回与零误召回）。
单独调试时可以只跑其中一条，但**声称完成前跑的一定是 `./init.sh` 整条**，且要是本次跑出来的。

## 状态与交接：承接方是工程 skill

- **工程流程归 `mattpocock`** 这一套 skill，由它按阶段分派。

- **状态与阻塞边 → `to-tickets`**，落在 GitHub Issues（见 `docs/agents/issue-tracker.md`）
- **会话交接 → `handoff`**
- **本文件不记录任何进度状态。** 状态指针会过期，而指令文件里的过期指针会被**执行**，不是被阅读。
- 没装这套 skill 就先跑 `/setup-matt-pocock-skills`

## Agent skills

### Issue tracker

Issues and specs live in this repo's GitHub Issues, driven through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, using the default label strings verbatim. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root, ADRs under `docs/adr/`. See `docs/agents/domain.md`。

## 社区 / BYOK 功能

`GLOSSARY.md` 与 `docs/adr/0001`–`0006` 记录了 2026-10-01 grill 敲定的社区/BYOK 设计。
动这块前先读那两份文档，别重新推导已经定过的东西。

**实现进度不要从本文件读**——工单会一张张关掉，本文件不会跟着变。判据是工单自身：

- 哪些还没做：`gh issue list --state open`
- 哪些做完了：`gh issue list --state closed`，并在 `git log` 里找到对应提交
- **别信票面标题，也别信本文件**——跑一遍测试，看它声称的行为是不是真守住了

## 工作规则

上位原则十条。细则与判据在别处，这里只留「该想什么」和路由：

| 细则 | 在哪 |
|---|---|
| 断言外显行为 / 判别法 / 接缝 / 变异判据 / 新表读出口 | `docs/TESTING-STRATEGY.md` §6 |
| 前端两套 runner 与两条 runner 纪律 | `docs/TESTING-STRATEGY.md` §3.5 |
| 现存覆盖缺口（按优先级排） | `docs/TESTING-STRATEGY.md` §5 |
| 平台形态、代理与网络边界、运维事实 | `docs/OPERATIONS.md` |

1. **断言外部可观察行为。** 不测私有函数、不断言内部调用顺序、不绑实现细节。
   写完问两句：**功能整体坏掉时它还会绿吗？拿它去测另一种实现，会不会红？**
2. **结构性约束用变异确认它真能变红。** 报 SURVIVED 时先怀疑断言够不着它，而不是变异没用。
   跑前预检 anchor 命中数——「0 命中」有两条成因，先问**是文件的问题还是我锚点的问题**；
   `KILLED` 必须伴随 `AssertionError` 而不是语法错误。修好一条就扫同族。
3. **文本断言要么用 AST / 函数归属限定作用域，要么它产生的是约束而不是守卫。**
   查字面量只挡得住自己写的那一种写法；会被无关的等价改写打倒的守卫，红里就混着噪声。
   **漏报是守卫最坏的失败模式。** 判定写成 `A and B` 时两半各写一条断言——
   它们在当前树上等价，正是因为等价才各自都显得冗余。
4. **读出口按「谁在读」枚举，不按「数据在哪张表」。** 每个写入者配一条不依赖旧表任何行的读路径。
   只挂在孤儿端点上的测试，再钉一条**前端真在调**的承继端点——端点被清理时覆盖会一起消失，
   且没有任何东西变红。
5. **夹具不是免检区。** 假实现自己要被测：钉死的时钟要有一条「改成空操作」的变异，
   每次都建全新库的 `init_db()` 夹具要配一条**从真缺列的老库启动**的升级路径测试。
6. **守卫与注释都会过期，而过期之后它们仍在产生约束力。** 改完回头看你动过的文件里
   哪些句子描述的是「改动之前」的状态。注释里的语义错了比代码错了更贵。
7. **断言与它自己的失败信息对着读。** 若失败信息描述的正是「断言会通过」的那个状态，它就是反的——
   它在强制缺陷存在。**正确的修复让一条守卫转红时，先怀疑那条守卫**，并在原处写明它曾经是反的。
   每条守卫配一条**反向对照**（不该管的改动必须保持绿）：它转红 = 守卫越权，比变异存活更严重。
8. **一次一个交付物。** 多代理先划所有权边界到文件，并要求 worker 报告它**没碰**哪些越界文件。
   **别信票面标题**——动手前先读相关代码。
9. **证据：先问那条测试真的跑那条路径了吗，再谈「实测过」。** 声称跨语言的实测，
   缺的往往不是断言，是另一侧根本没写。门禁在功能分支上绿 ≠ 授权推送——
   **合并后的树才是要推的东西**，要在它上面再跑一次。本仓是 **PUBLIC**：
   `.gitignore` 与 `git rm --cached` 都只管最新一次提交，审计要扫全 ref 全历史 **与 commit message**。
   没有证据不得标记完成。
10. **给 API 的返回值引入「新的一态」时，它必须与既有值域不相交。** 先问：
    这个值在旧语义下可能出现吗。撞车的症状是不报错、不抛异常，只是慢慢错。

## 范围边界

- 领域定义在 `GLOSSARY.md`，设计决策在 `docs/adr/`。动社区/BYOK/管理后台之前先读 `0001`–`0013`，别重新推导已定过的东西。
- **明确不做**：会员制、点赞/收藏/评论/关注、内容审核与举报**流程**（请举报、复审、自动违规录制）、视频下载功能本身的改动（**纯音频下载除外**，见下一条）、推荐算法与个性化排序、移动端原生应用、多语言界面、**社区视频的下架（软删除）**、**封禁 / 解封账号**。
- **例外 · 纯音频下载**（用户明确要求，已实现）：`_extract_formats` 曾用 `if not has_video: continue` 把 yt-dlp 的纯音频流整批丢掉——DASH 源（B站 / YouTube）本来就只给分离的视频流和音频流，于是「只下音频」在界面上根本不是选项。抖音侧 `_get_media_url(mode="audio")` 早写好了却无人调用，`main.py` 也不传 mode，选了音频会**静默**下回视频。现在：每条格式自报 `kind`（`video`/`audio`），15 项上限内**先给音频留位**，前端分「选择画质」/「纯音频」两块，按钮文案随选中项变。**只提供原生音频容器（m4a / webm-opus / mp3），不做 MP3 转码。**
- **已纳入管理后台**（ADR 0012）：建号、删号、管理员标记。删号语义是**有名下订单或解析历史就 409，绝不级联**；VIP 不在其中（仍只能由订单支付写入）。
- **已纳入管理后台**（ADR 0013）：社区标签可改、社区条目可删（**真删，不是下架**）。删条目**只删 `videos` 那一行，用户的解析历史一行不动**；标签永远受固定词表约束，词表只有服务端一份真值。
- 会员判定与相关额度语义**留在代码里不动**（前端入口已关），但**新增测试不得锁会员行为**。
- 社区视频表**不做条数裁剪**。解析历史表保留 **1000** 条（`MAX_PARSE_HISTORY_PER_USER`），单页 20 条；**收藏（`is_favorite`）永不参与滚动裁剪，且收藏条数不受上限约束**。删一条收藏要 `force=true`，否则 409。

## 完成定义

一张工单满足以下**全部**条件才算完成：

1. `bash ./init.sh` 退出 0，且是**本次跑出来的**（不是引用旧记录、不是"应该没问题"）
2. 工单里留了证据：命令 + 结果摘要 + 退出码，或 CI 链接
3. 没有顺手改掉范围边界之外的东西
4. 其它工单不被这次改动破坏（全量测试仍然绿）

**无证据不得标记完成。**「声称完成但测试没过」是头号失败模式。
子代理说「已修好」不算证据——自己看 diff、自己跑命令。

## 改动怎么进 master

master 受分支保护保护：**force push 与删除分支被挡住**，直推 master 要求「门禁」与「生产构建」
两个检查在最新提交上通过。所以改动走：切分支 → 推分支 → 开 PR → 等检查全绿 → 合入 master。
PR 模板在 `.github/pull_request_template.md`（要求贴命令与退出码）。详见 `docs/OPERATIONS.md` §14.1.1。

三条现实约束，别照着字面想当然：

- **推分支**：本机 git 的代理（127.0.0.1:33210）已经死了，直连要绕开它：
  `git -c http.proxy= -c https.proxy= push -u origin <分支>`。
- **`CODEOWNERS` 目前没有强制力**：它只是声明。分支保护没开「至少 1 个 approve」——
  单人仓库里 PR 作者不能 approve 自己的 PR，设了就是死锁，所以改用「必过检查 + PR 模板要求贴证据」兜。
- **保护规则只存在于 GitHub，不在仓库里**：谁改了 master 的保护设置，仓库内没有任何机制会告警。
  动它之前先 `gh api repos/meisijiya/VidDigest/branches/master/protection` 记一份基线。

## 会话结束

- 跑 `bash ./init.sh`，如实报告结果（过了就说过了，没过就说没过，附完整输出与退出码）
- 状态更新写进工单，**不写进本文件**
- 需要跨会话交接时用 `handoff`，不要手写交接文件

## 升级处理

- **门禁红了**：先定位**一个**根因，查一次完整堆栈。别一次派多个代理同时查同一批红测试。
- **测试红了且根因不明**：读完整堆栈，不要边猜边改。
- **收到复审意见**：先核实，再有依据地反驳或记为显式延后。不要表演性赞同，也不要盲目照改。
- **大面积机械改动**（如批量替换、格式重写）：先在一次性脚本里做，改完逐个核对，不要用管道直接覆写源文件。

## 必需产物

本仓应留下：

- [x] `AGENTS.md`（本文件，工程档，承接方已具名）
- [x] `init.sh`（可执行门禁，三项检查都是真命令，不存在占位）
- [x] `GLOSSARY.md` + `docs/adr/`（领域词汇与设计决策）
- [x] `docs/agents/`（tracker、标签、领域三份约定）

状态与交接产物**不在此列**——它们归 `to-tickets` 与 `handoff`，本文件不代建。
