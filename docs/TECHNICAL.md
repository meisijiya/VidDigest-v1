# VidDigest 技术说明

> 通用视频解析 / 下载 + AI 字幕总结工具。FastAPI（Python 3.11）+ Vue 3（TypeScript）两个独立进程。
> 本文只写**当前实现**。两类读者：面试官与技术评审读 §0 → §1 → §4 → §6 → §9；接手维护的人读 §8 → §4 → §5 → §6 → §3。
> 文中每个数字的口径与复现命令集中在[附录 A](#附录-a数字来源清单)；决策理由在 [`docs/adr/`](adr/)，不在本文复述。

---

## 0. 这份文档是什么、给谁读

**这一节服务两类读者。** 本文不覆盖六件事，它们各有归属：

| 你想知道 | 去这里 |
| --- | --- |
| 怎么装、怎么跑起来 | [`README.md`](../README.md) / [`README.en.md`](../README.en.md) |
| 某个端点的字段与请求示例 | [`docs/API.md`](API.md) |
| 部署、备份、巡检、故障处置步骤 | [`docs/OPERATIONS.md`](OPERATIONS.md) |
| 测什么、每类用哪种测试、覆盖缺口 | [`docs/TESTING-STRATEGY.md`](TESTING-STRATEGY.md) |
| 领域词的定义（视频 / 解析记录 / 社区 / 额度） | [`GLOSSARY.md`](../GLOSSARY.md) |
| 某个设计决策的取舍与推翻条件 | [`docs/adr/`](adr/) |

**冲突口径**：代码、测试与文档三者不一致时，以代码与 `init.sh` 为准；本文只报告冲突，不替别的文档改口径（`AGENTS.md` 文档规则 2）。§10 列的是已核实的冲突清单，每条写了「谁说什么 / 真值在哪 / 该谁改」。

**判据**：读完这一节，读者知道自己该去哪，且不会把本文当成安装手册或 API 字典。

---

## 1. 一页纸现状

**这一节服务面试官与技术评审。** 一句话定位：把「解析一次、视频结论全站一份、AI 总结按成本性质分别计额度、用户自带 Key 不落库」四件事做成可验证的机制，而不是做成页面上的说法。

### 1.1 规模

| 维度 | 当前值 | 来源（复现命令见附录 A） |
| --- | --- | --- |
| REST 端点 | 44 | 路由装饰器计数（A1） |
| 挂鉴权依赖的端点 | 32（`get_current_user` 15 + `require_admin` 14 + `get_optional_user` 3） | 依赖注入计数（A2） |
| SSE（流式）端点 | 2（`/api/summarize`、`/api/chat`） | `EventSourceResponse` 路由（A3） |
| 后端用例 | 1533 | `pytest --collect-only`（A4） |
| 前端静态 / 契约断言 | 538 | `npm test`（A5） |
| 前端挂载用例 | 218 | `npm run test:mount`（A6） |
| 变异脚本 | 24（后端 20 + 前端 4） | 文件清单（A7） |
| 门禁 | 11 关，真值在 `init.sh` | `bash ./init.sh`（A8） |
| CI | 4 个 job 定义，展开 26 个实例 | `.github/workflows/ci.yml`（A9） |
| 业务表 | 7 张 + 1 个 FTS5 虚表 + 6 个触发器 | 建表语句（A10） |
| 公开端点限流 | 6 个端点按来源 IP 限速，每分钟 20 / 10 / 30 / 60 / 10 / 5 | `backend/ratelimit.py`（A19、ADR 0020） |
| 设计决策 | 30 篇 ADR（29 篇生效，1 篇已被取代） | [`docs/adr/`](adr/)（A11） |
| 受管理的 Markdown | 50 个 | `git ls-files '*.md'`（A12） |
| 编码代理的工单 | 85 条（全部关闭） | `gh issue list --state all --limit 200 --json labels`（A13） |

### 1.2 两条主线

- **解析与下载**：链接 → 平台解析模块 → 每条格式自报 `kind`（`video` / `audio`）→ 下载。同一视频全站只解析一次，后来者复用首份结果。
- **AI 总结**：字幕提取 → 一次模型调用产出摘要 / 导图 / 标签三段产物 → SSE 流式吐给前端；额度按「解析」与「追问」两个计数器分别记账。

### 1.3 三条硬约束

1. **后端单进程**（建议，非正确性约束）：SQLite 写入串行，多 worker 只会让写锁竞争更频繁（[`docs/OPERATIONS.md`](OPERATIONS.md) §5.1.1）。
2. **前端只监听 IPv6 回环**：开发态用 `http://localhost:5173`，`127.0.0.1:5173` 连不上（`AGENTS.md` 环境事实）。
3. **凭据只在 `backend/.env`**：文件不在版本库，但 `.gitignore` 只挡文件名，门禁第 3 关扫的是**内容**。

**判据**：读者读完能复述「它是什么、几个进程、多大规模、哪三条约束不能碰」。

---

## 2. 进程与部署形态

**这一节服务维护者。**

### 2.1 两个进程

| | 开发态 | 生产 / 容器态 |
| --- | --- | --- |
| 后端 | `backend/venv/Scripts/python.exe -m uvicorn main:app`（`:8000`） | 容器内 `uvicorn main:app --host 0.0.0.0 --port 8000`（[`backend/Dockerfile`](../backend/Dockerfile)） |
| 前端 | Vite dev server（`:5173`） | 构建产物交给 nginx（`frontend/nginx.conf`，容器内 `:80`，宿主 `:8080`） |

- **改 Python 必须重启**：后端没有 `--reload`，改了不重启等于没改。
- **前端 API 走相对路径 `/api`**：开发态由 Vite 代理到后端，生产态由 nginx 同域反代。所以开发与生产都是同源，浏览器不会触发跨域。
- **`/admin` 必须是真实路径**：nginx 用 `try_files` 回落 `index.html`，否则刷新管理后台会 404。

### 2.2 环境硬约束

- 后端解释器固定用仓库自带的 venv（3.11.9）；系统 Python 版本不对。
- Node `^20.19.0 || >=22.12.0`（Vite 的 `engines` 要求）。
- 第一次装环境跑 `bash scripts/bootstrap.sh`：建 venv、装后端依赖、在前端跑 `npm ci`。CI 走同一条路，否则「本地绿、CI 红」只能靠猜。

### 2.3 容器形态

- [`docker-compose.yml`](../docker-compose.yml) 两个服务：`backend`（healthy 后前端才起）+ `frontend`（nginx）。
- **数据与日志用 bind mount**（`./backend/data`、`./backend/logs`），不用命名卷：宿主机上的备份与巡检脚本要操作**容器正在用的那一份**文件，否则「备份的是不是正在跑的那份库」只能靠推断。
- **入口页不许缓存**：`frontend/nginx.conf` 对 `/index.html` 加 `Cache-Control: no-store`。否则容器重建后浏览器仍拿旧 bundle，出现「改了没生效」。
- **凭据不进镜像**：`backend/.env` 被 `backend/.dockerignore` 排除，运行时由 compose 的 `env_file` 注入。
- **`JWT_SECRET` 缺失则后端拒绝启动**：这是设计好的失败，不是待修的 bug。

**判据**：读者改完代码知道要做什么（重启后端 / 重建镜像）、知道凭据在哪、知道为什么容器里没有 `.env`。

---

## 3. 模块地图

**这一节服务维护者。**

### 3.1 后端（25 个模块）

| 模块 | 职责 |
| --- | --- |
| `backend/main.py` | 应用入口、生命周期（建表、种子管理员、退出清理）、健康检查、非模块化路由 |
| `backend/api_auth.py` | 注册、登录、本人信息 |
| `backend/api_summarize.py` | 两个 SSE 端点的编排：取字幕 → 调模型 → 扣额度 → 落库 → 吐事件（占位与覆盖闸门在 `backend/video_claim.py`，ADR 0031） |
| `backend/api_community.py` | 社区浏览、搜索、发布、标签词表 |
| `backend/api_history.py` | 解析历史、收藏、会话 |
| `backend/api_payment.py` | 订单、支付回调、VIP 判定 |
| `backend/admin_api.py` | 管理后台：用户、社区、模型清单、标签词表 |
| `backend/auth.py` | JWT、bcrypt、三个鉴权依赖 |
| `backend/database.py` | SQLite 数据层：建表、迁移、额度、仲裁、检索 |
| `backend/summarizer.py` | 字幕提取 + 模型调用（总结 / 导图 / 问答 / ASR 兜底） |
| `backend/video_claim.py` | 解析占位权与覆盖闸门：谁能调模型、谁只能等着；退出上下文即释放（ADR 0031） |
| `backend/credentials.py` | 用户自带凭据的解析与脱敏 |
| `backend/model_catalog.py` | 模型清单表与默认模型 |
| `backend/url_canonical.py` | 链接的比较用归一 |
| `backend/tags.py` | 固定标签词表与校验 |
| `backend/downloader.py` | yt-dlp 封装、格式归类、下载 |
| `backend/douyin.py` | 抖音解析 |
| `backend/formats.py` | 清晰度 / 音轨归类 |
| `backend/prompt_template.py` | 总结与导图的 prompt 模板 |
| `backend/mindmap.py` | 思维导图的结构规则：深度 / 每支扇出 / 全图节点数上限 |
| `backend/provider_config.py` | 平台 provider 的协议 / 端点 / 模型配置与客户端构造（ADR 0028） |
| `backend/ratelimit.py` | 公开端点的速率闸门与并发闸门（ADR 0020 / 0021 / 0022） |
| `backend/download_artifacts.py` | 下载产物的落盘与「用完即删」清理口径 |
| `backend/cors_config.py` | 跨域白名单的解析与挂载：默认不开，`VIDDIGEST_ALLOWED_ORIGINS` 显式开口 |
| `backend/logging_setup.py` | JSON 行日志与轮转 |
| `backend/main.py` 之外的支撑 | 上述模块互不反向依赖：路由层 → 依赖层 / 编排层 → 数据层 → 支撑模块 |

### 3.2 前端

- `frontend/src/api/`：8 个客户端模块，axios + 自建 SSE 解析。
- `frontend/src/components/` + `frontend/src/views/`：共 54 个 `.vue`。页面级视图只有 `views/HomeView.vue`，`HistoryPage` / `CommunityPage` / `AdminPage` / `InviteCodesPage` 实际放在 `components/` 里。
- `frontend/src/router/index.ts`：5 条真实路径（`/`、`/history`、`/community`、`/admin`、`/admin/invite-codes`）。
- `frontend/src/stores/session.ts`：唯一的 store，持有登录态与管理员标志的内存镜像。回查 `/api/auth/me` 回 **401** 时（服务端不认这个 token）本地会话作废——localStorage 只是种子，服务端才是权威（ADR 0010）；**只有 401** 才作废，5xx 与网络故障是「问不着」不是「被拒」，那两种情况把人踢下线等于把一次抖动变成一次登出。
- `frontend/src/lib/byok.ts`：用户自带 Key 的唯一真值出口。
- `frontend/src/config/features.ts`：功能开关，`MEMBERSHIP_ENABLED = false`（会员入口按范围边界关着）。
- `frontend/tsconfig.json`：不开 `baseUrl`、不设 `paths`——源码里没有 `@/` 之类的别名，而 `baseUrl` 从 TypeScript 7 起失效；`vue-tsc --noEmit` 是门禁第 6 关。

### 3.3 三个唯一真值出口

| 事实 | 唯一出处 | 其它地方怎么用 |
| --- | --- | --- |
| 当前是哪个页面 | `frontend/src/router/index.ts` 的 `meta.page` | `App.vue` 里的 `currentPage` 是它的派生状态，不是独立事实源 |
| 登录态落盘 | `frontend/src/api/auth.ts` | `stores/session.ts` 只持内存镜像 |
| 用户自带凭据 | `frontend/src/lib/byok.ts` | `getPublicState()` 的返回类型里**结构上就没有 key 这个键** |

**判据**：给一个功能名，读者能在 10 秒内定位到要改的文件；并能说出某个状态的事实源在哪、谁只是它的副本。

---

## 4. 三条关键链路

**这一节服务维护者与面试官。** 每条链路给出步骤与失败表现。

### 4.1 链路 A：解析与下载

1. 前端提交链接 → `POST /api/parse`。
2. `backend/downloader.py` / `backend/douyin.py` 解析出格式列表；每条格式自报 `kind`（`video` / `audio`）。
3. 15 项上限内**先给音频留位**（否则「只下音频」在界面上不是选项）。
4. 下载走 `POST /api/download` 或 `GET /api/direct-url`；产物在进程退出时清理。

**失败表现**：链接不合法 → 前端本地正则先挡（`这不是一个链接`）；平台侧解析失败 → `backend/main.py` 抛 400，`detail` 里带 `success: false` 与 `error`；平台 WAF → 抖音模块有自己的绕过路径。

### 4.2 链路 B：AI 总结（SSE）

`POST /api/summarize`（`backend/api_summarize.py`）：

1. 占位：向 `videos` 插一行 `pending`，唯一索引仲裁出「谁负责这次解析」。
2. 取字幕：有字幕轨道 → 直接用；没有 → ASR 兜底（需单独配置）。
3. 一次模型调用产出三段产物：摘要、导图、标签（一次调用、一条输出，按哨兵切成三段）。
4. 扣额度：`UPDATE ... WHERE count < limit`，判据与赋值写在同一条 SQL。
5. 落库并吐事件。

**事件集合是写死的契约**（`backend/tests/test_sse_event_contract.py` 与前端两张路由表双向比对）：

| 端点 | 事件集合 |
| --- | --- |
| `summarize_video` | `subtitle`、`quota`、`summary`、`mindmap`、`tags`、`done`、`error`（7 个） |
| `chat_with_video` | `quota`、`answer`、`done`、`error`（4 个） |
| `_replay_events` | `subtitle`、`quota`、`summary`、`mindmap`、`tags`、`ownership`、`done`（7 个） |
| `fail` | `error`（1 个） |

**失败表现**：上游网关在正文之后补一帧只带 usage 的空帧 → 旧代码直接取 `chunk.choices[0].delta` 会 IndexError，被外层翻译成「凭据无效」，让用户看到「答案出来了，然后说一句凭据无效」（已修，见 §10 的工单引用）。

### 4.3 链路 C：社区与历史

1. 解析完成即进 `videos` 表（全局表，不是个人历史表）。
2. 社区浏览 / 搜索走 FTS5 虚表 + 触发器同步。索引覆盖标题、标签、链接、**总结与字幕**；正文里的词也搜得到（ADR 0030）。
3. `parse_history` 是个人记录：每用户保留 1000 条、单页 20 条，**收藏不参与滚动裁剪**。
4. 覆盖权由 `parsed_by` 判定，作者本人可覆盖自己的那一份。

**失败表现**：未登录访问需要身份的接口 → 鉴权依赖直接抛 401，不返回空、不返回半份。
前端把这一层翻译成「登录入口」，而不是把 401 甩给用户：本地没有登录态时**根本不发请求**
（历史列表、标签都不发），服务端真的回 401 时也翻译成同一个登录入口
（`frontend/src/components/HistoryPage.vue` 的 `needLogin` 分支 → `App.vue` 弹登录框）。
社区搜索那一侧同理（`frontend/src/api/community.ts` 的 `needLogin`）。

**判据**：维护者能复述每条链路的 5 步，并说出每一步失败时用户看到什么。

---

## 5. 数据与状态

**这一节服务维护者。**

### 5.1 存储

- SQLite，`journal_mode=WAL`、`foreign_keys=ON`；连接线程本地复用。
- **写是串行的**：`sqlite3.connect` 未传 `timeout`，走 Python 默认的 5 秒等待窗口；长事务会撞 `database is locked`。这是后端单进程建议的来源。
- **下载目录 `backend/downloads/` 是运行期产物**，不是数据：文件发完就删，构建上下文也排除它
  （不进镜像）。异常残留由巡检第 6 项按超龄收走，详见 `docs/OPERATIONS.md` §8.4。

### 5.2 表与关键列

| 表 | 位置 | 职责与关键列 |
| --- | --- | --- |
| `users` | `backend/database.py` | 账号；额度覆盖列 `parse_limit_override` / `chat_limit_override`、管理员标志 |
| `videos` | `backend/database.py` | 社区内容全局表；`canonical_url`、`parsed_by`、`status`、覆盖闸门两列 |
| `parse_history` | `backend/database.py` | 个人解析记录；`is_favorite` |
| `chat_messages` | `backend/database.py` | 追问记录 |
| `orders` | `backend/database.py` | 订单；`order_no` 与 `stripe_session_id` 各有唯一约束 |
| `model_providers` | `backend/model_catalog.py` | 模型清单（**元数据表**，凭据永远留在 `backend/.env`） |
| `videos_fts`（FTS5 虚表） | `backend/database.py` | 社区检索；索引标题、标签、链接、总结、字幕 5 列，由 3 个触发器与 `videos` 同步（ADR 0030） |

**仲裁的物理保证**：`videos` 上的唯一索引 `idx_videos_url`（`backend/database.py`）。同一链接并发插入时只有一个 INSERT 成功，另一个撞唯一索引后**另开事务重读**（不能在同一事务里读，否则读到的是已回滚的快照）。

### 5.3 迁移

- 用 `PRAGMA table_info` 探测缺列再补，幂等：老库缺列也能启动。
- 老库升级路径有专属用例：`backend/tests/test_users_legacy_upgrade.py`——`users` 表的老结构**逐字取自提交 `afab2b6`**（建表语句不凭记忆写），断言补列之后老行的语义没变（老账号不是管理员、覆盖列是 NULL 而不是 0、计数器初值是 0）；`backend/tests/mutation_legacy_users.py` 用 5 个变异确认这些用例真能转红。
- 仍然缺的一块：仓库无 tag / release，取不到真实的上一版本库作输入。老结构是从提交历史里逐字抄的，不等于从真实库导出。

### 5.4 额度与状态

| 项 | 当前值 | 位置 |
| --- | --- | --- |
| 解析额度 | 3 次 / 日 | `backend/database.py` 的 `DAILY_PARSE_LIMIT` |
| 追问额度 | 10 次 / 日 | `backend/database.py` 的 `DAILY_CHAT_LIMIT` |
| 两个计数器按 UTC 日期各自重置 | — | 同上 |
| 额度三态 | `>= 0` 已扣 / `-1` 无限 / `None` 没扣成 | **0 不表示没扣成**（上限 3 的第 3 次扣成功后剩余恰为 0） |
| 解析历史保留 | 1000 条，单页 20 条 | `backend/database.py` 的 `MAX_PARSE_HISTORY_PER_USER` / `HISTORY_PAGE_SIZE_DEFAULT` |
| 覆盖闸门 TTL | 1800 秒 | `backend/database.py` 的 `VIDEO_REGENERATE_TTL_SECONDS` |

**判据**：读者能回答「这张表能不能删」「这列谁写谁读」「额度剩 0 到底扣没扣成」。

---

## 6. 质量与门禁

**这一节服务两类读者。**

### 6.1 门禁的唯一入口

```bash
bash ./init.sh          # 退出码 0 = 已验证
```

`init.sh` 里真跑 11 关，顺序即执行顺序：

| # | 关卡 | 它证明什么 |
| --- | --- | --- |
| 1 | `backend: pytest` | 1521 条后端用例通过 |
| 2 | `backend: compileall` | 后端全部模块可编译 |
| 3 | `secrets: content-level scan` | 跟踪文件与可达 git 历史里没有凭据形态的字符串 |
| 4 | `frontend: npm test` | 538 条静态 / 契约断言通过 |
| 5 | `frontend: npm run test:mount` | 218 条挂载用例通过 |
| 6 | `frontend: npm run typecheck` | `vue-tsc --noEmit` 无类型错误 |
| 7 | `frontend: npm run lint` | ESLint 无告警 |
| 8 | `frontend: npm run format:check` | Prettier 全部文件符合格式 |
| 9 | `refs: ADR 与路径引用一致性` | 文档与注释里提到的 `ADR NNNN`、源码路径都还指得到 |
| 10 | `gate: 关卡数口径一致性` | 文档里写的关卡数等于 `init.sh` 真跑的关卡数（真值从 `init.sh` 数，不硬编码） |
| 11 | `eval: 社区检索质量` | 索引列集合与同步触发器的结构判定，加上固定语料上的 top-1 准确率、正文词召回、2 字词召回与零误召回 |

**第 9、10 关守的是门禁自身。** 规约、决策记录、测试策略与源码注释之间互相指着，被改写时**没有任何既有用例会红**——用例断言的是行为，不是「你写的这句话还指得对」。这不是假设：本仓确实出现过源码注释指向一篇不相干的 ADR。

**第 11 关守的是「搜得到」而不是「跑得通」。** 检索退化时的症状不是报错，而是「接口照常返 200，只是搜不出东西了」——索引少一列、正文没进索引、触发器漏写一列，三者的症状都是这个，任何「功能正常」的用例都发现不了。语料与阈值同源于 `scripts/eval/corpus.json`，合成语料不代表真实分布，它保证的是**可复算**。

### 6.2 变异装置

20 个脚本各破坏一条不变量（鉴权边界、链接归一、数据库夹具、支付回调、敏感内容扫描、门禁自身的引用与口径检查、社区检索的索引列与分词器……），断言关卡会转红。

- **在 CI 矩阵里跑，不在 `init.sh` 里跑**：变异运行慢、会改写源码，且与并行开发冲突。
- 每个脚本一个 job，互相独立、并行、互不污染（脚本自己按字节还原）。
- **变异存活先怀疑断言够不着它**，而不是先怀疑变异没用。

### 6.3 前端两套 runner

| runner | 收什么 | 证明什么 |
| --- | --- | --- |
| `node --test`（`*.test.mjs`） | 纯函数与跨语言契约断言 | 纯逻辑与契约层的行为 |
| `vitest run`（`*.spec.mjs`） | 真挂载（jsdom + `@vue/test-utils`） | 组件在真实生命周期里的行为 |

两者刻意分开：绿色的那一层遮不住红的那一层。`vitest.config.js` 的 `include` 只收 `*.spec.mjs`——默认 include 会顺手吃掉 `node:test` 的文件，而 `node:test` 的 `describe` 体抛异常时 `vitest` 仍退出 0，那会让这一关**恒绿**。

**判据**：读者知道改完跑哪条命令、什么算过、变异存活先查哪一侧。

---

## 7. 外部依赖与失败语义

**这一节服务维护者。**

| 依赖 | 角色 | 失败语义 |
| --- | --- | --- |
| `yt-dlp` | 主解析通道，覆盖大量平台 | 解析失败多为平台侧变化，升级 `yt-dlp` 是第一手段 |
| `backend/douyin.py` | 抖音独立通道 | 有自己的 WAF 绕过路径 |
| 模型（百炼 / 用户自带） | 总结、导图、问答 | **分支在实例化时选定，调用失败不跨厂商自动重试**；用户重试才重新选分支 |
| ASR | 没有字幕轨道时的兜底 | 需单独配置；失败原因随结果回传（`FAIL_NO_TRACK` / `FAIL_FETCH_FAILED` / `FAIL_ASR_NOT_CONFIGURED`） |
| SQLite | 唯一存储 | 写锁竞争 → `database is locked` |
| Stripe | 会员支付 | 回调按事件幂等处理 |
| 自家限流（`backend/ratelimit.py`） | 六个不挂鉴权的公开端点 | 超限返回 `429`，`Retry-After` 头写明等几秒；体是 `{"success": false, "code": "rate_limit", "error": ...}`，`code` 是机器可读的协议、中文只给人看；是**速率**闸门不是并发闸门（ADR 0020） |
| 自家并发闸门（`backend/ratelimit.py`） | 解析与下载两个重活端点 | 同一来源同时在跑的任务超上限就返回 `429`，同样带 `Retry-After`；体是 `{"success": false, "code": "concurrency_limit", "error": ...}`，`error` 说清是「同时在跑太多」；**立刻拒，不排队**（ADR 0021） |

**额度口径**：解析含字幕提取与批量生成，追问只是一次模型调用，因此拆成两个计数器（ADR 0003）。共用内容（社区里已有的那一份）不扣额度。

**判据**：遇到「AI 不通」，能分辨是分支、凭据、额度还是字幕缺失。

---

## 8. 故障定位表

**这一节服务维护者。** 每行给一条可直接粘贴的命令。

| 症状 | 首查命令 | 常见根因 | 落点 |
| --- | --- | --- | --- |
| 门禁第 1 关红 | `cd backend && ./venv/Scripts/python.exe -m pytest tests -q` | 用例失败或基线本来就红 | 按堆栈定位；先读完整堆栈再改 |
| 门禁第 3 关红（凭据形态） | `backend/venv/Scripts/python.exe scripts/check_secrets.py` | 凭据被抄进文档或提交说明 | 把内容从文件里拿掉，别只靠 `.gitignore` |
| 门禁第 9 关红 | `backend/venv/Scripts/python.exe scripts/check_refs.py` | 文档或注释里的 `ADR NNNN`、路径锚点指不到 | 改引用或补文件；不要放宽检查 |
| 门禁第 10 关红 | `backend/venv/Scripts/python.exe scripts/check_gate_consistency.py` | 某处写了与 `init.sh` 不一致的关卡数 | 以 `init.sh` 为准改文档 |
| 门禁第 11 关红 | `backend/venv/Scripts/python.exe scripts/eval/check_search_quality.py` | 索引列集合与触发器不同步，或正文召不回 | 看输出里是哪一条 `<FAIL>`：结构轨红 = 索引与触发器不一致；质量轨红 = 召回掉了。ADR 0030 |
| `database is locked` | `backend/venv/Scripts/python.exe scripts/ops/inspect_runtime.py` | 长事务占住写锁；SQLite 写入串行 | 缩事务；确认后端是单进程（§1.3） |
| 解析报「解析失败」 | `backend/venv/Scripts/python.exe scripts/ops/inspect_runtime.py --health-url http://127.0.0.1:8000/api/health` | 平台侧变化、链接不支持、网络 | 先换已知可解析的链接验证通路；再升级 `yt-dlp` |
| 总结不出字幕 | 看 SSE 事件序列里有没有 `subtitle` | 没有字幕轨道且 ASR 未配置 | 配 ASR，或用带字幕的视频 |
| 额度提前用完 | `backend/venv/Scripts/python.exe scripts/ops/inspect_runtime.py` | 解析与追问是两个计数器；失败会退款 | 看 `users` 的两个计数列与当天日期列 |
| 请求收到 429 | `curl -i http://127.0.0.1:8000/api/parse` 看 `Retry-After` | 同一来源 IP 在固定窗口内用满了该端点的额度（ADR 0020） | 读响应里的 `Retry-After` 决定等多久；上限可用 `VIDDIGEST_RATE_LIMIT_*` 覆盖；响应体里的 `detail.code` 说明是哪道闸门（`rate_limit` = 来得太勤，`concurrency_limit` = 同时在跑太多） |
| 伪造 `X-Forwarded-For` 能绕开限流（连打很多次也不出 429） | `backend/venv/Scripts/python.exe scripts/ops/check_xff_bypass.py` | 边缘代理没**覆盖**这个头（用了 `$proxy_add_x_forwarded_for`），或后端取的不是末段 | 边缘覆盖 + 后端取末段，两边都要（ADR 0022）；信任模型与部署口径见 `docs/OPERATIONS.md` §10.4 |
| SSE 中途断流 | 看 `backend/logs/app.log` 的 JSON 行 | 上游断流或网关补空帧 | 空帧已被 `backend/summarizer.py` 的 `_iter_delta_text` 跳过；真断流无自动重连 |
| 前端改了没生效 | `curl -I http://127.0.0.1:8080/index.html` | 浏览器缓存了入口页 | 入口页已标 `no-store`；重建镜像后再验 |
| 端口连不上 | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health` | Vite 只监听 IPv6 回环；宿主端口被占 | 用 `http://localhost:5173`；换端口启动 |
| 下载目录一直变大 | `backend/venv/Scripts/python.exe scripts/ops/inspect_runtime.py` | 下载失败/进程被杀留下的残留没人收 | 巡检第 6 项按超龄清（`--max-download-age-hours`，下限 1 小时）；见 `docs/OPERATIONS.md` §8.4 |
| 后端起不来 | 看启动日志里是否提到 `JWT_SECRET` | 缺 `JWT_SECRET` 是设计好的拒绝启动 | 在 `backend/.env` 里配置 |

**不在本仓的问题**：平台反爬、厂商限流、模型上下文长度限制。

**判据**：拿到症状，5 分钟内定位到文件或环境变量。

---

## 9. 设计决策索引

**这一节服务面试官与评审。** 每条只给「一句话 + 代价 + 链接」，正文在 ADR 里。

| ADR | 决策 | 代价 |
| --- | --- | --- |
| [0001](adr/0001-global-videos-table.md) | 社区视频用独立全局表 | 无法为同一视频累积多版本 |
| [0002](adr/0002-store-subtitles.md) | 字幕全文随社区视频存储 | 存储体积随社区线性上升 |
| [0003](adr/0003-split-quota-counters.md) | 解析额度与追问额度分离 | 用户要理解两套余额；加第三种要改三处 |
| [0004](adr/0004-byok-forwarded-not-stored.md) | 用户自带 Key 由服务端转发但绝不落盘 | 服务端仍是流量中间人，隐私姿态弱于浏览器直连 |
| [0005](adr/0005-fixed-tag-vocabulary.md) | 标签取自人工固定词表 | 新增标签要改代码发版 |
| [0006](adr/0006-first-parse-wins.md) | 重复解析保留首份且不重复计费 | 后来者仍白跑一次字幕提取 |
| [0007](adr/0007-owner-may-regenerate-own-summary.md) | 首份可由作者本人覆盖 | 同一作者可能留下多代改写，没有版本选择器 |
| [0011](adr/0011-model-catalog-in-db-keys-stay-in-env.md) | 模型清单入库，平台凭据留在 `.env` | 换厂商要改表数据 |
| [0012](adr/0012-admin-user-lifecycle.md) | 账号生命周期进管理后台 | 删号语义需要额外约束（有内容即 409） |
| [0013](adr/0013-community-moderation.md) | 社区审核：改标签、真删条目 | 删条目只删社区行，用户历史不动 |
| [0014](adr/0014-card-fields-written-at-reservation.md) | 卡片字段在占位时写入 | 早期写入让占位行承担展示字段 |
| [0015](adr/0015-regenerate-gate-in-db.md) | 覆盖闸门落库 | 每次覆盖多两个写事务，并触发一轮 FTS 重建 |
| [0016](adr/0016-url-canonical-compare-canonical-echo-original.md) | 比较用规范值，回显用原文 | 同一份数据上并存两种表示 |
| [0018](adr/0018-vue-router-adopted.md) | 引入 `vue-router`，取代手写路由 | 多一个运行时依赖；`/admin` 需要服务端回落 |
| [0019](adr/0019-container-ops-evidence-chain.md) | 容器化与运维证据链 | bind mount 让容器与本机开发共用一份数据文件 |
| [0020](adr/0020-public-endpoint-rate-limit.md) | 公开端点按来源 IP 限速 | 进程内计数，重启清零；多实例各数各的 |
| [0021](adr/0021-public-endpoint-concurrency-gate.md) | 解析与下载再加一道并发闸门 | 进程内计数，重启清零；超限立刻拒，不排队 |
| [0022](adr/0022-reverse-proxy-client-identity.md) | 边缘覆盖 `X-Forwarded-For`，后端取末段 | 前置多一层代理时不配 realip 会让所有用户同桶、集体误伤 429 |
| [0030](adr/0030-community-search-body-indexing.md) | 社区检索索引正文与字幕，2 字词走子串兜底 | 索引体积翻近一倍；2 字兜底是全表扫描、没有相关度 |

范围界定类：[0010](adr/0010-admin-console-scope-and-auth.md)（管理后台的边界；其后的账号生命周期与社区审核由 0012 / 0013 扩展）。实施层：[0008](adr/0008-graphite-lake-blue-visual-identity.md)（视觉）、[0009](adr/0009-theme-override-via-css-variables.md)（主题）、[0019](adr/0019-container-ops-evidence-chain.md)（运维）。已被取代：[0017](adr/0017-lightweight-routing-no-vue-router.md)（手写轻量路由，被 0018 取代）。

上表只列与「数据、检索、限流、部署」这条主线相关的决策。其余决策按编号查原文：[0023](adr/0023-invite-code-registration.md)（注册要邀请码）、[0025](adr/0025-invite-codes-management-page.md)（邀请码管理页）、[0026](adr/0026-summary-template-catalog.md)（摘要模板清单单一真值）、[0027](adr/0027-mindmap-levels-and-legacy-rebuild.md)（思维导图层级与存量重建）、[0028](adr/0028-platform-model-and-provider-protocols.md)（平台模型与厂商协议）、[0029](adr/0029-mindmap-single-source-and-legacy-truncation.md)（思维导图单一真源与存量截断）。

**判据**：对任一模块能问出「为什么是这样」并点到对应 ADR。

---

## 10. 已知边界与未修事项

**这一节服务两类读者。** 写在这里的都是**当前的真实状态**，不是待办清单。

### 10.1 已核实的文档口径冲突

当前仍然存在的冲突只有一条：

| # | 位置 | 现在的说法 | 真值 | 该谁改 |
| --- | --- | --- | --- | --- |
| 1 | `docs/API.md` | 44 条端点只有 6 条有字段级详情，没有统一的错误响应字典 | 端点清单以代码为准 | API 文档（未修，属缺口） |

### 10.2 验收结论里的未修项

收尾验收（验收测试工程师视角）判定为「**有条件可交付：功能面可交付，生产运维面不可交付**」。经过第二次独立验收复核，以下是**当前仍未修**的部分——已经修掉的条目不再列在这里。

| 级别 | 事项 | 现状 |
| --- | --- | --- |
| P1 | 多实例与跨进程并发写无验证 | 只有同进程多线程用例；compose 单实例、单 worker |
| P1 | 真实第三方 API 传输层故障无注入 | 超时、连接重置、429 带 `Retry-After`、5xx、流中途断开都无覆盖 |
| P1 | 生产形态备份恢复无演练 | 备份脚本这一侧是闭合的（在线备份 + `integrity_check` + 防撞名 + 6 条用例 + 3 个变异）；仍缺两件：运维手册里的手动备份还是裸拷贝口径，以及一次真正的恢复演练 |
| P1 | 跨端与无障碍无自动化 | 无视口断点断言，全仓没有 `prefers-reduced-motion` 降级，而像素动画有无限循环 |
| P1 | 用户自带 Key 长期留在 localStorage | 切回平台模式不清除 |
| P1 | 必过检查依赖外部包仓库与镜像源 | 「门禁」「生产构建」两个必过检查都要拉 npm 包与基础镜像，外部抖动会直接挡住合并 |
| P2 | 变异装置未覆盖大体量模块 | 社区库、模型清单等模块没有被变异确认过（不等于没有测试） |
| P2 | 敏感扫描只覆盖可达历史 | 被 force-push 掉的凭据仍可从对象库取回（force push 现在被保护规则挡住，但对象库里的旧对象还在） |
| P2 | 后端依赖不可复现 | `backend/requirements.txt` 全是下界、无锁无 hash；前端有锁文件，后端没有；也没有 SBOM |
| P2 | 容器运行态细节未在 Linux 环境验 | 非 root 用户（uid 10001）对 bind mount 目录的可写性只在文档里 |
| P2 | 日志与凭据边界无机器断言 | 只有注释声明，没有自动化断言 |
| P2 | SSE 真实断流、超时、重连无用例 | 前端测试用假流解析契约 |
| P2 | OS 矩阵缺口 | CI 只有 ubuntu；Windows 仅本地，macOS 从未跑过 |
| P2 | 保护规则与 CODEOWNERS 不在仓库内 | master 的保护设置只存在于 GitHub，仓库内没有任何机制会告警它漂移；`.github/CODEOWNERS` 目前只是声明（未开人工 approve：单人仓库里 PR 作者不能 approve 自己的 PR） |
| P2 | 依赖的大版本升级没有人工评估的记录 | `.github/dependabot.yml` 放行补丁与次版本，`typescript >=6`、`vue-router >=5`、`@vueuse/core >=15` 三个大版本被 ignore 挡住——它们要进来得先读 changelog 并单独验一条链路 |
| P2 | 前置 CDN / 多层反代的来源身份未验证 | 边缘覆盖 `X-Forwarded-For` + 后端取末段已就位，并有端到端守卫 `scripts/ops/check_xff_bypass.py`；但前置再架一层 CDN 时的 realip 还原没在本仓验证过——不配 `set_real_ip_from` / `real_ip_header` 会让所有用户落进同一个桶、集体误伤 429（ADR 0022）。另外直连后端端口（不经 nginx）时单段 `X-Forwarded-For` 仍可自报，所以后端端口不得对外暴露 |

### 10.3 明确不做

范围边界以 `AGENTS.md` 为准：会员制、点赞 / 收藏 / 评论 / 关注、内容审核与举报流程、下载功能本身的改动（纯音频下载除外）、推荐算法与个性化排序、移动端原生应用、多语言界面、社区视频下架、封禁 / 解封账号。

另外三件是**有意不做**，不是没做完：

- **多实例部署下的共享并发计数**：并发闸门的计数在进程内（ADR 0021）。要跑成两个以上进程 / 容器共享同一份数据库文件时，计数得换成共享存储——那是另一次改造，本轮不做。
- **公开端点仍不做鉴权**：六个公开端点不挂鉴权依赖是**有意保留**的（ADR 0020、ADR 0021）。防滥用靠速率闸门 + 并发闸门 + 落盘配额这三层兜，不靠鉴权。
- **分支保护的人工审核**：单人仓库里 PR 作者不能 approve 自己的 PR，开「至少 1 个 approve」就是死锁。所以 master 的保护只开「必过检查 + 生产构建」，不指望人工审核（见 `docs/OPERATIONS.md` §14.1.1）。

### 10.4 跨端耦合

有些口径横跨前后端，改一边就必须改另一边：

- **自家 429 的分类协议是 `detail.code`，不是中文文案**：后端在 429 的错误体里带
  `code`（`rate_limit` / `concurrency_limit`，真值在 `backend/ratelimit.py`），前端在
  `frontend/src/lib/errors.ts` 的 `looksLikeServerRateLimit` /
  `looksLikeServerConcurrencyLimit` 里先认这个字段。改文案不会改分类。
- **残留的一处文案耦合在兼容分支**：老后端（本地进程还没重启、错误体里没有
  `code`）返回的 429，前端会退回按中文子串匹配后端 `error` 文案
  （`服务器限流` / `同时进行的任务太多` / `同时在跑的任务已超过` / `并发上限`）。
  改后端文案时这几条子串要一起改——只在开发期没重启后端时才会撞上。

**判据**：读者分得清「已知问题」与自己踩到的新坑，也知道哪些是产品边界而不是没做完。

---

## 11. 这份文档怎么维护

**这一节服务维护者。**

1. **只写现在是什么。** 发现旧数字、旧方案、已结束的阶段编号，写成 §10.1 那样的冲突行，不写进正文。
2. **引用而不复述。** 每节至少两处外链；复制别处的正文不超过 3 行。
3. **数字必须带来源。** 每个数字写口径 + 复现命令 + 真值文件；写不出复现命令的数字不写。
4. **冲突先报告，不代改。** 别处与代码不一致时，在 §10.1 写「谁说什么 / 真值在哪 / 该谁改」，修法走独立工单。
5. **路径与 ADR 编号必须真实存在**，行号不超过目标文件的行数——门禁第 9 关会逐个校验。
6. **每节标服务对象**，每节给一个「读完能回答什么」的判据。
7. **改完跑 `bash ./init.sh`**，贴本次输出与退出码；退出 0 之前不要标记完成。
8. **改了本文要同步**：`README.md` / `README.en.md` 的验证表、`docs/README.md` 的索引、§10.1 的冲突清单。

**判据**：任何人改完能自查是否违反门禁、是否留下新的过期数字。

---

## 附录 A：数字来源清单

每个数字一条：口径 + 复现命令 + 真值文件 + 变化时机。

| # | 数字 | 口径 | 复现命令 | 真值文件 | 变化时机 |
| --- | --- | --- | --- | --- | --- |
| A1 | 44 | 非测试 Python 里的 FastAPI 路由装饰器计数 | `grep -rnE '@(app\|router)\.(get\|post\|put\|delete\|patch)' backend --include=*.py \| grep -vE 'venv\|tests' \| wc -l` | `backend/main.py` 等路由模块 | 随路由增删 |
| A2 | 32 | `get_current_user` 15 + `require_admin` 14 + `get_optional_user` 3（端点级：不含 `backend/auth.py` 里 `require_admin` 自身的 `Depends(get_current_user)`，也不含 `backend/api_community.py` 顶部文档里提到的 `Depends(get_current_user)`） | `grep -rnE 'Depends\((get_current_user\|require_admin\|get_optional_user)\)' backend --include=*.py \| grep -vE 'venv\|tests' \| grep -vE 'backend[/\\]auth\.py' \| grep -vE 'api_community\.py:17' \| wc -l` | `backend/auth.py` + 各路由模块 | 随依赖增减 |
| A3 | 2 | 返回 `EventSourceResponse` 的路由 | `grep -c 'response_class=EventSourceResponse' backend/api_summarize.py` | `backend/api_summarize.py` | 随流式端点增删 |
| A4 | 1533 | pytest 收集到的用例数 | `cd backend && ./venv/Scripts/python.exe -m pytest tests -q --collect-only` | `backend/tests/` | 每次加删用例 |
| A5 | 538 | node:test 断言数 | `cd frontend && npm test` | `frontend/tests/*.test.mjs` | 每次加删断言 |
| A6 | 218 | vitest 用例数 | `cd frontend && npm run test:mount` | `frontend/tests/*.spec.mjs` | 每次加删用例 |
| A7 | 24 | `scripts/` 11 + `backend/tests/` 9 + `frontend/tests/` 4 | `ls scripts/mutation_*.py backend/tests/mutation_*.py frontend/tests/mutation*` | 同左 + `.github/workflows/ci.yml` 的矩阵 | 随脚本增删 |
| A8 | 11 | `init.sh` 真跑的关卡数（真值由脚本从 `init.sh` 数出） | `bash ./init.sh` 的收尾行；`backend/venv/Scripts/python.exe scripts/check_gate_consistency.py` | `init.sh` | 改 `init.sh` 即改 |
| A9 | 4 个 job 定义 / 26 个实例 | 1 个门禁 job + 1 个生产构建 job + 20 个后端变异实例 + 4 个前端变异实例 | 读 `.github/workflows/ci.yml` 的矩阵 | `.github/workflows/ci.yml` | 工作流改则改 |
| A10 | 7 张业务表 + 1 张 FTS5 虚表 + 6 条触发器（3 条 `*_fill_canonical` + 3 条 `videos_fts_*`） | 库里实际存在的建表 / 建虚表 / 建触发器对象（FTS5 自带的 4 张影子表 `videos_fts_data` / `_idx` / `_docsize` / `_config` 不计） | `backend/venv/Scripts/python.exe -c "import sqlite3;print(sqlite3.connect('backend/data/app.db').execute(\"select type,name from sqlite_master where name not like 'sqlite_%'\").fetchall())"` | `backend/database.py`、`backend/model_catalog.py` | 迁移则改 |
| A11 | 30 | `docs/adr/` 下的文件数（29 篇生效 + 1 篇已被取代） | `ls docs/adr/*.md \| wc -l` | `docs/adr/` | 随决策增补 |
| A12 | 50 | 版本库里受管理的 Markdown 文件数 | `git ls-files '*.md' \| wc -l` | 整个仓库 | 随文件增删 |
| A13 | 72（`ready-for-agent` 标签）/ 85（全部工单） | 编码代理可领的工单数 / 工单总数（关 + 开） | `gh issue list --state all --limit 200 --json number,labels --jq '[.[] \| select(any(.labels[]; .name=="ready-for-agent"))] \| length'` 与 `gh issue list --state all --limit 200 --json number --jq 'length'` | GitHub Issues | 工单增删 |
| A14 | 解析 3 / 追问 10 | 每日额度上限，可由环境变量覆盖 | `grep -n 'DAILY_PARSE_LIMIT\|DAILY_CHAT_LIMIT' backend/database.py` | `backend/database.py` | 常量可改 |
| A15 | 1000 / 20 | 解析历史保留条数 / 单页条数 | `grep -n 'MAX_PARSE_HISTORY_PER_USER\|HISTORY_PAGE_SIZE_DEFAULT' backend/database.py` | `backend/database.py` | 常量可改 |
| A16 | 1800 | 覆盖闸门 TTL（秒） | `grep -n 'VIDEO_REGENERATE_TTL_SECONDS' backend/database.py` | `backend/database.py` | 常量可改 |
| A17 | 7 / 4 / 7 / 1 | 四个端点的事件集合大小 | `backend/tests/test_sse_event_contract.py` 的期望表 | `backend/tests/test_sse_event_contract.py` + `frontend/src/api/summarize.ts` | 随事件增减 |
| A18 | 55 / 8 / 25 | 前端 `.vue` 数 / `api/` 客户端数 / 后端 `.py` 模块数 | `find frontend/src -name '*.vue' \| wc -l`；`ls frontend/src/api/*.ts \| wc -l`；`ls backend/*.py \| wc -l` | 整个仓库 | 随文件增删 |
| A19 | 20 / 10 / 30 / 60 / 10 / 5 | 六个公开端点的每分钟上限（解析 / 下载 / 直链 / 缩略图 / 登录 / 注册）；可用环境变量覆盖，0 表示不限流 | `grep -n 'VIDDIGEST_RATE_LIMIT' backend/ratelimit.py` | `backend/ratelimit.py`（ADR 0020） | 常量可改 |

**两条口径说明**：①「11 关」的真值只在 `init.sh`，`scripts/check_gate_consistency.py` 从那里数；本文与任何文档都不得再出现与它不一致的关卡数。②「4 个 job 定义，展开 26 个实例」与运维手册里的「四个 job」不矛盾：前者是展开后的实例数，后者是工作流里的 job 定义数。③限流上限是「每个来源 IP 每个固定窗口」；计数在进程内，重启即清零，多实例部署时每个实例各算各的。

---

## 附录 B：相关文档索引

| 文档 | 它负责什么 | 本文与它的关系 |
| --- | --- | --- |
| [`README.md`](../README.md) | 上手路径、安装、最小配置、用户可见功能、FAQ | §2 只引用启动与环境要求 |
| [`README.en.md`](../README.en.md) | 英文画像：架构图、技术栈、ADR 一览、文档索引 | §9 只引用 ADR 编号与链接 |
| [`docs/API.md`](API.md) | 端点的字段级契约、请求与响应示例 | §4 只讲端点在链路中的位置与失败表现 |
| [`docs/OPERATIONS.md`](OPERATIONS.md) | 部署、启停、备份、巡检、排查步骤、安全清单、CI 说明 | §8 索引到具体章节，不复制排查正文 |
| [`docs/TESTING-STRATEGY.md`](TESTING-STRATEGY.md) | 测试方法论、覆盖缺口、变异装置、示例写法 | §6 与 §10 引用其结论，不复述写法 |
| [`docs/AI功能核心技术说明.md`](AI功能核心技术说明.md) | 字幕提取、总结与导图与问答的实现细节、SSE 事件序列、错误链 | §4 引用机制细节，只在本文讲失败语义 |
| [`GLOSSARY.md`](../GLOSSARY.md) | 领域词唯一真值 | 领域词首次出现给链接，不在本文重新定义 |
| [`docs/adr/`](adr/) | 决策与取舍，编号只增不改 | §9 只给一句话代价 + 链接 |
| [`docs/README.md`](README.md) | 文档索引 | 本文是它表格里的一行 |
| [`AGENTS.md`](../AGENTS.md) | 范围边界、验证命令口径、工作规则 | §10.3 引用范围边界，不复制工作规则 |
