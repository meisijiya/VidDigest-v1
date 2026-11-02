import { http, isUnauthorized } from '../lib/http.ts'

/**
 * 一张社区卡片。
 *
 * 字段名对应后端 `database.COMMUNITY_CARD_FIELDS`：
 * 列表无论登录与否都只给这几列，多给就是给未登录访客多拿一份。
 */
export interface CommunityCard {
  id: number
  video_url: string
  cover_url: string
  video_title: string | null
  tags: string[]
}

/** 社区详情比卡片多几列（后端 `COMMUNITY_DETAIL_FIELDS`）。 */
export interface CommunityDetail extends CommunityCard {
  summary_md?: string
  mindmap_md?: string
  subtitle_text?: string
  created_at?: string | null
  updated_at?: string | null
}

/** 社区列表 / 搜索的分页信封。 */
export interface CommunityList {
  items: CommunityCard[]
  page: number
  page_size: number
  total: number
  total_pages: number
  /** 搜索结果附带：这次是按链接精确命中、按文本命中，还是只是浏览。 */
  mode?: 'url' | 'text' | 'browse'
}

/** 搜索结果。`needLogin` 是前端加的标记，不是后端字段。 */
export interface CommunitySearchResult extends CommunityList {
  needLogin: boolean
  /**
   * 全文检索的字符下限与「这次查询太短」。
   *
   * 后端在 text 模式下把这两个一起返回：不足下限的查询**必然**召回 0
   * （1 个字没有意义），但接口照常 200。不把这个边界讲出来的话，
   * 「搜不到」与「社区里真没有」在界面上完全一样。
   *
   * 2 个字是例外：后端对 2 字查询走子串兜底，所以它搜得到、也不算太短。
   * 下限由后端给出，前端不自己判断长度。
   *
   * 未登录回落那条路（needLogin）不经过检索，所以这两个字段没有值。
   */
  min_chars?: number
  q_too_short?: boolean
}

/** 标签筛选选项（带计数）。 */
export interface CommunityTag {
  tag: string
  count: number
}

/** 浏览参数。空标签**不传**（见 browseParams 的说明）。 */
export interface CommunityQuery {
  page?: number
  pageSize?: number
  tag?: string
}

/**
 * 列表 / 搜索共用的查询参数。
 *
 * 空标签**不传**：未选中任何标签时应当退回公开浏览，而「不带 tag」与
 * 「带一个空 tag」在代码里是两回事——后者读起来像「筛选了一个叫空
 * 字符串的标签」。
 */
function browseParams({ page = 1, pageSize = 20, tag = '' }: CommunityQuery = {}) {
  const params: { page: number; page_size: number; tag?: string } = { page, page_size: pageSize }
  if (tag) params.tag = tag
  return params
}

/**
 * 社区列表。**任何人都能看**，未登录同样可用——所以这里刻意不因为
 * 没有 token 就跳过请求。翻页与按标签筛选同属浏览，未登录也开放。
 */
export async function fetchCommunityVideos(query: CommunityQuery = {}): Promise<CommunityList> {
  const res = await http.get('/api/community/videos', {
    params: browseParams(query),
  })
  return res.data
}

/**
 * 社区里出现过的全部标签及条数。**任何人可读。**
 *
 * 标签选项必须来自这里，不能从当前页卡片汇总：后者随结果集变化——
 * 翻页或选中某个标签之后，其余标签会从筛选行里整片消失，用户没法
 * 再叠加第二个筛选。这份清单与服务端是同一份真值，取一次后常驻。
 */
export async function fetchCommunityTags(): Promise<CommunityTag[]> {
  const res = await http.get('/api/community/tags')
  return res.data.items || []
}

/**
 * `/api/community/videos/by-url` 的响应体。
 *
 * 它只回答两件事：社区视频表里有没有这一条、这一条是不是我解析的。
 * **不回内容**——内容统一由 /api/summarize 出（读的是同一张社区视频表）。
 */
export interface CommunityByUrl {
  exists: boolean
  can_regenerate: boolean
}

/**
 * 社区里有没有这一条，以及我能不能改写它。**需登录**。
 *
 * 判据必须是社区视频表（服务端事实），不能拿「我解析过这个视频」推断：
 * 陌生人打开一条别人解析的视频时，他自己的历史里当然没有这一条，
 * 那样判的结果是「社区里没有」——于是他看不到复用提示，
 * 「重新解析」按钮也永远不会出现。
 */
export async function fetchCommunityByUrl(url: string): Promise<CommunityByUrl | null> {
  const res = await http.get('/api/community/videos/by-url', { params: { url } })
  return res.data
}

/**
 * 社区搜索：视频名称关键词 / 视频链接精确定位 / 标签。**需登录**。
 *
 * 401 在这里被翻译成 needLogin 标记返回，而不是抛异常：
 * 「没登录」是这个页面的常态之一（列表照样能看），不该和「搜索失败」
 * 一起变成一个红色报错框。
 *
 * tag 接受**逗号分隔的多值**，后端按并集处理（命中任一标签即命中）。
 */
export async function searchCommunity({
  q = '',
  tag = '',
  page = 1,
  pageSize = 20,
}: CommunityQuery & { q?: string } = {}): Promise<CommunitySearchResult> {
  try {
    const res = await http.get('/api/community/search', {
      params: { q, ...browseParams({ page, pageSize, tag }) },
    })
    return { ...res.data, needLogin: false }
  } catch (err) {
    if (isUnauthorized(err)) {
      // 分页字段照请求回填，不留 undefined：消费方（社区页的分页条）
      // 直接读 res.page / res.page_size，缺字段会让「第 1 页」变成
      // NaN 页，症状是翻页按钮点不动。
      return {
        items: [],
        page,
        page_size: pageSize,
        total: 0,
        total_pages: 0,
        needLogin: true,
      }
    }
    throw err
  }
}

/** 回填社区卡片的标题与封面（解析成功后调用一次）。需登录。 */
export async function publishCommunityCard(payload: {
  url: string
  video_title?: string
  cover_url?: string
}): Promise<unknown> {
  const res = await http.post('/api/community/cards', payload)
  return res.data
}
