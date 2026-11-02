# VidDigest 文档

## 用户文档

| 文档 | 说明 |
|:----|:-----|
| [使用手册](../README.md) | 快速开始、安装、配置、使用指南、FAQ |

## 开发者文档

| 文档 | 说明 |
|:----|:-----|
| [API 文档](API.md) | 完整 API 接口说明、请求/响应示例、认证方式 |
| [运维手册](OPERATIONS.md) | 部署架构、启动 / 关停、进程管理、故障排查、安全清单、升级维护 |
| [测试策略](TESTING-STRATEGY.md) | 测什么、每类用哪种测试、覆盖目标、缺口清单、示例用例写法 |
| [AI 功能核心技术说明](AI功能核心技术说明.md) | 字幕提取、AI 总结 / 导图 / 问答、SSE 事件流、额度与组件数据流 |
| [技术文档](TECHNICAL.md) | 当前实现的机制、可复现的数字口径、故障定位表、已知边界 |
| [邀请码说明](INVITE-CODES.md) | 邀请码的发放、消费、作废语义与运维操作 |

### 架构决策与代理约定

这两类文档不在上面那张表里，但改代码前值得先翻：

- [`adr/`](adr/) —— 已采纳的架构决策记录（ADR）。**一份一个决策**，编号只增不改。要动某块结构，先看有没有对应的 ADR 与它冲突。
- [`agents/`](agents/) —— 给编码代理用的约定：issue 追踪方式、分诊标签词汇、领域文档规则。
- [`../GLOSSARY.md`](../GLOSSARY.md) —— 仓库根的领域词汇表（全仓唯一一份 `GLOSSARY.md`）。

## 项目结构

```
universal-video-downloader/
├── backend/          # FastAPI 后端
│   ├── main.py           # 入口 + 主路由（/api/parse、/api/download 等）
│   ├── downloader.py     # yt-dlp 封装
│   ├── douyin.py         # 抖音解析
│   ├── summarizer.py     # 字幕提取 + AI（总结 / 导图 / 问答 / ASR 回退）
│   ├── prompt_template.py# 总结与导图的 prompt 模板
│   ├── prompts/          # prompt 模板正文（改措辞改这里）
│   ├── formats.py        # 清晰度 / 音轨的格式归类
│   ├── url_canonical.py  # 视频链接的比较用归一（ADR 0016）
│   ├── tags.py           # 固定标签词表与校验（ADR 0005）
│   ├── credentials.py    # BYOK 用户凭据的解析与脱敏（ADR 0004）
│   ├── model_catalog.py  # 模型清单表与默认模型（ADR 0011）
│   ├── provider_config.py # 平台 provider 的协议 / 端点 / 模型配置（ADR 0028）
│   ├── mindmap.py        # 思维导图的结构与层级 / 节点数上限（ADR 0027 / 0029）
│   ├── ratelimit.py      # 公开端点的速率闸门与并发闸门（ADR 0020 / 0021 / 0022）
│   ├── download_artifacts.py # 下载产物的落盘与清理
│   ├── auth.py           # JWT + bcrypt
│   ├── cors_config.py    # 跨域白名单（默认不开，按环境变量显式开口）
│   ├── logging_setup.py  # JSON 行日志与轮转
│   ├── database.py       # SQLite 数据层 + 迁移
│   ├── api_*.py          # API 路由模块（auth / community / history / payment / summarize）
│   ├── admin_api.py      # 管理后台路由
│   ├── tests/            # 后端测试与变异装置
│   └── .env.example      # 环境变量模板
├── frontend/         # Vue 3 SPA
│   ├── src/
│   │   ├── api/          # HTTP + SSE 请求
│   │   ├── components/   # UI 组件
│   │   ├── composables/  # 组合式函数
│   │   ├── config/       # 功能开关
│   │   ├── lib/          # 纯逻辑（BYOK 状态、标签筛选等）
│   │   ├── router/       # 五条真实路径的路由表
│   │   ├── stores/       # Pinia store（登录态）
│   │   └── views/        # 路由级页面
│   ├── tests/        # 两套 runner 的用例与变异装置
│   └── Dockerfile    # 构建产物 → nginx 运行段
├── scripts/          # 门禁脚本、变异装置、运维脚本（备份 / 巡检）
│   └── eval/         # 固定语料的评测集与检索质量判据（门禁第 11 关）
├── docs/             # 文档
│   ├── adr/         # 架构决策记录
│   ├── agents/      # 编码代理约定
│   └── *.md         # API / 运维 / 测试策略 / 技术文档 / AI 功能说明 / 邀请码
├── init.sh           # 门禁入口（唯一）
├── docker-compose.yml# 容器化编排
└── .github/workflows/# CI 工作流
```
