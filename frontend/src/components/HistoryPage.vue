<template>
  <div class="min-h-[calc(100vh-4rem)]">
    <div class="max-w-5xl mx-auto px-4 sm:px-6 py-8">
      <!-- 页头 -->
      <div class="flex items-center justify-between mb-5">
        <div class="flex items-center gap-3">
          <button
            @click="$emit('back')"
            class="w-9 h-9 rounded-xl bg-panel border border-line flex items-center justify-center text-gray-500 hover:text-gray-800 hover:border-gray-300 transition-colors"
          >
            <svg
              class="w-4.5 h-4.5"
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M19 12H5" />
              <path d="M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h1 class="text-xl font-bold text-gray-900">解析历史</h1>
            <p class="text-xs text-gray-400 mt-0.5">
              最多保留 {{ cap }} 条记录
              <span v-if="total">· 共 {{ total }} 条</span>
            </p>
          </div>
        </div>
        <button
          v-if="items.length"
          @click="onClearAll"
          :disabled="clearing"
          class="text-xs text-gray-400 hover:text-red-500 transition-colors px-3 py-1.5 rounded-lg hover:bg-red-50 disabled:opacity-50"
        >
          {{ clearing ? '清空中...' : '清空全部' }}
        </button>
      </div>

      <!-- 搜索：与社区页同一套形态（关键词框 + 搜索 + 清除） -->
      <form @submit.prevent="doSearch" class="flex flex-wrap items-center gap-2 mb-3">
        <div class="relative flex-1 min-w-[220px]">
          <svg
            class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
          <input
            v-model="keyword"
            type="search"
            placeholder="搜视频名称关键词，或粘贴完整视频链接"
            class="w-full pl-9 pr-3 py-2 rounded-xl bg-panel border border-line text-sm text-gray-800 placeholder-gray-400 focus:border-blue-200 focus:outline-none"
          />
        </div>
        <button
          type="submit"
          :disabled="searching"
          class="px-4 py-2 rounded-xl bg-blue text-on-primary text-sm font-medium hover:bg-blue-600 transition-colors disabled:opacity-50"
        >
          {{ searching ? '搜索中...' : '搜索' }}
        </button>
        <button
          v-if="hasFilters"
          type="button"
          @click="clearFilters"
          class="px-3 py-2 rounded-xl text-xs text-gray-400 hover:text-gray-700 hover:bg-panel transition-colors"
        >
          清除
        </button>
      </form>

      <!-- 三个独立筛选维度，任意组合 -->
      <div class="flex flex-wrap items-center gap-2 mb-3">
        <!-- AI 状态三档。三个档必须互斥且穷尽，否则「共 N 条」加不起来 -->
        <div
          class="flex items-center gap-1 rounded-xl border border-line bg-panel p-0.5"
          role="group"
          aria-label="解析状态"
        >
          <button
            v-for="opt in AI_OPTIONS"
            :key="opt.value"
            type="button"
            @click="setAi(opt.value)"
            :aria-pressed="ai === opt.value"
            :class="[
              'px-2.5 py-1 rounded-lg text-xs font-pixel transition-colors',
              ai === opt.value ? 'bg-blue text-on-primary' : 'text-gray-500 hover:text-gray-700',
            ]"
          >
            {{ opt.label }}
          </button>
        </div>

        <!-- 仅收藏 -->
        <button
          type="button"
          @click="favoriteOnly = !favoriteOnly"
          :aria-pressed="favoriteOnly"
          :class="[
            'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-pixel transition-colors border',
            favoriteOnly
              ? 'bg-blue text-on-primary border-blue'
              : 'bg-panel text-gray-500 border-line hover:border-gray-300',
          ]"
        >
          <svg
            class="w-3.5 h-3.5"
            :fill="favoriteOnly ? 'currentColor' : 'none'"
            viewBox="0 0 24 24"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <polygon
              points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"
            />
          </svg>
          仅收藏
        </button>

        <!-- 标签多选取并集：不选就是不按标签筛 -->
        <span class="text-[11px] text-gray-400">多选取并集</span>
      </div>

      <!-- 标签行：选项来自服务端 facets，**不随筛选变化**，
           所以选了标签之后其余标签还都在（可以继续叠加）。 -->
      <div
        v-if="facetError"
        class="mb-3 rounded-xl px-4 py-2.5 bg-amber-50 border border-amber-100 text-xs text-amber-700"
      >
        标签筛选没加载出来，其余功能不受影响：{{ facetError }}
      </div>
      <div v-else-if="facets.length" class="mb-4">
        <TagFilterRow
          :selected="activeTags"
          :tags="facets"
          multiple
          @update:selected="onTagsChange"
          all-label="全部"
          :show-count="true"
          label="历史标签筛选"
        />
      </div>

      <!-- 加载骨架 -->
      <div v-if="loading" class="space-y-3">
        <div v-for="n in 5" :key="n" class="skeleton h-20 rounded-2xl"></div>
      </div>

      <!-- 没登录：给入口，不给错误码。错误码是写给开发者看的，
           用户看到「需要登录」才知道下一步该干什么。 -->
      <div v-else-if="needLogin" class="flex flex-col items-center justify-center py-24">
        <p class="text-sm mb-1 text-gray-600">历史记录需要登录后查看</p>
        <p class="text-xs text-gray-400">解析记录只属于你自己，登录后就能看到</p>
        <button
          @click="$emit('need-login')"
          class="mt-4 px-4 py-2 rounded-xl bg-blue text-on-primary text-sm font-medium hover:bg-blue-600 transition-colors"
        >
          去登录
        </button>
      </div>

      <!-- 真故障与「没有内容」必须分开：一次 500 渲染成「暂无解析历史」，
           用户会以为是自己解析错了，于是反复重试。 -->
      <div v-else-if="loadError" class="flex flex-col items-center justify-center py-24">
        <p class="text-sm mb-1 text-red-500">历史记录加载失败</p>
        <p class="text-xs text-gray-400">{{ loadError }}</p>
        <button
          @click="load"
          class="mt-4 px-3 py-1.5 rounded-lg bg-panel border border-line text-xs text-gray-500 hover:border-gray-300 transition-colors"
        >
          重试
        </button>
      </div>

      <!-- 空状态：有没有筛选条件，说的是两件不同的事 -->
      <div v-else-if="!items.length" class="flex flex-col items-center justify-center py-24 text-gray-400">
        <p class="text-sm mb-1">{{ hasFilters ? '没有匹配的记录' : '暂无解析历史' }}</p>
        <p class="text-xs text-gray-400">
          {{ hasFilters ? '换个关键词，或清掉筛选条件' : '完成一次视频解析后，记录会出现在这里' }}
        </p>
      </div>

      <!-- 记录列表 -->
      <ul v-else class="space-y-3">
        <li v-for="item in items" :key="item.id">
          <div
            class="group bg-panel rounded-2xl border border-line hover:border-blue-200 transition-all duration-200 cursor-pointer overflow-hidden"
            @click="openRecord(item)"
          >
            <div class="flex items-start gap-4 p-4">
              <img
                v-if="item.cover_url"
                :src="proxyThumbnail(item.cover_url)"
                :alt="item.video_title || '视频封面'"
                loading="lazy"
                class="w-24 h-16 rounded-xl object-cover bg-panel border border-line flex-shrink-0"
              />
              <div
                v-else
                class="w-24 h-16 rounded-xl bg-blue-50 border border-line flex-shrink-0 flex items-center justify-center"
              >
                <svg
                  class="w-6 h-6 text-blue-300"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.8"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <polygon points="23 7 16 12 23 17 23 7" />
                  <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
                </svg>
              </div>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 flex-wrap">
                  <h3 class="text-sm font-semibold text-gray-800 truncate">{{ item.video_title || '未命名视频' }}</h3>
                  <span
                    v-if="item.has_ai_result"
                    class="flex-shrink-0 text-[10px] font-pixel bg-blue-50 text-blue-400 border border-blue-100 px-1.5 py-0.5 rounded font-medium"
                    >AI</span
                  >
                  <span
                    v-if="item.is_favorite"
                    class="flex-shrink-0 text-[10px] font-pixel bg-amber-50 text-amber-600 border border-amber-200 px-1.5 py-0.5 rounded font-medium"
                    >收藏</span
                  >
                </div>
                <p class="text-xs font-pixel text-gray-400 truncate mt-1">{{ item.video_url }}</p>
                <p v-if="item.summary_preview" class="text-xs text-gray-500 mt-1.5 line-clamp-2 leading-relaxed">
                  {{ item.summary_preview }}
                </p>
                <div v-if="item.tags && item.tags.length" class="flex flex-wrap gap-1.5 mt-2">
                  <span
                    v-for="tag in item.tags"
                    :key="tag"
                    class="text-[10px] font-pixel bg-blue-50 text-blue-400 border border-blue-100 px-1.5 py-0.5 rounded font-medium"
                    >{{ tag }}</span
                  >
                </div>
                <div class="flex items-center gap-3 mt-2 text-[11px] text-gray-400">
                  <span>{{ formatTime(item.updated_at) }}</span>
                  <span v-if="item.has_chat" class="text-gray-500">含问答记录</span>
                </div>

                <!-- 收藏项的第二道确认。
                     服务端不返回 force 时是 409，这里把那一行显示出来；
                     写成一整块「删除 + 二次确认」按钮也行，但那样第一下
                     点击和点错卡片打开详情长得一模一样。 -->
                <div
                  v-if="pendingDeleteId === item.id"
                  class="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 flex items-center gap-2 flex-wrap"
                >
                  <span class="text-[11px] text-amber-700">这是你收藏的记录，删掉就找不回来了。</span>
                  <button
                    type="button"
                    @click.stop="confirmDelete(item)"
                    class="px-2.5 py-1 rounded-lg bg-amber-600 text-white text-[11px] hover:bg-amber-700 transition-colors"
                  >
                    确认删除
                  </button>
                  <button
                    type="button"
                    @click.stop="pendingDeleteId = null"
                    class="px-2.5 py-1 rounded-lg bg-panel border border-amber-200 text-[11px] text-amber-700 hover:bg-amber-100 transition-colors"
                  >
                    取消
                  </button>
                </div>
              </div>

              <div class="flex flex-col items-center gap-1 flex-shrink-0">
                <!-- 收藏星标：幂等设置，不是翻转 -->
                <button
                  @click.stop="toggleFavorite(item)"
                  :disabled="starringId === item.id"
                  :aria-pressed="item.is_favorite"
                  :aria-label="item.is_favorite ? '取消收藏' : '收藏这条解析'"
                  class="w-8 h-8 rounded-lg flex items-center justify-center transition-colors disabled:opacity-50"
                  :class="
                    item.is_favorite
                      ? 'text-amber-600 hover:bg-amber-50'
                      : 'text-gray-300 hover:text-amber-600 hover:bg-gray-100'
                  "
                  title="收藏：收藏过的记录不会被滚动删除，也不怕误删"
                >
                  <svg
                    class="w-4 h-4"
                    :fill="item.is_favorite ? 'currentColor' : 'none'"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  >
                    <polygon
                      points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"
                    />
                  </svg>
                </button>
                <button
                  @click.stop="removeItem(item)"
                  :disabled="deletingId === item.id"
                  class="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors disabled:opacity-50"
                  title="删除记录"
                >
                  <svg
                    v-if="deletingId !== item.id"
                    class="w-4 h-4"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                  >
                    <polyline points="3 6 5 6 21 6" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                  <svg v-else class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </li>
      </ul>

      <!-- 翻页 -->
      <div v-if="totalPages > 1" class="flex items-center justify-center gap-3 mt-6">
        <button
          @click="goPage(page - 1)"
          :disabled="page <= 1 || loading"
          class="px-3 py-1.5 rounded-lg bg-panel border border-line text-xs text-gray-500 hover:border-gray-300 transition-colors disabled:opacity-40"
        >
          上一页
        </button>
        <span class="text-xs text-gray-400 font-pixel">{{ page }} / {{ totalPages }}</span>
        <button
          @click="goPage(page + 1)"
          :disabled="page >= totalPages || loading"
          class="px-3 py-1.5 rounded-lg bg-panel border border-line text-xs text-gray-500 hover:border-gray-300 transition-colors disabled:opacity-40"
        >
          下一页
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import TagFilterRow from './TagFilterRow.vue'
import { tagsToQuery } from '../lib/tag-filter.ts'
import { isLoggedIn, isUnauthorized } from '../lib/http.ts'
import { useSessionStore } from '../stores/session.ts'
import {
  clearHistories,
  deleteHistory,
  fetchHistories,
  fetchHistoryDetail,
  fetchHistoryFacets,
  setHistoryFavorite,
} from '../api/history.ts'
import type { HistoryFacet, HistoryItem } from '../api/history.ts'

const emit = defineEmits(['back', 'open-record', 'error', 'need-login'])

/** 与后端 HISTORY_PAGE_SIZE_DEFAULT 同值：两个列表的翻页手感不该不一样。 */
const PAGE_SIZE = 20
/** 后端 MAX_PARSE_HISTORY_PER_USER。这里写死是为了页头那句话，
 *  真值仍在后端；两处各有一份是因为「上限」本来就要在界面上说出来，
 *  而这不是可以被单测盯住的那种重复。 */
const cap = 1000

const AI_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'parse', label: '仅解析' },
  { value: 'ai', label: 'AI解析' },
]

const items = ref<HistoryItem[]>([])
const total = ref(0)
const totalPages = ref(0)
const page = ref(1)
const facets = ref<HistoryFacet[]>([])

const keyword = ref('')
const activeQuery = ref('')
const activeTags = ref<string[]>([])
const ai = ref('')
const favoriteOnly = ref(false)

const loading = ref(true)
const searching = ref(false)
const clearing = ref(false)
const deletingId = ref<number | null>(null)
const starringId = ref<number | null>(null)
/** 哪条正在等第二道确认。null = 没有。 */
const pendingDeleteId = ref<number | null>(null)

/**
 * 故障与「没有内容」必须分开。
 * 把 500 渲染成「暂无解析历史」，用户会以为是自己解析错了，于是反复重试。
 */
const loadError = ref('')
/** 标签加载失败单独说：它是次要功能，坏了不该让整个页面变成一句「加载失败」。 */
const facetError = ref('')

/**
 * 「没登录」不是故障，是这一页的常态之一。
 *
 * 以前这里把服务端的 401 原样塞进 loadError，于是用户看到的是
 * 「历史记录加载失败 / Request failed with status code 401」——
 * 错误码是写给开发者看的，普通用户看不懂，也不知道下一步该干什么。
 * 现在：没登录就**不发这个请求**（判登录放在本地，不去撞 401），
 * 服务端真回了 401 也翻译成同一件事：给一个「去登录」的入口。
 */
const needLogin = ref(false)

const hasFilters = computed(() => !!(activeQuery.value || activeTags.value.length || ai.value || favoriteOnly.value))

function params() {
  return {
    q: activeQuery.value,
    tag: tagsToQuery(activeTags.value),
    ai: ai.value,
    favorite: favoriteOnly.value ? true : undefined,
    page: page.value,
    page_size: PAGE_SIZE,
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  if (!isLoggedIn()) {
    // 没登录就不去撞 401：这一页的每一条记录都是私有的，
    // 未登录时它注定是空的，撞一次 401 只会把错误码甩到用户脸上。
    needLogin.value = true
    items.value = []
    total.value = 0
    totalPages.value = 0
    loading.value = false
    return
  }
  try {
    const body = await fetchHistories(params())
    needLogin.value = false
    items.value = body.items || []
    total.value = body.total || 0
    totalPages.value = body.total_pages || 0
    page.value = body.page || 1
  } catch (e) {
    if (isUnauthorized(e)) {
      // token 在本地还在、但服务端不认（过期 / 被踢下线）——同一件事，同样处理。
      needLogin.value = true
    } else {
      loadError.value = (e as { message?: string }).message || '加载失败'
    }
    items.value = []
    total.value = 0
    totalPages.value = 0
  } finally {
    loading.value = false
  }
}

async function loadFacets() {
  facetError.value = ''
  if (!isLoggedIn()) {
    // 标签同样是私有数据：没登录时它是空的，不是「加载失败」。
    facets.value = []
    return
  }
  try {
    facets.value = await fetchHistoryFacets()
  } catch (e) {
    facets.value = []
    if (isUnauthorized(e)) {
      // 401：标签也是私有数据，没登录时它就是空的，不该变成一条橙色报错条。
      //
      // ⚠️ 这里**不**把整页切成「需要登录」——列表可能已经加载出来了。
      // 因为标签取不到就把用户已经拿到的列表藏起来，是比报错更糟的错。
      // 整页的登录态由列表那条请求说了算（见 load()）。
      return
    }
    facetError.value = (e as { message?: string }).message || '标签加载失败'
  }
}

function doSearch() {
  searching.value = true
  activeQuery.value = keyword.value.trim()
  page.value = 1
  return load().finally(() => {
    searching.value = false
  })
}

function setAi(value: string) {
  ai.value = value
  page.value = 1
  load()
}

function onTagsChange(next: string[]) {
  activeTags.value = next
  page.value = 1
  load()
}

function clearFilters() {
  keyword.value = ''
  activeQuery.value = ''
  activeTags.value = []
  ai.value = ''
  favoriteOnly.value = false
  page.value = 1
  load()
}

function goPage(n: number) {
  if (n < 1 || (totalPages.value && n > totalPages.value)) return
  page.value = n
  load()
}

/**
 * 缩略图走本站代理：视频站普遍有防盗链，直连会返回一片灰。
 * 与 CommunityPage 同一套口径——两个列表都显示封面，不该一个能看一个不能。
 */
function proxyThumbnail(url: string) {
  if (!url) return ''
  if (url.startsWith('/')) return url
  return `/api/proxy/thumbnail?url=${encodeURIComponent(url)}`
}

async function openRecord(item: HistoryItem) {
  try {
    const detail = await fetchHistoryDetail(item.id)
    emit('open-record', detail)
  } catch {
    emit('error', {
      title: '打不开这条记录',
      message: '加载记录详情失败，请重试。',
      hint: '可能是网络抖动。点「重试」或直接刷新页面再试一次。',
    })
  }
}

/** 收藏 / 取消收藏。幂等设置，所以重试不会把结果反过来。 */
async function toggleFavorite(item: HistoryItem) {
  starringId.value = item.id
  const next = !item.is_favorite
  try {
    await setHistoryFavorite(item.id, next)
    item.is_favorite = next
    // 开着「仅收藏」时取消收藏，这条会立刻从当前列表里消失——
    // 不重载的话它会留在这儿，筛选看着没生效。
    if (favoriteOnly.value && !next) load()
  } catch {
    emit('error', {
      title: '操作失败',
      message: next ? '没能收藏这条记录。' : '没能取消收藏。',
      hint: '通常是网络问题。刷新页面后重试一次。',
    })
  } finally {
    starringId.value = null
  }
}

async function removeItem(item: HistoryItem) {
  pendingDeleteId.value = null
  if (!item.is_favorite && !confirm('确定删除这条解析记录吗？')) return
  deletingId.value = item.id
  try {
    const res = await deleteHistory(item.id)
    if (res.refused) {
      // 服务端第二道保险。收藏项要显式 force 才删得掉。
      pendingDeleteId.value = item.id
      return
    }
    items.value = items.value.filter((i) => i.id !== item.id)
    total.value = Math.max(0, total.value - 1)
  } catch {
    emit('error', {
      title: '删除失败',
      message: '这条记录没能删掉，请重试。',
      hint: '通常是网络问题。刷新页面确认记录是否还在。',
    })
  } finally {
    deletingId.value = null
  }
}

/** 第二道确认：显式带上 force，服务端才放行。 */
async function confirmDelete(item: HistoryItem) {
  pendingDeleteId.value = null
  deletingId.value = item.id
  try {
    await deleteHistory(item.id, { force: true })
    items.value = items.value.filter((i) => i.id !== item.id)
    total.value = Math.max(0, total.value - 1)
  } catch {
    emit('error', {
      title: '删除失败',
      message: '这条记录没能删掉，请重试。',
      hint: '通常是网络问题。刷新页面确认记录是否还在。',
    })
  } finally {
    deletingId.value = null
  }
}

async function onClearAll() {
  const msg = `确定清空 ${total.value} 条解析记录吗？\n\n` + '收藏过的记录会被保住。'
  if (!confirm(msg)) return
  clearing.value = true
  try {
    await clearHistories()
    await load()
    await loadFacets()
  } catch {
    emit('error', {
      title: '清空失败',
      message: '没能清空全部记录，部分可能已经删掉了。',
      hint: '列表已重新加载，可以看看还剩哪些。',
    })
  } finally {
    clearing.value = false
  }
}

function formatTime(ts: string | null) {
  if (!ts) return ''
  const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z')
  if (Number.isNaN(d.getTime())) return ts
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`
  return d.toLocaleDateString('zh-CN')
}

/**
 * 登录状态一变，这一页就得跟着变：
 * 刚登录 → 把列表取回来（不然用户登录完还杵在「需要登录」上）；
 * 刚登出 → 把列表收走（别把上一个用户的记录继续留在屏幕上）。
 *
 * 判据读 store 里的 isAuthenticated，不直接读 localStorage：
 * 登录成功后 token 是 App.vue 那边 applyUser 写进 store 的，
 * localStorage 只是它的落盘副本，读 store 才是同一份状态。
 */
const session = useSessionStore()
watch(
  () => session.isAuthenticated,
  (now) => {
    if (now) {
      needLogin.value = false
      void load()
      void loadFacets()
    } else {
      needLogin.value = true
      items.value = []
      total.value = 0
      totalPages.value = 0
      facets.value = []
    }
  },
)

// 「仅收藏」是纯开关，切过去就重新取一次。刻意用 watch 而不是模板上
// @click="load()"：两种写法都能跑，但后者会在有人重排模板按钮时被漏掉，
// 症状是「点了没反应」；watch 在任何程序化路径上也照样生效。
watch(favoriteOnly, () => {
  page.value = 1
  load()
})

onMounted(() => {
  load()
  loadFacets()
})
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
