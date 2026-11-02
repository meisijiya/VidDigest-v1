import { http } from '../lib/http.ts'

/**
 * 列表里的一条历史记录。
 *
 * 字段名与后端 `database.list_parse_histories` 的行形状一一对应，
 * 改后端查询时要一起改这里 —— 这里是这份形状在前端的唯一副本。
 */
export interface HistoryItem {
  id: number
  video_url: string
  video_title: string | null
  summary_preview: string
  updated_at: string | null
  created_at: string | null
  is_favorite: boolean
  cover_url: string
  has_ai_result: boolean
  tags: string[]
  /** 列表查询没有这一列，模板里按它决定要不要显示「含问答记录」。 */
  has_chat?: boolean
}

/** 列表信封。`total` 与 `total_pages` 都要用，不能拿 items.length 顶替。 */
export interface HistoryList {
  items: HistoryItem[]
  page: number
  page_size: number
  total: number
  total_pages: number
  has_ai: number
  favorites: number
}

/** 标签筛选选项（带计数）。 */
export interface HistoryFacet {
  tag: string
  count: number
}

/**
 * 一条追问轮次。
 *
 * 形状恒为 `{question, answer}`（后端 `get_chat_session` 的约定，
 * `database.py` 里写死「所有读点都必须走这里」）。
 *
 * `answer` 可缺省：老库的 `parse_history.chat_history` 列里存过只有
 * 问题没有答复的条目（`append_chat_history` 退役前就那样写），
 * 那个列现在只读不写，但读出来的东西还是那个形状。
 */
export interface ChatMessage {
  question: string
  answer?: string
}

/** 单条历史的完整内容（`database.get_parse_history_detail`）。 */
export interface HistoryDetail extends HistoryItem {
  summary_md: string
  mindmap_md: string
  video_data: Record<string, unknown> | null
  subtitle_data: Record<string, unknown> | null
  chat_history: ChatMessage[]
}

/** 列表查询参数。缺省项由后端处理。 */
export interface HistoryQuery {
  q?: string
  tag?: string
  ai?: string
  favorite?: boolean
  page?: number
  page_size?: number
}

/**
 * 解析历史列表（分页 + 关键词 / 标签 / 仅收藏 / AI 状态）。
 *
 * 返回**整个信封**而不是 items：界面上的「共 N 条」和翻页都要用到 total。
 * 拿 items.length 顶替的话，最后一页会显示成「共 7 条」—— 而库里其实有
 * 25 条，只是它们在第二页。
 */
export async function fetchHistories(params: HistoryQuery = {}): Promise<HistoryList> {
  const res = await http.get('/api/history', { params })
  return res.data
}

/**
 * 标签筛选的选项（带计数）。
 *
 * 刻意不在前端从当前页汇总：那样一来翻页或一筛选，标签就会增减，
 * 用户读起来是「这个筛选不生效」。
 */
export async function fetchHistoryFacets(): Promise<HistoryFacet[]> {
  const res = await http.get('/api/history/facets')
  return res.data.items || []
}

/**
 * 删除一条历史。
 *
 * 收藏项在**服务端**有第二道保险：不带 force 时回 409。这里把它翻译成
 * ``{ refused: true }`` 而不是抛异常 —— 「被拦住」不是故障，调用方要弹
 * 第二道确认。抛异常的话界面只会显示「删除失败」，而那条记录其实好好地
 * 还在列表里，用户会以为已经删了。
 */
export interface DeleteHistoryResult {
  refused: boolean
  message?: string
}

export async function deleteHistory(
  id: number,
  { force = false }: { force?: boolean } = {},
): Promise<DeleteHistoryResult> {
  try {
    await http.delete(`/api/history/${id}`, { params: { force } })
    return { refused: false }
  } catch (e) {
    const err = e as { response?: { status?: number; data?: { detail?: { code?: string; message?: string } } } }
    const detail = err.response?.data?.detail
    if (err.response?.status === 409 && detail?.code === 'favorited') {
      return { refused: true, message: detail.message }
    }
    throw e
  }
}

/**
 * 收藏 / 取消收藏。**幂等设置**，不是翻转。
 *
 * 翻转在网络重试下会把结果反过来：点一下收藏成功，重试一次就变成取消了。
 */
export async function setHistoryFavorite(id: number, isFavorite: boolean): Promise<void> {
  await http.patch(`/api/history/${id}/favorite`, { is_favorite: isFavorite })
}

/**
 * 清空历史。默认**保住收藏**，服务端也是这个默认。
 *
 * 不再是「前端循环调 N 次删除」：上限 1000 条时那是 1000 个请求，
 * 中途失败会留下一半删一半没删的列表，而用户看不出是哪一半。
 */
export async function clearHistories({ force = false }: { force?: boolean } = {}): Promise<unknown> {
  const res = await http.delete('/api/history', { params: { force } })
  return res.data
}

/** 获取单条历史完整内容 */
export async function fetchHistoryDetail(id: number): Promise<HistoryDetail> {
  const res = await http.get(`/api/history/${id}`)
  return res.data
}

/**
 * 当前用户与某个视频的追问会话（只有他自己读得到）。
 * 没有记录时返回空数组——那是正常状态，不是错误。
 * 不在这里吞异常：读不到要能被上游看见，而不是变成一个静默的空列表。
 */
export async function fetchChatSession(url: string): Promise<ChatMessage[]> {
  const res = await http.get('/api/history/chat', { params: { url } })
  return res.data.chat_history || []
}

/**
 * 保存/更新解析历史（同一视频去重，空字段不覆盖已有数据）。
 *
 * 失败**不打断**主流程：用户已经拿到总结了，为一条历史记录弹窗是本末倒置。
 * 但不能一个字都不留——这条请求失败的后果是「刷新一次页面，那次解析就不见了」，
 * 而这个症状与「根本没解析成功」在界面上完全同形，不留痕迹就永远查不出来。
 * 而且 `/api/summarize` 从不写这张表（个人历史由前端驱动是刻意的职责收窄），
 * 这一条是**唯一**的写入途径，它没了就真的没了。
 *
 * 与上面 fetchChatSession 同一口径：读要能被上游看见，写也要能被上游看见。
 */
export async function saveHistory(payload: {
  url: string
  video_title?: string
  /**
   * 原样转发的 JSON 负载。
   *
   * 这里用 `object` 而不是 `Record<string, unknown>`：转发的东西是
   * `SubtitlePayload` 那类**接口**，而接口没有隐式索引签名，
   * 赋给 `Record<string, unknown>` 会直接报错。服务端这一列存的是
   * JSON 文本，形状由它自己定，前端不预判。
   */
  video_data?: object | null
  summary_md?: string
  mindmap_md?: string
  subtitle_data?: object | null
}): Promise<void> {
  try {
    await http.post('/api/history/save', payload)
  } catch (err) {
    console.warn('[history] 保存解析历史失败：', err)
  }
}
