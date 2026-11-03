# VidDigest 运维手册

> 本文档面向**运维 / 部署 / 故障排查**场景。开发期的快速启动请看 [README.md](../README.md)，API 细节请看 [API.md](API.md)。

> **会员制不启用**：所有用户统一为每日 3 次免费额度。
> 前端付费入口由 `frontend/src/config/features.ts` 的 `MEMBERSHIP_ENABLED = false` 关闭。
> 后端 `api_payment.py`、`is_vip_active()`、`orders` 表与 Stripe 配置位属于契约的一部分，
> 动它们前先读 ADR 0010 / 0012。下方涉及 Stripe 的章节当前无需配置。

---

## 📑 目录

- [1. 部署架构](#1-部署架构)
- [2. 环境要求](#2-环境要求)
- [3. 首次部署](#3-首次部署)
- [4. 配置说明](#4-配置说明)
- [5. 启动 / 关停 / 重启](#5-启动--关停--重启)
- [6. 进程管理（hub）](#6-进程管理hub)
- [7. 日志与监控](#7-日志与监控)
- [8. 数据与备份](#8-数据与备份)
- [9. 故障排查手册](#9-故障排查手册)
- [10. 性能与限流](#10-性能与限流)
- [11. 安全清单](#11-安全清单)
- [11.5 生产静态托管（SPA rewrite）](#115-生产静态托管spa-rewrite)
- [12. 升级与维护](#12-升级与维护)
- [13. 容器化与运维证据链](#13-容器化与运维证据链)
- [14. 持续集成](#14-持续集成)

---

## 1. 部署架构

```mermaid
graph LR
  U[用户浏览器] -->|HTTP:5173| FE[Vue 3 SPA<br/>Vite Dev / 静态托管]
  FE -->|/api/* 代理| BE[FastAPI<br/>Uvicorn :8000]
  BE -->|HTTPS| YT[yt-dlp<br/>1800+ 平台]
  BE -->|HTTPS| LLM[平台模型 API<br/>百炼（OpenAI 兼容 / Anthropic 协议）]
  BE -->|HTTPS| ST[Stripe API]
  BE -->|可选 HTTPS| OA[OpenAI Whisper]
  BE --> DB[(SQLite<br/>data/app.db)]
  BE --> FS[(下载缓存<br/>downloads/)]
```

**核心进程**


| 进程             | 端口   | 依赖                                   | 备注                |
| :--------------: | :----: | ------------------------------------ | ----------------- |
| `backend-api`  | 8000 | Python 3.11+ / yt-dlp / `ALIYUN_BAILIAN_API_KEY` | FastAPI + Uvicorn |
| `frontend-dev` | 5173 | Node 20.19+ / 22.12+                  | 仅开发期使用，生产替换为静态文件  |


**生产部署时**：前端 `npm run release:build` → `dist/` 由 Nginx 托管；后端用单进程 Uvicorn + 反向代理。理由见 [5.1.1](#511-后端单进程性能建议不再是正确性约束)。

---

## 2. 环境要求


| 组件      | 最低版本 | 推荐         | 验证命令                |
| ------- | :----: | :----------: | ------------------- |
| Python  | 3.11 | 3.11       | `python --version`  |
| Node.js | 20.19 或 22.12 | 22 / 24 | `node --version`    |
| npm     | 10    | 10+        | `npm --version`     |
| ffmpeg  | 任意   | 最新稳定       | `ffmpeg -version`   |
| SQLite  | 3.x  | 系统自带       | `sqlite3 --version` |
| 磁盘      | —    | ≥ 10 GB 可用 | 下载产物用完即删，残留由巡检按超龄清（§8.4） |

> **Node 版本不是建议，是硬要求**：Vite 8 的 `engines` 写的是
> `^20.19.0 || >=22.12.0`，Node 18 会在 `npm install` 阶段直接失败。
> `frontend/package.json` 的 `engines` 字段与这张表同源。


**系统级依赖**

```bash
# Windows（管理员 PowerShell）
choco install ffmpeg        # 或手动下载 → 解压 → 把 bin/ 加入 PATH

# macOS
brew install ffmpeg

# Debian/Ubuntu
sudo apt update && sudo apt install -y ffmpeg sqlite3
```

> **ffmpeg 必须**：用于合并 YouTube/B站等分离的视频流（`bestvideo+bestaudio`），缺失会降级为单格式，可能没声音。

---

## 3. 首次部署

### 3.1 拉取代码

```bash
git clone <your-repo-url> viddigest
cd viddigest
```

### 3.2 后端

```bash
cd backend

# 1) 建虚拟环境
python -m venv venv

# 2) 升级 pip + 装依赖
# Windows（绝对路径绕开中文 cwd 转义问题）
"D:/path/to/backend/venv/Scripts/python.exe" -m pip install --upgrade pip
"D:/path/to/backend/venv/Scripts/python.exe" -m pip install -r requirements.txt

# Linux/macOS
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3) 复制环境变量模板
cp .env.example .env       # Linux/macOS
# Windows: copy .env.example .env

# 4) 编辑 .env 填入密钥（见 §4）
```

### 3.3 前端

```bash
cd frontend
npm install
```

### 3.4 健康检查

```bash
# 后端
curl http://127.0.0.1:8000/api/health        # → {"status":"ok"}

# 前端（开发期）—— 必须用 localhost：Vite 只监听 IPv6 回环
curl -o /dev/null -w "%{http_code}" http://localhost:5173/   # → 200
```

> `127.0.0.1:5173` **连不上**（Vite 绑的是 IPv6 回环 `::1`）。这是本项目最常见的
> 「前端起不来」误判来源。后端 `:8000` 不受此限，用 `127.0.0.1` 或 `localhost` 都可以。

---

## 4. 配置说明

**配置文件**：`backend/.env`（**已在 `.gitignore` 中，不要提交真实密钥**）

### 4.1 变量清单


| 变量                        | 必填  | 默认值                                                 | 用途                                      | 获取                                                                               |
| ------------------------- | :---: | --------------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------- |
| `ALIYUN_BAILIAN_API_KEY`  | ✅   | —                                                   | AI 总结 / 导图 / 问答（平台 provider，**无兜底**；缺失时进程照常启动，首次 AI 调用返回 500 并点名该变量）     | [https://bailian.console.aliyun.com/](https://bailian.console.aliyun.com/)       |
| `ALIYUN_BAILIAN_BASE_URL` | —   | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼兼容模式端点                                | 用 workspace 专属地址                                                                 |
| `ALIYUN_BAILIAN_MODEL`    | —   | 模型清单表里的 `bailian.default_model`（当前 `qwen3.7-flash`） | 模型名（**这一项优先于清单表**）                      | 百炼模型市场                                                                           |
| `ALIYUN_BAILIAN_PROTOCOL` | —   | `openai`                                             | provider 协议：`openai`（OpenAI 兼容）/ `anthropic`（Messages API） | ——                                                                        |
| `ALIYUN_BAILIAN_ENABLE_THINKING` | — | `false`（关）                                        | 思考开关。平台模型**默认开着**思考，不关会先吃掉 `max_tokens` 预算 | ——                                                                        |
| `JWT_SECRET`              | ✅   | 无默认值（缺失或纯空白则拒绝启动）                        | JWT 签名密钥                                | `openssl rand -hex 32`（32+ 位随机串）                                                  |
| `VIDDIGEST_ADMIN_EMAILS`  | —   | 空（合法：没有管理员）                                    | 首个管理员播种，逗号分隔；见 §10.4          | 就是目标账号的邮箱，逗号分隔                                                           |
| `VIDDIGEST_ALLOWED_ORIGINS` | — | 空（合法：**完全不允许**跨域，默认如此）                | 跨域白名单，逗号分隔                          | 按实际前端地址填                                                                        |
| `VIDDIGEST_DAILY_PARSE_LIMIT` | — | `3`                                                | 每日解析额度（改它只改默认值，不清零已用额度）        | 纯数字；非数字退回默认值                                                                   |
| `VIDDIGEST_DAILY_CHAT_LIMIT` | — | `10`                                               | 每日问答额度（同上，两个额度各自独立、次日 0 点 UTC 重置） | 纯数字；非数字退回默认值                                                              |
| `OPENAI_API_KEY`          | —   | —                                                   | 视频无字幕时 Whisper ASR 回退                   | [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys)     |
| `STRIPE_SECRET_KEY`       | —   | —                                                   | 支付服务接入                                  | [https://dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys)     |
| `STRIPE_PRICE_ID_MONTHLY` | —   | —                                                   | 月订阅价格 ID                                | Stripe Dashboard → Products                                                      |
| `STRIPE_WEBHOOK_SECRET`   | —   | —                                                   | Webhook 验签                              | Stripe Dashboard → Webhooks                                                      |
| `FRONTEND_URL`            | —   | `http://localhost:5173`                             | 支付成功跳转                                  | 按实际前端地址填                                                                         |


### 4.2 模板

```env
# ─── AI（必填）────────────────────────────────────────────
# 平台模型走**一个** provider：协议 + 端点 + 模型 + key，全部在这里配。
# 平台侧没有兜底 provider：缺 key 时进程照常启动，解析与下载照常工作，
# 首次调用 AI 接口会返回 500 并指名要配 ALIYUN_BAILIAN_API_KEY（见 §4.3）。
# 阿里云百炼：填写 ALIYUN_BAILIAN_API_KEY 即可。模型名留空就走模型清单表里的
# bailian.default_model（当前 qwen3.7-flash）；写死一个模型名就是显式选它，
# 且**这一项优先于清单表**。
ALIYUN_BAILIAN_API_KEY=sk-your-bailian-key
# ALIYUN_BAILIAN_MODEL=qwen3.7-flash
# ALIYUN_BAILIAN_PROTOCOL=openai          # openai（默认）/ anthropic
# ALIYUN_BAILIAN_ENABLE_THINKING=false    # 思考开关，默认关

# ─── 安全（必填，缺失则进程拒绝启动）─────────────
# JWT_SECRET 没有默认值。写死一个兜底常量等于让猜中常量的人
# 伪造任意管理员 token，所以缺失或纯空白时直接拒绝启动。见 ADR 0010。
JWT_SECRET=<把下面命令的输出粘进来>        # openssl rand -hex 32

# ─── 支付（可选）───────────────────────────────
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_PRICE_ID_MONTHLY=price_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
FRONTEND_URL=https://your-domain.com

# ─── ASR 回退（可选）──────────────────────────
OPENAI_API_KEY=sk-your-openai-key
```

### 4.3 提供商配置

平台模型（摘要 / 导图 / 标签 / 追问）接的是**一个** provider。它由四件事决定，
四件都来自环境变量——**不进数据库，也不进仓库**：

| 要素 | 变量 | 取值 |
| --- | --- | --- |
| 协议 | `ALIYUN_BAILIAN_PROTOCOL` | `openai`（默认，OpenAI 兼容协议）/ `anthropic`（Anthropic Messages API） |
| 端点 | `ALIYUN_BAILIAN_BASE_URL` | OpenAI 兼容协议填**前缀**（`.../compatible-mode/v1`，SDK 自己补 `/chat/completions`）；Anthropic 填**根**（`https://api.anthropic.com`，适配层自己补 `/v1/messages`） |
| 模型 | `ALIYUN_BAILIAN_MODEL` | 留空 = 取模型清单表里的平台默认模型（当前 `qwen3.7-flash`） |
| key | `ALIYUN_BAILIAN_API_KEY` | provider 的凭据 |

**当前只配置百炼**（`openai` 兼容模式）。`anthropic` 那条路已经实现并有离线用例
（`backend/provider_config.py` + `backend/tests/test_provider_config.py`），
但**没有真实联调过**——真机验证由维护者做。

#### 为什么协议不存进清单表

清单表管的是「有哪些厂商、默认用哪个模型」，凭据与协议不归它管（ADR 0004 / ADR 0011）：
管理后台的改字段入口是冻结的，加一个改不动的字段比不加更糟；而且 key 与协议本来就
同属部署配置，分两处配同一件事更容易配漏。

#### 三条容易踩的

- **没有兜底链**。缺 `ALIYUN_BAILIAN_API_KEY` 时，进程照常启动（解析与下载照常工作），
  首次调用 AI 接口返回 500，报错里点名要配哪个环境变量，而不是悄悄降级到别家模型。
  原因：兜底链会把第一个 provider 的真实错误盖住，排查时看到的是一个与原因无关的
  报文。见 [ADR 0028](adr/0028-platform-model-and-provider-protocols.md)。
- **环境变量优先于清单表**。`.env` 里写了 `ALIYUN_BAILIAN_MODEL` 时，清单表里的
  `qwen3.7-flash` 就不生效。想跟着平台走就把那一行留空。
- **思考默认开着**。平台模型不显式关掉思考的话，思维链会先吃掉 `max_tokens` 的
  预算（同一问句实测 845 vs 47 tokens），长总结于是提前截断。程序默认发
  `enable_thinking: false`；要开就设 `ALIYUN_BAILIAN_ENABLE_THINKING=true`。

#### key 不要提交进仓库

真值只进 `backend/.env`（已被 `.gitignore` 挡住）。但 `.gitignore` 挡得住文件，
挡不住「把口令抄进文档」——门禁第 4 关（`scripts/check_secrets.py`）扫**工作树与
全部可达 git 历史**（含 commit message）。模板 `backend/.env.example` 里刻意不留
任何具体凭据字符串，要值就现生成。

#### 平台模型 vs 用户自带凭据（BYOK）

BYOK 是**用户自己**的 key 与端点，只影响他自己那一次调用；平台模型是**服务方**的
key，走清单表里的默认模型。两者互不影响，BYOK 也不是「换个 provider 的办法」。

#### 换 provider

协议与端点是**启动时读一次**的：改完环境变量要重启后端。清单表里的平台默认模型
是运行时读的（改完不用重启），但 `ALIYUN_BAILIAN_MODEL` 会盖过它。

### 4.4 配置生效


| 方式                            | 是否需要重启                        |
| ----------------------------- | :-----------------------------: |
| 编辑 `backend/.env`             | **是**（`python-dotenv` 在启动时加载） |
| 操作系统环境变量                      | **是**                         |
| 容器化部署时 K8s ConfigMap / Secret | **是**（滚动重启）                   |


---

## 5. 启动 / 关停 / 重启

### 5.1 后端

```bash
cd backend

# ── 方式 A：hub 后台托管（推荐） ──
hub start --name backend-api \
  --application "D:/path/to/backend/venv/Scripts/python.exe" \
  --args "-m uvicorn main:app --host 0.0.0.0 --port 8000" \
  --cwd "D:/path/to/backend" \
  --ready "log=Uvicorn running on" --port 8000

# Linux 等价
hub start --name backend-api \
  --application "$(pwd)/venv/bin/python" \
  --args "-m uvicorn main:app --host 0.0.0.0 --port 8000" \
  --cwd "$(pwd)" \
  --ready "log=Uvicorn running on" --port 8000

# ── 方式 B：前台跑（看实时日志） ──
python -m uvicorn main:app --host 0.0.0.0 --port 8000
# Ctrl+C 退出

# ── 方式 C：项目入口 ──
python main.py

# ── 重启（改了代码） ──
hub restart --name backend-api

# ── 关停 ──
hub stop --name backend-api
```

### 5.1.1 后端单进程（性能建议，不再是正确性约束）

**当前仍建议单进程**，但它的理由变了：SQLite 的**写入是串行**的，多 worker 不
能提升写吞吐，只会让写锁竞争与 `database is locked` 更频繁。

需要留意的是「不能加 worker」这句话仍在多处出现，而它的理由已经换过一次——
照着旧理由做判断会得出「闸门还在内存里、所以绝对不能多进程」的错误结论。

现在的约束来自两处，都与内存状态无关：
覆盖闸门落在数据库里（ADR 0015），SQLite 的写锁是全局的。

旧约束的来源：覆盖闸门曾是 `api_summarize.py` 里的一个**进程内 `set[str]`**，
`--workers 2` 会把它劈成两份互不可见的副本，于是同一个作者并发点两次覆盖时两个
进程都判定「可以覆盖」——两次调模型、两次扣额度、结果互相覆盖，**不报错、
不告警**。

现在闸门是 `videos` 表上的两列（`regenerating_by` / `regenerating_at`），抢锁是
一条带条件的 UPDATE，而 SQLite 串行化写事务——两个 worker 同时抢只有一个拿到。
**两条路径（首次解析与覆盖）现在用的是同一种机制**：单条条件写 + 唯一裁决。
设计取舍与代价见 [ADR 0015](adr/0015-regenerate-gate-in-db.md)。

要加 worker 时真正要盯的：

- **SQLite 写入串行**：写并发不会变快，且单次写事务超过 5 秒时会 `database is locked`。
- **覆盖闸门的 TTL**：`VIDDIGEST_VIDEO_REGENERATE_TTL_SECONDS`（默认 1800 秒）
  必须大于一次覆盖的最长耗时，否则会在持闸者还在干活时把锁偷走。

关于 SQLite 本身：`database.py:211` 的 `sqlite3.connect(get_db_path())` 没传
`timeout`，走 Python 默认的 **5.0 秒**等待窗口（2026-10-06 实测：持锁 2s 的写者
让另一方等 1.62s 后成功；持锁 7s 则等满 5.5s 后抛 `database is locked`）。
所以**单进程下也可能偶发** `database is locked`——那说明有超过 5 秒的长事务
（备份、批量删除、schema 迁移），不是「上多进程了」。

### 5.2 前端

```bash
cd frontend

# ── 方式 A：hub 后台托管 ──
# ⚠️ 中文路径 + npx 会触发 PTY \0 注入 bug，必须用绝对路径 node
hub start --name frontend-dev \
  --application node \
  --args "node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173 --strictPort" \
  --cwd "D:/path/to/frontend" \
  --ready "log=VITE.*ready" --port 5173

# ── 方式 B：npm scripts ──
npm run dev                                  # 5173，被占时自动跳端口
npm run dev -- --port 5173 --strictPort      # 强占 5173
npm run dev -- --host 0.0.0.0                # 局域网可访问

# ── 生产构建 ──
npm run build       # → dist/，由 Nginx/CDN 托管
npm run preview     # 本地预览生产构建

# ── 重启（改了 vite.config.js 才需要，普通代码 HMR 自动刷新） ──
hub restart --name frontend-dev

# ── 关停 ──
hub stop --name frontend-dev
```

### 5.3 一键全启 / 全停

```bash
# 全启
hub start --name backend-api --application "<venv-python>" --args "-m uvicorn main:app --host 0.0.0.0 --port 8000" --cwd "<backend-dir>" --ready "log=Uvicorn running on" --port 8000
hub start --name frontend-dev --application node --args "node_modules/vite/bin/vite.js --host 0.0.0.0 --port 5173 --strictPort" --cwd "<frontend-dir>" --ready "log=VITE.*ready" --port 5173

# 全停
hub stop --name backend-api
hub stop --name frontend-dev
```

### 5.4 systemd 守护（Linux 生产）

`<app>` 是部署方自己定的服务名与安装根目录（下面用 `<app>` 占位，不要照抄字面量）。
单元文件放在 `/etc/systemd/system/<app>-backend.service`：

```ini
[Unit]
Description=VidDigest Backend (FastAPI)
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/<app>/backend
EnvironmentFile=/opt/<app>/backend/.env
ExecStart=/opt/<app>/backend/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
# 不要加 --workers：SQLite 写入串行，加 worker 不提升写吞吐（工单 #20 后
# 这已不是正确性约束——覆盖闸门落库了，理由见 5.1.1 与 ADR 0015）
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> 密钥走 `EnvironmentFile` 指向 `.env`，不要写成 `Environment=` 行内赋值 ——
> unit 文件是纯文本，进程列表和 `systemctl show` 都能读到行内值。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now <app>-backend
sudo systemctl status <app>-backend
```

### 5.5 测试账号（演示 / 压测 / 联调用）

> **凭据由部署方自定，本文不提供任何账号口令。**
> 仓库是 PUBLIC：把口令写进运维手册，等于把一份可用的登录凭据公开贴在 README 上，
> 与 `.gitignore` 里已经确立的「含明文口令的清单不入版本库」是同一条规矩。
> 本地开发库自带的测试账号清单是 `TEST_ACCOUNTS.md`，它同样在 `.gitignore` 里 ——
> 需要时看本机磁盘，不要指望从 git 里取。

需要两类账号，一类覆盖**免费额度受限**路径，一类覆盖**额度不设限**路径。

| 用途 | 账号 | 额度 | 覆盖场景 |
|------|------|:----:|----------|
| 免费用户 | 自定（建议用 RFC 6761 保留 TLD，如 `<name>.test`） | 3 次/日 | 配额限制、付费引导弹窗、错误文案 |
| 不限额度 | 自定 | 无限 | 总结 / 导图 / 问答的完整路径 |

口令与邮箱都从环境变量读，不落在命令行历史里：

```bash
export TEST_USER_EMAIL='<free-user>.test'
export TEST_USER_PASSWORD="$(openssl rand -base64 18)"
export TEST_VIP_EMAIL='<unlimited>.test'
export TEST_VIP_PASSWORD="$(openssl rand -base64 18)"

# 1) 注册（走标准 /api/auth/register，密码经 bcrypt 哈希）
for pair in "$TEST_USER_EMAIL:$TEST_USER_PASSWORD" "$TEST_VIP_EMAIL:$TEST_VIP_PASSWORD"; do
  curl -sS -X POST http://127.0.0.1:8000/api/auth/register \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"${pair%%:*}\",\"password\":\"${pair##*:}\"}"
  echo
done
```

**把不限额度账号的额度置为无限**（直接改库，不走支付）：

```bash
"<venv-python>" - <<'PY'
import os, sqlite3
conn = sqlite3.connect('backend/data/app.db')
conn.execute(
    "UPDATE users SET parse_limit_override=-1, chat_limit_override=-1 WHERE email=?",
    (os.environ["TEST_VIP_EMAIL"],),
)
conn.commit()
conn.close()
print("ok")
PY
```

> `parse_limit_override` / `chat_limit_override` 取值：`0` = 一条都不能用，`-1` = 无限，`NULL` = 用全局默认。
> 给有效 VIP 设覆盖不生效 —— VIP 在上限校验之前就短路了。

**重置账号（如被压测数据污染）**

```bash
"<venv-python>" - <<'PY'
import os, sqlite3
conn = sqlite3.connect('backend/data/app.db')
conn.execute("DELETE FROM users WHERE email IN (?, ?)",
             (os.environ["TEST_USER_EMAIL"], os.environ["TEST_VIP_EMAIL"]))
conn.commit()
conn.close()
print("ok")
PY
```

**自动化测试**：CI 需要的凭据由 CI 自己的 secret 注入，不写进任何入库文件。
免费用户每天解析 3 次、追问 10 次（见 §10.3），连续跑测试前先把
`VIDDIGEST_DAILY_PARSE_LIMIT` / `VIDDIGEST_DAILY_CHAT_LIMIT` 调大，或清掉
`daily_parse_count` / `daily_chat_count` 字段。

## 6. 进程管理（hub）

`hub` 是 omp 自带的进程编排器，所有长跑进程都应托管在它下面。

### 6.1 常用命令


| 命令                                                                                     | 作用                      |
| -------------------------------------------------------------------------------------- | ----------------------- |
| `hub start --name <n> --application <exe> --args <argv...> --cwd <dir> --ready <expr>` | 启动并等待就绪                 |
| `hub stop --name <n>`                                                                  | 优雅停止（SIGTERM → SIGKILL） |
| `hub restart --name <n>`                                                               | 复用启动配置重启                |
| `hub ps`                                                                               | 列出当前项目托管的所有进程           |
| `hub describe --name <n>`                                                              | 查看启动参数 + 状态             |
| `hub logs --name <n>`                                                                  | 最近 100 行日志              |
| `hub logs --name <n> --follow`                                                         | 实时跟随日志                  |
| `hub logs --name <n> --grep "ERROR"`                                                   | 过滤关键字                   |
| `hub send --name <n> --text "..."`                                                     | 给进程 stdin 发命令           |


### 6.2 启动配置示例

```bash
# --ready 同时匹配日志 + 端口，两个条件都必须满足才视为就绪
hub start --name backend-api \
  --application "D:/path/venv/Scripts/python.exe" \
  --args "-m uvicorn main:app --host 0.0.0.0 --port 8000" \
  --cwd "D:/path/backend" \
  --ready "log=Uvicorn running on" --port 8000 \
  --restart on-failure
```


| `--restart`  | 行为             |
| ------------ | -------------- |
| `no`（默认）     | 失败不重启          |
| `on-failure` | 异常退出自动重启（指数退避） |
| `always`     | 不管退出码都重启       |


### 6.3 故障排查起步

```bash
hub ps                              # 看进程状态 / uptime / restarts
hub describe --name backend-api     # 看启动参数是否正确
hub logs --name backend-api --tail 200 --grep "ERROR"
hub logs --name backend-api --follow
```

---

## 7. 日志与监控

### 7.1 日志位置


| 来源           | 位置                                         | 用途              |
| ------------ | ------------------------------------------ | --------------- |
| backend-api  | `hub logs --name backend-api`              | Uvicorn 访问 + 异常 |
| frontend-dev | `hub logs --name frontend-dev`             | Vite HMR + 编译错误 |
| yt-dlp 调试    | 进程内拿不到细节（`downloader.py` 固定 `quiet` + `no_warnings`）；在 `backend/` 下手动跑 `venv/Scripts/yt-dlp -v --no-playlist "<视频链接>"` 看 stderr | 抓取失败详情          |
| SQLite       | `backend/data/app.db`                      | 用户 / 订单 / 摘要计数  |


### 7.2 关键监控点

```bash
# 1) 后端存活
curl -fs http://127.0.0.1:8000/api/health || alert "Backend down"

# 2) 前端可达（注意：Vite 只监听 IPv6 回环，127.0.0.1 连不上）
curl -fs -o /dev/null http://localhost:5173/ || alert "Frontend down"

# 3) 代理链路
curl -fs http://localhost:5173/api/health || alert "Vite proxy broken"

# 4) 磁盘空间（下载产物用完即删，这里看的是残留占用）
du -sh backend/downloads/ && df -h backend/

# 5) 数据库大小
ls -lh backend/data/app.db

# 6) yt-dlp 更新（每月一次）
"D:/path/venv/Scripts/python.exe" -m pip install --upgrade yt-dlp
```

### 7.3 推荐告警阈值


| 指标                        | 阈值          | 处置                   |
| ------------------------- | ----------- | -------------------- |
| `backend-api` uptime 重启次数 | 1h 内 ≥ 3 次  | 看 `hub logs` 查 panic |
| `/api/health` 连续失败        | 30s 内 3 次   | 重启后端                 |
| `downloads/` 大小           | &gt; 10 GB  | 巡检第 6 项会判不通过并清超龄残留（§8.4） |
| `data/app.db` 大小          | &gt; 500 MB | 归档 + 重建索引            |
| 磁盘剩余                      | &lt; 10%    | 紧急清理                 |


---

## 8. 数据与备份

### 8.1 数据位置


| 数据             | 路径                       | 是否备份          |
| -------------- | ------------------------ | :-------------: |
| 用户 / 订单 / 摘要计数 | `backend/data/app.db`    | ✅             |
| 下载缓存           | `backend/downloads/`     | ⛔ 可重建         |
| yt-dlp 缓存      | 用户家目录 `~/.cache/yt-dlp/` | ⛔ 可重建         |
| `.env`         | `backend/.env`           | ✅ **绝不入 git** |


### 8.1.1 Windows 开发栈：宿主与容器别同时写同一个 SQLite 文件

`docker-compose.yml` 把 `./backend/data` bind mount 进容器，于是**宿主上的
`backend/venv` 与容器里的 backend 指向同一个 `app.db`**。SQLite 的
WAL 模式下 `-wal` / `-shm` 两个文件承载事务，而 **WAL 索引是平台相关的**：
Windows 的 SQLite 与 Linux 的 SQLite 不能安全地共用同一组 `-wal` / `-shm`。
混着写会出现两个症状，都不报错：

- 容器里**新起的进程打不开库**：`sqlite3.OperationalError: unable to open
  database file`（连 `PRAGMA journal_mode=WAL` 都执行不了）。
- 容器里**常驻的 app 进程拿着旧句柄继续服务**，读不到宿主刚写进去的行——
  症状是「刚在宿主建好的邀请码，接口报『邀请码无效』」。

**正确做法：同一个库只由一个平台写。**

| 场景 | 怎么做 |
| --- | --- |
| 要在宿主建号/建邀请码 | `docker compose exec backend python -c "..."`（在容器里写） |
| 要在宿主跑后端进程 | `docker compose stop backend`，起完再 `docker compose start backend`；**两者不要同时在跑** |
| e2e / 探针脚本 | 只走 HTTP，不直接 import 数据层去写库 |

**已经混写过、容器视图读不到新数据时的修复**（顺序不能换）：

```bash
docker compose stop backend                      # 先停容器，避免并发写
backend/venv/Scripts/python.exe -c "import sqlite3;print(sqlite3.connect('backend/data/app.db').execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())"
rm -f backend/data/app.db-wal backend/data/app.db-shm   # 已在 app.db 里并完账，才能删
docker compose up -d backend                     # 容器重建自己的 -wal / -shm
```

`wal_checkpoint(TRUNCATE)` 必须**先于** `rm`：`-wal` 里可能有已提交但还没
并回 `app.db` 的事务（宿主这边建的账号、邀请码就在里面），直接删等于丢数据。

### 8.2 备份策略

```bash
# 每日凌晨 3 点归档（crontab 示例；<app> 与备份根目录按你的部署填）
0 3 * * * sqlite3 /opt/<app>/backend/data/app.db ".backup '/backup/<app>-$(date +\%Y\%m\%d).db'"

# 手动备份
cp backend/data/app.db backup/app-$(date +%Y%m%d).db

# 手动恢复
cp backup/app-20260101.db backend/data/app.db
hub restart --name backend-api
```

### 8.3 重置数据库

```bash
# ⚠️ 会清空所有用户、订单、摘要计数
rm -f backend/data/app.db
hub restart --name backend-api      # lifespan 启动时会自动重建表结构
```

### 8.4 下载产物生命周期

**目录在哪**：`backend/downloads/`（容器内 `/app/downloads`）。通用 yt-dlp、抖音、
ASR 转写三条道写进**同一个**目录——所以清理口径只能有一份（`backend/download_artifacts.py`）。

**为什么下载要过服务器**：平台直链带签名、签名会过期，还要求用户的网络能直连平台；
服务端下载完再一次性发出去，用户拿到的始终是一个完整文件。代价是文件要在服务器上
落一次盘——所以落下来的东西必须用完即删。

**用完即删的时机**：

| 时机 | 谁清 | 清什么 |
| --- | --- | --- |
| 响应发完之后（含客户端中途断开） | `/api/download` 返回的响应对象自己（`ArtifactResponse`） | 本次返回的那个文件 |
| 下载失败 | 通用通道按 yt-dlp 进度钩子报上来的路径自己清；抖音通道自己清 | 本次写下的半截文件 |
| 进程被杀 / 机器断电 | 没人清 | 巡检按超龄收 |

清理**必须**挂在响应发完之后：响应体就是从那个文件里流出去的，先删就变成 0 字节
——用户拿到一个能打开、但是空的视频。

**中断路径怎么走**：清理挂在响应对象自己的 `finally` 上（`backend/download_artifacts.py`
的 `ArtifactResponse`），不挂在框架注入的后台任务上。后者（starlette 的 `FileResponse`
末尾那句 `await self.background()`）**不在 try/finally 里**——客户端中途断开时那一行
根本不执行，产物就永久留在盘上了。挂在 `finally` 上，正常发完与中途断开都会删。

**边界：清理范围只认本次下载自己写下的路径**：通用通道靠 yt-dlp 的进度钩子拿到
「这次写到哪儿了」，失败时逐个删那几个路径；抖音通道按自己拼出来的路径删。
**不扫目录**——按目录或时间窗口扫会把别的请求正在写的在途文件删掉（删了就是拿
「残留占地方」换「下载坏掉」）。所以偶尔会漏清——漏掉的由巡检按超龄收走。
这一层是兜底，不是主路径。

**已知边界：同名产物落在同一个路径上**：产物名是「清洗过的标题 + 扩展名」，不带唯一
id，两个同名视频会写进同一个路径。清理前会核对这个文件的身份（`st_dev` / `st_ino` /
大小 / 修改时间）：盘上的文件已经不是交出去的那一个，就跳过删除并记一行日志。
**覆盖本身没有被解决**——同一时刻两个同名下载，前一个会被后一个顶掉。

**异常残留怎么被巡检收走**：巡检第 6 项（`check_download_dir`）按超龄（默认 24 小时，
下限 1 小时）清掉没人管的残留，并报清掉几个；占用超阈（默认 2048 MB）判不通过。
下限是 1 小时：ASR 通道用 ffmpeg 转音频，超时 300 秒，阈值比它短就会删掉正在转写的文件。

**构建上下文**：`.dockerignore` 排除 `downloads/`，镜像里不留下载残留（CI 会查）。

**有意不做**：容器里的下载目录**不**用 bind mount 对齐到宿主机（见 ADR 0019）。
compose 部署形态下，宿主机巡检的第 6 项管的是宿主机那一份，容器内那一份随容器销毁。

---

## 9. 故障排查手册

### 9.1 启动类


| 症状                                               | 原因                     | 解决                                                                       |
| ------------------------------------------------ | ---------------------- | ------------------------------------------------------------------------ |
| `ModuleNotFoundError: No module named 'fastapi'` | venv 没建 / 没装           | `venv/Scripts/python -m pip install -r requirements.txt`                 |
| `python: can't open file 'main.py'`              | cwd 错误                 | `cd backend` 后再启动                                                        |
| uvicorn 端口被占                                     | 8000 被别的进程占            | `netstat -ano | grep 8000` 杀 PID 或换 `--port 8001`                        |
| vite 端口跳到 5174/5175                              | 5173 被旧实例占             | `taskkill /F /PID <pid>` + 加 `--strictPort`                              |
| hub 启动报 `CreateProcessW ... 不是有效的 Win32 应用程序`    | 中文 cwd + npx 触发 \0 bug | 用绝对路径 `node node_modules/vite/bin/vite.js`                               |
| `未检测到 AI 服务 API Key，请配置环境变量`（`ValueError`） | `.env` 缺失或没填；首次调用 AI 接口时抛出（HTTP 500） | 复制 `.env.example` → 填 `ALIYUN_BAILIAN_API_KEY` → 重启（平台侧**没有兜底 provider**） |
| `RuntimeError: 缺少环境变量 JWT_SECRET`               | `.env` 漏配 / 配了纯空白 / 启动进程读不到 | `openssl rand -hex 32` 生成 64 位随机串，写进 `backend/.env` 后重启            |


### 9.2 运行时类


| 症状                                          | 原因                        | 解决                                  |
| ------------------------------------------- | ------------------------- | ----------------------------------- |
| 前端 `404 Not Found` 但后端 200                  | vite 代理被旧实例顶掉             | `taskkill` 旧 vite → 重启 frontend-dev |
| `GET /api/health` 通过 5173 代理 404            | 同上                        | 同上                                  |
| 视频解析失败 `Unable to extract ...`              | yt-dlp 版本过期               | `pip install --upgrade yt-dlp`      |
| 下载视频有画面无声音                                  | 没装 ffmpeg                 | 安装 ffmpeg 并加入 PATH                  |
| `抖音解析失败`                                    | 短链过期 / 接口变更               | 重试 + 升级 yt-dlp                      |
| AI 总结 `请先登录`                                | 未鉴权                       | 注册 / 登录后重试                          |
| AI 总结 `今日次数已用完`                             | 免费配额 3 次/日                | 等次日重置（当前无会员制）                   |
| AI 总结 `没有可用的字幕`                             | 视频无字幕轨                    | 上传视频不会支持；选有字幕的视频                    |
| AI 总结无响应 / 卡住                               | 平台模型限流 / 网络          | 看 `hub logs` + 重试                   |
| Stripe 支付 500                               | 缺少 `STRIPE_SECRET_KEY`    | 填 `.env` 并重启                        |
| Webhook 400 `Webhook secret not configured` | 缺 `STRIPE_WEBHOOK_SECRET` | 同上                                  |
| 注册 400 `邮箱或密码错误`                            | 用 username 而不是 email      | 必须用邮箱登录                             |
| Bearer token 401 但 token 复制正确               | curl 在 Windows 长 token 截断 | 用 httpx / Postman / 浏览器测试           |


### 9.3 网络类


| 症状                 | 原因                      | 解决                                                                            |
| ------------------ | ----------------------- | ----------------------------------------------------------------------------- |
| yt-dlp 调用平台超时      | 国内访问 YouTube/Twitter 困难 | 配置代理：环境变量 `HTTPS_PROXY=http://127.0.0.1:<proxy-port>`                                 |
| 平台模型 502 / 超时  | 模型服务端问题          | 重试；检查端点与模型名是否匹配                                                             |
| Stripe webhook 收不到 | 公网回调不通                  | 用 `stripe listen --forward-to http://127.0.0.1:8000/api/payment/webhook` 本地测试 |


### 9.4 数据类


| 症状                   | 原因               | 解决                                    |
| -------------------- | ---------------- | ------------------------------------- |
| `database is locked` | 某次写事务持有写锁**超过 5 秒**（Python `sqlite3.connect` 默认等待窗口，见 5.1.1）；常见来源是备份、批量删除、schema 迁移 | 查是不是有长事务。加 `--workers` 既治不了它（SQLite 写入串行），也不再有静默失效风险——覆盖闸门已落库（ADR 0015）；但也**不会**因此变快 |
| 用户列表错乱               | 升级数据库 schema 没迁移 | 看 `database.py` 是否有 `ALTER TABLE`，手动补 |
| **AI 总结一直"正在分析"但无任何事件** | `vip_expire_at` 是 naive datetime，与带时区的 `datetime.now(timezone.utc)` 比较抛 TypeError；异常发生在 SSE `try` 块**之前**，前端拿不到 error 事件就一直转圈 | 读取侧 `is_vip_active` / `complete_order` 对 `tzinfo is None` 做兜底。**写入侧要求**：所有 `vip_expire_at` 统一用 `datetime.now(timezone.utc).isoformat()`（带 `+00:00`） |


### 9.5 思维导图

导图的结构规则只有一份真源：`backend/mindmap.py`（规则见 ADR 0027、0029）。落库前清洗，落库时按写迁移收拾存量行；前端**不改写**结构，拿到什么渲染什么。

| 症状 | 原因 | 解决 |
| --- | --- | --- |
| 导图只有一根光杆（没有二级标题） | 模型给的导图是空的或只有一层 | 解析时自动兜底：从同一份总结正文的 `##` / `###` 标题重建导图。存量行由 `backend/database.py` 的 `_migrate_mindmap_levels` 在 `init_db()` 时重建 |
| 导图少了几个分支 | 超过结构上限被截断：深度 ≤ 3 级、同一父节点下 ≤ 7 个子节点、全图 ≤ 30 个节点 | 截断规则是确定的——按文档顺序深度优先，丢弃的节点连同其子树一起丢，后面的兄弟节点仍保留。这是设计行为，不是 bug |
| 存量导图层级够，但分支或节点数超上限 | 这类行不满足「退化」，写迁移不拿总结正文替换它，只按当前规则截回上限之内（只丢超出的节点） | 老库升级时自动执行一次：`_migrate_mindmap_levels` 认得这类行 |
| 存量导图重建不了 | 该行没有总结正文（`summary_md` 为空） | 没有重建依据，这一行**原样保留**：宁可留一根光杆，也不编一棵导图出来 |
| 存量导图重建后仍然只有一层 | 该行的总结正文本身也没有 `##` 标题 | 重建依据就是总结正文，它没有结构就没有结构 |
| 导图渲染失败，显示原始内容 | 这行导图的结构不合规，markmap 建不出树；前端不改写结构，兜底只显示原始 markdown | 新落的导图一定合规，所以不合规的都是老库里的历史行：跑一次写迁移把它收拾到上限之内；仍然失败说明内容本身残缺，重新解析该视频 |


---

## 10. 性能与限流

### 10.1 当前瓶颈

- **yt-dlp 解析**：单次 2-8s，受平台反爬影响
- **平台模型流式生成**：受 prompt 长度 + token 速率影响，通常 10-30s
- **下载文件大小**：受 `downloads/` 磁盘限制

### 10.2 调优建议


| 场景           | 调优                                                    |
| ------------ | ----------------------------------------------------- |
| 高并发解析        | 闸门已落库（ADR 0015），不再有静默失效风险；加 `--workers` 前真正要盯的是 **SQLite 写入串行**（加 worker 不提升写吞吐）与**覆盖闸门 TTL**（`VIDDIGEST_VIDEO_REGENERATE_TTL_SECONDS` 必须大于一次覆盖的最长耗时，否则会在持闸者还在干活时把锁偷走）。详见 5.1.1                      |
| 下载慢          | 上 CDN / 引导用户用直链 `/api/direct-url`                     |
| AI 总结排队      | 换 PostgreSQL + Celery；当前是同步 + SSE                     |
| downloads 爆盘 | 巡检第 6 项已经管这件事：占用超阈判不通过并打印实际占用与清理办法，同一趟还会清掉超龄残留（§8.4）。别再另加 cron 定时删——删了不报，磁盘满了你才知道 |


### 10.3 免费用户配额

由**环境变量**控制，改后重启后端生效（发版前在 `backend/.env` 里设）：

| 环境变量 | 默认 | 管什么 |
|:--|--:|:--|
| `VIDDIGEST_DAILY_PARSE_LIMIT` | 3 | 每日可发起的解析次数（产出总结 + 思维导图 + 标签） |
| `VIDDIGEST_DAILY_CHAT_LIMIT` | 10 | 每日可发起的追问次数 |

两个计数器彼此独立、各自按 UTC 日期重置。共享内容（读社区视频的总结、思维导图、标签、字幕）不消耗任何额度。

调参入口是环境变量，不是 `database.py` 里的常量——改常量会在下一次改代码时被覆盖。

**以上是全局默认。** 单个用户的例外走管理后台的「额度」页签，
落在 `users` 表的 `parse_limit_override` / `chat_limit_override` 两列
（可空，NULL = 用全局）。取值：`0` = 一条都不能用，`-1` = 无限。

两个坑：

1. **给有效 VIP 设覆盖不生效**。VIP 走的是无限额度，额度校验在解析
   上限**之前**就短路了。覆盖值照写不误（VIP 到期后该生效），
   但界面会明说此刻不生效——不提示的话，管理员会以为是自己改错了。
2. 覆盖是**每用户**的，与两个环境变量无关。改了环境变量不会重置
   已有的覆盖值，要回落得在后台清空。

---

### 10.4 公开端点的限流与并发闸门

六个不挂鉴权依赖的公开端点按来源 IP 限速：解析、下载、直链、缩略图、登录、注册。超限返回 `429`，并在 `Retry-After` 头里写明等几秒（ADR 0020）。

**429 的错误体带机器可读的 `code`**：`{"detail": {"success": false, "code": "rate_limit", "error": "..."}}`。`code` 是**协议**，`error` 里的中文只给人看——前端按 `code` 分辨是哪道自家闸门，不靠认文案；文案怎么改都不影响分类。

| 环境变量 | 默认 | 管什么 |
| --- | --- | --- |
| `VIDDIGEST_RATE_LIMIT_ENABLED` | `1` | 总开关，关掉后所有桶都不限流 |
| `VIDDIGEST_RATE_LIMIT_WINDOW` | `60` | 固定窗口长度（秒） |
| `VIDDIGEST_RATE_LIMIT_PARSE` | `20` | `/api/parse` 每窗口上限 |
| `VIDDIGEST_RATE_LIMIT_DOWNLOAD` | `10` | `/api/download` 每窗口上限 |
| `VIDDIGEST_RATE_LIMIT_DIRECT_URL` | `30` | `/api/direct-url` 每窗口上限 |
| `VIDDIGEST_RATE_LIMIT_THUMBNAIL` | `60` | `/api/proxy/thumbnail` 每窗口上限 |
| `VIDDIGEST_RATE_LIMIT_LOGIN` | `10` | `/api/login` 每窗口上限 |
| `VIDDIGEST_RATE_LIMIT_REGISTER` | `5` | `/api/register` 每窗口上限 |

上限写 `0` 表示该桶不限流。

**每个来源每个端点各一个计数器**：六个桶的计数互不干扰——打满解析桶不会让登录被拒，反过来也一样。上限是**每个桶各自的**窗口上限，不是所有端点加在一起的总次数。

**怎么算同一个来源**：只看直接连上来的那个地址。只有直接对端落在回环、私网或链路本地网段时，才采信 `X-Forwarded-For`——但采信的是它的**最后一段**（边缘代理追加进去的权威地址），不是第一段（客户端可以随便伪造的那段）。公网对端送来的这个头整个不采信（否则任何人换一个头就能绕开限流）。

**反向代理信任模型**：来源身份由**边缘代理与后端共同**决定，两边都要对（ADR 0022）：

- **边缘必须覆盖 `X-Forwarded-For`**：`proxy_set_header X-Forwarded-For $remote_addr;`——**覆盖**，不是追加（`$proxy_add_x_forwarded_for`）。追加语义下客户端自带的值会排到最前、并且仍然留在头里（读这个头的别的东西会读到假值）；覆盖让头里只剩一个权威地址。
- **后端取末段**：`backend/ratelimit.py` 的 `client_identity()` 取这个头的最后一段。标准边缘代理用的是追加语义，把自己看到的对端地址追加在**末尾**，所以末位才是权威的，客户端自报的前缀失效。两边都做：任何一边被改坏，另一半仍守得住自己那半。
- **前面多一层 CDN / 反代时，必须改用 nginx 的 realip 模块**：`set_real_ip_from` + `real_ip_header` 把真实客户端地址还原成 `$remote_addr`。不做这一步，nginx 看到的 `$remote_addr` 是**那一层代理**的地址 → 所有用户落进同一个桶 → 正常用户互相误伤（集体 429）。**没有 realip 就硬上一层 CDN，是把「绕过」换成「误伤」**，后者更难查（它长得像「服务不稳定」）。
- **后端端口不得对外暴露**：绕过 nginx 直连后端（对端是回环/私网）并带一个单段 `X-Forwarded-For` 时，后端取到的末位就是那个自报值。它只能在容器网络或本机回环上可达。
- **怎么自检**：`python scripts/ops/check_xff_bypass.py`（只依赖标准库）——从栈**外面**连打 25 次带唯一伪造头的 `/api/parse`，断言至少出现一次 `429` 且 `detail.code == rate_limit`；一个 429 都没有 = 绕过成立，退出码 1。生产环境加 `--url https://your-domain.com/api/parse`。CI 的「生产构建」作业里跑的就是它（那一步起的是真镜像、走的是真 nginx）。

**这层的边界**：计数在进程内，重启即清零；多实例部署时每个实例各算各的。它是速率维度，不是并发闸门：同一来源的并发请求它不管——那道由下面这道闸门管。

#### 并发闸门

解析与下载这两个重活端点还挂一道**并发闸门**：同一个来源、同一个端点，**此刻还在跑**的任务超过上限就返回 `429`，同样带 `Retry-After` 头（ADR 0021）。

| 环境变量 | 默认 | 管什么 |
| --- | --- | --- |
| `VIDDIGEST_CONCURRENCY_PARSE` | `2` | `/api/parse` 同时在跑的任务数上限 |
| `VIDDIGEST_CONCURRENCY_DOWNLOAD` | `2` | `/api/download` 同时在跑的任务数上限 |

上限写 `0` 表示该桶不限并发。

**与速率闸门的区别**：速率那道拦的是「一分钟内来了多少次」，并发这道拦的是「此刻有几个还在跑」。同一个来源一分钟只发一次、但那个任务跑十分钟，速率闸门一次都不会拦——所以两道闸门都得开。**超限的请求立刻被拒，不排队等**：用户看到的提示是「等前面的任务结束再试」，不是「等一分钟」。

**并发 429 的 `Retry-After` 是 30 秒，它是保底下限不是承诺**：占着名额的任务可能跑几分钟（下载要出网 + 落盘），服务端那一刻并不知道前面的任务什么时候结束。30 秒给的是「别立刻重试」的最小间隔——按头回来重试多半还在等。真正要等多久取决于前面的任务。

**这层的边界**（与速率闸门一致）：计数在进程内，重启即清零；多实例部署时每个实例各算各的。直链、缩略图、登录、注册**不挂**这道闸门——它们是秒级往返，速率闸门对它们已经够用。

**并发 429 的错误体**里 `code` 是 `concurrency_limit`（速率那道是 `rate_limit`）——前端据此把「同时在跑太多」与「来得太勤」分成两种提示，前者的用户动作是「结束前面那个任务」，不是「等一分钟」。

**真值文件**：`backend/ratelimit.py`；判据与用例见 `backend/tests/test_ratelimit.py`（23 条）、`backend/tests/mutation_ratelimit.py`（11 个变异）、`backend/tests/test_concurrency_limit.py`（24 条）与 `backend/tests/mutation_concurrency_limit.py`（7 个变异）。来源身份那条链还有一道**端到端守卫**：`scripts/ops/check_xff_bypass.py`（从栈外面打带伪造头的请求，CI 的「生产构建」作业里跑）。

### 10.5 管理员播种

**第一个管理员不能从界面上产生**——能进后台的人才能提权，而进后台
本身就需要管理员。所以是启动时从环境变量播种一次：

```env
# 逗号分隔，首尾空格会被 strip，跳过空串。不做大小写折叠。
VIDDIGEST_ADMIN_EMAILS=you@example.com,other@example.com
```

三条语义要记住：

- **只播种，不同步**。从 `.env` 里删掉一个邮箱**不会**撤销他的管理员身份。
  撤销是人的操作，不是配置的副作用——否则 env 少写一个字符就会在下次
  重启时静默削掉一个管理员。
- **未配置是合法配置**。返回 0，不报错、不播种。
- 账号必须**已存在**才能被播种。播种是 `UPDATE` 不是 `INSERT`，
  先注册再重启。

改完 `.env` 要**重启后端**才生效（`main.py` 没有 `--reload`）。

之后的管理员增减走后台的 SQLite：`UPDATE users SET is_admin = 1 WHERE email = ?`。
提权与撤权**下一次请求即生效**——管理员身份不进 JWT，每次请求回查数据库。

### 10.6 邀请码发放

**注册是邀请码制**（ADR 0023）：公开注册端点必须带一枚邀请码，码由管理员发放。
速率闸门按来源 IP 计，换个 IP 就能重来，挡不住批量注册；邀请码把「能不能建号」
从「知道邮箱密码」变成「手上有一枚别人给的码」。

发码有两条路：

- **界面**：管理后台 →「管理邀请码」→ 独立页面 `/admin/invite-codes`（ADR 0025）。
  页面能**批量发**（一次 1–20 枚，每枚的明文各给一次）、能按状态筛选
  （可用 / 已用完 / 已过期 / 已作废）、能翻页，每行还回显**发放人**。
  **明文码只在创建成功的那一刻显示一次**，之后列表里只有脱敏码（前 4 后 4）。
  **没复制下来就作废重发**——明文不再取回。
- **命令行**（脚本化发放）：

  ```bash
  # 先取管理员 token（管理员是播种出来的，参见 §10.5）
  TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"email":"you@example.com","password":"<口令>"}' | python -c 'import json,sys;print(json.load(sys.stdin)["token"])')

  # 发一枚：max_uses=1（一次性）、expires_in_hours=24（不传 = 永不过期）
  curl -s -X POST http://127.0.0.1:8000/api/admin/invite-codes \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"max_uses":1,"expires_in_hours":24,"note":"给某某"}'

  # 批量发 5 枚（一次最多 20 枚）
  curl -s -X POST http://127.0.0.1:8000/api/admin/invite-codes \\
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \\
    -d '{"count":5,"max_uses":1,"expires_in_hours":24}'

  # 只看还能用的：GET 带 status 筛选
  curl -s 'http://127.0.0.1:8000/api/admin/invite-codes?status=active&limit=20&offset=0' \\
    -H "Authorization: Bearer $TOKEN"
  ```

  响应里的 `items[].code` 是**明文**，只给这一次（`item` 是第一枚）。
  **`item.code_masked` 不在创建响应里**——
  它是列表项的字段（`GET /api/admin/invite-codes` 的每一行），值形如 `ABCD****WXYZ`，
  由建码那一刻存下的 `code_hint` 给出，**不是明文、不能用来注册**。

**筛选与显示用同一份判定**：服务端把状态判定注册成 SQLite UDF，列表的 `WHERE` 与
返回项的 `status` 调的是同一个函数——两处各判一次，就会在「快过期」这类边界上分叉
（筛出来是可用、点进去却写着已过期）。`status` 可取 `active`/`exhausted`/`expired`/
`revoked`，写别的值会得到 400 并列出可选值。

库里存的是 sha256 摘要，**没有明文**：库被读走不等于在用的码被用掉。代价是明文取不回来
——复制走之后立刻发给对方，发漏了只能作废重发。老库（存明文的那种）在后端重启时
自动升级：明文被算成摘要填进新表，**升级前发出的码仍然可用**。

三条语义：

- **作废与用尽是两件事**。作废是「这枚码从此作废」（可能还没用），用尽是「名额用完了」
  （`used_count >= max_uses`，算出来的状态，不单独存一列）。
- **注册失败不消耗码**。校验码、建号、给码计数在同一个事务里（`BEGIN IMMEDIATE`），
  任一步失败整笔回滚。
- **并发抢同一枚一次性码只能成一个**。所以一次性码不要批量发同一枚给别人。

完整口径见 [`docs/INVITE-CODES.md`](INVITE-CODES.md)，设计取舍见
[`docs/adr/0023-invite-code-registration.md`](adr/0023-invite-code-registration.md)。

---

## 11. 安全清单


| 项                          | 状态  | 建议                                 |
| -------------------------- | :---: | ---------------------------------- |
| `JWT_SECRET` 无默认值且必填      | ✅   | fail-fast：缺失或纯空白即拒绝启动（ADR 0010） |
| `.env` 不入 git              | ✅   | `.gitignore` 已包含                   |
| CORS `allow_origins=["*"]` | ⚠️  | 生产改为具体域名                           |
| bcrypt 哈希密码                | ✅   | cost=12 默认                         |
| JWT 过期 72h                 | ✅   | 短 token + refresh token 更安全        |
| Stripe Webhook 验签          | ✅   | `STRIPE_WEBHOOK_SECRET` 必填         |
| 上传文件大小限制                   | ➖   | 不适用——后端零上传端点，视频只收链接（见 9.2）          |
| HTTPS                      | ⚠️  | 生产必须（Nginx + Let's Encrypt）        |
| 平台 API Key 不暴露前端              | ✅   | 只走后端（`backend/.env`）。**BYOK 用户自带 Key 是例外**：明文存于浏览器 `localStorage`，随请求体上送，后端不落库（ADR 0004） |
| 速率限制                       | ⛔   | 当前未做，建议加 slowapi / Nginx limit_req |


**生产前必做**：

```bash
# 1) 生成强 JWT 密钥
openssl rand -hex 32

# 2) 改 CORS 允许的来源（main.py）
allow_origins=["https://your-domain.com"]

# 3) 启用 HTTPS（Nginx 配置示例）
server {
  listen 443 ssl;
  server_name api.your-domain.com;
  ssl_certificate /etc/letsencrypt/live/api.your-domain.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/api.your-domain.com/privkey.pem;

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

---

## 11.5 生产静态托管（SPA rewrite）

> **这一节是工单 #15 补的。** 之前 §1 写「`dist/` 由 Nginx 托管」，
> 但没给配置 —— 而托管这份产物**必须**带 rewrite，否则生产上用户刷新 `/admin`
> 直接 404。

### 11.5.1 为什么必须有 rewrite

前端是**轻路由**：`App.vue` 的 `pageFromPath` 靠 `location.pathname` 判断，
`/admin` 与 `/admin/` 落到 admin 页，其余落首页。这条路径只在浏览器里跑，
静态服务器看到 `/admin` 会当成一个真实文件去找，找不到就 404。

Vite dev 有 history fallback，所以**开发期完全正常** —— 这个 bug 只在生产暴露。
同理 `npm run preview` 也有 fallback，**不能用它验证 rewrite 配置对不对**
（它会把缺失的 rewrite 掩盖掉）。

### 11.5.2 发布前：先做一次干净构建

`vite build` 默认会清空 `outDir`（`emptyOutDir` 对 `outDir` 在 `root` 内时默认开），
但本地开发时反复构建会在 `dist/assets/` 留下**多代哈希产物**（文件名带内容哈希，
旧的不会被自动清掉，除非整目录重建）。发布整个 `dist/` 就等于把陈旧 bundle 一起推上线。

**用 `npm run release:build`**（= `clean` + `vite build`），它先整目录删干净再构建：

```bash
cd frontend
npm run release:build              # 发布用这个
# 确认 assets/ 里只有 index.html 实际引用的那一代
grep -o 'assets/[^"]*' dist/index.html | sort -u
ls -la dist/assets/
```

两处应当**一一对应**。对不上就是有陈旧产物，再跑一次 `npm run release:build`。
（日常开发用 `npm run build` 即可，不必每次清空。）

### 11.5.3 Nginx：同域托管 + API 反代

前端所有 API 都是**同源相对路径**（`api/*.js` 里全是 `/api/...`），
`vite.config.js` 的 `server.proxy` **只在 dev 生效**。生产必须把 `/api/*` 反代到后端。

```nginx
server {
  listen 80;
  server_name your-domain.com;   # ← 换成实际域名
  return 301 https://$host$request_uri;
}

server {
  listen 443 ssl;
  server_name your-domain.com;   # ← 换成实际域名
  ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

  root /opt/<app>/frontend/dist;
  index index.html;

  # ── API 反代（必须在 rewrite 之前，见下方顺序说明）──
  location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    # 必须**覆盖** X-Forwarded-For，不能用 $proxy_add_x_forwarded_for：
    # 追加语义会把客户端自带的值排到最前并留在头里，而后端按 ADR 0022
    # 取这个头的**末段**来分桶（边缘覆盖 + 后端取末段，两边都做）。
    # 前面若还有一层 CDN / 反代，先用 realip 模块把真实地址还原成
    # $remote_addr（set_real_ip_from + real_ip_header），否则所有用户会
    # 落进同一个桶、互相误伤 429。
    proxy_set_header X-Forwarded-For   $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE：`/api/summarize` 与 `/api/chat` 是流式的。
    # 不关缓冲的话 Nginx 会攒着一起发，界面就一直转圈。
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;   # AI 总结 + 思维导图可能跑几分钟
    proxy_send_timeout 600s;
  }

  # ── 带哈希的构建产物：可以长缓存 ──
  location /assets/ {
    expires 1y;
    add_header Cache-Control "public, immutable";
    access_log off;
  }

  # ── SPA rewrite（关键一条）──
  # 未知路径回 index.html，让前端路由接手。
  # `try_files $uri $uri/ /index.html;` 最后的兜底就是这一行的全部作用。
  location / {
    try_files $uri $uri/ /index.html;
  }

  # favicon / manifest 等根级静态文件：存在就直出，不存在才走 SPA
  location = /favicon.svg { try_files $uri =404; }
  location = /browserconfig.xml { try_files $uri =404; }
  location = /site.webmanifest { try_files $uri =404; }
}
```

**顺序为什么重要**：`location /api/` 必须在 `location /` 之前或用更长的前缀匹配。
若 rewrite 那条先命中，`/api/xxx` 会被回成 `index.html`，前端拿到一段 HTML
去 `JSON.parse` → 500，且日志里看不到明显的路由错误。

### 11.5.4 上线后自检

```bash
# 1) 主页
curl -sS -o /dev/null -w '%{http_code}\n' https://your-domain.com/          # 200

# 2) SPA rewrite 真的生效（这一条就是本节存在的理由）
curl -sS -o /dev/null -w '%{http_code}\n' https://your-domain.com/admin     # 200，且是 index.html
curl -sS https://your-domain.com/admin | head -5                            # 应看到 <!DOCTYPE html> / <div id="app">

# 2b) 五条真实路径都要能刷新（前端是 vue-router，深链靠 rewrite 兜底）
for p in / /history /community /admin /admin/invite-codes; do
  printf '%s ' "$p"; curl -sS -o /dev/null -w '%{http_code}\n' "https://your-domain.com$p"
done

# 3) API 没被 rewrite 吃掉
curl -sS https://your-domain.com/api/health                                 # {"status":"ok"}

# 4) 静态产物正常
curl -sS -o /dev/null -w '%{http_code}\n' https://your-domain.com/favicon.svg

# 5) 限流的来源身份没被伪造的头绕开（ADR 0022）
#    边缘覆盖 + 后端取末段都到位时，这 25 次会落进同一个桶，第 21 次起出 429。
#    一个 429 都没有 = 绕过成立（退出码 1）。
python scripts/ops/check_xff_bypass.py --url https://your-domain.com/api/parse
```

第 2 条返回 404 = rewrite 没生效；返回 200 但第 3 条返回 HTML = rewrite 盖住了 API。第 5 条红 = 边缘没有覆盖 `X-Forwarded-For`（或后端取错了段），限流可被一个头绕过——先查第 10.4 节的「反向代理信任模型」。

### 11.5.5 其它静态托管平台

Caddy：

```caddyfile
your-domain.com {
  handle /api/* {
    reverse_proxy 127.0.0.1:8000
    flush_interval -1          # SSE
  }
  handle {
    root * /opt/<app>/frontend/dist
    try_files {path} /index.html
    file_server
  }
}
```

Nginx 之外的平台（对象存储 + CDN、Vercel、Netlify 等）**通常自带 SPA fallback**，
但要逐个确认：不少平台的默认规则只对 `/index.html` 生效，**不覆盖 `/admin` 这类无扩展名路径**。
以平台实际行为为准，别假设。

---

## 12. 升级与维护

### 12.1 依赖升级

```bash
# 后端
"D:/path/venv/Scripts/python.exe" -m pip install --upgrade pip
"D:/path/venv/Scripts/python.exe" -m pip install --upgrade -r requirements.txt

# 前端
cd frontend && npm update
npm outdated       # 看哪些有更新

# 强制升级 yt-dlp（解决平台兼容问题最常见）
"D:/path/venv/Scripts/python.exe" -m pip install --upgrade yt-dlp
```

### 12.2 数据库迁移

当前用 SQLite，**迁移在应用启动时由 `init_db()` 统一执行**（幂等，重复启动不会重复改）。
schema 变更分两类，写法不同：

| 类型 | 什么时候用 | 怎么写 | 例子 |
|------|-----------|--------|------|
| **加列 / 改列** | 老库缺一列，或列的默认值要变 | 在 `database.py` 里加一个 `_migrate_xxx_columns(conn)`，用 `PRAGMA table_info` 先判断列在不在，不在才 `ALTER TABLE ... ADD COLUMN` | `_migrate_quota_columns`、`_migrate_videos_template_column` |
| **写迁移** | 老数据的内容或形状本身要换 | 加一个 `_migrate_xxx(conn)`，在自己的事务里改写存量数据：重建表（DROP 旧表 → 建新表 → 逐行搬）或逐行 UPDATE | `_migrate_invite_codes_table`（把明文邀请码重建成只存 SHA-256 哈希的表）、`_migrate_mindmap_levels`（按新层级上限逐行改写存量导图） |

写迁移的硬要求：

1. **先备份**（`scripts/ops/backup_db.py` 或 `sqlite3.Connection.backup`），
   写迁移不自带回滚。
2. **在事务里做**：中途失败要能整体回滚，不能留下半张表。
3. **幂等**：老库、已迁移的库、被中断过的库都要能安全启动一次。
4. **迁移完的库要能正常启动**：迁移本身也要有测试覆盖（从**真缺列的老库**起一次）。

改完迁移后按 §12.4 的 checklist 走一遍：备份 → 停服务 → 换代码 → 起服务 → 看日志里
迁移是否执行 → 跑一次功能自检。

### 12.3 回滚

```bash
# 代码回滚
git log --oneline -10
git checkout <commit-hash> -- backend/ frontend/
hub restart --name backend-api
hub restart --name frontend-dev

# 数据库回滚
cp backup/app-20260101.db backend/data/app.db
hub restart --name backend-api
```

### 12.4 升级 checklist

- [ ] 备份 `data/app.db`
- [ ] 备份 `.env`
- [ ] 看 `git log` 与 `docs/adr/` 是否有破坏性变更
- [ ] 在 staging 环境跑通
- [ ] **前端做一次干净构建**，确认 `dist/assets/` 与 `dist/index.html` 的引用一一对应（见 [11.5.2](#1152-发布前先做一次干净构建)）
- [ ] **依赖目录不在源码里**：同步代码到服务器后，在目标目录跑一次 `npm ci`，再 `npm run release:build`（只同步源码、不装依赖就构建，产物会缺块）
- [ ] 确认静态托管的 **SPA rewrite** 生效（`curl https://<域名>/admin` 返回 200 且是 index.html，见 [11.5.4](#1154-上线后自检)）
- [ ] 五条真实路径都能刷新（`/`、`/history`、`/community`、`/admin`、`/admin/invite-codes`）
- [ ] 确认 `/api/*` 反代没被 rewrite 盖住（`curl https://<域名>/api/health` 返回 JSON）
- [ ] 生产滚动升级（先停前端再停后端，避免半截状态）
- [ ] 验证 `/api/health` + 主页 + `/admin` 刷新 + 注册登录 + 一次完整下载 + 一次 AI 总结（SSE 不缓冲）
- [ ] 验证**邀请码注册链路**：管理后台发一枚码 → 用它注册一个新账号 → 码的可用次数减一（注册失败不该消耗码）

---

## 13. 容器化与运维证据链

「可运维」不是一句形容词，是五件能执行的东西：镜像、编排、备份、巡检、日志。
本节把它们摆在一起。**不含监控告警平台**（Prometheus / Grafana / ELK 一律不做），
只有巡检脚本与阈值判定——不写「已搭建监控告警体系」这种话。

### 13.1 文件清单

| 文件 | 作用 |
|------|------|
| `backend/Dockerfile` | 后端镜像：非 root 运行、含 ffmpeg、带 healthcheck |
| `backend/.dockerignore` | 凭据与本机产物不进镜像 |
| `frontend/Dockerfile` | 两段式：Node 构建 → 最终镜像只有静态文件 + nginx |
| `frontend/nginx.conf` | SPA rewrite + `/api` 同域反代到后端 |
| `frontend/.dockerignore` | 同上，前端侧 |
| `docker-compose.yml` | 本地编排：两个服务 + 两个卷 |
| `backend/logging_setup.py` | JSON 行日志、分级、轮转 |
| `scripts/ops/backup_db.py` | SQLite 在线备份 + 完整性校验 + 保留策略 |
| `scripts/ops/inspect_runtime.py` | 运行期巡检，任一项不达标即非零退出 |
| `scripts/ops/check_xff_bypass.py` | 限流绕过自检：伪造 XFF 连打 25 次，断言至少一个 429（CI「生产构建」作业里跑） |
| `scripts/ops/probe_ai_e2e.py` | **AI 总结质量探针**（手动跑，**不进 init.sh**）：用真实视频跑完整链路，打印字幕字数 / 总结字数 / H2 数 / 导图层级与节点数 / 标签。换模型或换提示词后跑它做前后对照 |

### 13.2 起停与验证

```bash
# 构建并起
docker compose up -d --build

# 验证：真 curl 过才算数，「容器起来了」不算
curl -i http://localhost:8080/api/health

# 状态与日志
docker compose ps
docker compose logs -f backend

# 关停（卷留着，数据还在）
docker compose down
```

前端容器会等后端容器**真健康**再起（`depends_on: condition: service_healthy`），
省掉「前端先起、后端还没好」的一串 502。

### 13.3 数据持久化

| 卷 | 容器内路径 | 内容 |
|----|-----------|------|
| `viddigest-data` | `/app/data` | SQLite 库（`app.db`） |
| `viddigest-logs` | `/app/logs` | 运行期日志 |

`docker compose down` **不删卷**（`down -v` 才删）。要验证数据确实还在，
别靠配置推断，去库里查一行：

```bash
docker compose up -d
docker compose exec backend python -c "import sqlite3;print(sqlite3.connect('/app/data/app.db').execute('select count(*) from users').fetchone())"
```

### 13.4 备份与恢复

```bash
# 备份（热备份：服务在跑也能出一致快照）
python scripts/ops/backup_db.py
python scripts/ops/backup_db.py --keep 14

# 恢复
docker compose down
cp backend/backups/app-<时间戳>.db backend/data/app.db
docker compose up -d
```

**为什么不直接拷 `app.db`**：本库是 WAL 模式，服务在跑时当前状态不全在主文件里，
多出来的部分在 `app.db-wal`。直接 `cp` 拿到的是缺事务的快照，恢复后会安静地丢数据。
备份脚本走 SQLite 自带的在线备份 API，拷完还要过 `PRAGMA integrity_check`——
**校验不过就当失败**（退出码非零），因为一份打不开的备份比没有备份更危险。

### 13.5 巡检

```bash
python scripts/ops/inspect_runtime.py
python scripts/ops/inspect_runtime.py --health-url http://localhost:8080/api/health
```

| 检查项 | 判据 |
|--------|------|
| 健康端点 | HTTP 200 且响应是 JSON |
| 数据库 | 文件存在且 `PRAGMA integrity_check` 通过 |
| 数据目录 | 占用不超过阈值（默认 1024 MB） |
| 日志新鲜度 | 日志文件存在且最近有写入（默认 24 小时内） |
| 前端入口缓存 | 入口页响应带 `Cache-Control: no-store`（默认 http://127.0.0.1:8080/） |
| 下载目录 | 占用不超过阈值（默认 2048 MB），并清掉超龄残留（默认 24 小时） |

下载目录这一项的两个阈值可用 `--max-download-mb` / `--max-download-age-hours` 调。
超龄下限是 1 小时：ASR 通道用 ffmpeg 转音频，超时 300 秒，阈值比它短就会删掉正在
转写的文件。下载目录在容器里**不**与宿主机对齐（ADR 0019），所以这一项查的是
**宿主机**上那一份（§8.4）。

最后一项的由来：静态资源带内容哈希，可以长期缓存；`index.html` 里写着当前 bundle
的文件名，浏览器捏着旧入口页就会去加载旧 bundle，页面「改了没生效」——
排查半天才发现是缓存，不是代码。所以入口页必须每次回源（见 `frontend/nginx.conf`
里的 `location = /index.html`）。

任一项不达标 → 退出码非零。巡检报告如果永远显示「一切正常」，它就只是输出文字，
不是判据。

### 13.6 日志

- 位置：`backend/logs/app.log`（容器内 `/app/logs`）
- 格式：一行一条 JSON（`ts` / `level` / `logger` / `msg`，异常带 `exc`）
- 级别：`VIDDIGEST_LOG_LEVEL` 环境变量控制，默认 `INFO`
- 轮转：单文件 10 MB，保留 5 份

两个去处，级别不同：

| 去处 | 收什么 | 为什么 |
| --- | --- | --- |
| 文件 `backend/logs/app.log` | 全部级别（INFO 起） | 事后查证的依据，第三方库的 INFO 噪音也在这里 |
| 控制台（`docker compose logs`） | 警告及以上 + uvicorn 自己的横幅与访问日志 | stdout/stderr 是应用与宿主的协议面，正常请求期间必须干净；只留"要立刻看见的" |

时间戳统一用 UTC：容器、CI、开发者本机时区不同，只有统一成 UTC，
日志顺序才和真实顺序一致。

**日志里没有凭据**：请求体里的 `user_api_key` 由 `backend/main.py` 的
`RequestValidationError` 处理器遮蔽；BYOK 密钥不落盘（ADR 0004）。
脱敏发生在靠近数据的地方，不在日志层做正则糊。

### 13.7 凭据边界

- `backend/.env` 被 `backend/.dockerignore` 排除，**不进镜像**
- `docker-compose.yml` 用 `env_file` 在运行时注入，文件里只有变量名
- 镜像里没有 key，也没有默认密钥——`JWT_SECRET` 缺失时后端**拒绝启动**（ADR 0010）

### 13.8 线上实例（当前部署形态）

线上跑在一台腾讯云轻量应用服务器（4C/4G，Ubuntu 24.04，Docker + Compose）。
**部署信息不含任何凭据**：`backend/.env` 只落在服务器上（见 13.7），仓库里
只有 nginx 站点配置。

| 项 | 值 |
| --- | --- |
| 站点 | `https://ljhlovefjm.top:8443/`（nginx 站点配置：`deploy/nginx/viddigest-8443.conf`） |
| 应用 | `<部署目录>`（`git clone` 的仓库），`docker compose up -d --no-build` |
| 端口 | backend `127.0.0.1:8000`、frontend `127.0.0.1:8080`——**都只绑回环**，公网只开 nginx |
| 证书 | 复用主域名的证书（certbot webroot 自动续期）。证书上只有主域名，所以子域要用另一个域名 + 另签一张 |
| 数据库 | `<部署目录>/backend/data/app.db`（bind mount，容器以 `appuser` 身份写，宿主目录属主要跟容器一致） |
| 备份 | 每日 03:17 跑 `scripts/ops/backup_db.py`，保留 14 份，输出 `<部署目录>/backups` |

**镜像怎么上去的**：在那台机器上直接构建**不可行**——后端镜像的
`apt-get install ffmpeg` 在 buildkit 里要几十分钟（buildkit 网络命名空间取到
IPv6 地址却没有 IPv6 路由，每个连接先等一次 IPv6 超时）。所以镜像在本地
构建后传过去：

```bash
docker compose build
docker save universal-video-downloader-backend:latest universal-video-downloader-frontend:latest \
  | gzip -1 | ssh <host> "gunzip | docker load"
```

服务器上的 compose 项目名取自目录名（`viddigest`），本地项目名取自目录名
（`universal-video-downloader`），所以传过去之后要补一次打标签：

```bash
sudo docker tag universal-video-downloader-backend:latest viddigest-backend:latest
sudo docker tag universal-video-downloader-frontend:latest viddigest-frontend:latest
```

**更新线上代码**（只改了代码、没改依赖时）：

```bash
cd <部署目录> && sudo git pull
sudo docker compose up -d --no-build        # 镜像没变就不重建
```

改了依赖就要重新走「本地构建 → 传镜像 → 打标签」那条路。

**验收**（线上同样要过，不是「装上了就算」）：

```bash
curl -s https://ljhlovefjm.top:8443/api/health                  # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" https://ljhlovefjm.top:8443/admin   # 200（SPA rewrite）
python scripts/ops/check_xff_bypass.py --url https://ljhlovefjm.top:8443/api/parse --requests 25
```

第三条是限流判据（ADR 0022）：伪造的 `X-Forwarded-For` 必须打不出 429 才算
被绕过——线上 nginx 用覆盖语义（见站点配置的头部注释），所以伪造头无效。

**这台机器的网络限制**（实测，不是推测）：

| 目标 | 结果 | 影响 |
| --- | --- | --- |
| 百炼（模型 API） | 通 | AI 总结 / 导图 / 追问可用 |
| B 站（网页与接口） | **412 风控** | 机房 IP 被拦，视频页与接口都拿不到 → B 站视频解析在这台机上走不通 |
| YouTube / Google | 不通 | yt-dlp 的 YouTube 通道不可用；Whisper 回退也不可用 |
| 抖音 | 通 | 走项目自带的 `backend/douyin.py` |

也就是说：这台机器适合**自用 + AI 能力验证**，「通用视频下载」要覆盖 B 站与
YouTube 得换地域或加代理。

---

## 14. 持续集成

### 14.1 四个 job 定义，展开 26 个实例

`.github/workflows/ci.yml` 只做一件事：在干净的 Linux 机器上把门禁跑一遍。
它有 **4 个 job 定义**（门禁、生产构建、后端变异矩阵、前端变异矩阵），在 GitHub 上**展开成 26 个实例**（两个变异矩阵按脚本数各展开）。

| job | 跑什么 | 为什么这么切 |
| --- | --- | --- |
| `gate` | `bash scripts/bootstrap.sh` → `bash ./init.sh` | 门禁的真相在 `init.sh` 里，工作流不复述它。装环境也走 `bootstrap.sh`，本地与 CI 走同一条路，否则「本地绿、CI 红」只能靠猜 |
| `build` | `npm ci && npm run build` → `docker compose build` → 起容器做一次自检 | 这个 job 真跑前端生产构建、镜像构建与容器自检，构建产物不是只在文档里被描述过 |
| `mutation-backend` | 20 个后端变异脚本，每个一个 job | 变异脚本会故意改坏源码再还原，同一个工作区里串行跑会互相踩；分开跑互不污染 |
| `mutation-frontend` | 4 个前端变异脚本，每个一个 job | 同上 |

触发条件：推 `master`、向 `master` 提 PR、手动触发。`fail-fast: false`——一个变异脚本红了，
其余的仍要跑完，否则不知道是这一条坏了还是一起坏了。

### 14.1.1 改动怎么进 master

master 受分支保护保护：force push 与删除分支被挡住，直推 master 要求「门禁」与「生产构建」
两个检查在最新提交上通过。要改 master，走这条路：

1. 从 master 切一条分支，改在上面
2. 推这条分支，开 PR（模板在 `.github/pull_request_template.md`，要求贴命令与退出码）
3. 等 PR 上的检查全绿
4. 合入 master

`变异验证 · …` 这些检查**不写进必过清单**：它们的检查名里带脚本路径，加一个脚本就得改一次
必过清单，旧名字再也不会出现，PR 会被永久卡住。必过的是「门禁」与「生产构建」两个名字稳定的检查。

### 14.2 本地预演

推上去之前先在容器里走一遍同一条路：

```bash
bash scripts/bootstrap.sh && bash ./init.sh
```

Windows 上跑不了的部分（Linux 路径、`python3 -m venv`）用容器补：

```bash
docker run --rm -v "$PWD:/repo" -w /repo node:22-bookworm bash -c '
  apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
  bash scripts/bootstrap.sh && bash ./init.sh'
```

**排除 `__pycache__` 与 `*.pyc`**：Windows 生成的 `.pyc` 里嵌的是 Windows 路径，
`linecache` 在 Linux 上读不到源文件，`inspect.getsource` 会报
`OSError: could not get source code`——那是复制方式的问题，不是平台问题。

### 14.3 第一次推上去之前

工作流是**写出来的，不是跑出来的**。第一次推上去之后要把真实运行结果贴回工单，
在那之前不要对外说「CI 全绿」。

---

## 🆘 紧急联系


| 问题类型  | 排查入口                                                                                                       |
| ----- | ---------------------------------------------------------------------------------------------------------- |
| 服务挂了  | `hub ps` → `hub describe` → `hub logs --follow`                                                            |
| 接口异常  | `curl -v` + 看 `hub logs --grep ERROR`                                                                      |
| 平台兼容  | 升级 yt-dlp + 提交 Issue                                                                                       |
| AI 不通 | 看 `ALIYUN_BAILIAN_API_KEY` 是否过期 / 余额；百炼要确认 `ALIYUN_BAILIAN_BASE_URL` 是 workspace 专属地址；确认 `ALIYUN_BAILIAN_MODEL` 没有被写成一个不存在的模型名（它**优先于**清单表） |
| 数据丢失  | 从 `backup/app-*.db` 恢复                                                                                     |


---

<p align="center"><sub>设计决策见 <a href="adr/">docs/adr/</a></sub></p>