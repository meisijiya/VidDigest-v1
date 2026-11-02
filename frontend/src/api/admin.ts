import { http } from '../lib/http.ts'

import { toItems, toModelItem } from './models.ts'
import type { ModelItem } from './models.ts'

/**
 * 管理端一条清单记录 → camelCase：在公共字段之上多两个
 * `enabled` 与 `sortOrder`。
 *
 * 与 `toModelItem` 分开而不是合成一个，是因为「全部行」是**管理视角**：
 * 公开端点按契约只给 `enabled = 1` 的行，混进管理页要用的字段会让
 * 那条边界在同一个函数里变得没法看。
 *
 * `enabled` 同样是 0 / 1 数字，与库表和接口口径一致。
 */
export interface AdminModelItem extends ModelItem {
  enabled: 0 | 1
  sortOrder: number
}

export function toAdminModelItem(raw: unknown): AdminModelItem {
  const row = (raw ?? {}) as Record<string, unknown>
  return {
    ...toModelItem(raw),
    enabled: Number(row.enabled) ? 1 : 0,
    sortOrder: Number(row.sort_order) || 0,
  }
}

/** 一次请求 + 一次形状检查。两种出口共用，免得两条路各断各的。 */
async function fetchAdminItems(map: (raw: unknown) => AdminModelItem): Promise<{ items: AdminModelItem[] }> {
  const res = await http.get('/api/admin/models')
  return { items: toItems(res.data, map) }
}

/**
 * 全部厂商清单（含 `enabled = 0` 的行）。**仅管理员**。
 *
 * 注意与 `api/models.ts` 的 `fetchPublicModelCatalog` 分开：普通用户调这个
 * 会拿到 403，BYOK 面板要用的是公开那一份。
 */
export async function fetchAdminModelCatalog(): Promise<{ items: AdminModelItem[] }> {
  return fetchAdminItems(toAdminModelItem)
}

// ── 分页列表（工单 #12）─────────────────────────────────────
//
// 服务端返回 `{items, total, limit, offset}`（数据层 `list_admin_users` /
// `list_admin_community` 的原样），而组件按 `page` / `pageSize` 想。
// 翻译收在**这一层**：组件不该知道 offset 怎么算，api 层也不该知道
// 组件内部有个 v.page。
//
// **返回的 pageSize 用服务端回传的那一个**，不是我们请求的那一个：
// 服务端对 limit 有上界（ADMIN_PAGE_SIZE_*），请求 500 会被夹到上界。
// 组件若还按自己请求的 500 算页数，翻到第二页就会漏行 / 重复行。

/** 把组件的 page/pageSize 翻成服务端的 limit/offset。page 从 1 起。 */
interface LimitOffset {
  limit: number
  offset: number
}

function toLimitOffset(page: number, pageSize: number): LimitOffset {
  const p = Number.isFinite(page) && page >= 1 ? Math.floor(page) : 1
  const size = Number.isFinite(pageSize) && pageSize >= 1 ? Math.floor(pageSize) : 20
  return { limit: size, offset: (p - 1) * size }
}

/** 形状不对就抛，与 `toItems` 同一纪律：接口没按约定返回 ≠ 这一页是空的。 */
interface PageEnvelope<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  page: number
  pageSize: number
}

function toPage<T>(data: unknown, what: string): PageEnvelope<T> {
  const body = (data ?? {}) as { items?: T[]; total?: number; limit?: number; offset?: number }
  if (!body || !Array.isArray(body.items)) {
    throw new Error(`${what}响应形状不对`)
  }
  const limit = Number(body.limit) > 0 ? Number(body.limit) : body.items.length
  const offset = Number(body.offset) >= 0 ? Number(body.offset) : 0
  return {
    items: body.items,
    total: Number(body.total) || 0,
    limit,
    offset,
    page: Math.floor(offset / limit) + 1,
    pageSize: limit,
  }
}

/**
 * 后端契约是 snake_case，前端只用 camelCase。
 *
 * **这个文件是转换的唯一发生地**（工单 #15 收口）。三份转换 —— 用户 / 社区 / 模型
 * —— 都在这里，组件里不再自己写第二份。
 *
 * 列表出口与回读出口**现在都在这里翻**（工单 #15 收口列表侧，#22 收口回读侧）：
 * `fetchAdminUsers` / `fetchAdminCommunity` 翻列表项，`setUserQuota` /
 * `createAdminUser` / `setUserAdmin` 翻回读的 `user`，`updateCommunityTags` 翻
 * 回读的 `item`，模型清单四条出口本来就连回读一起翻了。组件拿到的永远是
 * camelCase，自己一次都不翻。
 *
 * #22 收口前这四个出口是漏的，而漏法有具体后果：AdminPage.vue 里那行
 * `res.user.is_admin` 读到 undefined——**提权成功却在界面上提示「已撤销管理员
 * 权限」，且不报错**。同一个 `res.user` 在下一行交给 replaceUser 翻成 camelCase、
 * 在这一行直接读 snake_case，能并存全靠「api 层恰好没翻」。这类 bug 的成因
 * 永远是**读不到的键退化成默认值而不报错**，所以出口宁可多翻一次也不能少翻。
 *
 * 为什么非得收在一处：转换散进组件之后，同一份数据会有两种命名形态混在数据流里，
 * 而「读不到的键」不会报错、只会退化成 `undefined` 或默认值。工单 #15 实测过两例：
 * ① `enabled` 两种形态同名读得到，所以布尔/数字口径不一致时**症状只是某几列显示异常**；
 * ② `sort_order` 已翻成 `sortOrder`，`toAdminModelItem` 再读 `sort_order` 读不到就
 * 退回 0，于是**每保存一次就把该行排序号抹成 0**。两处单看都对，数据流上是错的。
 */
const num = (v: unknown): number => (typeof v === 'number' ? v : Number(v) || 0)

/** 后台用户行（后端是 snake_case，这里是它在前端的 camelCase 形态）。 */
export interface AdminUser {
  id: number
  email: string
  isAdmin: boolean
  isVip: boolean
  vipExpireAt: string | null
  createdAt: string
  parseUsed: number
  chatUsed: number
  parseLimit: number
  chatLimit: number
  parseLimitOverride: number | null
  chatLimitOverride: number | null
  parseLimitSource: string
  chatLimitSource: string
}

/**
 * 后台用户行 → camelCase。
 *
 * `is_admin` / `is_vip` 翻成布尔（模板只判真假）；额度那几项翻成数字
 * （`-1` 无限、`0` 停用，数字判定比布尔更直接）；`*_override` 保持原值，
 * 因为 `null`（=回落全局）与 `0`（=一条都不能用）是**两种不同状态**。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
export function toAdminUser(r: Record<string, any>): AdminUser {
  return {
    id: r.id,
    email: r.email,
    isAdmin: !!r.is_admin,
    isVip: !!r.is_vip,
    vipExpireAt: r.vip_expire_at,
    createdAt: r.created_at,
    parseUsed: num(r.parse_used),
    chatUsed: num(r.chat_used),
    parseLimit: num(r.parse_limit),
    chatLimit: num(r.chat_limit),
    parseLimitOverride: r.parse_limit_override,
    chatLimitOverride: r.chat_limit_override,
    parseLimitSource: r.parse_limit_source,
    chatLimitSource: r.chat_limit_source,
  }
}

/** 后台社区行（camelCase 形态）。 */
export interface AdminCommunityItem {
  id: number
  videoUrl: string
  title: string
  authorEmail: string
  tags: string[]
  status: string
  createdAt: string
}

/**
 * 后台社区行 → camelCase。
 *
 * `status` 原样透传：组件要靠它分「占位中」与「已就绪」，
 * 翻成布尔会把两个状态压成一个。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
export function toAdminCommunityItem(r: Record<string, any>): AdminCommunityItem {
  return {
    id: r.id,
    videoUrl: r.video_url,
    title: r.title,
    authorEmail: r.author_email,
    tags: Array.isArray(r.tags) ? r.tags : [],
    status: r.status,
    createdAt: r.created_at,
  }
}

/** 一页后台列表（分页信息 + 已翻好的 items）。 */
export interface AdminPage<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  page: number
  pageSize: number
}

/** 后台用户列表的查询参数。 */
export interface AdminUserQuery {
  page?: number
  pageSize?: number
  q?: string
}

/**
 * 后台用户列表（只读）。**需管理员**。
 *
 * 返回 **items 已转成 camelCase**（`toAdminUser`）。转换在 api 层做完，
 * 组件拿到就能直接用——不再自己 `.map(toUser)`。
 */
export async function fetchAdminUsers({ page = 1, pageSize = 20, q = '' }: AdminUserQuery = {}): Promise<
  AdminPage<AdminUser>
> {
  const { limit, offset } = toLimitOffset(page, pageSize)
  const res = await http.get('/api/admin/users', {
    params: { limit, offset, q: String(q || '').trim() },
  })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
  const page1 = toPage<Record<string, any>>(res.data, '用户列表')
  return { ...page1, items: page1.items.map(toAdminUser) }
}

/**
 * 后台社区记录列表。**需管理员**。
 *
 * 不过滤 status：pending 占位行恰恰是后台最该看的（谁占了位没解析完）。
 * status 现已在契约里，组件据此显示「占位中」——管理员要能分辨一条空壳与
 * 一条真内容，删之前才知道自己在删什么。
 *
 * 返回 **items 已转成 camelCase**（`toAdminCommunityItem`）。
 */
export async function fetchAdminCommunity({ page = 1, pageSize = 20 } = {}): Promise<AdminPage<AdminCommunityItem>> {
  const { limit, offset } = toLimitOffset(page, pageSize)
  const res = await http.get('/api/admin/community', {
    params: { limit, offset },
  })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
  const p = toPage<Record<string, any>>(res.data, '社区记录')
  return { ...p, items: p.items.map(toAdminCommunityItem) }
}

/**
 * 调整某个用户的解析 / 对话额度。**需管理员。**
 *
 * 字段名必须翻成 snake_case（`parse_limit` / `chat_limit`）才与服务端
 * `QuotaUpdateRequest` 对上——发 camelCase 的话 pydantic 会当成「两个字段
 * 都没出现」，于是 `exclude_unset` 得到空字典，**静默什么都不改还返回 200**。
 * 那是本页最容易犯也最难发现的错，所以在这里翻，不留给调用方。
 *
 * 语义（服务端 `QuotaUpdateRequest` 的注释）：
 *   - 传 `null` → 清除覆盖，回落全局
 *   - 键整个不出现 → 这一项不动
 * 本函数**总是**把两个键都发出去，因为组件的 `toQuotaValue` 只会产出
 * `null` 或整数（留空 = 清除），语义一致。
 *
 * `user` 是**回读**结果（服务端重查库），不是请求值的回显。
 * `note === 'vip_not_effective'` 时本次调整此刻不生效（VIP 短路）。
 */
export async function setUserQuota(
  userId: number,
  { parseLimit = null, chatLimit = null }: { parseLimit?: number | null; chatLimit?: number | null } = {},
): Promise<{ user: AdminUser; note: string | null; message: string }> {
  const res = await http.post(`/api/admin/users/${userId}/quota`, {
    parse_limit: parseLimit ?? null,
    chat_limit: chatLimit ?? null,
  })
  if (!res.data || !res.data.user || typeof res.data.user !== 'object') {
    throw new Error('额度调整响应形状不对')
  }
  return { user: toAdminUser(res.data.user), note: res.data.note ?? null, message: res.data.message ?? '' }
}

/** 改厂商行时可能带上的键（只带调用方真的传了的那些）。 */
export interface AdminModelPatch {
  label?: string
  hint?: string
  baseUrl?: string
  models?: string[]
  defaultModel?: string
  enabled?: boolean
  sortOrder?: number
}

/**
 * 改一个厂商行（ADR 0010「模型清单可改」）。**需管理员。**
 *
 * 两条不可省的翻译：
 *
 * 1. **只带调用方真的传了的键**。后端是 PATCH 语义：没出现的键 = 不改那一项。
 *    无条件把七个字段全发过去，等于每次改一个显示名都顺手把模型列表、
 *    端点、排序全刷成草稿里的值 —— 而草稿可能没加载全。
 * 2. **enabled 翻成 0/1**。后端显式拒布尔值（Python 里 `True == 1`，
 *    `{"enabled": true}` 会静静地变成「上架」）。前端必须先翻。
 *
 * 键名翻成 snake_case：发 camelCase 的话 pydantic 会当成「七个字段都没出现」，
 * 静默什么都不改还返回 200 —— 与 setUserQuota 那条是同一类坑。
 *
 * `item` 是**回读**结果。`platformDefault` 让前端能当场显示
 * 「这一改会影响平台默认模型是什么」，而不是让管理员去猜。
 */
export async function updateAdminModel(
  providerId: string,
  patch: AdminModelPatch = {},
): Promise<{ item: AdminModelItem; platformDefault: string | null }> {
  const body: Record<string, unknown> = {}
  if ('label' in patch) body.label = patch.label
  if ('hint' in patch) body.hint = patch.hint
  if ('baseUrl' in patch) body.base_url = patch.baseUrl
  if ('models' in patch) body.models = patch.models
  if ('defaultModel' in patch) body.default_model = patch.defaultModel
  if ('enabled' in patch) body.enabled = patch.enabled ? 1 : 0
  if ('sortOrder' in patch) body.sort_order = patch.sortOrder

  if (Object.keys(body).length === 0) {
    // 与后端同一条纪律：空 patch 会被判 400，这里先挡住，省一次往返
    throw new Error('没有任何要改的字段')
  }

  const res = await http.patch(`/api/admin/models/${providerId}`, body)
  if (!res.data || !res.data.item) {
    throw new Error('模型清单更新响应形状不对')
  }
  return {
    item: toAdminModelItem(res.data.item),
    platformDefault: res.data.platform_default || null,
  }
}

/**
 * 后台建号（ADR 0012）。**需管理员。**
 *
 * `email` / `password` 原样发（它们本来就是 snake_case 同形），`is_admin`
 * 显式翻成布尔——后端声明的是 `bool`，发 0/1 虽然也能过，但让「这个字段
 * 是不是开关」这件事在契约上只有一个答案。
 *
 * `user` 是**回读**结果，不是请求值的回显。
 */
export async function createAdminUser({
  email,
  password,
  isAdmin = false,
}: { email?: string; password?: string; isAdmin?: boolean } = {}): Promise<{ user: AdminUser }> {
  const res = await http.post('/api/admin/users', {
    email,
    password,
    is_admin: !!isAdmin,
  })
  if (!res.data || !res.data.user || typeof res.data.user !== 'object') {
    throw new Error('建号响应形状不对')
  }
  return { user: toAdminUser(res.data.user) }
}

/**
 * 改管理员标记（ADR 0012）。**需管理员。**
 *
 * **只发 is_admin 一个键**：后端 `UserAdminUpdateRequest` 是 extra="forbid"，
 * 多带一个键会整个 422（其中包括 is_vip——VIP 不在后台可改范围）。
 */
export async function setUserAdmin(userId: number, isAdmin: boolean): Promise<{ user: AdminUser }> {
  const res = await http.patch(`/api/admin/users/${userId}`, {
    is_admin: !!isAdmin,
  })
  if (!res.data || !res.data.user || typeof res.data.user !== 'object') {
    throw new Error('权限调整响应形状不对')
  }
  return { user: toAdminUser(res.data.user) }
}

/** 标签词表的一组。 */
export interface TagGroup {
  name: string
  tags: string[]
}

/**
 * 标签词表（分组 + 上限）。**需管理员。**
 *
 * 前端**不**自己维护一份词表：`CommunityPage.vue` 的标签筛选已经是
 * 「从已加载的卡片汇总」，不在前端抄第二份。后台要让人**勾选**标签就绕不开
 * 词表，所以从服务端取一次——多这一个端点，好过词表在两个地方各活一份。
 *
 * 保留 groups 是因为词表分组顺带说明了每个标签的适用语境，摊平就把这个
 * 信息丢了。maxTags 由服务端给出，是上限的唯一真值。
 */
export async function fetchTagVocabulary(): Promise<{ maxTags: number; groups: TagGroup[] }> {
  const res = await http.get('/api/admin/tags/vocabulary')
  const d = res.data || {}
  if (!Array.isArray(d.groups) || !d.groups.length) {
    throw new Error('标签词表响应形状不对')
  }
  // 上限只有服务端那一个出处。读错键名会静默退回到写死的数字，
  // 而写死的数字恰好等于当前 MAX_TAGS 时症状是「上限改了界面没变」
  // ——管理员能勾到后端会 400 拒掉的标签数（admin_api 用 MAX_TAGS
  // 校验）。与上面 groups 同一个处置：宁可抛（工单 #41）。
  if (!(Number(d.maxTags) > 0)) {
    throw new Error('标签词表响应里没有 maxTags')
  }
  return {
    maxTags: Number(d.maxTags),
    groups: d.groups
      .filter((g: TagGroup) => g && Array.isArray(g.tags) && g.tags.length)
      .map((g: TagGroup) => ({ name: String(g.name || ''), tags: g.tags.map(String) })),
  }
}

/**
 * 改某条社区视频的标签。**需管理员。**
 *
 * `tags` 发**词表内**的值，最多 3 个。服务端会严格校验：词表外一律 400 并在
 * detail 里点名被拒的值——不像模型那条路径会静默回落到「其他」，因为管理员
 * 打错字不该被藏起来（见 ADR 0013）。
 *
 * `item` 是**回读**结果，不是请求值的回显。
 */
export async function updateCommunityTags(videoId: number, tags: string[]): Promise<{ item: AdminCommunityItem }> {
  const res = await http.patch(`/api/admin/community/${videoId}`, {
    tags: (tags || []).map(String),
  })
  if (!res.data || !res.data.item || typeof res.data.item !== 'object') {
    throw new Error('改标签响应形状不对')
  }
  return { item: toAdminCommunityItem(res.data.item) }
}

/**
 * 删掉一条社区视频（ADR 0013）。**需管理员。**
 *
 * **只删 `videos` 那一行**。解析过它的用户在自己「历史」里的记录不受影响——
 * 库里没有任何外键指向 videos，这正是该语义成立的前提。
 */
export async function deleteCommunityVideo(videoId: number): Promise<{ deleted: number }> {
  const res = await http.delete(`/api/admin/community/${videoId}`)
  return { deleted: res.data?.deleted ?? videoId }
}

/**
 * 删号（ADR 0012）。**需管理员。**
 *
 * 名下有订单或解析历史时后端回 **409**，body 形如
 * `{ detail, blockers: { orders: 2, parse_history: 5 } }`。
 * 本函数把 blockers 原样带出去（不塞进 message）：前端要的是数字，
 * 「还剩几行要处理」不能靠从中文里正则抠。
 *
 * 409 时抛出的 Error 附带 `.status` 与 `.blockers`。
 */
export async function deleteAdminUser(userId: number): Promise<{ deleted: number }> {
  const res = await http.delete(`/api/admin/users/${userId}`)
  return { deleted: res.data?.deleted ?? userId }
}

// ── 邀请码（工单 #99 / #103 / #104）──────────────────────────
//
// 注册的闸门钥匙由管理员发。库里只存 sha256 摘要（工单 #103），
// **明文只在创建那一次响应里给**——之后任何出口都拿不回来。
//
// 这一节把三件事收进 api 层：取列表、批量发码、作废。
// 组件拿到的永远是 camelCase，自己一次都不翻（与本文件其余出口同纪律）。

/** 邀请码的一行（camelCase 形态）。 */
export interface AdminInviteCode {
  id: number
  /** 脱敏码（前 4 后 4）。**列表出口才有**；创建响应里给的是明文。 */
  codeMasked: string
  /** 明文码：**只在创建响应里给一次**，之后任何出口都拿不到。 */
  code?: string
  createdBy: number | null
  /** 发放人邮箱：管理员要能看出「这枚码是谁发的」（工单 #104）。 */
  createdByEmail: string | null
  createdAt: string
  expiresAt: string | null
  maxUses: number
  usedCount: number
  revoked: number
  note: string | null
  expired: boolean
  exhausted: boolean
  /** 状态词：active / exhausted / expired / revoked。 */
  status: string
}

/**
 * 后端原文（snake_case）→ 前端的邀请码行。
 *
 * 与本文件其余转换同一个纪律：**只在 api 层翻一次**。
 * `code` 与 `code_masked` 只会有一个存在（创建给明文、列表给脱敏码），
 * 缺的读成空串——不是 undefined，因为模板里直接显示它。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
function toAdminInviteCode(r: Record<string, any>): AdminInviteCode {
  return {
    id: num(r.id),
    codeMasked: String(r.code_masked ?? ''),
    ...(r.code ? { code: String(r.code) } : {}),
    createdBy: r.created_by === null || r.created_by === undefined ? null : num(r.created_by),
    createdByEmail: r.created_by_email ? String(r.created_by_email) : null,
    createdAt: String(r.created_at ?? ''),
    expiresAt: r.expires_at ? String(r.expires_at) : null,
    maxUses: num(r.max_uses),
    usedCount: num(r.used_count),
    revoked: num(r.revoked),
    note: r.note ? String(r.note) : null,
    expired: !!r.expired,
    exhausted: !!r.exhausted,
    status: String(r.status ?? ''),
  }
}

/** 邀请码列表的查询参数。`status` 空串 = 不筛。 */
export interface AdminInviteCodeQuery {
  page?: number
  pageSize?: number
  status?: string
}

/**
 * 邀请码列表。**需管理员。**
 *
 * 列表里**没有明文**——后端在数据层就把完整码拿掉了，只给脱敏提示。
 * 筛选按状态走 `status` 参数（active / exhausted / expired / revoked），
 * 留空 = 全部。
 */
export async function fetchAdminInviteCodes({
  page = 1,
  pageSize = 20,
  status = '',
}: AdminInviteCodeQuery = {}): Promise<AdminPage<AdminInviteCode>> {
  const { limit, offset } = toLimitOffset(page, pageSize)
  const res = await http.get('/api/admin/invite-codes', {
    params: { limit, offset, ...(status ? { status } : {}) },
  })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
  const p = toPage<Record<string, any>>(res.data, '邀请码列表')
  return { ...p, items: p.items.map(toAdminInviteCode) }
}

/**
 * 批量发邀请码。**需管理员。**
 *
 * 每次发几枚由 `count` 决定（服务端有上界，超了回 400）。
 * **明文只在这次响应里给**：每枚码各给一次，之后列表里只有脱敏码。
 *
 * 键名翻成 snake_case：服务端是 `extra="forbid"`，多带一个键会整个 422。
 */
export async function createAdminInviteCodes({
  count = 1,
  maxUses = 1,
  expiresInHours = null,
  note = '',
}: {
  count?: number
  maxUses?: number
  expiresInHours?: number | null
  note?: string
} = {}): Promise<{ items: AdminInviteCode[] }> {
  const res = await http.post('/api/admin/invite-codes', {
    count,
    max_uses: maxUses,
    expires_in_hours: expiresInHours,
    note: note || null,
  })
  const raw = res.data?.items
  if (!Array.isArray(raw)) {
    throw new Error('发邀请码响应形状不对')
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
  return { items: (raw as Record<string, any>[]).map(toAdminInviteCode) }
}

/** 作废一枚邀请码。**需管理员。** */
export async function revokeAdminInviteCode(inviteId: number): Promise<{ item: AdminInviteCode }> {
  const res = await http.post(`/api/admin/invite-codes/${inviteId}/revoke`)
  if (!res.data?.item || typeof res.data.item !== 'object') {
    throw new Error('作废邀请码响应形状不对')
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- 后台接口返回的行没有运行时校验，any 只允许出现在这一层（读字段时逐个收窄）
  return { item: toAdminInviteCode(res.data.item as Record<string, any>) }
}
