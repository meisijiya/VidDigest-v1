<template>
  <div class="bg-panel rounded-2xl border border-line overflow-hidden card-hover">
    <!-- Tab 导航 -->
    <div class="flex border-b border-line bg-panel-2/50">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        :class="[
          'flex-1 py-3.5 text-sm font-medium transition-all duration-200 relative',
          activeTab === tab.key ? 'text-blue-400 bg-panel' : 'text-gray-500 hover:text-gray-700 hover:bg-panel-2/50',
        ]"
      >
        <span class="flex items-center justify-center gap-1.5">
          <component :is="tab.icon" v-if="tab.icon" class="w-4 h-4" />
          {{ tab.label }}
        </span>
        <div v-if="activeTab === tab.key" class="absolute bottom-0 left-3 right-3 h-0.5 bg-blue rounded-full"></div>
      </button>
    </div>

    <!-- 额度显式 + 停止入口：常驻可见，停止按钮在整个流式期间都可用 -->
    <div
      v-if="videoUrl"
      class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 px-5 py-2 border-b border-line bg-panel-2/30"
    >
      <span class="text-xs text-gray-500">每日免费额度</span>
      <div class="flex items-center gap-2">
        <span class="text-xs font-medium px-2 py-0.5 rounded-full" :class="quotaBadgeClass">{{ quotaLabel }}</span>
        <button
          v-if="loading || chatLoading"
          @click="loading ? stopSummarize() : stopChat()"
          class="px-2 py-0.5 rounded-full border border-line text-xs text-gray-500 hover:text-gray-900 hover:bg-gray-100 transition-all duration-200 active:scale-95"
        >
          停止
        </button>
      </div>
      <!-- 自带凭据：挂在**常驻**额度行而不是某个 Tab 里。
           它是账号级状态（解析与追问共用同一份设置），放进「AI 问答」Tab
           会让正在看总结的人完全看不到自己正在用谁的额度。 -->
      <p v-if="byokNotice" class="w-full text-[11px] text-emerald-600">
        本次{{ byokNoticeWhat }}用了你自己的 API Key 与端点，没有消耗平台额度。
      </p>
      <p v-else-if="props.byok?.mode === 'byok' && !props.byok?.hasKey" class="w-full text-[11px] text-amber-600">
        你选了「使用自己的 API Key」但还没填，解析与追问仍会走平台额度。
        <button type="button" @click="$emit('open-byok')" class="underline">去填写</button>
      </p>
      <p
        v-else-if="props.byok?.mode === 'byok'"
        class="w-full text-[11px] text-gray-400 truncate"
        :title="`${props.byok.baseUrl || '平台默认端点'} · ${props.byok.model || '默认模型'}`"
      >
        当前使用你自己的 API Key（{{ props.byok.model || '默认模型' }}），解析与追问都不消耗平台额度。
      </p>
    </div>

    <!-- 视频标签：属于视频的属性而不是总结的内容，所以挂在面板常驻行（切 Tab 都在）。
         数组为空时不渲染容器，只留 v-if 在容器上、不与 v-for 同元素。 -->
    <div
      v-if="videoTags.length"
      class="flex flex-wrap items-center gap-1.5 px-5 py-2 border-b border-line bg-panel-2/30"
    >
      <span class="text-[10px] font-pixel text-gray-500">#TAGS</span>
      <span
        v-for="tag in videoTags"
        :key="tag"
        class="text-[10px] font-pixel bg-blue-50 text-blue-400 border border-blue-100 px-1.5 py-0.5 rounded"
      >
        #{{ tag }}
      </span>
    </div>

    <!-- Tab 内容 -->
    <div class="p-5 min-h-[200px]">
      <!-- 空状态 -->
      <div v-if="!videoUrl" class="flex flex-col items-center justify-center py-12 text-gray-400">
        <svg
          class="w-12 h-12 mb-3 text-gray-300"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
        </svg>
        <p class="text-sm">⬆ 粘贴视频链接开始解析</p>
      </div>

      <!-- AI 解析待启动：不自动消耗请求次数，由用户手动触发 -->
      <div v-else-if="!started" class="flex flex-col items-center justify-center py-14 text-center animate-fade-in">
        <div class="w-14 h-14 rounded-2xl bg-blue-50 border border-line flex items-center justify-center mb-4">
          <svg
            class="w-7 h-7 text-blue-400"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" />
            <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z" />
          </svg>
        </div>
        <h3 class="text-base font-semibold text-gray-800 mb-1.5">AI 智能解析</h3>
        <p class="text-sm text-gray-500 max-w-sm leading-relaxed">
          解析视频字幕，生成总结摘要、思维导图，并支持针对视频内容的 AI 问答
        </p>
        <p class="text-xs text-gray-500 mt-1.5 mb-6">仅下载视频的话，无需启动此功能</p>
        <!-- 总结模板（工单 #105）：选项从服务端清单读，前端不硬编码名单 -->
        <div v-if="templateOptions.length || templateLoadFailed" class="w-full max-w-sm mb-6">
          <label for="summary-template-select" class="block text-xs font-medium text-gray-600 mb-1.5"> 总结模板 </label>
          <select
            v-if="templateOptions.length"
            id="summary-template-select"
            v-model="selectedTemplate"
            class="w-full px-3 py-2 rounded-xl border border-line bg-ink/60 text-sm text-gray-800 focus:ring-2 focus:ring-blue-500 outline-none transition-all"
          >
            <option v-for="tpl in templateOptions" :key="tpl.id" :value="tpl.id">
              {{ tpl.name }}
            </option>
          </select>
          <p v-if="templateLoadFailed" class="text-[11px] text-gray-400 mt-1.5">模板清单没取到，这次用默认模板。</p>
          <p v-else-if="selectedTemplateDescription" class="text-[11px] text-gray-400 mt-1.5">
            {{ selectedTemplateDescription }}
          </p>
        </div>
        <!-- 显式括号是必须的：写成 @click="startSummarize" 会把 MouseEvent
             当成 overwrite 传进去，于是「开始 AI 解析」会静默地变成一次覆盖。 -->
        <button
          @click="startSummarize()"
          class="px-6 py-2.5 rounded-xl bg-blue text-on-primary text-sm font-medium hover:bg-blue-600 transition-all duration-200 active:scale-95 flex items-center gap-2"
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
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
          开始 AI 解析
        </button>
      </div>

      <!-- 总结摘要 -->
      <div
        v-else-if="activeTab === 'summary'"
        class="prose prose-slate max-w-none prose-headings:text-gray-900 prose-headings:font-semibold prose-p:text-gray-600 prose-p:leading-relaxed prose-strong:text-gray-900"
      >
        <div v-if="noSubtitle" class="flex flex-col items-center justify-center py-10 text-gray-400">
          <svg
            class="w-10 h-10 mb-2 text-gray-300"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <p class="text-sm">该视频没有可用字幕，无法生成总结</p>
        </div>
        <div v-else-if="errorMsg" class="flex items-center gap-3 text-red-500 bg-red-50 rounded-xl px-4 py-3 text-sm">
          {{ errorMsg }}
        </div>
        <div v-else-if="!summaryMd && loading" class="space-y-3">
          <div class="skeleton h-5 w-3/4"></div>
          <div class="skeleton h-4 w-full"></div>
          <div class="skeleton h-4 w-5/6"></div>
          <div class="skeleton h-4 w-2/3"></div>
          <div class="text-center text-gray-400 text-sm mt-4 animate-pulse">AI 正在分析视频内容...</div>
        </div>
        <!-- eslint-disable-next-line vue/no-v-html -- 正文已在 marked 的 html 渲染器里转义过，原始 HTML 不会进来 -->
        <div v-else v-html="renderedSummary" class="summary-content animate-fade-in"></div>
      </div>

      <!-- 字幕文本 -->
      <div v-else-if="activeTab === 'subtitle'" class="text-sm text-gray-600">
        <div v-if="errorMsg" class="flex items-center gap-3 text-red-500 bg-red-50 rounded-xl px-4 py-3 text-sm">
          {{ errorMsg }}
        </div>
        <div v-else-if="!subtitleData && loading" class="space-y-2">
          <div v-for="n in 6" :key="n" class="skeleton h-4" :style="{ width: `${60 + Math.random() * 30}%` }"></div>
          <div class="text-center text-gray-400 text-sm mt-4 animate-pulse">提取字幕中...</div>
        </div>
        <div v-else-if="subtitleData?.has_subtitle">
          <div
            class="flex flex-wrap items-center gap-2 text-xs text-gray-500 mb-3 bg-ink/60 border border-line rounded-lg px-3 py-2"
          >
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
            </svg>
            <span>共 {{ subtitleData.segments?.length || 0 }} 条字幕</span>
            <!-- 导出字幕 -->
            <div class="ml-auto flex items-center gap-1.5">
              <button
                v-if="subtitleData.segments?.length"
                @click="exportSubtitle('srt')"
                class="px-2.5 py-1 rounded-md bg-panel border border-line text-gray-500 hover:text-cyan-300 hover:border-cyan-200 transition-colors flex items-center gap-1 font-pixel"
                title="导出带时间轴的 SRT 字幕文件"
              >
                <svg
                  class="w-3 h-3"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                SRT
              </button>
              <button
                @click="exportSubtitle('txt')"
                class="px-2.5 py-1 rounded-md bg-panel border border-line text-gray-500 hover:text-cyan-300 hover:border-cyan-200 transition-colors flex items-center gap-1 font-pixel"
                title="导出纯文本字幕（每句一行）"
              >
                <svg
                  class="w-3 h-3"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                TXT
              </button>
            </div>
          </div>
          <!-- 结构化展示：每条字幕独立一行（代码解析，不经 LLM 处理） -->
          <div v-if="subtitleData.segments?.length" class="space-y-0.5">
            <p
              v-for="(seg, i) in subtitleData.segments"
              :key="i"
              class="leading-relaxed text-gray-600 px-1 rounded hover:bg-panel-2 transition-colors"
            >
              {{ seg.text }}
            </p>
          </div>
          <p v-else class="whitespace-pre-wrap leading-relaxed">{{ subtitleData.full_text }}</p>
        </div>
        <div v-else class="flex flex-col items-center justify-center py-10 text-gray-400">
          <svg
            class="w-10 h-10 mb-2 text-gray-300"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <p class="text-sm">该视频没有可用字幕</p>
        </div>
      </div>

      <!-- 思维导图 -->
      <div v-else-if="activeTab === 'mindmap'" class="w-full">
        <div v-if="noSubtitle" class="flex flex-col items-center justify-center py-10 text-gray-400">
          <svg
            class="w-10 h-10 mb-2 text-gray-300"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <p class="text-sm">该视频没有可用字幕，无法生成思维导图</p>
        </div>
        <div v-else-if="errorMsg" class="flex items-center gap-3 text-red-500 bg-red-50 rounded-xl px-4 py-3 text-sm">
          {{ errorMsg }}
        </div>
        <div v-else-if="!mindmapMd && loading" class="space-y-3">
          <div class="skeleton h-5 w-1/2"></div>
          <div class="skeleton h-4 w-2/3 ml-4"></div>
          <div class="skeleton h-4 w-1/2 ml-4"></div>
          <div class="skeleton h-4 w-3/4 ml-8"></div>
          <div class="text-center text-gray-400 text-sm mt-4 animate-pulse">生成思维导图中...</div>
        </div>
        <div v-else-if="mindmapMd && mindmapRenderFailed" class="prose prose-slate max-w-none text-sm">
          <div class="flex items-center gap-2 text-yellow-600 bg-yellow-50 rounded-xl px-4 py-2.5 mb-4 text-xs">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            思维导图渲染失败，显示原始内容：
          </div>
          <pre class="whitespace-pre-wrap text-gray-600 bg-ink/60 border border-line rounded-xl p-4 text-xs">{{
            mindmapMd
          }}</pre>
        </div>
        <!-- markmap 要求容器为 <svg> 元素，传 div 会导致图形静默不渲染 -->
        <svg v-else-if="mindmapMd" ref="markmapContainer" class="w-full h-[480px] animate-fade-in"></svg>
        <div v-else class="flex flex-col items-center justify-center py-10 text-gray-400">
          <svg
            class="w-10 h-10 mb-2 text-gray-300"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          <p class="text-sm">暂无思维导图</p>
        </div>
      </div>

      <!-- AI 问答 -->
      <div v-else-if="activeTab === 'chat'" class="space-y-4">
        <div class="flex gap-2">
          <!-- 问答历史收纳按钮 -->
          <button
            v-if="chatHistoryList.length"
            @click="showChatHistory = !showChatHistory"
            :class="[
              'flex-shrink-0 w-11 rounded-xl border flex items-center justify-center transition-all duration-200',
              showChatHistory
                ? 'bg-blue-50 border-blue-200 text-blue-400'
                : 'bg-ink/60 border-line text-gray-400 hover:text-gray-600 hover:bg-gray-100',
            ]"
            title="问答历史记录"
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
              <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
              <path d="M3 3v5h5" />
              <path d="M12 7v5l4 2" />
            </svg>
          </button>
          <div class="relative flex-1">
            <svg
              class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
            <input
              v-model="chatQuestion"
              type="text"
              placeholder="输入关于视频的问题..."
              class="w-full pl-10 pr-4 py-2.5 rounded-xl border border-line bg-ink/60 text-sm text-gray-800 focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition-all duration-200 placeholder:text-gray-500"
              @keyup.enter="handleChat"
            />
          </div>
          <button
            @click="handleChat"
            :disabled="chatLoading || !chatQuestion.trim()"
            class="px-5 py-2.5 rounded-xl bg-blue text-on-primary text-sm font-medium hover:bg-blue-600 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 active:scale-95 whitespace-nowrap flex items-center gap-2"
          >
            <svg v-if="chatLoading" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            {{ chatLoading ? '思考中...' : '提问' }}
          </button>
        </div>

        <!-- 自带凭据的说明挂在常驻的额度行（见上）。这里曾有一份重复的
             折叠面板，那是「一份状态两个地方」的根源：填在追问框里的 key
             解析路径看不见，用户以为两边都生效。 -->

        <!-- 问答历史抽屉 -->
        <Transition name="drawer">
          <div
            v-if="showChatHistory && chatHistoryList.length"
            class="rounded-2xl border border-line bg-panel-2/60 overflow-hidden"
          >
            <div class="flex items-center justify-between px-4 py-2.5 border-b border-line">
              <div class="flex items-center gap-2 text-xs text-gray-500 font-medium">
                <svg
                  class="w-3.5 h-3.5"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                  <path d="M3 3v5h5" />
                  <path d="M12 7v5l4 2" />
                </svg>
                问答历史（{{ chatHistoryList.length }}）
              </div>
              <button @click="showChatHistory = false" class="text-gray-400 hover:text-gray-600 transition-colors">
                <svg
                  class="w-4 h-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                >
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <ul class="max-h-64 overflow-y-auto divide-y divide-line/60">
              <li v-for="(chat, i) in chatHistoryList" :key="i">
                <button
                  @click="applyChatHistory(chat)"
                  class="w-full text-left px-4 py-3 hover:bg-panel transition-colors group"
                >
                  <p class="text-xs font-medium text-gray-700 truncate group-hover:text-blue-400">
                    <span class="text-blue-400 mr-1 font-pixel">Q</span>{{ chat.question }}
                  </p>
                  <p class="text-[11px] text-gray-500 mt-1 truncate">
                    <span class="text-gray-500 mr-1 font-pixel">A</span>{{ chat.answer?.slice(0, 60)
                    }}{{ (chat.answer || '').length > 60 ? '...' : '' }}
                  </p>
                </button>
              </li>
            </ul>
          </div>
        </Transition>

        <div v-if="noSubtitle" class="flex flex-col items-center justify-center py-8 text-gray-400">
          <svg
            class="w-10 h-10 mb-2 text-gray-300"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <p class="text-sm">该视频没有可用字幕，无法回答问题</p>
        </div>
        <div v-else-if="errorMsg" class="flex items-center gap-3 text-red-500 bg-red-50 rounded-xl px-4 py-3 text-sm">
          {{ errorMsg }}
        </div>
        <div v-else-if="chatAnswer" class="prose prose-slate max-w-none text-sm animate-fade-in">
          <!-- eslint-disable-next-line vue/no-v-html -- 正文已在 marked 的 html 渲染器里转义过，原始 HTML 不会进来 -->
          <div v-html="renderedChatAnswer"></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import type { PropType } from 'vue'
import type { ParsedVideo } from '../api/video.ts'
import type { HistoryDetail } from '../api/history.ts'
import type { SessionUser } from '../api/auth.ts'
import type { ByokPublicState } from '../lib/byok.ts'
import { marked } from 'marked'
import { Transformer } from 'markmap-lib'
import { Markmap } from 'markmap-view'
import { summarizeVideo, chatWithVideo, fetchQuota, fetchSummaryTemplates } from '../api/summarize.ts'
import type { SseStream, SubtitlePayload, SummaryTemplate } from '../api/summarize.ts'
import { fetchChatSession, saveHistory } from '../api/history.ts'
import type { ChatMessage } from '../api/history.ts'
import { describeQuota, quotaBadgeClass as quotaBadgeClassOf } from '../lib/quota.ts'
import type { QuotaPayload } from '../lib/quota.ts'
import { getRequestCredential } from '../lib/byok.ts'

// 单换行也渲染为换行，避免 LLM 输出被合并成一段
marked.setOptions({ gfm: true, breaks: true })

/**
 * Markdown 里的原始 HTML 一律转义后再渲染。
 *
 * 摘要与问答的正文来自模型、也来自用户给的视频标题；marked 默认会把
 * 原文里的 HTML 标签原样吐出来，再交给 v-html 插进页面——那等于把
 * 别人的 <script> 插进本页。这里接管 marked 的 html 渲染器：原始
 * HTML 变成可见文本，Markdown 自己生成的标签（标题、列表、代码块）
 * 不受影响。
 */
function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

marked.use({
  renderer: {
    html({ text }: { text: string }): string {
      return escapeHtml(text)
    },
  },
})

const props = defineProps({
  videoUrl: { type: String, default: '' },
  videoTitle: { type: String, default: '' },
  videoData: { type: Object as PropType<ParsedVideo | null>, default: null },
  // 只用于回填**个人问答历史**，不再用于回填总结 / 思维导图 / 字幕。
  // 原因写在 setup 里那段注释处：那些内容只走 /api/summarize（社区视频表）。
  initialHistory: { type: Object as PropType<HistoryDetail | null>, default: null },
  user: { type: Object as PropType<SessionUser | null>, default: null },
  /**
   * 社区里已经有这一份结果。为真时组件挂载即自动拉取（服务端原样回放，
   * 不调模型、不扣额度）；为假时保持手动触发。
   * 这个开关决定「用户会不会白扣额度」，所以它必须由**服务端事实**
   * （by-url 查社区视频表）驱动，不能由「点过了解析」这种本地状态推断。
   */
  hasCommunityResult: { type: Boolean, default: false },
  /**
   * 一次性信号：这一次挂载是用户点了「重新解析」，不是自动复用。
   *
   * 做成 prop 而不是命令式调用（ref / expose），是因为父组件重建本组件的
   * 唯一手段就是换 :key，而 watch 是 immediate 的——信号必须活到挂载那一刻。
   * 父组件点完就把它置回 false，所以它只对**这一次**挂载生效。
   */
  regenerateRequested: { type: Boolean, default: false },
  /**
   * 自带凭据的**公开**状态（来自 lib/byok.getPublicState）。
   * 结构上不含 key —— 它只用来在页面上说明「现在用的是谁的额度」，
   * 以及点「去填写」时把弹窗叫出来。改动这里的形状要同步改 lib/byok。
   */
  byok: { type: Object as PropType<ByokPublicState | null>, default: null },
})

const emit = defineEmits(['ownership', 'regenerating', 'open-byok'])

const activeTab = ref('summary')
const tabs = [
  { key: 'summary', label: '总结摘要', icon: null },
  { key: 'mindmap', label: '思维导图', icon: null },
  { key: 'subtitle', label: '字幕文本', icon: null },
  { key: 'chat', label: 'AI 问答', icon: null },
]

const loading = ref(false)
const started = ref(false)
const summaryMd = ref('')
const mindmapMd = ref('')
const subtitleData = ref<SubtitlePayload | null>(null)
// 视频标签：一次解析产出的视频级属性，后端保证非空且已去重，这里只存不加工
const videoTags = ref<string[]>([])
const errorMsg = ref('')
const noSubtitle = ref(false)
const renderedSummary = computed(() => (summaryMd.value ? marked(summaryMd.value) : ''))
const markmapContainer = ref<HTMLElement | null>(null)
const mindmapRenderFailed = ref(false)

const chatQuestion = ref('')
const chatAnswer = ref('')
const chatLoading = ref(false)
const renderedChatAnswer = computed(() => (chatAnswer.value ? marked(chatAnswer.value) : ''))
const chatHistoryList = ref<ChatMessage[]>([])
const showChatHistory = ref(false)

// ── 自带凭据（BYOK）────────────────────────────────────
//
// 状态全部在 lib/byok 里，这里**只读不存**。
// 曾经这个组件自己存了一份（localStorage + 三个 ref），后果是：
// 解析路径完全看不到它 —— 用户在追问框填了 key，以为解析也用上了，
// 解析照样扣额度，而页面上没有任何地方能解释这个差别。
//
// 三条纪律仍然成立，位置换到 lib/byok：
// 1. 真值只从 getRequestCredential() 一条出口出去，绝不绑进会渲染的节点。
// 2. 输入框在弹窗保存后立刻清空（留在 DOM 里会进自动填充与「检查元素」）。
// 3. 只交给请求层一次，由 summarize.js / 这里的调用拼进请求体。
const byokNoticeWhat = ref('追问')
const byokNotice = ref(false)

// 额度显式：只读展示，不消耗。解析与追问是两个独立计数器，各显示各的。
const quotaInfo = ref<QuotaPayload | null>(null)
const quotaLabel = computed(() => describeQuota(quotaInfo.value))
const quotaBadgeClass = computed(() => quotaBadgeClassOf(quotaInfo.value))

async function refreshQuota() {
  try {
    quotaInfo.value = await fetchQuota()
  } catch {
    quotaInfo.value = null
  }
}

// ── 总结模板（工单 #105）──────────────────────────────────
//
// 模板清单只有**一份**真值，在服务端（prompts/templates.json）。这里只读不存：
// 存一份本地副本就等于把「有哪些模板」复制成第二份，名单一改本地那份就过期，
// 而过期的症状是「下拉框里选了个后端不认的模板」——服务端会报 422，
// 但没人说得清是自己选错了还是清单不同步。
//
// 拿不到清单**不该**挡住解析：服务端本来就会用默认模板，那才是多数用户要的。
// 所以这里失败时只留一句说明，不弹错、不拦按钮。
const templateOptions = ref<SummaryTemplate[]>([])
const selectedTemplate = ref('')
const templateLoadFailed = ref(false)
const selectedTemplateDescription = computed(
  () => templateOptions.value.find((t) => t.id === selectedTemplate.value)?.description || '',
)

async function loadTemplates() {
  try {
    const list = await fetchSummaryTemplates()
    templateOptions.value = list.templates || []
    // 默认模板以**服务端**说的为准。已经选过的（还在清单里）保留——
    // 用户刚选过就被改掉，比默认错更让人恼火。
    const stillThere = templateOptions.value.some((t) => t.id === selectedTemplate.value)
    if (!stillThere) selectedTemplate.value = list.default || ''
    templateLoadFailed.value = false
  } catch {
    templateOptions.value = []
    templateLoadFailed.value = true
  }
}

function applyQuotaEvent(d: QuotaPayload) {
  // 自带凭据的那次调用服务端**没有余额可报**（它刻意没查额度），
  // 只带一个 byok 标记。整体替换会把用户看到的余额抹成 undefined，
  // 所以这条分支只记一条提示，余额原样留着。
  if (d?.byok) {
    byokNotice.value = true
    return
  }
  // 整体展开事件，而不是逐项挑字段：槽位与中文名（labels）都由服务端给
  // （ADR 0033），这里不认种类名——只挑顶层 remaining/limit 会把按种类的
  // 数字丢掉，界面退回单数字。
  quotaInfo.value = { ...d, logged_in: true, unlimited: !!d.unlimited }
}

// 进行中的流句柄，用于用户主动停止
let summaryStream: SseStream | null = null
let chatStream: SseStream | null = null

let markmapInstance: Markmap | null = null

// 刻意**没有**「从个人历史回填 AI 结果」这条捷径（工单 #7 顺带修）。
//
// 原来这里是：拿到 initialHistory.id 就直接渲染自己那份 summary_md，
// 根本不请求 /api/summarize。后果是社区里明明只有一份总结，有过个人
// 历史的人却永远看不到它——同一链接在两个人屏幕上呈现两份不同内容，
// 与「无论谁先解析，看到的都是同一份」直接抵触。
//
// 现在唯一的详情数据源是 /api/summarize，它读的就是社区视频表：
// 已有结果原样回放（不调模型、不扣额度），没有才真正解析。
// 少一条路径，就少一处能让内容来源分叉的地方。
//
// 唯一还从个人记录里读的，是**问答历史**：追问本来就是按用户隔离的个人
// 对话，社区视频表里没有、也不该有它（ADR 0001 的两表分工）。
// 注意只读 chat_history 一个字段，summary / mindmap / subtitle 一律不碰。
if (Array.isArray(props.initialHistory?.chat_history)) {
  chatHistoryList.value = props.initialHistory.chat_history
}

/**
 * 导图结构由后端定：`backend/mindmap.py` 是导图规则的**唯一真源**
 * （层级上限、每支子节点上限、总数上限、清洗规则全在那边）。
 *
 * 前端**不再自己清洗一遍**：两份规则迟早分叉，而分叉的那一份没人测。
 * 拿到的 markdown 原样交给 markmap 渲染；渲染不了就走模板里的失败兜底
 * （把原始 markdown 显示出来），空内容走「暂无思维导图」空态。
 */
function renderMindmap() {
  if (!markmapContainer.value || !mindmapMd.value) return
  mindmapRenderFailed.value = false
  try {
    if (markmapInstance) {
      markmapInstance.destroy()
      markmapInstance = null
    }
    markmapContainer.value.innerHTML = ''
    const transformer = new Transformer()
    const { root } = transformer.transform(mindmapMd.value)
    markmapInstance = Markmap.create(
      markmapContainer.value,
      {
        zoom: true,
        pan: true,
        maxWidth: 600,
        /* 节点按层级平涂品牌色。markmap 的 color 回调要的是字面颜色，
         拿不到 CSS 变量，所以这里用 @theme 里的 hex（与城墙同一套）。 */
        color: (node) => ['#2fa1da', '#1b5a79', '#f49a34', '#60ebc6'][node.state?.depth % 4] || '#1b5a79',
      },
      root,
    )
  } catch (e) {
    console.error('Markmap render error:', e)
    mindmapRenderFailed.value = true
  }
}

watch(mindmapMd, (val) => {
  if (val && activeTab.value === 'mindmap') {
    nextTick(() => renderMindmap())
  }
})

watch(activeTab, (tab) => {
  if (tab === 'mindmap' && mindmapMd.value) {
    nextTick(() => renderMindmap())
  }
})

/**
 * 视频链接变化：重置状态。
 *
 * hasCommunityResult 为真时**自动**发起请求：社区里已经有这一份，
 * 服务端会原样回放，不调模型、不扣额度（claim=="reuse" 在
 * consume_quota 之前就 return 了）。所以「自动」在这里是零成本的，
 * 用户从历史/社区点进来能直接看到内容。
 *
 * 为假时**不**自动发起：那时是真要调模型、要扣每日额度，
 * 保持用户手动点「开始 AI 解析」。少看一次内容好过额度被白扣。
 *
 * regenerateRequested 为真时也自动发起，但那次请求带 overwrite——
 * 用户明确要求改写自己那一份，替他再点一次「开始」没有意义。
 */
watch(
  () => props.videoUrl,
  async (newUrl) => {
    // 换视频时先中止上一个视频的流
    summaryStream?.cancel()
    chatStream?.cancel()
    started.value = false
    loading.value = false
    summaryMd.value = ''
    mindmapMd.value = ''
    subtitleData.value = null
    videoTags.value = []
    errorMsg.value = ''
    noSubtitle.value = false
    chatAnswer.value = ''
    chatHistoryList.value = []
    showChatHistory.value = false

    if (newUrl && (props.hasCommunityResult || props.regenerateRequested)) {
      startSummarize(props.regenerateRequested)
    }
  },
  { immediate: true },
)

/**
 * @param {boolean} overwrite 是否覆盖社区里已有那一份（ADR 0007）。
 *   为真时服务端只认首次解析者本人，不是本人的请求会直接回错误事件。
 */
function startSummarize(overwrite = false) {
  if (!props.videoUrl || started.value) return
  started.value = true
  loading.value = true
  // 父组件的按钮要跟着转：覆盖期间没有「正在解析中」那层占位提示可看，
  // 唯一的进度信号就是横幅上那个按钮本身。
  emit('regenerating', true)
  summaryMd.value = ''
  mindmapMd.value = ''
  subtitleData.value = null
  videoTags.value = []
  errorMsg.value = ''
  noSubtitle.value = false
  chatAnswer.value = ''
  chatHistoryList.value = []
  showChatHistory.value = false
  byokNotice.value = false
  byokNoticeWhat.value = '解析'

  let failed = false
  let stopped = false
  summaryStream = summarizeVideo(
    props.videoUrl,
    'zh',
    {
      // 复用回放会先发这一条：它决定父组件的「重新解析」是能点还是只能看。
      onOwnership: (data) => emit('ownership', data),
      onSubtitle: (data) => {
        subtitleData.value = data
      },
      onSummary: (token) => {
        summaryMd.value += token
      },
      onMindmap: (data) => {
        mindmapMd.value = data.markdown
      },
      onTags: (data) => {
        videoTags.value = data
      },
      onQuota: applyQuotaEvent,
      onCancel: () => {
        stopped = true
      },
      onError: (err) => {
        failed = true
        if (err.message.includes('没有可用的字幕')) {
          noSubtitle.value = true
        } else {
          errorMsg.value = err.message
        }
      },
      onDone: () => {
        // 断流 / 异常 / 主动取消都会走到这里（onDone 保证恰好一次）
        loading.value = false
        summaryStream = null
        emit('regenerating', false)
        // 必须在上面的 chatHistoryList 清空**之后**回填：
        // startSummarize 开头把列表清空了，早于它调就会白填一次。
        hydrateChatHistory()
        if (!failed && !stopped) {
          persistHistory()
        } else {
          // 出错或用户主动停止：额度可能已经扣掉，重新拉一次真实值
          refreshQuota()
        }
      },
    },
    {
      overwrite,
      // 没有自带凭据时传 undefined 而不是 null：options.credential 的类型是
      // `SummarizeCredential | undefined`，null 不在其中。
      credential: getRequestCredential() ?? undefined,
      // 平台元数据跟着解析请求一起走（工单 #17 第 2 项）。
      // 封面取 videoData.thumbnail —— /api/parse 的返回里它就叫 thumbnail，
      // 而社区卡片那一列叫 cover_url，两个名字指的是同一个东西。
      videoTitle: props.videoTitle || '',
      coverUrl: props.videoData?.thumbnail || '',
      // 提示词模板 id（工单 #105）。空串 = 服务端用它自己的默认模板；
      // 这里不猜、不兜底——猜错一次的症状是「选了 A 拿到 B 的总结」。
      summaryTemplate: selectedTemplate.value,
    },
  )
}

/**
 * 从服务端回填追问记录。
 *
 * 为什么必须由服务端给：追问记录按 (user_id, video_url) 存，**不依赖这个
 * 用户解析过这个视频**。B 追问 A 解析的视频时他并没有 parse_history 行，
 * 只读 initialHistory.chat_history 的话，刷新一次就再也看不到自己问过什么。
 *
 * 空结果不覆盖本地列表：服务端返回空说明这一轮没能落库（写失败或被拒），
 * 拿空列表盖掉用户刚看到的回答，比不同步更糟。
 */
async function hydrateChatHistory() {
  if (!props.user || !props.videoUrl) return
  try {
    const turns = await fetchChatSession(props.videoUrl)
    if (turns.length) chatHistoryList.value = turns
  } catch {
    /* 读不到就保持现状，不影响主流程 */
  }
}

function stopSummarize() {
  summaryStream?.cancel()
}

/**
 * AI 解析完成后保存到解析历史（仅登录用户）。
 *
 * 失败不打断主流程，但**不留白**：saveHistory 内部会记一条 warn。
 * 这条请求是个人历史唯一的写入途径（`/api/summarize` 刻意不写这张表），
 * 它静默失败时用户刷新一次页面，那次解析就凭空消失了。
 */
function persistHistory() {
  if (!props.user || !props.videoUrl) return
  if (!summaryMd.value && !mindmapMd.value) return
  saveHistory({
    url: props.videoUrl,
    video_title: props.videoTitle || '',
    video_data: props.videoData || null,
    summary_md: summaryMd.value,
    mindmap_md: mindmapMd.value,
    subtitle_data: subtitleData.value || null,
  })
}

async function handleChat() {
  // 没有视频链接时连请求都不该发：URL 是必填参数，
  // 拿 undefined 当 URL 发出去只会换一个看不懂的报错。
  if (!props.videoUrl || !chatQuestion.value.trim() || chatStream) return
  chatLoading.value = true
  chatAnswer.value = ''
  const question = chatQuestion.value.trim()
  byokNotice.value = false
  byokNoticeWhat.value = '追问'
  // 真值只在这一行离开 lib/byok，之后只作为请求体里的一个字段存在。
  const credential = getRequestCredential()
  let failed = false
  let stopped = false
  const stream = chatWithVideo(
    props.videoUrl,
    question,
    {
      onAnswer: (token) => {
        chatAnswer.value += token
      },
      onQuota: applyQuotaEvent,
      onCancel: () => {
        stopped = true
      },
      onError: (err) => {
        failed = true
        chatAnswer.value = `错误：${err.message}`
      },
      onDone: () => {
        // 断流 / 异常 / 主动取消都会走到这里（onDone 保证恰好一次）
        chatLoading.value = false
        if (chatStream === stream) chatStream = null
        if (failed || stopped) {
          refreshQuota()
          return
        }
        // 组件内即时展示。持久化由服务端在答案产出时自己落（工单 #8）：
        // 前端不再回调保存接口——记录存不存在不该取决于浏览器有没有多发一个请求。
        chatHistoryList.value.push({ question, answer: chatAnswer.value })
        showChatHistory.value = false
        // 以服务端为准再拉一次：刷新后回来读的就是它，不依赖这次 push。
        hydrateChatHistory()
      },
    },
    { credential: credential ?? undefined },
  )
  chatStream = stream
  await stream.done
}

function stopChat() {
  chatStream?.cancel()
}

/** 点击历史记录行：回填问题与已保存的答复（不发起新 AI 请求，零消耗） */
function applyChatHistory(chat: ChatMessage) {
  if (!chat?.question) return
  chatQuestion.value = chat.question
  chatAnswer.value = chat.answer || ''
  errorMsg.value = ''
  showChatHistory.value = false
}

/** 字幕导出：SRT（带时间轴）或 TXT（每句一行），纯前端生成下载 */
function exportSubtitle(format: 'srt' | 'txt') {
  const segs = subtitleData.value?.segments || []
  const title = (props.videoTitle || 'subtitle').replace(/[\\/:*?"<>|]/g, '_').slice(0, 60)
  let content: string
  let ext = format

  if (format === 'srt') {
    const ts = (s: number) => {
      const t = Math.max(0, Number(s) || 0)
      const h = String(Math.floor(t / 3600)).padStart(2, '0')
      const m = String(Math.floor((t % 3600) / 60)).padStart(2, '0')
      const sec = String(Math.floor(t % 60)).padStart(2, '0')
      const ms = String(Math.round((t % 1) * 1000)).padStart(3, '0')
      return `${h}:${m}:${sec},${ms}`
    }
    content = segs
      .map((seg, i) => `${i + 1}\n${ts(seg.start)} --> ${ts(seg.end || seg.start + 2)}\n${seg.text}`)
      .join('\n\n')
    if (!content) {
      // 无时间轴数据时降级为纯文本
      content = subtitleData.value?.full_text || ''
      ext = 'txt'
    }
  } else {
    content = segs.length ? segs.map((s) => s.text).join('\n') : subtitleData.value?.full_text || ''
  }

  const blob = new Blob([`\ufeff${content}`], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${title}.${ext}`
  a.click()
  URL.revokeObjectURL(url)
}

// ── 生命周期 ──────────────────────────────────────────────
onMounted(() => {
  refreshQuota()
  // 模板清单：只读、不消耗额度。失败不拦解析（服务端会用默认模板）。
  loadTemplates()
})

onUnmounted(() => {
  // 组件被卸载/换页时中止在途请求，避免往已销毁的组件上写状态
  summaryStream?.cancel()
  chatStream?.cancel()
  markmapInstance?.destroy?.()
  markmapInstance = null
})
</script>

<style scoped>
/* 思维导图适配：markmap 节点文字经 foreignObject 渲染，颜色取自 svg 上的
   --markmap-text-color 变量（库默认 #333，深色底不可见）。
   库默认样式定义于 .markmap（0,1,0），此处用 svg.markmap（0,1,1）提权覆盖。
   这些是 CSS 自定义属性，所以引用令牌即可跟随明亮主题。 */
:deep(svg.markmap) {
  --markmap-text-color: var(--color-gray-800);
  --markmap-a-color: var(--color-info);
  --markmap-a-hover-color: var(--color-cyan-400);
  --markmap-code-bg: var(--color-panel-2);
  --markmap-code-color: var(--color-info);
  --markmap-highlight-bg: color-mix(in srgb, var(--color-primary) 28%, transparent);
  --markmap-highlight-node-bg: color-mix(in srgb, var(--color-primary) 14%, transparent);
}
/* 问答历史抽屉展开/收起动画 */
.drawer-enter-active,
.drawer-leave-active {
  transition: all 0.25s ease;
  transform-origin: top;
}
.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
  transform: scaleY(0.9) translateY(-6px);
}
</style>
