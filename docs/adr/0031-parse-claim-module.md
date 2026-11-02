# ADR 0031：解析占位权收进独立 module，释放契约由上下文承担

- 状态：已采纳（2026-11-02）
- 相关：[ADR 0006 首次解析先到先得](0006-first-parse-wins.md)、[ADR 0007 本人可覆盖自己的那份](0007-owner-may-regenerate-own-summary.md)、[ADR 0014 卡片字段在占位时写入](0014-card-fields-written-at-reservation.md)、[ADR 0015 覆盖闸门落库](0015-regenerate-gate-in-db.md)

## 背景

「谁可以调模型、谁只能等着」这条承诺落在两条边上：首次解析的**占位**，与覆盖自己那一份的**闸门**。两条边的实现原本都在路由的私有函数里：轮询顺序、等待超时、闸门配对、失败释放，全靠调用方手写，正确顺序只存在于 docstring。

后果不是「代码难看」，而是三件具体的事：

1. 路由直接调 12 次数据库原语，任何一次改动都要先读懂那段循环才能动。
2. 释放靠 `finally` 里的自觉：「位置是否结清」与「额度是否已扣」两根标志挤在同一个函数里，历史上已经错过一次（工单 #17 第 3 项：落库失败时替这条失败路径做了一次不该做的还位）。
3. 协议无法单独测：要么走 HTTP（SSE、额度、字幕提取一起进来），要么调私有函数（把实现细节钉成契约）。

## 决策

把两条边收进一个 module：`backend/video_claim.py`。

它有且只有两个 interface，都是 **async context manager**：

- `acquire_claim(video_url, user_id, video_title, cover_url, *, timeout, poll_interval)` —— 首次解析的占位权，产出 `owner` / `reuse` / `busy` 三种 outcome。
- `reclaim(video_url, user_id)` —— 覆盖自己那一份的闸门，产出 `regenerate` / `forbidden` / `busy` / `skip` 四种 outcome。

**退出上下文即释放**：占位没落库就还位，闸门用完就放掉。释放不再依赖调用方的 `finally`，也不依赖任何标志位。

「位置已经结清」由 `ParseClaim.settle()` 表达：调用它表示「结果已落库」或「位置已不归自己」，此后退出上下文不再还位。已落库与已还位互斥——漏了 settle 会删掉刚写进社区的那一行，误 settle 则留下一行永远 pending 的死占位。

### 为什么是 context manager，而不是两个函数加一个 try/finally

「抢到」与「还回」必须成对。写成两个函数，成对这件事就退回给调用方的自觉——而那正是本 ADR 要消除的东西。写成 context manager，忘记还位在语言层面就不可能发生。

### 为什么 outcome 仍是字符串

`owner` / `reuse` / `busy` 这套词汇是这套状态机对外的契约，有结构层守卫钉着（`backend/tests/test_claim_state_machine.py` 扫函数体里 return 的 outcome 槽位）。具名常量由这个 module 导出，路由不再手写字符串；守卫读的是常量的值，所以改字面量会被守卫逮到。

### 为什么等待超时是参数

`timeout` 与 `poll_interval` 是函数参数，缺省时取 module 常量，且**调用时**才读（写成默认参数的话那个值在 import 时就定死，改常量不生效）。这样测试能传小值，生产值仍然只有一处。

## 明确不做

- **额度扣减与退款不进这个 module**。额度是独立领域概念（GLOSSARY.md），把两件事捆进一个 interface 会让两边都更难测。
- **视频行读取不进这个 module**。它只管抢权，不当数据入口；复用路径要的那一行由路由自己读，读不到就明确报错而不是拿空值去重放。
- **追问路径不占位**，不接入这个 module（追问不产生社区内容）。
- **不新建后台清理任务**。陈旧占位的回收沿用 `probe_video` / `reserve_video` 内部的 TTL 判定。
- **不引入替身**。占位权的一半行为在事务边界上（唯一索引裁决、条件 UPDATE、DELETE 的 WHERE），替身会把这些语义复制一份；module 级测试用真 SQLite（tmp_path 独立库）。

## 代价

1. 调用点要迁移：路由的分支结构，加上若干个测试文件里的直接调用。旧函数删除，不留转发层。
2. 断言位置变了：占位是真的被占着的，退出上下文就还回去，所以「占位期间那一行是什么状态」这类断言只能写在上下文里面。
3. 路由多了一层 `AsyncExitStack`：两个上下文（覆盖闸门、占位）按进入顺序的反序退出。

## 结果

协议知识只存在于 `backend/video_claim.py` 一个文件里；路由只关心「拿到什么 outcome」与「结果落库了吗」。两条边的释放契约由上下文承担，不再由标志位承担。
