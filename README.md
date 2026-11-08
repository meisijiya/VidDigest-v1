> **本项目已归档（2026-11-08）。** 现网服务已下线，本仓只作参考基线，不再接受功能开发。
> 后继项目见 [meisijiya/VidDigest](https://github.com/meisijiya/VidDigest)。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/VidDigest-AI%20视频理解与下载平台-7c3aed?style=for-the-badge&logo=github">
    <img alt="VidDigest" src="https://img.shields.io/badge/VidDigest-AI%20视频理解与下载平台-2563eb?style=for-the-badge&logo=github">
  </picture>
</p>

<p align="center">
  <a href="#-功能特性">功能</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-安装指南">安装</a> ·
  <a href="#-使用指南">使用</a> ·
  <a href="#-配置">配置</a> ·
  <a href="#-文档">文档</a> ·
  <a href="#-常见问题">FAQ</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Vue_3-4FC08D?logo=vuedotgithub&logoColor=white" alt="Vue 3">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/yt--dlp-1800+-blue" alt="yt-dlp 1800+">
  <img src="https://img.shields.io/badge/LLM-AI-8b5cf6" alt="LLM AI">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

<p align="center">
  <b>中文</b> · <a href="README.en.md">English</a>
</p>

---

<p align="center">
  <b>VidDigest</b> 是一个基于 yt-dlp + LLM 的全栈视频理解与下载平台。<br>
  粘贴链接即可获得 <b>AI 总结、思维导图、AI 问答</b>，同时支持 <b>1800+</b> 平台视频解析下载。
</p>

---

## ✨ 功能特性

| 功能 | 说明 | 免费 |
|------|------|:----:|
| 🧠 **AI 总结摘要** | LLM 分析字幕生成结构化总结，四种模板可选（详细总结 / 精简摘要 / 步骤教程 / 深度分析） | 每日 3 次 |
| 🗺️ **思维导图** | AI 提取知识结构，生成可缩放拖拽的交互式导图（最多 3 层、每层 7 个分支、合计 30 个节点） | 与总结共用 |
| 💬 **AI 问答** | 基于视频内容对话，追问细节、深入理解 | 每日 10 次 |
| 📝 **字幕导出** | 提取并导出 SRT / TXT 格式字幕 | 随解析 |
| 🎬 **视频解析下载** | YouTube、B站、抖音等 1800+ 平台，选择清晰度一键下载 | ✅ 不限次 |
| 🎧 **纯音频下载** | 只要音频时可选平台原生音频容器（如 m4a / webm-opus / mp3），不做转码 | ✅ 不限次 |
| 🗂️ **社区浏览** | 公开社区：任何人可读他人已解析的总结、字幕、思维导图 | ✅ 不消耗额度 |
| 🕘 **解析历史** | 按关键词 / 标签 / 收藏筛选自己的解析记录 | ✅ |
| 🛠️ **管理后台** | 用户额度、管理员标记、用户增删、社区条目与标签词表、模型清单 | 仅管理员 |
| 🎟️ **邀请码管理** | 批量发放 / 作废邀请码，明文只在创建成功时显示一次（`/admin/invite-codes`） | 仅管理员 |
| 🛡️ **限流保护** | 公开端点按来源 IP 限速，重任务有并发闸门（默认同时 2 个） | ✅ |

> 视频解析下载无需登录，AI 功能需要注册账号。**注册需要邀请码，从管理员处获取。**
> 免费额度是**两个独立计数器**：
> 每日 3 次解析（产出总结 + 思维导图 + 标签）、每日 10 次追问；
> 读社区里别人已解析的内容**不消耗任何额度**。
> 具体数值由环境变量 `VIDDIGEST_DAILY_PARSE_LIMIT` / `VIDDIGEST_DAILY_CHAT_LIMIT` 控制。

---

## 🖼️ 界面预览

<table>
  <tr>
    <td width="50%" align="center"><img src="docs/screenshots/home.png" alt="首页：粘贴链接即可解析" /><br><sub>首页</sub></td>
    <td width="50%" align="center"><img src="docs/screenshots/home-dark.png" alt="暗色主题" /><br><sub>暗色主题</sub></td>
  </tr>
</table>

<p align="center">
  <img src="docs/screenshots/features.png" alt="核心功能" width="820" />
</p>

> 截图取自本机 `docker compose up -d --build` 跑起来的当前版本（1440×900）。

---

## 🚀 快速开始

```bash
# 后端（Windows；Linux/macOS 见安装指南）
cd backend
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env             # 配置 API Key（见下方）
venv\Scripts\python.exe main.py    # → http://localhost:8000

# 前端（新终端）
cd frontend
npm install
npm run dev                    # → http://localhost:5173
```

> ⚠️ **后端一律用 `venv\Scripts\python.exe`**，不要用 `python main.py`
> —— 那会用系统 Python，而本项目的依赖装在 venv 里。
> 也不要用 `source venv/Scripts/activate`（那是 Windows 的 activate 批处理，
> 在 Git Bash / Linux 下不适用）。

浏览器打开 **http://localhost:5173** → 粘贴视频链接 → 解析下载 / AI 总结。

> ⚠️ 前端用 `localhost` 而不是 `127.0.0.1`：Vite 只监听 IPv6 回环，
> `127.0.0.1:5173` 连不上。

### 🐳 或者用 Docker 跑

仓库自带 `docker-compose.yml`，一条命令起前后端（后端通过 `env_file` 读
`backend/.env` 里的密钥，镜像里不含任何密钥）：

```bash
docker compose up -d --build
curl http://127.0.0.1:8000/api/health     # → {"status":"ok"}
# 前端入口：http://127.0.0.1:8080
# 前端端口把 /api/ 反代到同一个后端，健康检查也一样通：http://127.0.0.1:8080/api/health
docker compose down                       # 停掉并删掉容器
```

> 停容器**不删数据**：`backend/data` 与 `backend/logs` 是 bind mount，
> SQLite 库文件留在宿主机上，容器内外看到的是同一份。

---

## 📦 环境要求

| 工具 | 最低版本 | 用途 |
|------|:--------:|------|
| Python | 3.11+ | 后端运行（开发实测 3.11.9） |
| Node.js | 20.19+ 或 22.12+ | 前端构建（Vite 8 的硬要求，**Node 18 装不上**） |
| npm | 10+ | 前端依赖管理 |
| ffmpeg | 任意 | 高清视频音画合并（强烈建议安装） |

> **ffmpeg** 用于合并 YouTube 等平台的视频+音频流。未安装时自动降级为单一格式。
> - Windows: 从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载，将 `bin` 目录加入 PATH
> - macOS: `brew install ffmpeg`
> - Linux: `sudo apt install ffmpeg`

---

## 🔧 安装指南

### 后端

```bash
# Windows
cd backend
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env             # 编辑 .env 填入密钥
venv\Scripts\python.exe main.py
```

```bash
# Linux / macOS
cd backend
python3 -m venv venv
source venv/bin/activate           # 注意是 bin/activate，不是 Scripts/
pip install -r requirements.txt
cp .env.example .env
python main.py
```

> 改了 Python 代码要**重启进程**：`main.py` 没有 `--reload`。

### 前端

```bash
cd frontend
npm install
npm run dev                      # 开发模式 → http://localhost:5173
npm run build                    # 日常构建 → dist/
npm run release:build            # 发布用：先清空 dist/ 再构建（避免陈旧 bundle 上线）
```

生产构建产物需要**静态托管 + SPA rewrite**（否则刷新 `/admin` 得到 404）。
完整 Nginx / Caddy 配置与上线后自检见
[docs/OPERATIONS.md §11.5](docs/OPERATIONS.md#115-生产静态托管spa-rewrite)。

---

## ✅ 验证

第一次跑之前先装环境（两栈一起装）：`bash scripts/bootstrap.sh`
——它会建 `backend/venv`、装后端依赖，并在 `frontend/` 跑 `npm ci`。

本仓库只有一个门禁，它决定一次改动能不能算「做完了」：

```bash
bash ./init.sh          # 退出码 0 = 已验证
```

| # | 检查 | 它证明什么 |
|---|------|-----------|
| 1 | `backend: pytest` | 后端全部用例通过 |
| 2 | `backend: compileall` | 后端全部模块可编译 |
| 3 | `secrets: content-level scan` | 跟踪文件**与可达 git 历史**里没有凭据形态的字符串（`.gitignore` 只挡文件名，挡不住被抄进文档的口令） |
| 4 | `frontend: npm test` | 前端全部静态 / 契约断言通过（node:test） |
| 5 | `frontend: npm run test:mount` | 前端 218 条挂载层用例通过（Vitest）——两套 runner 刻意分开，绿色的那一层遮不住红的那一层 |
| 6 | `frontend: npm run typecheck` | `vue-tsc --noEmit` 无类型错误（全部 `.vue` 组件与 TS 模块一起过一遍类型检查） |
| 7 | `frontend: npm run lint` | ESLint 无告警（未使用的变量、无用转义、模板可访问名缺失都在这一关现形） |
| 8 | `frontend: npm run format:check` | Prettier 全部文件符合格式 |
| 9 | `refs: ADR 与路径引用一致性` | 文档与注释里提到的每个 `ADR NNNN`、每条源码路径都还指得到 |
| 10 | `gate: 关卡数口径一致性` | 本节写的关卡数就是门禁真跑的关卡数（从 `init.sh` 里数出来，不硬编码） |
| 11 | `eval: 社区检索质量` | 社区搜索的结构判定（索引列集合、三个同步触发器、分词器）加上固定语料上的 top-1 准确率、正文词召回、2 字词召回与零误召回 |

关卡本身也要被验证：24 个变异脚本每次故意破坏一条不变量（鉴权边界、链接归一、
数据库夹具、支付回调、敏感内容扫描、社区检索的索引列……），断言门禁会转红。详见
[docs/TESTING-STRATEGY.md](docs/TESTING-STRATEGY.md)。

---

## ⚙️ 配置

复制 `backend/.env.example` 为 `backend/.env`。模板里每一项都带注释，本节讲**哪些必填、填错会怎样**。

### AI 服务（不填就没有 AI 功能：解析与下载照常能用）

平台模型（摘要、思维导图、标签、追问）走**一个** provider。它的凭据、端点、模型
都在环境变量里：

| 变量 | 用途 | 从哪拿 | 缺了会怎样 |
|------|------|--------|-----------|
| `ALIYUN_BAILIAN_API_KEY` | **必填**：平台模型的凭据（阿里云百炼 OpenAI 兼容模式） | [bailian.console.aliyun.com](https://bailian.console.aliyun.com/) → API-KEY 管理 | AI 功能直接报错（平台侧**没有兜底 provider**，见下面的「提供商配置」） |
| `ALIYUN_BAILIAN_BASE_URL` | 端点，默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼控制台给的 workspace 专属地址 | 用默认值 |
| `ALIYUN_BAILIAN_MODEL` | 模型，**留空则取模型清单表里的 `bailian.default_model`**（当前 `qwen3.7-flash`） | 同上 | 用清单表里的值 |
| `ALIYUN_BAILIAN_PROTOCOL` | 协议：`openai`（默认）/ `anthropic` | —— | 用 `openai` |
| `ALIYUN_BAILIAN_ENABLE_THINKING` | 思考开关，默认**关** | —— | 关 |
| `OPENAI_API_KEY` | **只给 Whisper ASR**：视频没有字幕时转写音频 | [platform.openai.com](https://platform.openai.com/) | 跳过 ASR，不影响其它 AI 功能 |
| `YTDL_PROXY` | yt-dlp 的出网代理（`http://` / `socks5://`）。机房 IP 被平台风控（视频页与 view API 412）时唯一解法 | 你自己的代理地址 | 直连 |
| `YTDL_COOKIES_FILE` | yt-dlp 读的 cookies 文件（Netscape 格式）。登录态解一部分风控，AI 字幕轨要它才有 | 浏览器插件导出；docker 部署填 `/app/cookies/<文件名>` | 匿名请求 |

> **环境变量优先于模型清单表。** `ALIYUN_BAILIAN_MODEL` 一旦写了，就盖过清单表里的
> 平台默认模型；不写就用清单表里的 `qwen3.7-flash`。

> 平台侧**没有兜底 provider**：缺 `ALIYUN_BAILIAN_API_KEY` 时进程照常启动，
> 解析与下载照常工作，首次调用 AI 接口返回 500 并点名要配哪个变量，
> 而不是悄悄降级到别家模型。原因见
> [ADR 0028](docs/adr/0028-platform-model-and-provider-protocols.md)：
> 兜底链会把第一个 provider 的真实错误盖住。

用户自己带凭据（BYOK）时，平台 key 不参与。

### 🧩 提供商配置

平台模型接的是**一个** provider，它由四件事决定：**协议 + 端点 + 模型 + key**，
四件都来自环境变量（不落库、不进仓库）：

| 要素 | 变量 | 取值 |
|------|------|------|
| 协议 | `ALIYUN_BAILIAN_PROTOCOL` | `openai`（默认，OpenAI 兼容协议）/ `anthropic`（Anthropic Messages API） |
| 端点 | `ALIYUN_BAILIAN_BASE_URL` | OpenAI 兼容协议填**前缀**（`.../compatible-mode/v1`）；Anthropic 填**根**（`https://api.anthropic.com`，尾部带 `/v1` 也不会变成 `/v1/v1/messages`） |
| 模型 | `ALIYUN_BAILIAN_MODEL` | 留空 = 取模型清单表里的平台默认模型（当前 `qwen3.7-flash`） |
| key | `ALIYUN_BAILIAN_API_KEY` | provider 的凭据 |

两种协议都支持，**当前只配置百炼**（`openai` 兼容模式）。Anthropic 那条路用仓库
已有的 `requests` 手写适配层（POST `/v1/messages` + `x-api-key` +
`anthropic-version` + SSE 流），不装 anthropic SDK。

**平台模型 vs 用户自带凭据（BYOK）**：BYOK 是**用户自己**的 key 与端点，只影响他自己
那一次调用；平台模型是**服务方**的 key，走模型清单表里的默认模型。两者互不影响，
BYOK 也不是「换个 provider 的办法」。

> **别把 key 提交进仓库。** 真值只进 `backend/.env`（已被 `.gitignore` 挡住）；但
> `.gitignore` 挡得住文件，挡不住「把口令抄进文档」，所以门禁第 4 关
> （`scripts/check_secrets.py`）还会扫工作树与全部可达 git 历史。模板
> `backend/.env.example` 里刻意不留任何具体凭据字符串。

**换 provider 要改环境变量并重启**：协议与端点是启动时读一次的。

### 其它必填项

| 变量 | 用途 | 缺了会怎样 |
|------|------|-----------|
| `JWT_SECRET` | Token 签名密钥，**必填、无兜底**（ADR 0010） | 进程在 import 阶段就拒绝启动 —— 不会拿兜底值凑合 |
| `VIDDIGEST_ALLOWED_ORIGINS` | 跨域白名单，逗号分隔；留空 = 完全不允许跨域（默认） | 前端同源访问不受影响 |
| `VIDDIGEST_ADMIN_EMAILS` | 逗号分隔的管理员账号 | 没人是管理员 |
| `VIDDIGEST_DAILY_PARSE_LIMIT` / `VIDDIGEST_DAILY_CHAT_LIMIT` | 每日免费额度，默认 3 / 10，各自独立、次日 0 点（UTC）重置 | 用默认值；**改它只改默认值，不清零已用额度** |

生成一个 JWT 密钥：

```bash
openssl rand -hex 32
```

### 支付（当前不启用）

`STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_ID_MONTHLY` 只保留配置位：项目不做会员制，填了也不会被用。

> **真实值只进 `backend/.env`。** 它已被 `.gitignore` 挡住，但 `.gitignore` 是**文件名级**的 —— 它挡得住这个文件，挡不住「把口令抄进文档」。所以本仓还有一道内容级扫描（`scripts/check_secrets.py`，`init.sh` 第 4 关）扫工作树与全部可达 git 历史。模板 `backend/.env.example` 里刻意**不留任何具体凭据字符串**（连示例口令都没有），要值就现生成。

> 不配置 LLM API Key 时，视频下载功能仍可用，AI 功能不可用。

完整变量清单与生效方式见 [docs/OPERATIONS.md §4](docs/OPERATIONS.md)，逐项注释的模板见 [backend/.env.example](backend/.env.example)。

---

## 📖 使用指南

### 🎬 视频解析下载

```
① 打开首页 → 粘贴视频链接 → ② 点击「解析」
③ 在信息面板里选「选择画质」中的清晰度，或在「纯音频」里选音频容器
④ 点击「下载视频」/「下载音频」
```

> 纯音频只提供**平台原生音频容器**（如 m4a / webm-opus / mp3），不转码。

**支持的平台：**

| 平台 | 链接示例 |
|:----:|----------|
| Bilibili | `https://www.bilibili.com/video/BVxxx` |
| 抖音 | `https://v.douyin.com/xxx/` |
| YouTube | `https://www.youtube.com/watch?v=xxx` |
| Twitter/X | `https://x.com/user/status/xxx` |
| Instagram | `https://www.instagram.com/p/xxx/` |
| ... 1800+ 其他 | 几乎所有主流视频平台 |

> **部署口径**：解析走服务器自己的出口。境内部署时境内平台（抖音、腾讯视频等）可用；
> **B 站会按出口 IP 风控返回 412**，需要配置 `YTDL_PROXY` 或 `YTDL_COOKIES_FILE`；
> 境外平台（YouTube 等）在没有出墙线路的机器上不可达。实测数据见
> [运维手册 §9.3.1](docs/OPERATIONS.md#931-出站可达性实测-2026-11-04)。

### 🧠 AI 视频总结

注册登录后，解析视频 → 点击「AI 总结」→ 选一个模板，界面自动展示四个 Tab：

| Tab | 说明 |
|:---:|------|
| **总结摘要** | 结构化的视频概述、内容大纲、核心知识要点（流式显示） |
| **思维导图** | 可缩放拖拽的交互式导图（基于 markmap），最多 3 层、每层 7 个分支、合计 30 个节点 |
| **字幕文本** | 提取到的原始字幕，含时间戳 |
| **AI 问答** | 基于视频内容自由提问，AI 实时回答 |

四种模板（前端下拉选择）：

| 模板 | 用途 |
|:---:|------|
| **详细总结** | 完整分析：详实的总结正文、思维导图与标签 |
| **精简摘要** | 只留最关键的结论，一屏读完 |
| **步骤教程** | 把视频内容拆成能照着做的步骤，附前置条件与注意事项 |
| **深度分析** | 抽出论点、论据与反面观点，给出结论与可迁移的判断 |

### 🎟️ 注册与邀请码

注册是**邀请码制**：注册接口必须带一枚有效邀请码，码由管理员发放（管理后台 →
「管理邀请码」）。一枚码的可用次数与有效期在发放时设定；校验码、建号、扣减次数
在同一个事务里完成，注册失败不消耗码。

### 💎 免费额度

额度按**成本性质**拆成两个计数器，各自每日重置（ADR 0003）：

| 用户类型 | AI 总结 | AI 追问 |
|:--------:|:--------:|:-------:|
| 未登录 | 0 次/日 | 0 次/日 |
| 注册用户 | **3 次/日** | **10 次/日** |

每个账号每日各 3 次解析、10 次追问，0 点重置。本项目不设会员制。

---

## 🏗️ 技术栈

| 层 | 技术 | 用途 |
|:--:|------|------|
| 前端 | Vue 3 + Vite + TailwindCSS 4 | SPA 框架、界面与流式渲染 |
| 后端 | FastAPI + Uvicorn | REST API + SSE 流式 |
| 视频解析 | yt-dlp + 抖音独立模块 | 1800+ 平台 |
| AI | OpenAI 兼容 / Anthropic 双协议（当前接阿里云百炼） | 总结 / 导图 / 标签 / 问答 |
| ASR 回退 | OpenAI Whisper API | 语音转文字（需配置） |
| 认证 | JWT + bcrypt | 用户鉴权 |
| 数据库 | SQLite | 用户与额度、订单、社区视频、解析历史、追问会话 |

---

## 📖 文档

| 文档 | 内容 |
|------|------|
| [docs/API.md](docs/API.md) | 完整端点清单与认证要求 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 运行时事实、部署、静态托管、监控要点 |
| [docs/TESTING-STRATEGY.md](docs/TESTING-STRATEGY.md) | 测试策略、覆盖缺口、变异装置 |
| [docs/INVITE-CODES.md](docs/INVITE-CODES.md) | 邀请码语义：发放、消费、作废 |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | AI 链路端到端技术说明 |
| [GLOSSARY.md](GLOSSARY.md) | 领域词汇 |
| [docs/adr/](docs/adr/) | 架构决策记录 |

---

## 🛟 常见问题

<details>
<summary><b>视频解析失败？</b></summary>

- **"无法解析该链接"** → `pip install --upgrade yt-dlp`
- **下载有画无声** → 安装 ffmpeg
- **抖音解析失败** → 确认分享的是短链接，稍后重试
</details>

<details>
<summary><b>AI 总结用不了？</b></summary>

- **"没有可用的字幕"** → 该视频没有字幕轨道，不支持 AI 总结
- **"今日次数已用完"** → 每账号每日 3 次，次日 0 点重置
- **"请先登录"** → 注册/登录后使用
- **无响应** → 检查 `.env` 中的 LLM API Key（`ALIYUN_BAILIAN_API_KEY`；平台侧没有兜底 provider）
</details>

<details>
<summary><b>注册不了？</b></summary>

- **"邀请码无效 / 已被使用"** → 注册需要邀请码，从管理员处获取。码有可用次数与有效期，
  用尽或过期后需要重新领取。
- **"该邮箱已注册"** → 直接登录，或换一个邮箱。
</details>

<details>
<summary><b>遇到 429 / 请求太快？</b></summary>

- 公开端点按来源 IP 限速（解析 20 次/分钟、下载 10 次/分钟、直接链接 30 次/分钟等），
  解析与下载还有并发闸门（默认同时 2 个）。稍等重试即可。
</details>

<details>
<summary><b>数据库相关？</b></summary>

```bash
# 重置数据库（删除所有用户和订单数据）
rm -f backend/data/app.db     # 重启后端后自动重建

# 查看数据
sqlite3 backend/data/app.db "SELECT * FROM users;"
sqlite3 backend/data/app.db "SELECT * FROM orders;"
```
</details>

<details>
<summary><b>更多帮助</b></summary>

访问 [docs/](docs/) 查看完整 API 文档、部署指南和架构说明。
</details>

---

## 🤝 贡献

欢迎提交 Issue 和 PR。开发指南见 [docs/](docs/)。

## 📄 License

[MIT](LICENSE)

---

<p align="center">
  <sub>Built with Vue 3 + FastAPI + yt-dlp + LLM</sub>
</p>
