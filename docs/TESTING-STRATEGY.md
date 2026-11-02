# VidDigest 测试策略

本文档说明：测什么、每类用哪种测试、覆盖目标定在哪、示例用例怎么写、现有覆盖缺什么。

**数据来源**：本文引用的行覆盖数据来自一次历史快照采集，不是估算，也不是当前值。复现方式见文末「本文数据怎么重新采」。

⚠️ **本文刻意不写死统计数字**（用例条数、覆盖率、模块行数）。写死过一次，代码一增长就过期，而**没有任何东西会报警**——本文自己都不会知道自己错了。要当前值，跑一次门禁或重新采一遍覆盖率。

---

## 0. 先读这一段：本文数字的三个局限

策略文档最怕的是拿一个不可信的覆盖率数字去定目标。所以先把局限讲在前面。

### 0.1 这是行覆盖，不是「代码质量」

一次采集给出的行覆盖率里，混合了两类完全不同的东西。用**比例**说而不是绝对值——绝对值每次跑都在动，比例的量级不会骗人：

- **比例偏低的部分是真的**：整块能力从未被任何测试碰过（见第 5 节清单）。
- **比例精确的部分也是真的**：分母由 AST 节点起始行得出，不含注释、docstring、续行。

所以那个百分比**不能**被解读成「测试覆盖了一半」。它混合了两类完全不同的东西：

| 类别 | 例子 | 该不该追 |
|---|---|---|
| 可离线测的编排与数据逻辑 | 额度计算、FTS 查询、并发占位行、凭据脱敏 | **应该追到高覆盖** |
| 必须真网/真模型的 I/O | `douyin.py` 的解析函数、`_extract_bilibili` | 追覆盖率是浪费，应改用契约桩 |

一个更诚实的说法是：**可测的那部分覆盖得不错，不可测的那部分是 0**。策略的重点因此是前者守住、后者换测法。

### 0.2 函数级分母被高估，`X/Y` 里的 Y 偏大

报告里函数级的 `X/Y`（`Y` = 命中行 / 函数体物理行范围）会**系统性低估真实覆盖率**，因为分母把多行字符串字面量的物理行算了进去。

实测证据 —— `database.init_db` 报出来是个个位数：

```
backend/database.py:235  def init_db():
backend/database.py:237      with get_db() as conn:
backend/database.py:248          conn.executescript("""      ← 从这里到 390 是一整段 SQL 字符串字面量
backend/database.py:249              CREATE TABLE IF NOT EXISTS users (
...
```

`executescript` 里那一百多行是 **SQL 文本，不是 Python 语句**，永远不会产生 `line` 追踪事件。所以 `init_db` 的真实情况是「几乎全执行」，不是报出来的那个个位数。

**结论**：凡涉及多行 SQL / 长 prompt / 模板字符串的函数（`init_db`、`_build_full_prompt`、`quota_limit` 等），`Y` 不可信，**不要照着数字定优先级**。定性结论（这个函数有没有被测过）仍然可信，定量结论不可信。

### 0.3 变异装置不在 `init.sh` 里，但都在 CI 矩阵里

仓库里跟踪着 24 个变异装置（以 `glob **/mutation*` 的实际结果为准）：`scripts/` 下 11 个、`backend/tests/` 下 9 个、`frontend/tests/` 下 4 个。

`init.sh` 里的 pytest 与两套前端 runner 都**不收集** `tests/` 目录下这几个文件——目录位置不决定收集行为，文件名模式才决定：

| 文件 | 是否被 `init.sh` 收集 | 是否在 CI 里跑 |
|---|---|---|
| `backend/tests/mutation_check.py` | ❌ 不匹配 `test_*.py`，pytest 默认不收集 | ✅ 后端变异矩阵 |
| `backend/tests/mutation_check_quota.py` | ❌ 同上 | ✅ 后端变异矩阵 |
| `backend/tests/mutation_prompt.py` | ❌ 同上 | ✅ 后端变异矩阵 |
| `backend/tests/mutation_legacy_users.py` | ❌ 同上 | ✅ 后端变异矩阵 |
| `backend/tests/mutation_backup_naming.py` | ❌ 同上 | ✅ 后端变异矩阵 |
| `frontend/tests/mutation_wiring.mjs` | ❌ 不匹配 `*.test.mjs`，`npm test` 不收集 | ✅ 前端变异矩阵 |
| `frontend/tests/mutation-nav-wall.mjs` | ❌ 既不匹配 `*.test.mjs`，也不匹配 `*.spec.mjs` | ✅ 前端变异矩阵 |
| `frontend/tests/mutation-ui-fixes.mjs` | ❌ 同上 | ✅ 前端变异矩阵 |

前端那三个**两套 runner 都不收**：`*.test.mjs` 走 `node --test`，`*.spec.mjs` 走 `vitest run`（`vitest.config.js` 的 `include` 写死了），而变异装置两个后缀都不匹配。

`scripts/` 下的变异装置分两类：门禁自身的关卡（`scripts/mutation_secrets_gate.py`、`scripts/mutation_refs_gate.py`、`scripts/mutation_search_index.py`）各自证明一道关卡真能转红；其余 8 个针对具体机制。两类都由 CI 的后端变异矩阵逐个跑，不在 `init.sh` 里跑。

含义有二：

1. 变异装置在 CI 里是**独立 job**，不并进 `init.sh`（理由见 §7）。
2. 本地 `init.sh` 绿不代表变异装置已经跑过；要看变异结论，去 CI 的矩阵里看，或本地手动跑脚本。

**还有一类既不是用例也不是变异装置的判据**：**端到端守卫**。`scripts/ops/check_xff_bypass.py` 从栈**外面**打真实请求，断言限流的来源身份没被伪造的 `X-Forwarded-For` 绕开（绕过成立就退出码 1）。它同样不进 `init.sh`——它要起真镜像、走真 nginx，不是单进程能判的；它跑在 CI 的「生产构建」作业里，compose 栈起来之后跑一遍，红了就把那个作业打红。

---

## 1. 现状快照

**门禁跑 12 关**（`init.sh`，退出 0 才算通过）：

| # | 关卡 | 判什么 |
|---:|---|---|
| 1 | `backend: pytest` | 后端全量用例 |
| 2 | `backend: compileall` | 全部后端源码能编译 |
| 3 | `scripts/check_secrets.py` | 跟踪文件 + 全部可达历史里没有内容级凭据 |
| 4 | `frontend: npm test` | `*.test.mjs`，静态 / 契约断言（`node --test`） |
| 5 | `frontend: npm run test:mount` | `*.spec.mjs`，真挂载（`vitest run` + jsdom） |
| 6 | `frontend: npm run typecheck` | `vue-tsc --noEmit`，前端源码的类型 |
| 7 | `frontend: npm run lint` | ESLint（flat config + `eslint-plugin-vue`） |
| 8 | `frontend: npm run format:check` | Prettier 格式 |
| 9 | `scripts/check_refs.py` | 源码注释与文档里的 ADR 编号、路径锚点指向真实存在的东西 |
| 10 | `scripts/check_gate_consistency.py` | 文档里写的关卡数等于 `init.sh` 真跑的关卡数 |
| 11 | `scripts/eval/check_search_quality.py` | 社区检索的结构判定与召回质量 |
| 12 | `scripts/check_database_index.py` | `docs/TECHNICAL.md` §3.4 的概念索引与 `backend/database.py` 的顶层函数序列一致 |

第 3 关扫的是**内容**不是文件名——`.gitignore` 挡得住 `.env`，挡不住「把口令抄进文档」。第 4、5 关是两套独立 runner、分开计数，理由见 3.5。

第 6、7、8 关是前端搬完 TypeScript 之后接上的三关：类型、写法、格式各判一类，混在一处就看不出是哪一类红了。它们的判据与前两关不同——前两关判「行为对不对」，这三关判「写法过不过关」，后者换一个实现也会红。

第 9、10 关守的是**门禁自身**：第 9 关查「指令与注释指着的东西还在不在」——AGENTS.md、ADR、源码注释互指着，那一头没了，没有任何一条既有用例会红；第 10 关查「文档里写的关卡数是不是真的关卡数」——真值只在 `init.sh` 里，脚本从那里数，不硬编码。

第 12 关判的是**索引表与源码是否还接得上**：`backend/database.py` 一个文件里 113 个顶层函数，不按表拆开（63 个名字被 52 个测试文件导入），导航靠 §3.4 那张表。表在文档里、函数在源码里，源码加函数表不会自己动；本仓已有前例——`check_refs.py` 只看行号越界，指错了没人知道。判据是「相邻两行首尾相接」，于是新增函数不登记就红；反向对照（给函数加注释、改别的文档）必须保持绿。

第 11 关判的是**「搜得到」而不是「跑得通」**：检索退化的症状是接口照常返 200、只是搜不出东西，索引少一列、正文没进索引、触发器漏写一列都长这样，任何「功能正常」的用例都发现不了。它与 `scripts/ops/probe_ai_e2e.py` 那类端到端探针的差别是：后者要花钱、要联网、产出是质量不是对错，所以不进 `init.sh`；这一关本地、无网络、无 key、判据是确定的对错，所以进。

**跑之前先装环境**：`bash scripts/bootstrap.sh` 建虚拟环境、装后端依赖、前端按锁文件安装；
`init.sh` 自己只管跑，不负责装。门禁的解释器路径按存在性挑选（Windows 走 `venv/Scripts/python.exe`，
Linux/macOS 走 `venv/bin/python`），挑不到就拒绝通过——它不静默回退到系统 Python。

**用例数与覆盖率不写死**：这几项每次跑都在动，写进文档就等于给自己埋一个永远不会报警的过期数字。当前值跑 `./init.sh` 看输出。

| 项 | 怎么取当前值 |
|---|---|
| 后端用例数 / 文件数 | `init.sh` 第 1 关的 pytest 摘要 |
| 前端静态断言数 | `init.sh` 第 4 关的 `node --test` 摘要 |
| 挂载用例数 | `init.sh` 第 5 关的 vitest 摘要 |
| 行覆盖率 | 需重新采集，见文末「本文数据怎么重新采」 |
| 从未被执行的函数 | 同上，采集时才产生 |
| 被执行但覆盖不全的函数 | 同上，采集时才产生 |

### 分模块行覆盖

**下面的「评价」列仍然有效**，「快照覆盖率」列来自一次历史采集，只用于看**量级**，不要当当前值。

| 模块 | 快照覆盖率 | 评价 |
|---|---:|---|
| `tags.py` | 高 | 词表校验是 ADR 0005，值得守 |
| `credentials.py` | 高 | BYOK 是安全边界，值得守 |
| `api_community.py` | 高 | 读出口齐（见 5.4 已补齐） |
| `api_summarize.py` | 高 | 事件契约有测试钉着 |
| `auth.py` | 表面高，**有欺骗性** | 见 5.2：密码一侧仍有裸奔的部分 |
| `api_history.py` | 中 | 读出口已被 `test_history_search_favorites.py` 大量走到 |
| `database.py` | 中 | 核心数据层 |
| `main.py` | 中 | 主入口已被 `test_audio_download.py` 覆盖（见 5.1） |
| `summarizer.py` | 低 | 多为真网 / 真模型 I/O，见 0.1 |
| `api_auth.py` | 低 | 活跃但 `/register`、`/login` 仍裸奔（见 5.2） |
| `api_payment.py` | 低 | 活跃但裸奔（见 5.3） |
| `downloader.py` | 低 | 真网 I/O，见 5.5 |
| `douyin.py` | 低 | 真网 + WAF 对抗，见 5.5 |

---

## 2. 测试金字塔在本项目的映射

```
        /  冒烟 E2E  \        极少，只跑门禁能覆盖的
       /  集成（HTTP 层）\    一批，FastAPI TestClient 真路由
      /     单元 + 契约   \   主体，纯函数与数据层
```

条数不写死——它们每次跑都在动。当前分布跑 `./init.sh` 看两关的摘要。

**这个项目的形状是倒过来的 —— 而且是对的。**

原因：本仓的绝大部分价值在「额度算得对不对、并发有没有串、FTS 能不能搜到、凭据有没有落盘」这类**判定逻辑**上，而不是在 HTTP 报文本身。HTTP 层用 `TestClient` 打进去成本很低，但它只能证明「路由通了」，证明不了「额度扣对了」。

所以：

- **单元层是承重层**，额度、并发、FTS、脱敏的判据全在这里。
- **集成层是接线层**，验的是依赖注入、鉴权、事务、真实客户端序列化。
- **E2E 只做冒烟**，且**永不引入真实第三方依赖**（真实 B 站、真实模型、真实 Stripe）—— 一旦引入，门禁就会因网络和凭据而变成不稳定信号，而不稳定信号会被习惯性忽略，等于门禁失效。

---

## 3. 分组件测什么、怎么测

### 3.1 额度与并发（最高优先级）

**风险**：这是本仓最容易出错、且出错后用户直接受损的地方。已知的两个真实坑：

- `daily_chat_count` 记的是**已用次数**（`+1`），`remaining` 才是 `-1`。写反了不会报错，只会让用户额度虚增一倍。
- 直接 `UPDATE` 额度而**不碰 `last_chat_date`**，跨天时会被重置逻辑覆盖掉。测试必须**反复调 `consume_quota`** 才能观察到真实行为，单次调用看不出这个问题。

**测法**：
- 单元：`consume_quota` / `check_quota_kind` / `quota_limit` 的表驱动测试，每个分支一条用例。
- 并发：`Barrier(2)` 放在**路由调用之前**，不是模型调用里。放错位置会死锁 —— 这是本仓已踩过的坑。
- **变异确认**：结构性约束（占位行 + 等待复用、唯一 `finally` 回滚）必须用变异装置确认真能变红。已验证 12 条变异全杀。

**不建议**：对 `quota_limit` 追行覆盖率（1/9 是分母虚高）。用**表驱动行为断言**代替。

### 3.2 社区搜索（FTS5 + trigram）

**风险**：SQLite 的 FTS5 不可用时**必须报错，不许把 MATCH 路径降级成 `LIKE`**。降级了功能还在，但 trigram 分词能力没了 —— 症状是「某些中文查询搜不到」，极难定位。

**唯一的例外是 2 字词**：trigram 按 3 字符滑窗切，2 字中文词在 MATCH 路径恒为 0 命中，所以 2 字走**显式、限定**的子串兜底（原始 `videos` 表上 `LIKE ? ESCAPE '\'`，ADR 0030）。它是有意设计的第二路径，不是降级：兜底的列集合与索引一致、可见性口径与 MATCH 一致、元字符逐个转义，且 1 字仍然拒绝。

**测法**：
- 单元：`search_community_videos` 的真实 SQLite 库测试（不是 mock）。
- 构造性：FTS5 不可用的环境里必须抛错。**这条要靠变异确认**（删触发器看测试是否转红）。
- 召回：固定语料上的正文词召回、2 字词召回、零误召回（`scripts/eval/check_search_quality.py`，门禁第 11 关）。**只断言「能搜到」的用例发现不了索引少一列**——症状是「该搜到的没搜到」，不是「报错」。
- 已验证：5 条变异全杀，包括 `bm25()` 缺表名、触发器被删；检索索引另有 3 条变异全杀（索引退回三列、触发器只写三列、分词器被换掉），见 `scripts/mutation_search_index.py`。

### 3.3 追问会话（跨用户隔离）

**风险**：**假通过**。本仓的真实教训 —— 「用户 B 读不到 A 的记录」这条断言一度成立，原因是**谁的记录都读不到**（功能缺失），不是隔离生效。

**测法（强制）**：
- 每个写入者都要有一条**不依赖旧表任何行**的读路径。B 追问了 A 的视频，B 自己没解析过 —— 这条路径必须在测试里被独立走通。
- 隔离断言要**双向**：B 读不到 A 的是数据；B 读得到自己的是功能。两者都要。
- **变异确认**：打断读出口（返回空 / 恒返回常量）必须让测试转红。已验证 M6 变异仍红。

### 3.4 BYOK 凭据（安全边界）

**风险**：裸串泄漏。三条硬约束：**不进 URL、不进请求头、不进任何别的请求、不落盘**。

**测法**：
- 哨兵法：用一个任何服务商都不会签发的 `SENTINEL`，把所有抓到的请求摊开逐处搜。
- 源码层：`.vue` 模板与 `<script setup>` 都要查，且**要去掉注释**再查（注释里的说明不算代码）。
- 凭据路径**一条日志都不记** —— 用「抓全部日志 + 搜哨兵」断言。
- 已验证：3 条变异全杀。

**注意**：`UserCredential.from_secret` 的意义是**让裸串只存在于 `credentials.py` 内**。所以扫描测试要追**数据来源**（沿赋值/helper return 回溯到 `os.getenv`），而不是只扫函数源码里出现了什么名字 —— 只扫名字会被「把取值挪进一个 helper」这种最自然的重构绕过去。

### 3.5 前端

前端有**两套 runner、两类判据**（工单 #18 建的）。判据不是按文件类型分的，是按**这条用例断言的对象**分的：

| 断言的对象 | 放哪 | 怎么跑 |
|---|---|---|
| 「渲染出了什么」 | `tests/*.spec.mjs` | `vitest run`（`npm run test:mount`），jsdom + `@vue/test-utils` 真挂载组件 |
| 「源码里不许出现什么」 | `tests/*.test.mjs` | `node --test`（`npm test`），读源码文本断言 |

`.js` 里的行为两种都算：用假 fetch 真跑一遍是 `*.test.mjs` 的主流写法，而组件级行为归 `*.spec.mjs` 挂载。

**为什么文本断言不能一律迁成挂载**：色值字面量、snake_case 取值、转换函数收口这些判据的**对象就是源码文本**。挂载后看不到模板源码了，断言会退化成「界面上没出现那个字符串」—— 而模板写了、只是没渲染到，恰恰是要抓的回归。**迁过去是变弱，不是变强。**

反过来，源码接线断言守不住渲染正确性：模板语法错、条件渲染漏一个分支，文本断言在绿灯下依然存在。挂载层就是为了关掉这个缺口而建的，所以「没有挂载环境」这个老前提**已经不再成立**。

**两个必须守住的 runner 纪律**（都踩过）：

1. `vitest.config.js` 的 `include` **必须显式写死成 `tests/**/*.spec.mjs`**。默认 include 覆盖 `.test`，会顺手吃掉 node:test 的文件；而 vitest 遇到 `describe` 体抛异常时**仍然退出 0**——那会让挂载关卡**恒绿**。
2. 前端测试**不要在 `describe` 体里做会抛异常的事**（`readFileSync` 之类）。node 对 `describe` 体里抛出的异常给出的退出码是 **0**，runner 还会报 `tests 0 / pass 0 / fail 0`——那个 suite 一条都没注册、没运行。真 AssertionError 被埋在摘要下面，而门禁只看退出码。**要读源码就把 `readFileSync` 提到模块顶层**（模块加载失败是响的）。

---

## 4. 覆盖目标

**不给单一数字目标。** 单一数字会逼着人去测不值得测的东西。

按风险分档：

| 档 | 范围 | 目标 | 判据 |
|---|---|---|---|
| **A · 承重** | 额度、并发占位、隔离、凭据脱敏、FTS 可用性 | 每个判定分支有对应用例；**每条结构性约束经变异确认能变红** | 变异装置全杀 |
| **B · 接线** | 全部 HTTP 路由、鉴权依赖、事务边界 | 每个路由至少一条正例 + 一条拒绝/错误例 | 路由清单 vs 测试清单逐条对账 |
| **C · 契约** | 真网 I/O（`douyin`、`downloader`、`_extract_bilibili`、Stripe） | **不追行覆盖**；改为对「我方契约」建测试：URL 识别、响应字段映射、错误分类 | 每个外部依赖至少一组 fixture 契约测试 |
| **D · 不测** | `__init__`、纯转发 getter、框架胶水、`features.js` 开关 | 不测 | — |

**关于行覆盖的绝对值**：A + B 档的**行为**覆盖应当接近 100%，即使行覆盖数字停在六成上下。行覆盖只是 A/B 档是否做够了的**副产品**，不是目标本身。

---

## 5. 缺口清单（按优先级）

### 5.1 P0（已部分消解）—— 产品主入口

⚠️ **本节曾经写着「`main.py` 6 个函数从未被执行」，那句话已经不成立。** 当时的后端测试确实一条都没打过主入口；`test_audio_download.py` 与 `test_main_routes.py` 补上之后，主入口与两条辅助路由都有了真覆盖。

行号一律取**函数定义行**（不是装饰器行，也不是类行）：

| 函数 | 路由 | `main.py` 行 | 现状 |
|---|---|---:|---|
| `parse_video` | `POST /api/parse` | 118 | ✅ `test_audio_download.py:125`，真 client 打真路由 |
| `download_video` | `POST /api/download` | 138 | ✅ `test_audio_download.py:355,363,372,380`（四条全走抖音分支） |
| `ParseRequest.clean_url` | — | 93 | ⚠️ 被上面两条**间接**走到，但只喂了裸 URL |
| `DownloadRequest.clean_url` | — | 103 | ⚠️ 同上。`test_main_routes.py:166` 喂过分享文案，但 URL 后面紧跟的是半角空格（已被字符集里的 `\s` 排除） |
| `get_direct_url` | `POST /api/direct-url` | 176 | ✅ `test_main_routes.py` 的 `TestDirectUrlRoute` 3 条 |
| `proxy_thumbnail` | `GET /api/proxy/thumbnail` | 271 | ✅ `test_main_routes.py` 15 条：SSRF 判定 5 条、公网走通与上游失败映射 10 条 |

**剩下的真缺口有四条**，每条一个工单：

| # | 缺口 | 去向 |
|---|---|---|
| 1 | **`clean_url` 的中文标点字符集变异存活。** 它存在的理由是「从分享文案里抽出 URL 且截断在中文标点前」，而现有唯一一条非裸 URL 用例后面跟的是半角空格（早已被 `\s` 排除）。另有 `match is None → strip()` 退化分支零覆盖。该正则在 `main.py` 两处与前端两处共四处复制，无一钉住。 | #52 |
| 2 | **`download_video` 的 `except HTTPException: raise` 零测试。** 现有 4 条用例因桩真写文件而永远走不到 500 分支，删掉那两行全量照样绿。 | #53 |
| 3 | **平台分流没有显式断言。** 现有判别力全靠「对侧没被 stub 所以会红」，没有任何用例断言「走了哪一边」；在有网的机器上它可能变成慢红或假绿。 | #56 |
| 4 | **`/api/download` 的非抖音分支与 `/api/parse` 的错误映射路由层零覆盖。** 全仓 4 处该路由调用全是抖音域名；`_parse()` 助手自带 `assert 200`，把错误分支永久挡在测试之外。 | #53 |

下面这些用例仍然值得补（`clean_url` 是纯函数，最容易测）：

注意 `ParseRequest` 的字段名是 `url`（不是 `video_url`），且 **`clean_url` 不去追踪参数** ——
它的真实契约是「从用户粘贴的分享文本里正则抽出第一个 URL」：

```python
# 1. clean_url 从分享文本里抽 URL —— 纯函数，零成本。
#    真实行为：https://v.douyin.com/abc/ 复制出来的分享文案前后带中文和表情
def test_clean_url_extracts_first_url_from_share_text():
    # 抽到 URL，且截断在中文标点前（「。」不在 URL 字符集里）
    assert ParseRequest(url="【标题】https://v.douyin.com/abc/ 快来看").clean_url() \
           == "https://v.douyin.com/abc/"
    # 抽不到时退化为原样 strip —— 这个分支同样值得钉住
    assert ParseRequest(url="  没有链接  ").clean_url() == "没有链接"
```

第 1 条的正则字符集 `[^\s）\)\"\'＞，。、；：！？》>\]]+` 刻意排除了中文标点，
所以「URL 后面跟一个逗号」是这个函数存在的**全部理由**。删掉那个字符集它就会转红。

```python
# 2. 分流：抖音 URL 走 douyin_parser，其余走 downloader
#    ⚠️ 这条的一半已经被 test_audio_download.py 覆盖（:372 打 /api/download
#    用抖音 URL，:125 打 /api/parse 用 B 站 URL）。仍缺的是**分流本身**：
#    断言"走了哪一边"，而不只是"返回了 200"。
def test_parse_routes_douyin_to_douyin_parser(client, stub_douyin, stub_downloader):
    r = client.post("/api/parse", json={"url": "https://v.douyin.com/abc/"})
    assert stub_douyin.calls == 1 and stub_downloader.calls == 0

# 3. 异常透传：download_video 里 HTTPException 必须原样抛出，
#    不能被 except Exception 吞成 400
def test_download_video_does_not_swallow_http_exception(...):
    # 把 FileResponse 前的 os.path.exists 打成 False → 应得 500 "下载的文件不存在"
    # 若被 except 吞掉会变成 400，说明 except HTTPException: raise 被删了
```

第 3 条是**变异友好**的：删掉 `except HTTPException: raise` 它就会转红。这条**至今没有任何测试钉住**。

### 5.2 P0 —— 认证链路活跃但裸奔

⚠️ **本节曾经写着「`auth.py` 4 个函数 + `api_auth.py` 2 个函数从未执行」，前半句已经不成立。** `verify_password` 已被 `test_admin_user_lifecycle.py:203-204` 测到（建用户后验一次真密码、验一次错密码）。**其余五个仍未执行**：

| 函数 | 路由/位置 | 行 | 现状 |
|---|---|---:|---|
| `hash_password` | — | 32 | ❌ 无直接测试（只经 `verify_password` 的夹具间接触达） |
| `verify_password` | — | 36 | ✅ `test_admin_user_lifecycle.py:203-204` |
| `validate_email` | — | 61 | ❌ 无直接测试 |
| `validate_password` | — | 65 | ❌ 无直接测试 |
| `register` | `POST /api/auth/register` | 60 | ❌ `backend/tests/` 下无任何测试打 `/api/auth/register` |
| `login` | `POST /api/auth/login` | 83 | ❌ 同上 |

以上五行由 **#51** 一张单处理。两条路由都是前端 `src/api/auth.js` 的真实调用目标（活端点，不是孤儿）。

`auth.py` 的行覆盖率具有**欺骗性**：高分来自 token 签发/解析（那部分确实测过），而**密码的写入侧与校验规则仍没测**。

**为什么还是 P0 而不是 P1**：`/register`、`/login` 是 `frontend/src/api/auth.ts` 的真实调用目标，两条路由**零测试**。`validate_email` / `validate_password` 的返回值直接进 400 响应体（`raise HTTPException(detail=err)`），文案一旦变了就是对外可见的 API 变更。

**示例用例**：

```python
# 密码哈希用 bcrypt.gensalt()：同一密码两次哈希必须不同（随机盐），
# 但都能 verify 通过
def test_hash_is_salted_and_verifiable():
    a, b = auth.hash_password("pw"), auth.hash_password("pw")
    assert a != b, "同一密码两次哈希相同 = 没加盐"
    assert auth.verify_password("pw", a) and auth.verify_password("pw", b)
    assert not auth.verify_password("wrong", a)

# validate_password 返回的是**错误文案或 None**，不是布尔。
# 钉住两个边界：6 位（含）和 51 位
def test_validate_password_bounds():
    assert auth.validate_password("a" * 6) is None
    assert auth.validate_password("a" * 5) == "密码长度不能少于 6 位"
    assert auth.validate_password("a" * 51) == "密码长度不能超过 50 位"

# 登录失败不泄露是哪个字段错了（钉住当前已有的正确行为）
# 现状：邮箱不存在与密码错误都抛 400 + 同一句「邮箱或密码错误」
def test_login_does_not_leak_which_field_was_wrong(client):
    r1 = client.post("/api/auth/login", json={"email": "nobody@x.com", "password": "pw"})
    r2 = client.post("/api/auth/login", json={"email": "real@x.com",  "password": "bad"})
    assert r1.status_code == r2.status_code == 400
    assert r1.json()["detail"] == r2.json()["detail"] == "邮箱或密码错误"
```

最后一条的价值是**防回归**：把两处 400 拆成「邮箱不存在」/「密码错误」两种文案，
是一个看起来无害的重构，却会立刻造成用户名枚举。这条测试能拦住它。

`validate_password` 那条防的是 off-by-one：把 `len(password) > 50` 改成 `>= 50`，
用户就在恰好 50 位时被拒。这类改动不会报错，只会让一个边界值莫名其妙登不上去。

**这两个函数不是死代码**：`register`（`api_auth.py:61-69`）逐个调用它们，
且 `validate_password` 的返回值**直接进 400 响应体**（`raise HTTPException(detail=err)`）。
文案一旦变了就是对外可见的 API 变更 —— 钉住它不是洁癖。

### 5.3 P1 —— 支付链路活跃但裸奔

`api_payment.py` 4 个函数里只有 `create_checkout_session` 有真覆盖（工单 #42 补的 5 条），其余三条零覆盖：

| 函数 | 路由 | 行 | 现状 |
|---|---|---:|---|
| `_generate_order_no` | — | 31 | ⚠️ 只被 `create_checkout_session` 间接调用，无直接断言 |
| `create_checkout_session` | `POST /api/payment/create-checkout` | 40 | ✅ `test_endpoint_coverage_gaps.py` 5 条 |
| `stripe_webhook` | `POST /api/payment/webhook` | 103 | ❌ 零覆盖 |
| `list_orders` | `GET /api/payment/orders` | 141 | ❌ 零覆盖，且**前端无任何调用方**（孤儿端点） |

配套的 `database.py` 四个函数同样没有真覆盖：`create_order`(2035) 只在 `test_admin_user_lifecycle.py:130` 被当作建数调用（测的是删号 409，不是订单逻辑）；`update_order_stripe_session`(2044) 与 `complete_order`(2052)、`get_user_orders`(2103) **零覆盖** —— 后两者正是 webhook 幂等与订单列表的实际落点。

以上由 **#54** 一张单处理。

**为什么是 P1 不是「直接删」**：`frontend/src/config/features.ts` 明确写着「不做会员制，但后端 VIP 判定与 Stripe 支付代码**保留不删**」，而 `App.vue:643` 仍在调 `createCheckoutSession('monthly')`。**它是活的、但零测试的。**

这里测试的价值不在于「测得好」，而在于**钉住它当前的行为**。`stripe_webhook`（32 行，处理外部回调）尤其需要最少限度的一条：**签名校验失败必须拒绝**。

```python
# 1. 签名无效必须拒绝 —— 当前返回 400 "Invalid signature"
#    注意它依赖环境变量 STRIPE_WEBHOOK_SECRET，测试要先把它设上，
#    否则会在更早的分支就返回 "Webhook secret not configured"（也是 400，但理由不同）
def test_webhook_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    r = client.post("/api/payment/webhook",
                    content=RAW_BODY,
                    headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400
    assert r.json()["error"] == "Invalid signature"
```

⚠️ 这条有个真实的坑：**签名未配置和签名无效都返回 400**，只靠状态码分辨不出
「因为没配 secret」和「因为签名错了」。断言必须落在 `error` 文案上。

```python
# 2. 幂等：同一个 checkout.session.completed 重放两次，complete_order 只能生效一次。
#    幂等性在 database.complete_order 里（只处理 pending 状态），不在路由里——
#    所以这条要断言**效果**（VIP 到期时间没被刷新成第二次的时间），
#    而不是断言 complete_order 被调了几次。
def test_replayed_webhook_does_not_extend_membership_twice(...):
    # 第一次：VIP 到期 = T1
    # 重放同一事件
    # 断言：到期时间仍是 T1，且订单 status 仍是 paid 而非二次更新
```

第 2 条是变异友好的：把 `complete_order` 里的 `status == "pending"` 判断删掉，
重复回调测试就会转红。**这类「幂等」断言必须断在效果上**——断调用次数的话，
把 `complete_order` 换成恒返回 None 也能全绿。

### 5.4 ~~P1 —— 历史读出口未测~~（已消解）

⚠️ **本节曾经写着「`api_history.py` 3 个函数从未执行」，那句话已经不成立。** 当时 `backend/tests/` 下确实没有任何测试打 `/api/history`；现在 `test_history_search_favorites.py` 里有 40+ 处打这个前缀，覆盖了列表、收藏切换、facets、删除等路径。

| 函数 | 路由 | 行 | 现状 |
|---|---|---:|---|
| `list_history` | `GET /api/history` | 38 | ✅ 被 `test_history_search_favorites.py` 大量走到 |
| `save_history` | `POST /api/history/save` | 55 | ✅ 同上 |
| `remove_history` | `DELETE /api/history/{id}` | 130 | ✅ 同上 |

3.3 的纪律仍然成立、而且正因为补上了才值得记住：**新表的读出口按「谁在读」枚举，不要按「数据在哪张表」枚举。** 补测试时每个写入者都要有一条**不依赖旧表任何行**的读路径。

### 5.5 P2 —— C 档：真网 I/O 换测法

`douyin.py`、`downloader.py`、`summarizer.py` 三个模块的 I/O 部分**不要追覆盖率**（函数条数以实际采集为准）。

正确做法是对**我方契约**建 fixture 测试：

| 目标 | 测什么 |
|---|---|
| `douyin.is_douyin_url` | URL 识别纯函数 |
| `douyin._decode_b64` | b64 解码纯函数 |
| `douyin._fmt_duration` | 时长格式化 |
| `DouyinParser._extract_video_id` | 从分享链接抽 ID 的规则 |
| `douyin._build_result` | 字段映射契约，**最值得** |
| `summarizer._parse_vtt` | VTT 解析 |
| `summarizer.clean_mindmap_markdown` | 思维导图清洗，**值得优先** |
| `summarizer._time_to_seconds` | 时间解析 |

判据很简单：**这个函数的输入输出是确定的吗？** 是 → 用 fixture 测。否（真网、真 WAF 对抗、真模型流）→ 只测它在失败时怎么归类错误。

`clean_mindmap_markdown` 特别值得优先：它是**纯函数**，而且它的输出直接决定前端 markmap 能不能渲染 —— 错了是「思维导图空白」，用户看不出原因。

### 5.6 P3 —— 部分执行的高价值函数

按「值不值得补」排序（非按覆盖率排序）。**「命中/范围」两列来自一次历史采集，只用于看量级，不作判据**——要当前值得重新采集（见文末）。

| 函数 | 判断 |
|---|---|
| `database.publish_video_card` | 值得。卡片「先到先得」语义（ADR 0006）复杂 |
| `database.search_community_videos` | 值得，搜索是核心功能 |
| `database.reserve_video` | 值得，并发语义的承重墙 |
| `tags.validate_tags` | 值得，词表校验是 ADR 0005 |
| `database._migrate_video_card_columns` | 值得，迁移代码出错不可逆 |
| `summarizer._build_full_prompt` | 追覆盖率无意义（分母虚高），改为断言**关键约束**：字幕不外传、去重逻辑 |
| `database.quota_limit` | 同上，改表驱动行为测试 |
| `database._fts_phrase` | 值得，FTS 短语转义，错了会 SQL 注入或搜不到 |

---

## 6. 示例用例的写法要求

本仓已有的六条纪律（源自已关闭工单的复审 FAIL），补测试时必须遵守：

### 6.1 断言外部可观察行为

不测私有函数、不断言内部调用顺序、不绑实现细节。
判断标准：**功能整体坏掉时，它还会绿吗？**

### 6.2 结构性约束用变异确认

声称「这条测试守住了 X」的，必须做一次变异（把 X 改坏）确认它转红。
**注意变异判据**：`pytest` 的 exit 5 = **一条测试都没选中**，不等于「测试失败」。脚本里要显式检查 `ran > 0`：

```python
if ran == 0:
    verdict = "NO-TESTS-RAN"   # 不是 KILLED
```

`-k` 表达式里不要粘类名 —— 类名含 `as` / `and` / `or` / `in` 等 Python 关键字时会被当关键字解析，静默选出 0 条。

### 6.3 新表读出口按「谁在读」枚举

每个写入者配一条**不依赖旧表任何行**的读路径。

### 6.4 接缝优先

用 `tests/seams.py` 已建好的三条接缝，别绕过：

1. `StubSummarizer` —— **按方法分别计数**。`calls`（总和）不能单独用来断言「某方法没被调用」。
2. `close_all_thread_connections()` —— 路由层用 `run_in_executor`，连接按线程缓存。
3. `make_client()` —— 鉴权依赖必须**真的**被解析。直接调路由函数时 `user` 拿到的是依赖对象本身，「传空用户测 401」只是手写了依赖返回值。

### 6.5 夹具不是免检区

夹具与假实现自己要被测。两种具体形态：

- **钉死的时钟要有一条「改成空操作」的变异。** `frozen_clock` 若被改成空操作，
  「时钟被钉住了」这件事就没有任何东西守——将来有人为省事删掉它（或换成一个
  碰巧在多数时候成立的替身），全量照样绿。`backend/tests/test_frozen_clock.py` 测的就是夹具本身。
- **每次都建全新库的 `init_db()` 夹具要配一条升级路径测试。** 它每次出一个**全新**库，
  于是「老库缺列、靠迁移补上」那条路径在它下面永远走不到；补列排错顺序，全量绿，
  真库一升级就 `no such column` 打不开。用 `legacy_db` 夹具（只换 `DB_PATH` + 清连接、不建表）
  从一个**真缺列**的库启动——判据是「新写迁移必须自己验升级路径」，不是「CI 能挡住回归」。

假类要**继承**被替掉的类型而不是顶替它，只实现用到的那几个方法会让别的路径**因错误的原因**抛错。

### 6.6 反向对照

每条守卫配一条「不该管的改动必须保持绿」的对照。它转红 = **守卫越权**，比变异存活更严重，
而两者在摘要上都读起来像「守卫没生效」。

- 「上限由服务端给，前端不写死 3」如果断言写成 `assert.match(src, /maxTags \|\| 3/)`，
  正则**要求**那个兜底存在，而它自己的失败信息写的是「前端把它写死了」——
  测试名说 A、断言做 ¬A、失败信息说 A。**把断言与它自己的失败信息对着读**，
  信息描述的正是「断言会通过」的那个状态时，它就是反的（在强制缺陷存在）。
- 反向对照转红，说明这条判据管了它不该管的东西（例：后端从不发的 `export: 'onExport'`
  被加进前端路由表，而判据只查了「后端发的都有路由」这一个方向——**死路由比缺路由更隐蔽**，
  缺路由的症状是「界面上的东西不动」，死路由的症状是**什么都没有**）。

---

## 7. 不做什么

- **不引入真实第三方依赖**（真 B 站 / 真模型 / 真 Stripe）。门禁必须任何机器都能跑。
- **不为 C 档 I/O 追覆盖率。**
- **不测 `__init__`、纯转发、框架胶水。**
- **不把变异装置并进 `init.sh`**（它们在 CI 的变异矩阵里跑，这是有意的）。变异运行慢，且会临时改写源码，与并行开发冲突；矩阵 job 彼此隔离，一个装置失败不影响门禁 job 的结论。
- **不把覆盖率数字写进文档**。写死一次就过期一次，而没有任何东西会报警；覆盖率是采集产物，不是文档内容。

---

## 本文数据怎么重新采

```bash
# 唯一门禁入口。退出 0 才算通过，输出里带各关的用例摘要。
& 'C:\Program Files\Git\bin\bash.exe' ./init.sh
```

**行覆盖率目前无法用仓库内的脚本复现。** 本文历史上引用过三个采集脚本（用例盘点、行覆盖收集、覆盖报告），它们都放在 `.scratch/` 下、**从未被跟踪**，现在已经不在仓库里了。`.scratch/` 现有内容只有 `gate*.log`、`issue*.md` 与 `verify/`。

所以：

- 要**门禁口径**的现状，跑上面的 `init.sh` 看输出。
- 要**覆盖率**，装 `pytest-cov` 自己跑一次；本仓历史上那套自制收集器的分母口径见第 0.2 节（它把多行字符串字面量的物理行算进了分母，与 `pytest-cov` 不可直接比较）。
- 别把本文任何百分比当成当前值。
