<template>
  <header
    ref="headerRef"
    :class="[
      'fixed top-0 left-0 right-0 z-50 transition-all duration-300',
      scrolled ? 'bg-ink/90 backdrop-blur-xl border-b border-line' : 'bg-transparent',
    ]"
  >
    <div class="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between">
      <!-- Logo：整块可点并带 title，否则用户不知道这里能回首页 -->
      <button
        type="button"
        @click="$emit('go-home')"
        title="回到首页"
        class="flex items-center gap-2.5 rounded-xl px-1.5 py-1 -ml-1.5 hover:bg-gray-100/70 transition-colors"
      >
        <PixelLogo :size="32" />
        <div class="flex items-baseline gap-1.5">
          <span class="text-lg font-bold text-gray-900">VidDigest</span>
        </div>
      </button>

      <!-- 中间导航（移动端仅图标，md 及以上显示图标+文字） -->
      <nav class="flex items-center gap-0.5 sm:gap-1">
        <button
          @click="$emit('go-home')"
          :title="'首页'"
          :class="[
            'p-2 sm:px-4 sm:py-1.5 rounded-lg transition-colors flex items-center gap-1.5',
            page === 'home'
              ? 'text-blue-400 bg-blue-50 font-medium'
              : 'text-gray-500 hover:text-gray-800 hover:bg-gray-100',
          ]"
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
            <polyline points="9 22 9 12 15 12 15 22" />
          </svg>
          <span class="hidden md:inline text-sm">首页</span>
        </button>
        <button
          @click="$emit('open-history')"
          :title="'解析历史'"
          :class="[
            'p-2 sm:px-4 sm:py-1.5 rounded-lg transition-colors flex items-center gap-1.5',
            page === 'history'
              ? 'text-blue-400 bg-blue-50 font-medium'
              : 'text-gray-500 hover:text-gray-800 hover:bg-gray-100',
          ]"
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          <span class="hidden md:inline text-sm">历史</span>
        </button>
        <!-- 社区：常驻第三项。原先只在首页营销区放一个「浏览社区 →」按钮，
             解析出视频之后那个按钮就没了，社区成了只在特定状态下可达的角落。 -->
        <button
          @click="$emit('open-community')"
          :title="'社区'"
          :class="[
            'p-2 sm:px-4 sm:py-1.5 rounded-lg transition-colors flex items-center gap-1.5',
            page === 'community'
              ? 'text-blue-400 bg-blue-50 font-medium'
              : 'text-gray-500 hover:text-gray-800 hover:bg-gray-100',
          ]"
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
            <circle cx="9" cy="7" r="4" />
            <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
            <path d="M16 3.13a4 4 0 0 1 0 7.75" />
          </svg>
          <span class="hidden md:inline text-sm">社区</span>
        </button>
        <!-- 管理后台：只对管理员显示（v-if 挂在 isAdmin 上）。
             刻意只在**没有**这个按钮时才谈得上「看不到」——前端隐藏只是
             体验，真正的边界是后端 require_admin 的 403（ADR 0010）。
             isAdmin 由 App.vue 给：先用 localStorage 里的登录用户渲染，
             挂载后再用 /api/auth/me 回查一次。 -->
        <button
          v-if="isAdmin"
          type="button"
          @click="$emit('open-admin')"
          :title="'管理后台'"
          aria-label="管理后台"
          :class="[
            'p-2 sm:px-4 sm:py-1.5 rounded-lg transition-colors flex items-center gap-1.5',
            page === 'admin' || page === 'admin-invites'
              ? 'text-blue-400 bg-blue-50 font-medium'
              : 'text-gray-500 hover:text-gray-800 hover:bg-gray-100',
          ]"
        >
          <svg
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <span class="hidden md:inline text-sm">管理</span>
        </button>
      </nav>

      <!-- Desktop Actions -->
      <div class="flex items-center gap-2">
        <!-- 主题切换：放在登录态判断之外，游客与登录用户都能切。
             图标按当前主题给出「切到哪个」，不是「现在是什么」——
             后者要求用户先在脑子里做一次取反。 -->
        <button
          type="button"
          @click="toggleTheme"
          :title="theme === 'dark' ? '切换到明亮主题' : '切换到暗色主题'"
          :aria-label="theme === 'dark' ? '切换到明亮主题' : '切换到暗色主题'"
          :aria-pressed="theme === 'light'"
          class="flex-shrink-0 w-9 h-9 rounded-lg grid place-items-center text-gray-500 hover:text-gray-900 hover:bg-gray-100 border border-line transition-colors"
        >
          <svg
            v-if="theme === 'dark'"
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="4" />
            <path
              d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
            />
          </svg>
          <svg
            v-else
            class="w-4 h-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
          </svg>
        </button>

        <template v-if="user">
          <div class="flex items-center gap-2 mr-1">
            <!-- 悬停/点击展开额度面板：额度拆成「解析 / 追问」两个计数器
                 （工单 #4）之后，藏在页面里的话用户根本不知道自己还剩几次。 -->
            <div class="relative" @mouseenter="onQuotaEnter" @mouseleave="quotaOpen = false">
              <button
                type="button"
                @click="quotaOpen = !quotaOpen"
                :title="'查看额度'"
                class="flex items-center gap-1.5 px-2 py-1 rounded-lg text-sm text-gray-500 hover:text-gray-800 hover:bg-gray-100 transition-colors"
                :aria-expanded="quotaOpen"
              >
                <span class="max-w-[120px] truncate">{{ user.email }}</span>
                <svg
                  class="w-3.5 h-3.5 flex-shrink-0"
                  :class="quotaOpen && 'rotate-180'"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2.5"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </button>

              <!-- 面板外层是**命中区**，内层才是视觉。
                   这里不能用 `mt-2` 拉开间距：绝对定位元素的 top margin
                   会在按钮与面板之间留下一条 8px 的**空带**，那条带既不属于
                   按钮也不属于面板——鼠标从按钮往下移到「使用平台 Key」的
                   途中必然穿过它，于是 mouseleave 先一步把面板收走了，
                   按钮永远点不到（用户报的就是这个）。

                   所以：外层用 `-mt-2` 顶回去、再用 `pt-2` 把同样的 8px
                   补成**实心命中区**。视觉上仍是原来的间距，命中区连续。 -->
              <div v-if="quotaOpen" class="absolute right-0 top-full -mt-2 pt-2 w-64 z-50">
                <div class="rounded-2xl bg-panel border border-line shadow-xl shadow-black/20 p-4">
                  <p class="text-xs font-medium text-gray-400 mb-3">今日额度</p>
                  <div class="space-y-2.5">
                    <div v-for="slot in quotaRows" :key="slot.key" class="flex items-center justify-between gap-3">
                      <span class="text-sm text-gray-600">{{ slot.label }}</span>
                      <span
                        class="text-sm font-medium font-pixel"
                        :class="slot.exhausted ? 'text-red-500' : 'text-gray-800'"
                        >{{ slot.text }}</span
                      >
                    </div>
                    <div v-if="quotaRows.length === 0" class="text-xs text-gray-400 py-1">额度读取中…</div>
                  </div>
                  <p class="text-[11px] text-gray-400 mt-3 pt-3 border-t border-line">
                    每天 0 点重置。带入自己的 API key 可不消耗额度。
                  </p>

                  <!-- API Key 入口：与额度放在一起，因为它们是同一个决定的两面
                     ——「还剩几次」和「用谁的额度花」。分成两处的话，
                     用户填了 key 却不知道解析仍会扣额度。 -->
                  <button
                    type="button"
                    @click="$emit('open-byok')"
                    class="mt-3 w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-xl border border-line text-xs transition-colors hover:border-blue-200 hover:bg-blue-50/60"
                  >
                    <span class="flex items-center gap-1.5 min-w-0">
                      <svg
                        class="w-3.5 h-3.5 flex-shrink-0"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      >
                        <path
                          d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3"
                        />
                      </svg>
                      <span class="truncate">{{ byokLabel }}</span>
                    </span>
                    <span
                      v-if="byok?.mode === 'byok'"
                      class="flex-shrink-0 px-1.5 py-0.5 rounded-full text-[10px]"
                      :class="byok.hasKey ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600'"
                    >
                      {{ byok.hasKey ? '不扣额度' : '未填 Key' }}
                    </span>
                    <span v-else class="flex-shrink-0 text-[10px] text-gray-400">管理</span>
                  </button>
                </div>
              </div>
            </div>
            <span
              v-if="showVipEntry && user.is_vip"
              class="inline-flex items-center gap-1 text-xs bg-yellow-50 text-yellow-700 px-2.5 py-0.5 rounded-full font-medium border border-yellow-200/60"
            >
              <svg class="w-3 h-3" viewBox="0 0 24 24" fill="currentColor">
                <path
                  d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"
                />
              </svg>
              VIP
            </span>
          </div>
          <button
            @click="$emit('logout')"
            class="text-sm text-gray-500 hover:text-gray-700 transition-colors px-3 py-1.5 rounded-lg hover:bg-gray-100"
          >
            退出
          </button>
        </template>
        <template v-else>
          <button
            @click="$emit('login')"
            class="text-sm text-gray-600 hover:text-gray-900 transition-colors px-4 py-1.5 rounded-lg hover:bg-gray-100"
          >
            登录
          </button>
          <button
            @click="$emit('register')"
            class="text-sm bg-blue text-on-primary px-5 py-1.5 rounded-full hover:bg-blue-600 transition-all duration-200 active:scale-95 flex items-center gap-1.5"
          >
            <span class="w-1.5 h-1.5 bg-on-primary rounded-[1px]"></span>
            注册
          </button>
        </template>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import type { PropType } from 'vue'
import type { SessionUser } from '../api/auth.ts'
import type { ByokPublicState } from '../lib/byok.ts'
import { quotaRows as buildQuotaRows } from '../lib/quota.ts'
import type { QuotaPayload } from '../lib/quota.ts'
import PixelLogo from './PixelLogo.vue'
import { useTheme } from '../composables/useTheme.ts'

const { theme, toggleTheme } = useTheme()

const props = defineProps({
  user: { type: Object as PropType<SessionUser | null>, default: null },
  page: { type: String, default: 'home' },
  showVipEntry: { type: Boolean, default: false },
  /**
   * 是不是管理员。**只用来决定「管理」入口显不显示**。
   * 它来自 localStorage + /api/auth/me，两者都不是安全边界：
   * localStorage 里的值任何人都能改，服务端也随时可能撤权。
   * 真正的判断在服务端 require_admin（ADR 0010）。
   */
  isAdmin: { type: Boolean, default: false },
  quota: { type: Object as PropType<QuotaPayload | null>, default: null },
  quotaLoading: { type: Boolean, default: false },
  /**
   * 自带凭据的**公开**状态（lib/byok.getPublicState）。
   * 结构上不含 key —— 顶栏只用它决定显示「用平台」还是「不扣额度」，
   * 以及那个入口按钮的文案。
   */
  byok: { type: Object as PropType<ByokPublicState | null>, default: null },
})
const emit = defineEmits([
  'login',
  'register',
  'logout',
  'open-vip',
  'go-home',
  'open-history',
  'open-community',
  'open-admin',
  'request-quota',
  'open-byok',
])

const scrolled = ref(false)
const headerRef = ref<HTMLElement | null>(null)
const quotaOpen = ref(false)

/**
 * 额度面板的两行（或将来更多行）。
 *
 * 行构造在 `lib/quota.ts` 的 `quotaRows`：同一份额度如果页面上有两套解释
 * （「用完」到底看哪一类、中文名叫什么），用户会看到自相矛盾的提示。
 * 种类与文案由后端随 payload 下发（`labels`，ADR 0033），这里不认种类名。
 */
const quotaRows = computed(() => buildQuotaRows(props.quota))

function onQuotaEnter() {
  quotaOpen.value = true
  emit('request-quota')
}

/**
 * 入口按钮上那行字。厂商名取自预设表，厂商被删 / 改名时退回「使用自己的 Key」
 * 而不是显示一个空白按钮。三态分开写：没配、选了没填、已就绪，
 * 它们对用户是完全不同的三件事。
 */
const byokLabel = computed(() => {
  const b = props.byok
  if (!b || b.mode !== 'byok') return '使用平台 Key'
  return `使用自己的 Key · ${b.model || '默认模型'}`
})

// 点击页面别处关闭面板
function onDocClick(e: MouseEvent) {
  if (headerRef.value && !headerRef.value.contains(e.target as Node)) quotaOpen.value = false
}

function onScroll() {
  scrolled.value = window.scrollY > 20
}

onMounted(() => {
  window.addEventListener('scroll', onScroll, { passive: true })
  document.addEventListener('click', onDocClick)
})
onUnmounted(() => {
  window.removeEventListener('scroll', onScroll)
  document.removeEventListener('click', onDocClick)
})
</script>
