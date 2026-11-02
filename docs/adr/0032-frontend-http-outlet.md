# ADR 0032：前端 HTTP 出口收进 `lib/http`，凭据与 401 各一份

- 状态：已采纳（2026-11-02）
- 相关：[ADR 0004 用户自带密钥不落盘](0004-byok-forwarded-not-stored.md)、[ADR 0010 管理后台的范围与鉴权](0010-admin-console-scope-and-auth.md)

## 背景

前端把「这个请求带没带凭据」这件事写了五遍：

1. `api/auth.ts` 注册了一个全局 axios 请求拦截器，注入 `Authorization`
2. `api/admin.ts`、`api/community.ts`、`api/history.ts` 各自一份复制粘贴的 `client()`，读 localStorage 装头
3. `api/summarize.ts` 里的三处原生 `fetch`（SSE、额度、模板清单）自己读 token 装头

于是「凭据从哪读、哪个端点带凭据」要读五个文件才能回答；新增一个 api 模块，
第一件事是猜「照着哪份抄」。

401 的判定同样有三份：`stores/session.ts`、`components/HistoryPage.vue`、
`api/community.ts` 各写了一遍「`err.response.status === 401`」。三份逐字相同，
但改一份不会连带另外两份。

还有一条更要紧的：**公开端点带不带凭据，取决于模块加载顺序**。
全局拦截器注册在 `api/auth.ts` 的模块顶层，而应用启动就会 import 它——
于是真实运行里公开端点（`/api/models`）是**带着 token** 的；
而 `frontend/tests/model-catalog.test.mjs` 里那条「公开端点不该带凭据」的断言
之所以能过，只因为那个测试文件没有 import `api/auth.ts`。断言通过，
行为却相反——这类假绿比没有断言更贵。

## 决策

新增 `frontend/src/lib/http.ts`，它是 HTTP 出口的唯一真值：凭据注入、401 判定、
两种传输（axios 与原生 fetch）。

**四个出口，按凭据分**：

| 出口 | 凭据 | 用在哪 |
| --- | --- | --- |
| `http` | 带 | 需要登录的 api 调用 |
| `httpFetch` | 带 | 普通 JSON 请求（fetch） |
| `httpStream` | 带 | SSE：响应体当流读，取消靠 `init.signal` |
| `httpAnonymous` | **不带** | 公开端点、注册、登录 |

**凭据的落盘与读取归 `lib/http`**：token 存在哪、怎么读、怎么清，只有这一处。
用户快照（`auth_user`）留在 `api/auth.ts`——它是会话种子，不是凭据。

**401 判定只有一份**：`lib/http.isUnauthorized(err)`。清会话仍然归
`stores/session.ts`（那里才有 token 与用户状态），`lib/http` 不碰 store，
也就不与 store 成环。

### 为什么是薄包装，而不是 `axios.create()`

`axios.create()` 在**创建那一刻**就把 `axios.defaults` 拷走——adapter 与
baseURL 都在里面。Node 下的测试会在那之后改它们（把 adapter 换成 fetch、
把 baseURL 指向假主机，好让请求不真的出网），实例上那份是旧的，请求就真的
去解析域名了。

薄包装在**请求时**现读 `axios.defaults`，绕开这个时间差；包装只有一层，
`http.get(...)` 的写法与 axios 一致，调用点基本不变。

### 为什么公开端点要显式选匿名出口

「公开端点不带凭据」原来靠 import 副作用，是一条**看不见的规则**。
现在它是类型上的选择：公开端点用 `httpAnonymous`，带凭据的用 `http`。
写错的后果看得见（多了一个出口的差别），而不是靠记忆。

## 明确不做

- **不统一到 fetch**：axios 还要用（`params`、`responseType: 'blob'`），SSE 本来就只能走 fetch。两种传输收在一个 module 里就够了。
- **不做全局响应拦截器**：401 的后果各不相同（社区搜索翻成 `needLogin`、历史页给登录入口、store 作废会话），由调用点决定；`lib/http` 只提供判据。
- **不动 `AuthModal` 的一次性邀请码拦截器**：那是**请求**拦截器，作用域仅限那一次注册请求（`finally` 里 eject），与本 ADR 的凭据收口无关。
- **不动 `lib/errors.ts`**：那是「把失败分类成人话」，不是传输。
- **不动 api 层的 mappers**：snake_case → camelCase 是业务形状，与传输无关。
- **不给 api 层补类型义务**：`res.data` 照旧是不带约束的（泛型默认与 axios 一致）。给全仓 api 调用点加类型是另一件事。

## 代价

1. api 层的调用点从 `axios.get(...)` 改成 `http.get(...)`；三份 `client()` 与全局拦截器删除。
2. 文本断言的锚点要跟着改（`frontend/tests/history-page.test.mjs` 里那条「清空走服务端一次性接口」）。
3. `frontend/tests/model-catalog.test.mjs` 那条「公开端点不带凭据」的断言，从「碰巧不带」变成「显式不带」——它现在真的在验这件事。

## 结果

凭据注入从 5 个入口降到 1 个 module；401 判定从 3 份降到 1 份；
公开端点带不带凭据写进了类型。新增 api 模块时不用再猜「照着哪份抄」。

验证见 `frontend/tests/lib-http.spec.mjs`（token 注入四种出口、signal 透传、
401 判定两种形状），以及 `frontend/tests/mutation-ui-fixes.mjs` 的 M37
（把 401 判定改成 403 会让会话 store 的用例转红——判定本身被钉住了）。
