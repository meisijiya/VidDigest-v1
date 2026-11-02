# <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#8b5cf6" stroke-width="2" style="vertical-align:middle"><zap/></svg> AI 视频总结功能核心技术说明

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><cpu/></svg> 概述

AI 总结功能由三个子系统构成：

| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><hash/></svg> | 模块 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><target/></svg> 职责 |
|:-:|:-----|:------|
| 1 | **SubtitleExtractor** | 从视频 URL 提取字幕文本 |
| 2 | **VideoSummarizer** | 调用 OpenAI 兼容的 AI 服务生成总结/导图/问答 |
| 3 | **SSE Stream** | FastAPI `EventSourceResponse` 实时推送 |

**整体数据流：**

```
  ┌──────────┐    ┌─────────────────────────────────────┐
  │ 用户点击   │───▶  /api/summarize (SSE)               │
  │ 解析      │    │                                     │
  └──────────┘    └──────────┬──────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
  ┌─────────────────────┐    ┌─────────────────────────┐
  │ SubtitleExtractor   │    │ VideoSummarizer         │
  │                     │    │                         │
  │ ├─ B站: API直接获取  │    │ ├─ summarize_stream()   │
  │ ├─ 其他: yt-dlp     │    │ │  流式生成 Markdown     │
  │ └─ 回退: Whisper API  │    │ └─ generate_mindmap()   │
  │                     │    │   一次性生成思维导图      │
  └──────────┬──────────┘    └────────────┬────────────┘
             │                            │
             ▼                            ▼
  ┌─────────────────────────────────────────────────┐
  │  前端逐事件渲染                                 │
  │  subtitle → quota → summary* → mindmap → tags   │
  │  → done                                        │
  └─────────────────────────────────────────────────┘
```

---

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><file-text/></svg> 一、字幕提取（SubtitleExtractor）

**<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><file/></svg> 文件：** `backend/summarizer.py` `class SubtitleExtractor`

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><target/></svg> 职责

从视频 URL 中提取字幕文本，返回统一格式的结构化数据。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><code/></svg> 统一输出格式

```python
{
    "has_subtitle": bool,       # 是否存在字幕
    "language": str,            # 字幕语言代码，如 "zh-Hans"
    "subtitle_type": str,       # "manual"（人工）| "auto"（平台 ASR）
                                # | "asr"（Whisper 转写）| "none"
    "segments": [               # 分段字幕
        {"start": 0.0, "end": 1.0, "text": "..."},
    ],
    "full_text": str,           # 所有分段文本拼接（用于 AI 总结）
    "fail_reason": str,         # 平台字幕为什么没拿到（拿到时为空串）
    "asr_fail_reason": str,     # ASR 兜底为什么没救回来（救回来时为空串）
}
```

**这 7 个键是契约，不是约定**：`SUBTITLE_RESULT_KEYS`（`backend/summarizer.py:44-52`）是唯一的登记处，`_subtitle_result()` 是**全仓唯一允许写出这个形状的地方**，读方不需要再写 `.get`。`backend/tests/test_subtitle_result_shape.py` 读的就是这个元组。

最后两个键是后加的：加字段时漏改一处构造点，形状照样看着一致，只会在下游炸成 `KeyError` → 500。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><layers/></svg> 三种提取策略

```
                    ┌─────────────────────────────┐
                    │   SubtitleExtractor         │
                    │   .extract(url)             │
                    └──────────┬──────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
  ┌─────────────────┐ ┌──────────────┐ ┌────────────────┐
  │ ① B 站专用      │ │ ② 通用平台   │ │ ③ 回退方案     │
  │ Bilibili API    │ │ yt-dlp      │ │ Whisper API    │
  │ 最高优先级      │ │ 1800+ 平台  │ │ OpenAI 语音转写 │
  │ 不依赖 yt-dlp   │ │ 字幕/自动   │ │ 需 OPENAI_API_KEY │
  └─────────────────┘ └──────────────┘ └────────────────┘
```

#### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><play/></svg> 1. B 站专用（open API）

`_extract_bilibili(url)` 通过 B 站官方 API 获取字幕：

```
  B 站视频 URL
       │
       ▼
  GET https://api.bilibili.com/x/web-interface/view?bvid={bvid}
       │  获取 cid + aid
       ▼
  GET https://api.bilibili.com/x/v2/dm/view?aid={aid}&oid={cid}
       │  获取字幕列表
       ▼
  选择最佳字幕（优先中文人工字幕）
       │
       ▼
  下载 subtitle_url JSON → 解析 body 数组 → segments
```

不依赖 yt-dlp，稳定性高。B 站字幕是结构化的 JSON，包含 `from`/`to` 时间戳和 `content` 文本。

#### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><globe/></svg> 2. 通用平台（yt-dlp 元数据）

`_get_video_info(url)` 调用 yt-dlp 的 `extract_info(download=False)` 获取视频元数据，从中提取 `subtitles`（人工字幕）和 `automatic_captions`（自动字幕）。

**优先顺序：**

| 优先级 | 字幕类型 | 说明 | 质量 |
|:------:|:---------|:-----|:----:|
| 1 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><check/></svg> **manual** | 上传者提供 | ⭐⭐⭐ 最高 |
| 2 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:middle"><check/></svg> **auto** | 平台自动 ASR | ⭐⭐ 中等 |

**语言优先级：** `zh-Hans` > `zh` > `zh-CN` > `en` > `ja` > `ko`

找到匹配字幕 URL 后，`_download_subtitle_json()` 下载并解析为 segments。如果没有直接 URL，`_download_and_parse_via_ytdlp()` 通过 yt-dlp 下载 VTT 字幕文件，再用 `_parse_vtt()` 解析为分段。

#### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#ef4444" stroke-width="2" style="vertical-align:middle"><x/></svg> 3. 抖音（落穿到 ASR）

抖音视频没有平台字幕轨道，最终由 ASR 兜底。ASR 先要拿到音频：`_download_audio_for_asr()` 按域名分流——抖音链接交给 `DouyinParser` 下载，其余交给 yt-dlp。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><table/></svg> 字幕类型判断

| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><globe/></svg> 平台 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><file-text/></svg> 字幕来源 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><tool/></svg> 提取方式 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><bar-chart/></svg> 成功率 |
|:------:|:-----------|:-----------:|:--------:|
| Bilibili | 用户上传 / AI 生成 | B 站 API | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><check/></svg> 极高 |
| YouTube | 人工字幕 + 自动 ASR | yt-dlp metadata | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><check/></svg> 高 |
| 抖音 | 无外部字幕 | 直接返回空 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#ef4444" stroke-width="2" style="vertical-align:middle"><x/></svg> — |
| 其他 1800+ 平台 | 取决于平台 | yt-dlp metadata | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:middle"><alert-circle/></svg> 不定 |

---

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#8b5cf6" stroke-width="2" style="vertical-align:middle"><zap/></svg> 二、AI 总结生成（VideoSummarizer）

**<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><file/></svg> 文件：** `backend/summarizer.py` `class VideoSummarizer`

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#8b5cf6" stroke-width="2" style="vertical-align:middle"><target/></svg> 职责

调用 AI 服务（平台侧走阿里云百炼，支持 OpenAI 兼容与 Anthropic 两种协议，见下方「初始化」），基于字幕文本生成结构化总结、思维导图和问答。**没有兜底链**：provider 失败直接报错。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#8b5cf6" stroke-width="2" style="vertical-align:middle"><settings/></svg> 初始化

`VideoSummarizer.__init__`（`backend/summarizer.py`）有**两条分支**，不是单一后端：

```python
# 1. 用户自带凭据（BYOK）—— credential 不为 None 时走这条，provider="user_credential"
#    端点/模型默认仍走平台配置，用户显式指定了才用他的
# 2. 平台 provider —— provider="aliyun_bailian"，走 backend/provider_config.py
#    的 build_client()：按 ALIYUN_BAILIAN_PROTOCOL 选协议（openai 默认 / anthropic）
```

| 优先级 | 来源 | 触发条件 |
|---:|---|---|
| 1 | **用户自带凭据**（BYOK） | 调用方传了 `credential` |
| 2 | **平台 provider**（百炼，openai 或 anthropic 协议） | 配了 `ALIYUN_BAILIAN_API_KEY` |

两条都拿不到时抛 `ValueError`，提示配置环境变量。

⚠️ **没有兜底链**：两条路径都拿不到凭据时直接抛 `ValueError`，而不是悄悄降级到别家模型——兜底链会把第一个 provider 的真实错误盖住，让人排查时看到的是一个与原因无关的报文。见 [ADR 0028](adr/0028-platform-model-and-provider-protocols.md)。

平台模型名不是类属性：**每次实例化时从模型清单表读一次**（`self._bailian_model`），后台改默认值不用重启；默认落在 `qwen3.7-flash`。环境变量 `ALIYUN_BAILIAN_MODEL` **优先于**清单表（运维改模型时先看它）。平台路径**显式关思考**（`enable_thinking=False`）：qwen3.7-flash 是混合思考模型，默认开着会返回 `reasoning_content` 并把 `max_tokens` 预算吃掉（实测同一问句 845 vs 47 tokens）。

BYOK 分支的端点校验不在 `__init__` 里，而在 `credentials.validate_base_url`——那里是唯一一份规则。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:middle"><file-text/></svg> 总结摘要（流式）

`summarize_stream(subtitle_text, language)` 使用流式模式（`stream=True`），逐个 token yield 给调用方。

**System Prompt：**
> 你是一个专业的视频内容分析助手。你擅长从字幕中提取关键信息并给出结构化总结。

**输出格式要求：**

```
┌─────────────────────────────────────────┐
│  ## 视频概述                            │
│  （2-3 句话概括）                        │
│                                         │
│  ## 内容大纲                            │
│  （按逻辑顺序列出章节）                   │
│                                         │
│  ## 核心知识要点                        │
│  （提取重要知识点）                      │
│                                         │
│  ## 总结                                │
│  （1-2 句话总体评价）                    │
└─────────────────────────────────────────┘
```

**字幕截断：** 前 60000 字符（约 2 万 tokens 以内），超出部分丢弃。模型上下文窗口远大于此，但截断是为了控制处理延迟和成本。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#06b6d4" stroke-width="2" style="vertical-align:middle"><map/></svg> 思维导图

思维导图**不单独发一次模型请求**：它与总结同一次调用返回，藏在总结文本的哨兵之后的 JSON 里（见 `backend/summarizer.py` 的 `VideoSummarizer` 类注释）。

**输出格式：** 纯 Markdown 标题层级，**最多 3 级**（`#` → `##` → `###`），前端用 markmap 库渲染为交互式 SVG 导图。层级与数量的结构上限写在提示词里（`backend/prompts/summarize_full.md`），清洗与校验的唯一真源是 `backend/mindmap.py`（`MAX_HEADING_LEVEL`/`MAX_CHILDREN_PER_NODE`/`MAX_TOTAL_NODES`）。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><message-circle/></svg> AI 问答（流式）

`chat_stream(subtitle_text, question)` 基于字幕内容回答用户问题，流式返回 tokens。字幕同样截断前 12000 字符。

---

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#06b6d4" stroke-width="2" style="vertical-align:middle"><activity/></svg> 三、SSE 流式传输

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#06b6d4" stroke-width="2" style="vertical-align:middle"><server/></svg> 后端（FastAPI）

**<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><link/></svg> 端点：** `POST /api/summarize` → `EventSourceResponse`

**事件顺序：**

```
  SSE Event Stream
  ════════════════════════════════════════════════════
  
  event: subtitle     →  字幕元数据 { has_subtitle, language, segments, full_text }
  event: quota        →  剩余次数 { remaining, limit }
  event: summary      →  总结文本片段（多次发送，前端累加）
  event: mindmap      →  思维导图 Markdown { markdown: "..." }
  event: tags         →  标签数组 ["...", ...]
  event: done         →  [DONE]
```

⚠️ **`quota` 在 `summary*` 之前下发**（`api_summarize.py:612` vs `:638`）。额度是在真正调模型的那一刻扣的，扣完立刻推一次给前端——不是最后才结算。所以「前端先看到额度变了、再看到总结一点点长出来」是正常顺序，不是竞态。

错误事件走独立分支，不在上面这条主线上：未登录 / 超限时发 `event: error` 带 `need_login` / `need_vip`，额度耗尽那条发的字段是 `{ message, reason }`（`reason: "quota_exhausted"`），**不带** `need_vip`。

复放已缓存结果的路径（不调模型）事件顺序一致：`subtitle → quota → summary → mindmap → tags → done`。

后端使用 `ServerSentEvent` 类构造 SSE 事件。数据均以 JSON 序列化发送，确保中文正确编码（`ensure_ascii=False`）。

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><globe/></svg> 前端（ReadableStream）

**<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><file/></svg> 文件：** `frontend/src/api/summarize.ts`

`summarizeVideo(url, language, callbacks)` 函数：

```javascript
const res = await fetch('/api/summarize', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...auth headers },
  body: JSON.stringify({ url, language }),
})
const reader = res.body.getReader()
// 逐块读取、按行分割、解析 SSE 事件
```

**关键实现细节：**

| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><hash/></svg> | 步骤 | 说明 |
|:----:|:-----|:-----|
| 1 | **流式读取** | `res.body.getReader()` 获取 ReadableStream，逐 chunk 读取 |
| 2 | **行缓冲** | `buffer += decoded` 累积不完整的行，按 `\n` 分割 |
| 3 | **事件分发** | `event:` 标记事件类型，`data:` 分发到对应回调 |
| 4 | **组件安全** | `watch(() => props.videoUrl, callback, { immediate: true })` 自动发起 SSE |

### <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:middle"><lock/></svg> 权限控制

```python
# 两步走：先只判定，不扣
def _check_quota_permission(user: dict | None, kind: str):
    limit = quota_limit(kind)          # 走模块当前值，不在 import 期冻结
    label = spec(kind).label           # 取注册表的中文名："解析" / "追问"
    if not user:
        return False, 0, "请先登录后使用 AI 功能"
    allowed, remaining = check_quota_kind(user["id"], kind)
    if not allowed:
        return False, 0, f"今日{label}次数已用完（每日 {limit} 次）"
    return True, remaining, None

# 真正要调模型的那一刻才扣 —— 字幕提取失败不扣额度
consume_quota(user["id"], kind)
```

`kind` 的取值由 `backend/quota_kinds.py` 的 `QUOTA_KINDS` 注册表定义：现为 `"parse"`
（解析，产出总结 + 思维导图 + 标签）与 `"chat"`（追问）。两个计数器在同一张 `users`
表上但各占自己的列与日期字段，彼此独立、各自按 UTC 日期重置。中文名也在注册表里，
随 `/api/quota` 的 `labels` 下发，前端不再自己抄一份。

| 用户状态 | 响应行为 |
|:---------|:---------|
| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#9ca3af" stroke-width="2" style="vertical-align:middle"><user-x/></svg> 未登录 | `event: error` → `need_login: true` |
| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f59e0b" stroke-width="2" style="vertical-align:middle"><alert-circle/></svg> 免费用户超限 | `event: error` → `need_vip: true` |
| <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#eab308" stroke-width="2" style="vertical-align:middle"><star/></svg> VIP 用户 | 无限次（跳过次数检查） |

---

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><refresh/></svg> 四、组件生命周期与数据流

**<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><file/></svg> 文件：** `frontend/src/components/VideoSummary.vue`

组件通过 `:key="summaryKey"` 控制销毁重建：

```
  用户输入 URL
       │
       ▼
  App.vue 解析成功
       │
       ├─ summaryKey++          (旧 VideoSummary 销毁)
       └─ currentUrl = url      (新 VideoSummary 收到 videoUrl)
              │
              ▼
  watch({ immediate: true })
       │
       ▼
  summarizeVideo() → SSE 连接
       │
       ▼
  各事件回调更新响应式 ref
       │
       ▼
  Vue 自动渲染对应 Tab 内容
```

**四组 Tab 各司其职：**

| Tab | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><database/></svg> 数据源 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><eye/></svg> 渲染方式 | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#6b7280" stroke-width="2" style="vertical-align:middle"><activity/></svg> 状态 |
|:---:|:----------|:------------|:------:|
| 总结摘要 | `summaryMd` (ref) | `marked()` → `v-html` | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><refresh/></svg> 流式累加实时显示 |
| 思维导图 | `mindmapMd` (ref) | markmap SVG | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><check/></svg> 一次性生成后渲染 |
| 字幕文本 | `subtitleData` (ref) | `whitespace-pre-wrap` | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#10b981" stroke-width="2" style="vertical-align:middle"><check/></svg> 一次性接收全部 |
| AI 问答 | `chatAnswer` (ref) | `marked()` → `v-html` | <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#3b82f6" stroke-width="2" style="vertical-align:middle"><refresh/></svg> 流式累加，提问触发 |

---

## <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#ef4444" stroke-width="2" style="vertical-align:middle"><alert-triangle/></svg> 五、错误处理链

```
  ┌───────────────────────────────────────┐
  │  subtitle 提取失败                     │
  │    → fallback: has_subtitle=false      │
  │    → 前端显示 "没有可用的字幕"          │
  └───────────────────────────────────────┘
  
  ┌───────────────────────────────────────┐
  │  AI 服务调用失败                       │
  │    → except 捕获异常                   │
  │    → event: error + 错误描述           │
  │    → 前端显示错误信息                  │
  └───────────────────────────────────────┘
  
  ┌───────────────────────────────────────┐
  │  yt-dlp 解析失败（如抖音 cookies）      │
  │    → 抖音直返无字幕                    │
  │    → 其他平台 → error event           │
  └───────────────────────────────────────┘
  
  ┌───────────────────────────────────────┐
  │  网络断开                              │
  │    → fetch 抛出异常                    │
  │    → onError 回调 → errorMsg 显示     │
  └───────────────────────────────────────┘
```
