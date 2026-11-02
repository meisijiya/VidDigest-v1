<template>
  <div class="min-h-screen flex flex-col">
    <AppHeader
      :user="currentUser"
      :page="currentPage"
      :quota="quotaInfo"
      :quota-loading="quotaLoading"
      :byok="byok"
      @request-quota="refreshQuota"
      @open-byok="byokDialogOpen = true"
      @login="showAuthModal('login')"
      @register="showAuthModal('register')"
      @logout="handleLogout"
      :show-vip-entry="membershipEnabled"
      :is-admin="isAdmin"
      @go-home="goHome"
      @open-history="openHistory"
      @open-community="openCommunity"
      @open-admin="openAdmin"
    />
    <ByokDialog :visible="byokDialogOpen" :loggedIn="!!currentUser" @close="byokDialogOpen = false" />
    <main class="flex-1 pt-16">
      <!--
        页面由路由决定（ADR 0018）。<router-view> 按当前路由渲染对应组件，
        App.vue 不再自己挑「显示哪一页」——以前那份页键和 URL 各存一份，
        靠 pushState 手工同步，漏一处就是「地址栏变了、页面没变」。
        首页要喂的状态多，用 v-bind 单独喂；其余三页自带状态，事件统一转交。
      -->
      <router-view v-slot="{ Component }">
        <component :is="Component" v-bind="route.meta.page === 'home' ? homeViewProps : {}" v-on="viewListeners" />
      </router-view>
    </main>

    <AppFooter @go-home="goHome" />
    <AuthModal
      :visible="authModalVisible"
      :initialMode="authModalMode"
      @close="authModalVisible = false"
      @success="handleAuthSuccess"
    />
    <ErrorModal
      :visible="errorModal.visible"
      :title="errorModal.title"
      :message="errorModal.message"
      :hint="errorModal.hint"
      @close="errorModal.visible = false"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { parseVideo, downloadViaServer, type ParsedVideo } from './api/video.ts'
import type { SessionUser } from './api/auth.ts'
import { isLoggedIn } from './lib/http.ts'
import { fetchQuota } from './api/summarize.ts'
import { createCheckoutSession } from './api/payment.ts'
import { saveHistory, type HistoryDetail } from './api/history.ts'
import { publishCommunityCard, fetchCommunityByUrl, type CommunityCard } from './api/community.ts'
import { getPublicState, subscribe as subscribeByok, type ByokPublicState } from './lib/byok.ts'
import type { QuotaPayload } from './lib/quota.ts'
import { useSessionStore } from './stores/session.ts'
import type { PageKey } from './router/index.ts'

import AppHeader from './components/AppHeader.vue'
import AuthModal from './components/AuthModal.vue'
import AppFooter from './components/AppFooter.vue'
import ByokDialog from './components/ByokDialog.vue'
import ErrorModal from './components/ErrorModal.vue'
import { MEMBERSHIP_ENABLED as membershipEnabled } from './config/features.ts'
import { classifyError } from './lib/errors.ts'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const authModalVisible = ref(false)
const authModalMode = ref<'login' | 'register'>('login')

/**
 * 登录态与管理员判据都在 session store 里（ADR 0010）。
 *
 * store 分两级：先用 localStorage 里的种子渲染那一帧（不闪一下），
 * 挂载后回查 /api/auth/me，以服务端为准（撤权即时生效）。
 * 前端这一层只做体验，真正的边界是 AdminPage 里那次请求的 403。
 */
const currentUser = computed(() => session.user)
const isAdmin = computed(() => session.isAdmin)

/**
 * 当前页由路由决定（ADR 0018）。
 *
 * URL 是唯一真值：以前自己维护一份 currentPage，再靠 pushState 与
 * popstate 手动同步——后退时页码对不上，刷新时 /history 与 /community
 * 又掉回首页。现在交给 vue-router，页面从 route.meta.page 派生。
 */
const currentPage = computed<PageKey>(() => route.meta.page ?? 'home')

const loading = ref(false)
const downloading = ref(false)
const videoData = ref<ParsedVideo | null>(null)
const currentUrl = ref('')
const summaryKey = ref(0)
const demoMode = ref(true)
const historyDetail = ref<HistoryDetail | null>(null)
const fromCache = ref(false)
const reparseLoading = ref(false)
/**
 * 这份社区总结是不是当前用户自己解析出来的（ADR 0007）。
 * 三态而不是两态：null = 还没问过服务端。复用回放会先发 ownership 事件，
 * 在那之前把按钮直接判成「不可点」是安全的；判成 false 则会在事件到达前
 * 闪一句「这是别人的」，而它可能正是自己的。
 */
const canRegenerate = ref<boolean | null>(null)
/** 一次性信号：下一次 VideoSummary 挂载时按「重新解析」发起，而不是复用。 */
const regenerateRequested = ref(false)

/**
 * 自带凭据的**公开**状态。真实 key 始终关在 lib/byok 的闭包里，
 * 这里只存一个不含 key 的对象供渲染。
 *
 * 订阅而不是每次读一次：用户在顶栏弹窗里改完设置，正在打开的
 * 解析卡片上的状态提示要立刻跟着变，否则用户会以为保存没生效。
 *
 * 订阅在**挂载时**建立，卸载时退订。写成
 * `onUnmounted(() => subscribe(fn))` 会在页面卸载那一刻才订阅——
 * 顶栏于是永远停在「使用平台 Key」，而弹窗里明明已经选好了。
 */
const byok = ref<ByokPublicState>(getPublicState())
const stopByokSubscription = subscribeByok((next) => {
  byok.value = next
})
onUnmounted(stopByokSubscription)
const byokDialogOpen = ref(false)
const errorModal = ref<{ visible: boolean; title: string; message: string; hint: string }>({
  visible: false,
  title: '',
  message: '',
  hint: '',
})

/** 顶栏额度面板的数据源。悬停时才拉，避免每次渲染都打一次接口。 */
const quotaInfo = ref<QuotaPayload | null>(null)
const quotaLoading = ref(false)

async function refreshQuota() {
  if (quotaLoading.value) return
  quotaLoading.value = true
  try {
    quotaInfo.value = await fetchQuota()
  } catch {
    quotaInfo.value = null
  } finally {
    quotaLoading.value = false
  }
}

/** 统一错误出口：所有失败都走这里，不再散落 alert() */
function showError(err: unknown, action: 'parse' | 'download' | 'payment' = 'parse') {
  const { title, message, hint } = classifyError(err, action)
  errorModal.value = { visible: true, title, message, hint }
}

/** 子组件已经组织好文案的错误，直接进同一个弹窗 */
function showErrorFromPage(payload: { title?: string; message?: string; hint?: string } | null) {
  errorModal.value = {
    visible: true,
    title: payload?.title || '出错了',
    message: payload?.message || '',
    hint: payload?.hint || '',
  }
}

/**
 * 回到起始页。
 *
 * 之前只改 currentPage，而 videoData 仍然留着——于是点了 Logo 之后
 * 页面确实切回了 home，但解析结果区还挂在下面，看起来像「没反应」。
 * 「回起始页」的含义是回到输入框，所以这里把解析态一并清掉。
 */
function goHome() {
  router.push('/')
  videoData.value = null
  currentUrl.value = ''
  historyDetail.value = null
  fromCache.value = false
  summaryKey.value++
  window.scrollTo({ top: 0 })
}

/**
 * 社区页的「返回首页」：**不清**解析态。
 *
 * 社区和首页是并列入口，从社区点进某个视频详情时结果区还得在
 * （openCommunityVideo 自己会处理）。历史页与管理后台的「返回」不同，
 * 那两处走 goHome（回到输入框）。
 */
function goHomeKeepingResult() {
  router.push('/')
}

/** 去历史页：只切页，不动解析态（历史列表自己渲染，历史记录在下面） */
function openHistory() {
  router.push('/history')
}

/**
 * 去社区页。**不清** videoData：社区和首页的输入框是并列入口，
 * 从社区点进某个视频详情时结果区还得在（openCommunityVideo 自己会处理）。
 * 与 openHistory 的差别是有意的。
 */
function openCommunity() {
  router.push('/community')
}

/**
 * 去管理后台。URL 真的变了，于是刷新、手输地址、后退都落得回来。
 *
 * 这里**不**判管理员：判据已经在 isAdmin 里算过一次，而真正的判断在
 * 服务端（ADR 0010：require_admin 每次回查数据库）。前端再拦一道只会
 * 造出第二种口径——今天两边依据不同步时，用户看到的就是「导航里没有
 * 管理，但手输地址能进」，反而更难解释。
 */
function openAdmin() {
  router.push('/admin')
}

/**
 * 去邀请码管理页（工单 #104 / ADR 0025）。
 *
 * 它是**独立路径**而不是后台里的又一个页签，所以后台页自己发一个事件出来，
 * 由这里路由过去。页面组件里不直接用 router —— 社区页的 `need-login`
 * 也是这个口径：组件只声明「我要去某处」，去哪儿由 App.vue 决定。
 */
function openInviteCodes() {
  router.push('/admin/invite-codes')
}

/** URL 规范化：剥离跟踪参数，提取平台视频 ID 作为历史/缓存的 key，提升命中率 */
function canonicalUrl(raw: string): string {
  let url = (raw || '').trim()
  try {
    const u = new URL(url)
    // B 站：以 BV/av 号为 key
    const bv = url.match(/(BV[0-9A-Za-z]{10})/)
    if (bv && u.hostname.includes('bilibili')) return `https://www.bilibili.com/video/${bv[1]}`
    if (u.hostname.includes('bilibili')) {
      const av = url.match(/av(\d+)/)
      if (av) return `https://www.bilibili.com/video/av${av[1]}`
    }
    // YouTube：以 v 参数 / youtu.be 短链为 key
    if (u.hostname.includes('youtube.com') && u.searchParams.get('v')) {
      return `https://www.youtube.com/watch?v=${u.searchParams.get('v')}`
    }
    if (u.hostname === 'youtu.be') {
      return `https://www.youtube.com/watch?v=${u.pathname.slice(1)}`
    }
    // 其他平台：仅剥离跟踪查询参数
    const keep = new URLSearchParams()
    for (const [k, v] of u.searchParams) {
      if (!/^(utm_|track|trackid|spm|vd_source|from|request_id)/i.test(k)) keep.append(k, v)
    }
    const qs = keep.toString()
    return `${u.origin}${u.pathname}${qs ? '?' + qs : ''}`
  } catch {
    return url
  }
}

/** 解析完成（或缓存命中）后落库：无论是否 AI 解析都进入历史记录 */
function persistParseRecord(url: string, data: ParsedVideo | null) {
  if (!isLoggedIn()) return
  saveHistory({
    url,
    video_title: data?.title || '',
    video_data: data || null,
  })
}

/** 本地校验的两种结果：要么不合法（带文案），要么给出规范化的链接。 */
type UrlCheck = { ok: false; title: string; message: string; hint: string } | { ok: true; url: string }

/**
 * 请求发出之前的本地校验。
 *
 * 为什么要在前端拦一道：拼错链接、只贴了半句、粘了一整段分享文案
 * 这三种最常见，交给后端也要跑一趟网络才知道结果，用户白等几秒
 * 还只看到一句「解析失败」。本地判掉能立刻给出可执行的提示。
 *
 * 刻意**不**做过度校验（比如要求必须是已知平台）：本仓定位是
 * 支持 1800+ 平台的白名单判不准，在这里拦掉合法链接比不拦更糟。
 */
function validateUrlInput(raw: string): UrlCheck {
  const text = (raw || '').trim()
  if (!text) {
    return {
      ok: false,
      title: '还没填链接',
      message: '请先粘贴一个视频链接。',
      hint: 'B 站请用 bilibili.com/video/BV... 这样的完整地址。',
    }
  }
  // 从分享文本里抽第一个 http(s) 链接——和后端 clean_url 同一套思路。
  const m = text.match(/https?:\/\/[^\s）)"'＞，。、；：！？》>\]]+/)
  if (!m) {
    return {
      ok: false,
      title: '这不是一个链接',
      message: text.slice(0, 80),
      hint: '没有找到 http:// 或 https:// 开头的地址。' + '如果你是整段复制了分享文案，请把其中的链接复制出来再试。',
    }
  }
  return { ok: true, url: m[0] }
}

async function handleParse(url: string) {
  const check = validateUrlInput(url)
  if (!check.ok) {
    errorModal.value = { visible: true, title: check.title, message: check.message, hint: check.hint }
    return
  }
  loading.value = true
  videoData.value = null
  historyDetail.value = null
  fromCache.value = false
  // 换视频就把「重新解析」这个一次性信号收回。必须在这里收而不是在
  // reparse() 里就地置回：同一 tick 内改回去的话，组件重渲染时读到的
  // 已经是被收走的值，信号在它出生前就蒸发了。
  regenerateRequested.value = false
  canRegenerate.value = null
  summaryKey.value++
  const key = canonicalUrl(check.url)
  currentUrl.value = key
  try {
    // 问一句「社区里有没有这一份」，只用于提示，不用于取内容。
    //
    // 这里问的是**社区视频表**（服务端事实），不是「我解析过没有」。
    // 原来用的是个人解析历史，于是陌生人打开一条别人解析的视频时
    // 必然被判成「社区里没有」——他看不到复用提示，
    // 「重新解析」按钮也就永远不会出现。内容一律由 VideoSummary 请求
    // /api/summarize 拿，那条路读的就是社区视频表。
    if (isLoggedIn()) {
      try {
        const found = await fetchCommunityByUrl(key)
        fromCache.value = !!found?.exists
        canRegenerate.value = found?.exists ? !!found.can_regenerate : null
      } catch {
        fromCache.value = false
        canRegenerate.value = null
      }
    }
    // 视频源信息（标题/封面/时长/格式）仍走 /api/parse：它不消耗额度，
    // 而且下载与时长这些字段只有解析结果里有，社区卡片不存。
    const res = await parseVideo(check.url)
    if (res.success) {
      videoData.value = res.data
      demoMode.value = false
      persistParseRecord(key, res.data)
      // 回填社区卡片的标题与封面：社区列表要显示它们，而服务端解析时
      // 拿不到（字幕流里没有平台标题与缩略图地址）。失败不影响主流程。
      publishCard(key, res.data)
    } else {
      showError({ message: res.error || '未知错误' })
    }
  } catch (err) {
    showError(err)
  } finally {
    loading.value = false
  }
}

/**
 * 回填社区卡片的标题与封面。
 *
 * 工单 #17 第 2 项之后，这条路只剩一个用途：**给老行补标题**——
 * 修复前建出来的那些 `videos` 行，标题是空串。
 *
 * 首次解析时这里**必然**拿到 `updated: 0`：那一行还没建出来
 * （要等用户点「AI 总结」才由 reserve_video 创建），
 * 而标题与封面已经跟着那次 /api/summarize 请求写进去了。
 * 所以 0 是预期结果，不是失败——报成错误只会让每一次首解析都吵一次。
 *
 * 真正该留痕迹的是**请求本身失败**（网络 / 401 / 500）。
 * 原来 `.catch(() => {})` 把那也一起吞了，于是「服务端为什么不填」
 * 在控制台里一个字都没有。这条路失败不该打断主流程，但必须看得见。
 */
function publishCard(url: string, data: ParsedVideo | null) {
  if (!isLoggedIn()) return
  publishCommunityCard({
    url,
    video_title: data?.title || '',
    cover_url: data?.thumbnail || '',
  }).catch((err) => {
    console.warn('[community] 卡片标题回填失败：', err)
  })
}

/**
 * 从社区点开一条视频。
 *
 * 详情需登录——但**判登录放在这里**，而不是让请求去撞 401：
 * 详情内容统一由 VideoSummary 请求 /api/summarize 拿（读的是社区视频表），
 * 未登录时那条请求会返回 SSE 的 need_login 错误事件。这里先拦一道，
 * 访客点卡片立刻得到登录框，而不是先白跑一次请求再看到报错。
 */
function openCommunityVideo(item: CommunityCard) {
  if (!isLoggedIn()) {
    showAuthModal('login')
    return
  }
  // 必须切回首页，否则社区页仍然盖在上面：请求发出去了、解析也成功了，
  // 用户看到的却还是社区列表——症状就是「点了卡片没反应」。
  router.push('/')
  handleParse(item.video_url)
}

/**
 * 重新解析：让作者本人改写自己那一份（ADR 0007）。
 *
 * 这里原来调的是 /api/parse——而那条路**不调模型**，只取视频元信息。
 * 于是点完什么都没重跑，总结仍旧从社区视频表原样回放：
 * 一个转圈的图标加一次完全相同的结果，这就是「这个功能失效」的全部真相。
 *
 * 现在它只做一件事：把「按覆盖发起」这个信号交给 VideoSummary。
 * 真正的重跑、扣额度、落库都在 /api/summarize 的 overwrite 分支里。
 *
 * 不清空 videoData / fromCache：横幅和按钮要留在原位转圈，
 * 整个结果区塌下去再长出来只会让用户以为页面挂了。
 */
function reparse() {
  if (!currentUrl.value || reparseLoading.value) return
  if (canRegenerate.value !== true) return
  reparseLoading.value = true
  regenerateRequested.value = true
  summaryKey.value++
}

/** VideoSummary 报来的写权限。服务端说了算，不在前端猜。 */
function onOwnership(data: { can_regenerate?: boolean } | null) {
  canRegenerate.value = !!data?.can_regenerate
}

/**
 * 按钮上的字。三态对应三种事实，缺一不可：
 * null（还没问过服务端）说「查一下」而不是「不能点」——后者会把
 * 「尚未知道」渲染成「已被拒绝」，而这两件事的用户含义完全不同。
 */
const reparseButtonText = computed(() => {
  if (reparseLoading.value) return '重新解析中...'
  if (canRegenerate.value === true) return '重新解析'
  if (canRegenerate.value === false) return '不是你的总结'
  return '重新解析'
})

/** 从历史页点击记录：回填视频源，并带上个人问答历史 */
async function handleOpenRecord(detail: HistoryDetail) {
  router.push('/')
  currentUrl.value = detail.video_url || ''
  // 历史里的 video_data 就是当初 /api/parse 的响应体；表里存的是 JSON，
  // TS 只能按 Record<string, unknown> 描述它，这里按 ParsedVideo 用。
  videoData.value = (detail.video_data as ParsedVideo | null) || null
  // 只为了个人问答历史（VideoSummary 只读 chat_history 一个字段）。
  // 总结 / 思维导图 / 字幕一律由 /api/summarize 从社区视频表出，
  // 不再从个人记录回填——那正是让同一链接呈现两份总结的根因。
  historyDetail.value = detail
  summaryKey.value++
  if (!detail.video_data) {
    // 历史记录缺少视频源信息时，重新解析补全（不消耗 AI 次数）
    await handleParse(detail.video_url)
    historyDetail.value = detail
    window.scrollTo({ top: 0 })
    return
  }
  // 问一句社区里有没有这一份，决定要不要让 VideoSummary 自动展示。
  // 必须在 summaryKey++ **之后**查：组件是靠 key 重建的，先查再改 key
  // 才能保证它拿到的是本次的结果而不是上一个视频的。
  // 查失败一律当作「没有」，退回手动触发——宁可多点一次，
  // 也不能在结果其实不存在时自动发起并扣掉额度。
  fromCache.value = false
  canRegenerate.value = null
  regenerateRequested.value = false
  try {
    const found = await fetchCommunityByUrl(detail.video_url)
    fromCache.value = !!found?.exists
    canRegenerate.value = found?.exists ? !!found.can_regenerate : null
  } catch {
    fromCache.value = false
    canRegenerate.value = null
  }
  summaryKey.value++
  window.scrollTo({ top: 0 })
}

async function handleDownload(formatId: string) {
  downloading.value = true
  try {
    const response = await downloadViaServer(currentUrl.value, formatId)
    const contentDisposition = response.headers['content-disposition']
    let filename = 'video.mp4'
    if (contentDisposition) {
      const match = contentDisposition.match(/filename\*?=(?:UTF-8'')?([^;\n]+)/i)
      if (match) filename = decodeURIComponent(match[1].replace(/"/g, ''))
    }
    const blob = new Blob([response.data])
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    window.URL.revokeObjectURL(url)
  } catch (err) {
    showError(err, 'download')
  } finally {
    downloading.value = false
  }
}

function showAuthModal(mode: 'login' | 'register') {
  authModalMode.value = mode
  authModalVisible.value = true
}

/**
 * 登录 / 注册成功后统一走这里（AuthModal 的 @success）。
 *
 * 管理员入口的可见性必须在这里重新算一次：那一帧的 isAdmin 只按
 * localStorage 种子求值，而 SPA 内登录**不刷新页面**，那份 localStorage
 * 里原本是空的——于是登录成管理员之后入口一直不出现，要按 F5 才有（工单 #33）。
 *
 * 走 store.refresh(false) 回查 /api/auth/me，而不是直接读 `user.is_admin`：
 *   1. 登录与注册共用这一个 handler，而**注册响应里没有 is_admin**
 *      （backend/api_auth.py:77 手写字典），直接读会让注册路径拿不到管理员态；
 *   2. localStorage 与响应体都不是安全边界，服务端才是权威（ADR 0010），
 *      撤权要即时生效。
 * 它是 async，入口因此晚一拍出现——这是有意的取舍，见 store 里 refresh 的注释。
 */
function handleAuthSuccess(user: SessionUser) {
  session.applyUser(user)
  void session.refresh(false)
}

/**
 * 登出。isAdmin 必须**同步**清掉：AppHeader 的入口是裸的 v-if="isAdmin"
 *（AppHeader.vue:59），没有并联 user，所以只清 currentUser 的话登出后入口
 * 还亮着，要 F5 才消失（工单 #33）。store.signOut 同时推进代次，
 * 让**在途**的 /api/auth/me 回查失效。
 */
function handleLogout() {
  session.signOut()
}

async function handleOpenVip() {
  if (!isLoggedIn()) {
    showAuthModal('login')
    return
  }
  try {
    const { checkout_url } = await createCheckoutSession('monthly')
    window.location.href = checkout_url
  } catch (err) {
    showError(err, 'payment')
  }
}

function checkPaymentResult() {
  const params = new URLSearchParams(window.location.search)
  if (params.get('payment') === 'success') {
    window.history.replaceState({}, '', window.location.pathname)
    if (isLoggedIn()) {
      // 付款只改 VIP 状态、**不改**管理员身份，所以这条路径过去「更新了
      // currentUser 却不管 isAdmin」也没人看出来。但它是同一根因的第三处
      // 漏网点：将来谁在这里塞提权逻辑，就会再踩一次「写 currentUser 顺手
      // 忘了写 isAdmin」的坑。走同一条受代次守卫的路径收口（syncUser=true：
      // VIP 状态的更新途径就是这一发，不能丢）。
      setTimeout(() => {
        void session.refresh(true)
      }, 1000)
    }
  }
}

/**
 * 首页视图（HomeView）的入参。
 *
 * 路由负责「渲染哪个视图」，视图负责「把这一屏渲染成什么样」；
 * 解析态仍然留在 shell 里，通过 props 往下传——它是一次解析会话的
 * 中间态，不是全局状态，放进 store 反而更难追踪。
 */
const homeViewProps = computed(() => ({
  loading: loading.value,
  videoData: videoData.value,
  demoMode: demoMode.value,
  fromCache: fromCache.value,
  reparseLoading: reparseLoading.value,
  canRegenerate: canRegenerate.value,
  reparseButtonText: reparseButtonText.value,
  currentUrl: currentUrl.value,
  downloading: downloading.value,
  historyDetail: historyDetail.value,
  summaryKey: summaryKey.value,
  user: currentUser.value,
  regenerateRequested: regenerateRequested.value,
  byok: byok.value,
  membershipEnabled,
}))

/**
 * 各视图的事件接线。
 *
 * 按页给：每个视图只认自己声明过的事件。多挂一个既没有意义，
 * 也会让「这个事件到底归谁」在代码里说不清。
 */
function listenersFor(page: PageKey): Record<string, (...args: never[]) => void> {
  switch (page) {
    case 'history':
      return {
        back: goHome,
        'need-login': () => showAuthModal('login'),
        'open-record': handleOpenRecord,
        error: showErrorFromPage,
      }
    case 'community':
      return {
        back: goHomeKeepingResult,
        'need-login': () => showAuthModal('login'),
        'open-video': openCommunityVideo,
      }
    case 'admin':
      return { back: goHome, 'open-invite-codes': openInviteCodes }
    case 'admin-invites':
      // 邀请码管理页的「返回」回**管理后台**：它是从后台进去的独立路径，
      // 回首页会把管理员丢在最外面一层。
      return { back: openAdmin }
    default:
      return {
        parse: handleParse,
        download: handleDownload,
        reparse,
        ownership: onOwnership,
        regenerating: (value: boolean) => {
          reparseLoading.value = value
        },
        'open-byok': () => {
          byokDialogOpen.value = true
        },
        'open-vip': handleOpenVip,
        'need-login': () => showAuthModal('login'),
      }
  }
}

const viewListeners = computed(() => listenersFor(currentPage.value))

onMounted(() => {
  checkPaymentResult()
  // 挂载后回查一次 /api/auth/me：localStorage 只是种子，服务端才是权威
  // （ADR 0010：管理员角色不进 JWT，每次请求回查数据库，撤权即时生效）。
  void session.refresh(false)
})
</script>

<style>
/* 全局过渡动画 */
.slide-up-enter-active {
  transition: all 0.6s cubic-bezier(0.16, 1, 0.3, 1);
}
.slide-up-leave-active {
  transition: all 0.3s ease-in;
}
.slide-up-enter-from {
  opacity: 0;
  transform: translateY(30px);
}
.slide-up-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}
</style>
