/**
 * 额度展示逻辑。额度拆成「解析 / 追问」两个独立计数器（工单 #4）后，
 * 用户需要同时看到两个数字——只有一个数字的话，拆分对用户不可见。
 *
 * 种类清单与中文名**不在这里**：后端随 payload 下发 `labels`
 * （`quota_kinds.labels()`，ADR 0033），本文件按它渲染。写死 `parse` / `chat`
 * 就等于把注册表抄一份到前端——后端加一种额度时前端会安静地少显示一种。
 *
 * 抽成纯函数而非留在组件里：这样没有构建工具也能测，
 * 也不会让展示规则散落在 computed 与模板之间。
 */

/**
 * 一种额度的一格。`remaining === -1` 表示无限（不是「剩负一次」）。
 */
export interface QuotaSlot {
  allowed?: boolean
  remaining: number
  limit: number
}

/**
 * 额度负载的形状。
 *
 * 顶层 `remaining` / `limit` / `unlimited` 是**兼容别名**，跟随「这次事件
 * 刚动的是哪类额度」（后端 `_quota_payload` 的注释说明了为什么）。
 * 底下按种类分的槽位是主结构，`labels` 给出它们的中文名与顺序。
 *
 * 索引签名是 `unknown` 而不是 `QuotaSlot`：后端可能加新的额度种类
 * （`QUOTA_KINDS` 是个注册表），写死成 QuotaSlot 就等于把这个表
 * 抄一份到前端——多一种额度时前端会静默少显示一种，而没有任何东西变红。
 */
export interface QuotaPayload {
  logged_in?: boolean
  unlimited?: boolean
  remaining?: number
  limit?: number
  /** 种类 → 中文名，顺序即渲染顺序。缺了它就没有分种类的行可渲染。 */
  labels?: Record<string, string>
  /** 自带凭据时为 true：这次没动平台额度 */
  byok?: boolean
  consumed?: boolean
  [kind: string]: unknown
}

/** 额度面板的一行。`text` 只是数值部分，标题在 `label`。 */
export interface QuotaRow {
  key: string
  label: string
  text: string
  exhausted: boolean
}

function quotaOf(q: QuotaPayload | null, kind: string): QuotaSlot | null {
  const v = q?.[kind]
  if (v && typeof v === 'object') {
    const slot = v as Partial<QuotaSlot>
    const remaining = Number(slot.remaining)
    const limit = Number(slot.limit)
    if (Number.isFinite(remaining) && Number.isFinite(limit)) {
      return { remaining, limit }
    }
  }
  return null
}

/** 兼容期回退：后端可能仍只给顶层 remaining / limit。 */
function fallbackQuota(q: QuotaPayload | null): QuotaSlot | null {
  const remaining = Number(q?.remaining)
  const limit = Number(q?.limit)
  if (Number.isFinite(remaining) && Number.isFinite(limit)) {
    return { remaining, limit }
  }
  return null
}

function isUnlimited(slot: QuotaSlot | null): boolean {
  return slot != null && slot.remaining === -1
}

/**
 * 额度面板的行。顶栏与总结页都从这里取，判定口径只有一份。
 *
 * 没有 `labels`（旧响应 / 兼容期）时返回空数组而不是退化成硬写的种类名：
 * 拿不到中文名就没有分种类的行可渲染，调用方退回总量文案。
 */
export function quotaRows(q: QuotaPayload | null): QuotaRow[] {
  if (!q) return []
  if (!q.logged_in) return [{ key: 'login', label: '状态', text: '登录后可用', exhausted: false }]
  if (q.unlimited) return [{ key: 'unlimited', label: '状态', text: '无限次', exhausted: false }]

  const labels = q.labels
  if (!labels || typeof labels !== 'object') return []

  const rows: QuotaRow[] = []
  for (const [key, label] of Object.entries(labels)) {
    const slot = quotaOf(q, key)
    if (!slot) continue
    if (isUnlimited(slot)) {
      rows.push({ key, label: String(label), text: '无限', exhausted: false })
    } else {
      const exhausted = slot.remaining <= 0
      rows.push({
        key,
        label: String(label),
        text: exhausted ? `已用完（0 / ${slot.limit}）` : `${slot.remaining} / ${slot.limit}`,
        exhausted,
      })
    }
  }
  return rows
}

/** 取不到任何有效行时不硬拼文案，直接给占位符。 */
export function describeQuota(q: QuotaPayload | null): string {
  if (!q) return '—'
  if (!q.logged_in) return '登录后可用'
  if (q.unlimited) return '无限次'

  const rows = quotaRows(q)
  if (rows.length > 0) return rows.map((r) => `${r.label} ${r.text}`).join(' · ')

  const fb = fallbackQuota(q)
  if (!fb) return '—'
  if (fb.remaining <= 0) return `今日解析已用完（0 / ${fb.limit}）`
  return `今日剩余 ${fb.remaining} / ${fb.limit} 次`
}

export function quotaBadgeClass(q: QuotaPayload | null): string {
  if (!q || !q.logged_in) return 'bg-ink/60 text-gray-500'
  if (q.unlimited) return 'bg-amber-100 text-amber-700'

  const rows = quotaRows(q).filter((r) => r.key !== 'login' && r.key !== 'unlimited')
  if (rows.length === 0) {
    const fb = fallbackQuota(q)
    if (!fb) return 'bg-ink/60 text-gray-500'
    return fb.remaining <= 0 ? 'bg-red-50 text-red-600' : 'bg-blue-50 text-blue-600'
  }

  // 任一额度用完就该警示：所有计数器都耗尽才是真正「用完」
  if (rows.some((r) => r.exhausted)) return 'bg-red-50 text-red-600'
  return 'bg-blue-50 text-blue-600'
}
