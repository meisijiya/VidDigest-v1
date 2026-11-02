# VidDigest API 文档

> 基础地址：`http://localhost:8000`

## 认证方式

需要认证的接口在请求头中携带 Token：

```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

Token 有效期 **72 小时**，过期需重新登录。

## API 列表

> 本文是**完整**清单。此前只覆盖 13 条，history / community / admin / models
> 四层整层缺席（工单 #15 补齐）。

### 公开 / 用户

| 方法 | 路径 | 认证 | 说明 |
|:----:|:-----|:----:|------|
| GET | `/api/health` | — | 健康检查 |
| POST | `/api/parse` | — | 解析视频信息 |
| POST | `/api/download` | — | 服务端下载视频 |
| POST | `/api/direct-url` | — | 获取视频直链 |
| GET | `/api/proxy/thumbnail` | — | 代理缩略图 |
| POST | `/api/summarize` | 可选 | AI 总结（SSE 流式） |
| POST | `/api/chat` | 可选 | AI 问答（SSE 流式） |
| POST | `/api/auth/register` | — | 用户注册 |
| POST | `/api/auth/login` | — | 用户登录 |
| GET | `/api/auth/me` | 必需 | 获取用户信息 |
| GET | `/api/models` | — | **公开**模型清单（只给 `enabled=1` 的行，供 BYOK 下拉） |
| GET | `/api/summary-templates` | — | **公开**提示词模板清单（`{default, templates[]}`），供前端下拉框渲染。**只给 id / 名称 / 说明，不暴露模板文件名** |
| GET | `/api/quota` | 可选 | 查询额度（**只读，不消耗**）。未登录也返回结构化额度槽位 |

### 社区（`api_community.py`）

| 方法 | 路径 | 认证 | 说明 |
|:----:|:-----|:----:|------|
| GET | `/api/community/videos` | — | 社区卡片列表。**任何人可访问，登录与否返回完全相同的形状**。支持 `page` / `page_size` / `tag`（**多选取并集**） |
| GET | `/api/community/tags` | — | 标签全量清单（带条数），不随当前页汇总 |
| GET | `/api/community/videos/by-url` | 必需 | 按 URL 查社区是否已有结果。用于「零成本自动展示」判定 |
| GET | `/api/community/videos/{video_id}` | 必需 | 单条详情。⚠️ **前端当前不调用**（详情统一走 summarize 复用回放），见工单 #17 |
| GET | `/api/community/search` | 必需 | 社区搜索。未登录由鉴权依赖抛 **401**（不返回半份结果） |
| POST | `/api/community/cards` | 必需 | 回填卡片标题 / 封面（先到先得，不覆盖已有值） |

### 历史（`api_history.py`）

| 方法 | 路径 | 认证 | 说明 |
|:----:|:-----|:----:|------|
| GET | `/api/history` | 必需 | 解析历史列表。支持 `page` / `page_size` / `q` / `tag`（并集）/ `ai`（`ai` / `parse`）/ `favorite` |
| POST | `/api/history/save` | 必需 | 写入 / 更新一条解析历史，并滚动裁剪到上限 1000（**收藏不参与裁剪**） |
| GET | `/api/history/chat` | 必需 | 取某个 URL 的追问会话，返回 `[{question, answer}]`；无记录返回 `[]` |
| GET | `/api/history/facets` | 必需 | 筛选用的分面数据（标签清单等），供筛选器渲染 |
| GET | `/api/history/{id}` | 必需 | 单条历史详情 |
| PATCH | `/api/history/{id}/favorite` | 必需 | 设为收藏 / 取消收藏（语义是「设为」不是「翻转」） |
| DELETE | `/api/history/{id}` | 必需 | 删一条。**收藏项需 `force=true`**，否则 409 |
| DELETE | `/api/history` | 必需 | 清空当前用户全部历史 |

### 管理后台（`admin_api.py`，全部需管理员）

| 方法 | 路径 | 说明 |
|:----:|:-----|------|
| GET | `/api/admin/models` | 全部厂商清单（**含** `enabled=0` 的行） |
| PATCH | `/api/admin/models/{provider_id}` | 改已有行的显示名 / 提示 / 端点 / 可选模型 / 平台默认 / 上下架 / 排序。不提供新增端点 |
| GET | `/api/admin/users` | 用户列表。`limit` / `offset` / `q`（按邮箱模糊） |
| POST | `/api/admin/users` | 建号。直接设初始密码；`is_admin` 可选。**不预置额度**，一律回落全局上限 |
| PATCH | `/api/admin/users/{id}` | 改管理员标记。**只做 `is_admin` 开关，VIP 一点不碰** |
| DELETE | `/api/admin/users/{id}` | 删号。名下有订单或解析历史 → **409，绝不级联**；响应带结构化 `blockers: {表名: 行数}` |
| POST | `/api/admin/users/{id}/quota` | 改额度覆盖。`null`=清除覆盖回落全局；给有效 VIP 设覆盖会返回 `note: "vip_not_effective"`（值仍写库，VIP 到期后生效） |
| GET | `/api/admin/community` | 社区记录列表（**不过滤 status** —— pending 占位恰是后台最该看的），行上带 `status` |
| PATCH | `/api/admin/community/{id}` | 改标签。**词表外 → 400 并点名被拒的标签**（不静默归入「其他」）；上限由服务端给 |
| DELETE | `/api/admin/community/{id}` | 删条目。**只删 `videos` 那一行**，用户的解析历史一行不动 |
| GET | `/api/admin/tags/vocabulary` | 标签词表（前端不抄第二份） |
| GET | `/api/admin/invite-codes` | 邀请码列表（分页 `limit` / `offset`，可按 `status` 筛 `active`/`exhausted`/`expired`/`revoked`）。**不回显完整码**，只有脱敏提示 `code_masked`（前 4 后 4）——库里存的是 sha256 摘要，明文取不回来。每行另给 `created_by_email`（发放人）与 `status` |
| POST | `/api/admin/invite-codes` | 发邀请码。**明文码只在这一次响应里给**（`items[].code`，`item` 是第一枚），之后只剩脱敏码。可带 `count`（一次发几枚，默认 1，**最多 20**）、`max_uses`（用几次，默认 1）、`expires_in_hours`（几小时后过期，缺省=永不过期）、`note` |
| POST | `/api/admin/invite-codes/{id}/revoke` | 作废一枚邀请码（已用完的不受影响） |

### 支付（会员制当前关闭）

| 方法 | 路径 | 认证 | 说明 |
|:----:|:-----|:----:|------|
| POST | `/api/payment/create-checkout` | 必需 | 创建 Stripe 支付会话 |
| POST | `/api/payment/webhook` | — | Stripe Webhook 回调（验签） |
| GET | `/api/payment/orders` | 必需 | 查看订单历史 |

> 会员制入口已关闭（`frontend/src/config/features.ts` 的 `MEMBERSHIP_ENABLED = false`）。
> 上述三个端点与 `orders` 表属于**契约的一部分**，动它们前先读 ADR 0010 / 0012。

---

## 接口详情

### POST `/api/parse` — 解析视频

**请求：**
```json
{"url": "https://www.bilibili.com/video/BV1uT4y1P7CX"}
```

**响应：**
```json
{
  "success": true,
  "data": {
    "id": "BV1uT4y1P7CX",
    "title": "视频标题",
    "thumbnail": "https://...",
    "duration": 212,
    "duration_string": "3:32",
    "uploader": "上传者",
    "platform": "BiliBili",
    "formats": [
      {
        "format_id": "30216",
        "ext": "mp4",
        "height": 1080,
        "label": "1080p MP4 (12.3MB)",
        "has_audio": true
      }
    ],
    "subtitles": ["zh-Hans"],
    "automatic_captions": []
  }
}
```

### POST `/api/summarize` — AI 总结（SSE 流式）

**请求：**
```json
{"url": "https://www.bilibili.com/video/BV1uT4y1P7CX", "language": "zh"}
```

可选字段 `summary_template` 选提示词模板（id 见 `GET /api/summary-templates`；**未知 id 直接 422，不静默回退**）：

```json
{"url": "https://www.bilibili.com/video/BV1uT4y1P7CX", "language": "zh", "summary_template": "brief"}
```

模板只改总结方向，**不改变事件集合**。命中社区已有结果走复用回放时**不重生成**，模板不生效（既有语义）。

**SSE 事件流（按顺序接收）：**

| 事件 | 数据 | 说明 |
|:----:|:----|:-----|
| `subtitle` | `{"has_subtitle": true, "language": "zh", "segments": [...], "full_text": "..."}` | 字幕元数据 |
| `quota` | `{"parse": {...}, "chat": {...}, "remaining": 2, "limit": 3, "unlimited": false}` | 额度。**在 `summary` 之前下发**，前端在流式开始前就能显示余额；自带凭据（BYOK）不消耗额度，改发 `{"byok": true, "consumed": false}` |
| `summary` | `"流式输出的文本片段..."` | 总结内容（多次推送，前端累加） |
| `mindmap` | `{"markdown": "# 标题\n## 章节\n..."}` | 思维导图 Markdown |
| `tags` | `["标签1", "标签2"]` | 标签。已按服务端词表校验，词表外的词不会出现在这里 |
| `error` | `{"message": "...", "need_login": false, "need_vip": false, "reason": "", "asr_reason": "", "byok": false}` | 错误。**六个键总是出现**（`backend/sse_events.py`，ADR 0034）：`need_login` / `need_vip` 是前端路由信号，`reason` 供前端判类（字幕失败四类见 `summarizer.FAIL_*`，额度耗尽为 `quota_exhausted`，其余为空串） |
| `done` | `[DONE]` | 流结束标记 |

命中社区已有结果时（复用回放）额外先发一条 `ownership`：

| 事件 | 数据 | 说明 |
|:----:|:----|:-----|
| `ownership` | `{"can_regenerate": true}` | 该用户能否覆盖这一份（是否为首次解析者）。**仅回放路径有**，必须在其余内容之前发 |

### POST `/api/chat` — 追问

针对某个视频继续追问，走 SSE 流式。**事件集与 `/api/summarize` 不同**：
没有 `subtitle` / `summary` / `mindmap` / `tags` / `ownership`，
只有下面 4 类。

**请求：**
```json
{"url": "https://www.bilibili.com/video/BV1uT4y1P7CX", "question": "这个人讲的核心观点是什么？", "subtitle_text": ""}
```

三个字段（`ChatRequest`，`backend/api_summarize.py:150-153`）：`url` 与 `question` 必填；
`subtitle_text` 可选，是**第二级兜底**——字幕按
「① 社区表里已存的 → ② 本字段 → ③ 现场重新提取」三级取（`api_summarize.py:820-826`）。
社区表优先是刻意的：省掉一次提取，也不给前端篡改字幕的机会；入参那条是**老调用方的兼容路径**。

**SSE 事件流（按顺序接收）：**

| 事件 | 数据 | 说明 |
|:----:|:----|:-----|
| `quota` | `{"parse": {...}, "chat": {...}, "remaining": 2, "limit": 3, "unlimited": false}` | 对话额度。与 summarize 同形，但扣的是**对话**计数器（两额度独立，见 ADR 0003） |
| `answer` | `"流式输出的文本片段..."` | 回答内容（多次推送，前端累加） |
| `error` | `{"message": "...", "need_login": false, "need_vip": false, "reason": "", "asr_reason": "", "byok": false}` | 错误。与 summarize 同一套六键形状（ADR 0034） |
| `done` | `[DONE]` | 流结束标记 |

⚠️ 额度耗尽时 `quota` 事件带 `{"byok": true, "consumed": false}`（自带凭据不消耗），
或走 `error` + `{"reason": "quota_exhausted"}` 分支——与 summarize 同一套口径。

⚠️ **自带凭据（BYOK）不豁免登录**：它只豁免额度，追问会话仍按用户隔离，
未登录由鉴权依赖抛 401（`api_summarize.py:787-799`）。

### POST `/api/auth/register` — 注册

**请求：**
```json
{"email": "user@example.com", "password": "123456"}
```

带邀请码注册时多一个 `invite_code` 字段（**可选**，但缺了或码无效会被拒）：

```json
{"email": "user@example.com", "password": "123456", "invite_code": "XXXXXXXXXXXXXXXXXXXXXXXXXXXX"}
```

邀请码规则见 `docs/INVITE-CODES.md`：库里只存 sha256 摘要（明文只在发放那一刻给管理员一次）；一个码用满 `max_uses` 次后失效，超期或被管理员作废同样失效；注册失败（码无效 / 邮箱已存在）**不消耗**码次。

**响应：**
```json
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "user": {"id": 1, "email": "user@example.com", "is_vip": false}
  }
}
```

### POST `/api/auth/login` — 登录

与注册相同的请求/响应格式。

### POST `/api/payment/create-checkout` — 创建支付

需要认证。创建一个 Stripe Checkout Session 并返回 URL。

**响应：**
```json
{
  "success": true,
  "data": {
    "url": "https://checkout.stripe.com/c/pay_xxx"
  }
}
```

前端应重定向到 `url`，支付完成后自动跳回 `FRONTEND_URL`。
