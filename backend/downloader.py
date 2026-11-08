import os
import re
import shutil
from typing import Optional

import yt_dlp

from download_artifacts import WrittenPaths
from formats import make_format
from url_canonical import url_for_ytdlp
from ytdlp_net import with_network

def _find_ffmpeg_path() -> Optional[str]:
    """查找 ffmpeg 可执行文件路径"""
    if shutil.which("ffmpeg"):
        return os.path.dirname(shutil.which("ffmpeg"))
    try:
        import static_ffmpeg
        paths = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        return os.path.dirname(paths[0])
    except Exception:
        return None


class VideoDownloader:
    """yt-dlp 封装层，提供视频解析、下载、直链获取能力"""

    DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")

    #: 格式卡片总数上限。分不清画质的人数会在这里被上限截断。
    MAX_FORMATS = 15
    #: 音频选项单独的上限。留得少是有意的：DASH 源的码率梯度很密，
    #: 列满 15 个音频位只会把真正要选的画质挤出去。
    MAX_AUDIO_OPTIONS = 3

    def __init__(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        self.ffmpeg_path = _find_ffmpeg_path()
        self.has_ffmpeg = self.ffmpeg_path is not None

    # ── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", name)

    @staticmethod
    def _format_filesize(size: Optional[int]) -> str:
        if not size:
            return "未知大小"
        if size < 1024 * 1024:
            return f"{size / 1024:.0f}KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.2f}GB"

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if not seconds:
            return "00:00"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    # ── 解析视频信息 ──────────────────────────────────────

    def parse_video(self, url: str) -> dict:
        """解析视频信息，不下载文件"""
        url = url_for_ytdlp(url)
        ydl_opts = with_network({
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("无法解析该链接")

        formats = self._extract_formats(info)
        platform = info.get("extractor", info.get("extractor_key", "Unknown"))

        return {
            "id": info.get("id", ""),
            "title": info.get("title", "未知标题"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "duration_string": self._format_duration(info.get("duration")),
            "uploader": info.get("uploader", info.get("channel", "未知")),
            "platform": platform,
            "view_count": info.get("view_count"),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description") or "")[:200],
            "formats": formats,
            "subtitles": list(info.get("subtitles", {}).keys()),
            "automatic_captions": list(info.get("automatic_captions", {}).keys())[:5],
        }

    # ── 提取并整理格式 ────────────────────────────────────

    def _extract_formats(self, info: dict) -> list:
        """从 yt-dlp info 中提取并整理可用格式。

        **音频轨必须进来。** 这里原来有一句 ``if not has_video: continue``，
        把 yt-dlp 返回的纯音频格式整批丢掉了 —— 而 DASH 源（B站 / YouTube）
        本来就只给分离的视频流和音频流，于是「只下音频」在界面上根本不是
        一个选项，是一条走不到的路。

        返回的每一项都带 ``kind``（``video`` / ``audio``）。前端要按它分组，
        而「缺字段就当视频」正是当初让这个问题看不见的那个默认。
        """
        raw_formats = info.get("formats", [])
        if not raw_formats:
            return []

        seen = set()
        videos = []
        audios = []
        audio_seen = set()

        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            ext = f.get("ext", "mp4")

            has_video = bool(vcodec and vcodec != "none")
            has_audio = bool(acodec and acodec != "none")

            filesize = f.get("filesize") or f.get("filesize_approx")

            if not has_video and has_audio:
                # 纯音频：按 (容器, 码率) 去重。DASH 源常给出四五个 opus 码率，
                # 彼此只差几十 k —— 全列出来会把有用的视频选项挤出上限，
                # 给用户一堆没有实际差别的选择。
                abr = f.get("abr") or f.get("tbr") or 0
                key = (ext, round(float(abr)))
                if key in audio_seen:
                    continue
                audio_seen.add(key)
                bitrate = f"{int(abr)}kbps" if abr else "未知码率"
                audios.append(make_format(
                    format_id=f.get("format_id", ""),
                    ext=ext,
                    filesize=filesize,
                    filesize_approx=filesize,
                    acodec=acodec,
                    abr=int(abr) if abr else None,
                    has_audio=True,
                    kind="audio",
                    label=f"{bitrate} {ext.upper()} (仅音频, "
                          f"{self._format_filesize(filesize)})",
                ))
                continue

            if not has_video:
                # 既无视频也无音频：预览图 / 字幕轨之类的，不是可下载内容。
                continue

            resolution = f"{f.get('width', '?')}x{height}" if height else "未知"
            size_label = self._format_filesize(filesize)

            if has_audio:
                label = f"{height}p {ext.upper()} ({size_label})"
                key = (height, ext, "av")
            else:
                label = f"{height}p {ext.upper()} (仅视频, {size_label})"
                key = (height, ext, "v")

            if key in seen:
                continue
            seen.add(key)

            videos.append(make_format(
                format_id=f.get("format_id", ""),
                ext=ext,
                resolution=resolution,
                height=height or 0,
                width=f.get("width") or 0,
                filesize=filesize,
                filesize_approx=filesize,
                vcodec=vcodec,
                acodec=acodec,
                has_video=True,
                has_audio=has_audio,
                kind="video",
                label=label,
            ))

        videos.sort(key=lambda x: x["height"], reverse=True)
        # 码率高的排前面；码率缺失（0）沉底，而不是排到最前面当默认项。
        audios.sort(key=lambda x: (x["abr"] or 0, x["filesize"] or 0), reverse=True)

        results = []

        # 如果所有视频格式都没有音频，添加一个合并格式选项
        if videos and not any(r["has_audio"] for r in videos):
            best_video = videos[0]
            merged = make_format(**{
                **best_video,
                "format_id": "bestvideo+bestaudio/best",
                "label": f"{best_video['height']}p 最佳 (视频+音频合并)",
                "has_audio": True,
                "acodec": "merged",
            })
            results.append(merged)

        # 上限是 15，但**先给音频留位置**。直接 videos[:15] 再往后拼音频，
        # 音频会被静默挤掉 —— 而这正是「用户看不到音频选项」的第二种成因。
        audio_budget = min(len(audios), self.MAX_AUDIO_OPTIONS)
        video_budget = max(1, self.MAX_FORMATS - len(results) - audio_budget)
        results.extend(videos[:video_budget])
        results.extend(audios[:audio_budget])
        return results[:self.MAX_FORMATS]

    # ── 下载视频 ──────────────────────────────────────────

    def download_video(self, url: str, format_id: str) -> dict:
        """下载视频到服务器临时目录，返回文件路径和元数据"""
        url = url_for_ytdlp(url)
        if not self.has_ffmpeg and "+" in format_id:
            format_id = "best"

        # 记下这次下载自己写下了哪些文件：失败时按这份清单清半截文件，
        # 不用去扫目录——扫目录会删掉别的请求正在写的在途文件。
        written = WrittenPaths()

        ydl_opts = with_network({
            "format": format_id,
            "outtmpl": os.path.join(self.DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "progress_hooks": [written.track_progress],
        })

        if self.has_ffmpeg:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path
            # merge_output_format 只对**合并**语义。纯音频下载不涉合并，
            # 把它写成上下文只会让下一个读人以为一个 m4a 下载会被
            # 强制转成 mp4（实际不会，但说法是假的）。
            if "+" in format_id:
                ydl_opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception:
            # 下载失败：把这次下载自己写下的半截文件清掉。
            # 抖音那条通道也这么干（见 `backend/douyin.py`），
            # 两边共用同一份清理口径（`backend/download_artifacts.py`）。
            written.remove_all()
            raise

        if not info:
            raise ValueError("下载失败")

        title = self._sanitize_filename(info.get("title", "video"))
        ext = info.get("ext", "mp4")
        filename = f"{title}.{ext}"
        filepath = os.path.join(self.DOWNLOAD_DIR, filename)

        if not os.path.exists(filepath):
            prepared = ydl.prepare_filename(info)
            if os.path.exists(prepared):
                filepath = prepared
                filename = os.path.basename(prepared)
            else:
                for f in os.listdir(self.DOWNLOAD_DIR):
                    if title in f:
                        filepath = os.path.join(self.DOWNLOAD_DIR, f)
                        filename = f
                        break

        return {
            "filepath": filepath,
            "filename": filename,
            "title": info.get("title", "video"),
            "ext": ext,
        }

    # ── 获取视频直链 ──────────────────────────────────────

    def get_direct_url(self, url: str, format_id: str) -> dict:
        """获取视频直链"""
        url = url_for_ytdlp(url)
        ydl_opts = with_network({
            "format": format_id,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("无法获取直链")

        direct_url = info.get("url")
        if not direct_url:
            requested = info.get("requested_formats")
            if requested and len(requested) > 0:
                direct_url = requested[0].get("url")

        if not direct_url:
            raise ValueError("该视频不支持直链下载，请使用服务端下载模式")

        return {
            "direct_url": direct_url,
            "ext": info.get("ext", "mp4"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "title": info.get("title", "video"),
        }
