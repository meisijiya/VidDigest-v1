import type { AxiosResponse } from 'axios'

import { http } from '../lib/http.ts'

/**
 * 一条可下载的格式。
 *
 * 键集合与后端 `backend/formats.py` 的 `FORMAT_DEFAULTS` 一一对应：
 * 后端那里是白名单（多一个键就抛 KeyError），这里也照着列全，
 * 免得后端加字段时前端这边悄悄少一块。
 */
export interface VideoFormat {
  format_id: string
  ext: string
  resolution: string
  height: number
  width: number
  filesize: number | null
  filesize_approx: number | null
  vcodec: string | null
  acodec: string | null
  abr: number | null
  has_video: boolean
  has_audio: boolean
  /** 视频轨 / 纯音频轨。前端按它分「选择画质」与「纯音频」两块。 */
  kind: 'video' | 'audio'
  label: string
}

/**
 * 解析结果。抖音与其它平台**统一**成这个形状
 * （后端 `douyin._build_result` 明确写着「与 yt-dlp 解析结果兼容」）。
 */
export interface ParsedVideo {
  id: string
  title: string
  thumbnail: string
  duration: number | null
  duration_string: string
  uploader: string
  platform: string
  view_count: number | null
  upload_date: string
  description: string
  formats: VideoFormat[]
  subtitles: string[]
  automatic_captions: string[]
}

/** `/api/parse` 的响应信封。 */
export interface ParseResponse {
  success: boolean
  data: ParsedVideo
  /**
   * 只有「200 但带错误信息」时才有。服务端 400 的错误体走 axios 异常路径，
   * 不进这个信封；留着这个字段是因为前端有一处「res.error || 兜底文案」的读法。
   */
  error?: string
}

export async function parseVideo(url: string): Promise<ParseResponse> {
  const res = await http.post('/api/parse', { url })
  return res.data
}

export async function downloadViaServer(url: string, formatId: string): Promise<AxiosResponse<Blob>> {
  return await http.post(
    '/api/download',
    { url, format_id: formatId },
    {
      responseType: 'blob',
    },
  )
}

export async function getDirectUrl(url: string, formatId: string): Promise<string> {
  const res = await http.post('/api/direct-url', { url, format_id: formatId })
  return res.data.data
}
