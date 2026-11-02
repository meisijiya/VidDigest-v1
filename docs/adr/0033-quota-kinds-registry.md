# ADR 0033：额度种类收进 `quota_kinds` 注册表，中文名随协议下发

- 状态：已采纳（2026-11-02）
- 相关：[ADR 0003 拆分额度计数器](0003-split-quota-counters.md)、[ADR 0032 前端 HTTP 出口](0032-frontend-http-outlet.md)

## 背景

「有哪几种额度」原本写在四处：`backend/database.py` 的列名表、
`backend/api_summarize.py` 的中文名 `_QUOTA_LABELS`、`backend/admin_api.py`
的字段命名与 pydantic 字段声明、以及前端 `lib/quota.ts` 与 `AppHeader.vue`
各自的种类清单**再加一份中文名**。

工单 #38 把列名收成了一个常量，并留了一条结构守卫（AST 扫描，`backend/`
下不许再出现第三处枚举）盯着剩下的手工登记点。但收口后仍有四处要人记：

| 位置 | 现状 | 漏掉时的症状 |
| --- | --- | --- |
| `api_summarize._QUOTA_LABELS` | 中文名 | 少显示一格额度，或提示文案里没有种类名 |
| `admin_api.QuotaUpdateRequest` | 静态 pydantic 字段 | 请求 200、额度不改（未声明的键被静默丢弃） |
| `frontend/src/lib/quota.ts` | `quotaOf(q, 'parse')` 三处 | 新种类不渲染，文案逐字不变 |
| `frontend/src/components/AppHeader.vue` | 种类 + 中文名 | 同上，且中文名在前端有第二份 |

最后一行是唯一跨层的真重复：同一种额度在两个仓库层各有一个中文名，
改一边不会连带另一边。而「加第三种额度」今天要动三列迁移 + 上述四处 + 六组测试。

## 决策

**一处注册表**：新增 `backend/quota_kinds.py`，一种额度 = 一条 `QuotaKind` 记录
（计数字段、日期字段、上限常量名、覆盖列名、中文名）。`spec(kind)` 与
`QUOTA_KINDS` 都住在这个 module 里——能判定「拼错的 kind」的只有表本身，
报错文本留在数据层会与表分家。`database.py` 不再定义 `QUOTA_KINDS`，
四个消费者（`database` / `api_summarize` / `admin_api` / 测试）全部从
`quota_kinds` 取。

**中文名进协议**：`_quota_payload` 的响应多一个 `labels` 字段
（`{"parse": "解析", "chat": "追问"}`，顺序即注册表顺序）。`/api/quota` 与 SSE
的 `quota` 事件同形——它们本来就是同一个函数产出的。前端按 `labels` 渲染额度行，
自己不再持有种类清单或中文名。

**请求体字段由注册表生成**：`QuotaUpdateRequest = create_model(...)`，一种额度
一个字段，未声明的键仍按 pydantic 默认静默丢弃（既有协议行为，改了要让前端与
文档一起评估）。

**前端行构造收进一处**：`lib/quota.ts` 的 `quotaRows(q)` 给出行数、中文名、
数值文案与「是否用完」，`describeQuota` 与 `AppHeader` 都只消费它——
改造前两处各自遍历、各自判「用完」。

**守卫跟着换**：原来两条「键集 == 注册表」的断言在构造即保证之后变成恒真，
删除；保留并强化结构扫描（注册表之外不许枚举）、新增「注册表里的列必须真的
在 `users` 表上」与「payload 的 `labels` 等于注册表且顺序一致」。

## 代价

- 前端在旧后端上（没有 `labels`）不再分种类显示，退回总量文案
  「今日剩余 X / Y 次」。这是刻意的：另一条路是把种类名抄回前端，
  而那正是本 ADR 要消掉的东西。
- 中文名放在后端数据 module 里，看着像「UI 文案进了数据层」。它其实是
  这个概念的**名字**（与「解析额度」「对话额度」同级），不是文案样式；
  真正的界面文案（「今日XX次数已用完」）仍只在 `api_summarize` 里。
- 协议多一个字段。代价很小：`labels` 与已有的槽位键同源同序，前端零解析负担。

## 影响

- `backend/quota_kinds.py`（新）、`backend/database.py`（删 `QUOTA_KINDS`、
  `_quota_spec`，改从注册表取）、`backend/api_summarize.py`（删 `_QUOTA_LABELS`、
  payload 带 `labels`）、`backend/admin_api.py`（`create_model` 生成请求体）
- `frontend/src/lib/quota.ts`（`quotaRows` + 按 `labels` 渲染）、
  `frontend/src/components/AppHeader.vue`（删除本地行构造）、
  `frontend/src/components/VideoSummary.vue`（事件整体展开，不再逐项挑种类）
- 守卫与变异：`backend/tests/test_quota_kinds_single_source.py` 重写；
  `backend/tests/mutation_check_quota.py` 加 Q23（payload 里另抄一份中文名必须转红）
