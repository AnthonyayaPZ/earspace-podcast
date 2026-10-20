import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path

from .transcripts import parse_json3


class MediaError(Exception):
    def __init__(self, message, *, retry=True):
        super().__init__(message)
        self.retry = retry


def run_command(command, directory, pulse, timeout, progress_start=0, progress_end=100):
    """Use an argument vector, bounded diagnostic file, cancellation and hard timeout."""
    started, last_pulse, last_progress = time.monotonic(), 0, progress_start
    with tempfile.TemporaryFile(mode="w+b") as log:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, **kwargs)
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now - started > timeout:
                    raise MediaError("处理超时；请检查网络或调高 EARSPACE_JOB_TIMEOUT_SECONDS")
                if now - last_pulse >= 2:
                    pulse(last_progress, None)
                    last_pulse = now
                    end = log.seek(0, 2)
                    log.seek(max(0, end - 4096))
                    tail = log.read().decode("utf-8", errors="replace")
                    matches = re.findall(r"\[download\]\s+(\d+(?:\.\d+)?)%", tail)
                    if matches:
                        last_progress = progress_start + (progress_end-progress_start) * float(matches[-1])/100
                    if end > 4 * 1024 * 1024:
                        log.seek(0)
                        log.truncate()
                time.sleep(.25)
            if process.returncode:
                log.seek(max(0, log.seek(0, 2) - 4000))
                diagnostic = log.read().decode("utf-8", errors="replace")
                # Do not expose raw provider URLs, cookies, server paths or console output.
                if any(marker in diagnostic.lower() for marker in (
                        "sign in to confirm", "sign in to verify", "login required")):
                    raise MediaError("YouTube 要求登录或访问验证；请配置或更新 EARSPACE_YOUTUBE_COOKIES_FILE 后手动重试",
                                     retry=False)
                elif "Requested format" in diagnostic or "format is not available" in diagnostic:
                    message = "此视频没有可用的兼容媒体格式；请更新 yt-dlp 或选择其他视频"
                elif "Private video" in diagnostic or "Video unavailable" in diagnostic:
                    message = "视频不可用、为私密视频或存在地区限制"
                else:
                    message = "媒体处理失败，请检查网络、磁盘空间、yt-dlp 和 FFmpeg 版本"
                raise MediaError(message)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=15)


def base_command():
    return [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings",
            "--no-colors", "--js-runtimes", "node", "--socket-timeout", "30", "--retries", "2",
            "--fragment-retries", "2"]


def youtube_command(settings, work):
    """Use a private per-attempt cookie jar; never update the supplied credential file."""
    command = base_command()
    if not settings.youtube_cookies_file:
        return command
    try:
        source = Path(settings.youtube_cookies_file).expanduser()
        if not source.is_file() or source.stat().st_size > 1024**2:
            raise ValueError("Missing or oversized cookie file")
        original = MozillaCookieJar(str(source))
        original.load(ignore_discard=True, ignore_expires=True)
        destination = Path(work) / ".youtube-cookies.txt"
        jar = MozillaCookieJar(str(destination))
        for cookie in original:
            domain = cookie.domain.lstrip(".").lower()
            if domain == "youtube.com" or domain.endswith(".youtube.com"):
                jar.set_cookie(cookie)
        if not len(jar):
            raise ValueError("No YouTube cookies")
        destination.touch(mode=0o600, exist_ok=False)
        jar.save(ignore_discard=True, ignore_expires=True)
    except (OSError, LoadError, UnicodeError, ValueError):
        raise MediaError("无法读取 YouTube Cookie；请检查 EARSPACE_YOUTUBE_COOKIES_FILE、文件权限和 Netscape 格式",
                         retry=False) from None
    return command + ["--cookies", str(destination)]


def download(db, job, pulse):
    settings = db.settings
    episode = db.episode(job["episode_id"])
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("未找到 FFmpeg/ffprobe，请安装并加入 PATH")
    if shutil.disk_usage(settings.data_dir).free < settings.min_free_mb * 1024**2:
        raise MediaError("磁盘剩余空间不足，已暂停下载")
    work_root = settings.data_dir / "work"
    work_root.mkdir(exist_ok=True)
    # Separate attempts use unique directories, including after a lease recovery.
    with tempfile.TemporaryDirectory(prefix=f"{job['id']}-{job['attempts']}-", dir=work_root) as working:
        work = Path(working)
        command = youtube_command(settings, work)
        url = f"https://www.youtube.com/watch?v={episode['id']}"
        timeout = settings.job_timeout
        pulse(2, "读取视频信息")
        run_command(command + ["--skip-download", "--write-info-json", "-o", "source.%(ext)s", url],
                    work, pulse, min(timeout, 180), 2, 5)
        info = json.loads((work / "source.info.json").read_text(encoding="utf-8"))
        duration = info.get("duration") or 0
        if info.get("is_live") or info.get("live_status") == "is_upcoming" or not 0 < duration <= settings.max_duration:
            raise MediaError("仅支持时长在配置范围内的已发布视频，不处理直播")
        mode = json.loads(job.get("payload") or "{}").get("mode", "video")
        if mode == "video":
            pulse(5, "下载并合并 MP4")
            run_command(command + [
                "--newline", "--progress", "--max-filesize", f"{settings.max_file_mb}M",
                "-f", "bv[vcodec^=avc1][height<=720]+ba[ext=m4a]/b[ext=mp4]/bv[height<=720]+ba/b",
                "--merge-output-format", "mp4", "--remux-video", "mp4",
                "-o", "video.%(ext)s", url], work, pulse, timeout, 5, 75)
            video = work / "video.mp4"
            if not video.is_file():
                raise MediaError("未生成 MP4，视频可能超过大小限制或无法合并")
            if video.stat().st_size > settings.max_file_mb * 1024**2:
                raise MediaError("合并后文件超过配置的大小限制")
            # Normalize video codec only when needed, so Safari/mobile playback uses H.264/AAC.
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                                    "-of", "json", str(video)], capture_output=True, timeout=30, check=True)
            streams = json.loads(probe.stdout).get("streams", [])
            if not any(s["codec_type"] == "video" for s in streams) or not any(s["codec_type"] == "audio" for s in streams):
                raise MediaError("下载结果缺少视频或音频轨道")
            compatible = all(s["codec_name"] == ("h264" if s["codec_type"] == "video" else "aac")
                             for s in streams if s["codec_type"] in ("video", "audio"))
            pulse(76, "优化播放文件")
            run_command(["ffmpeg", "-nostdin", "-y", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0",
                         "-c:v", "copy" if compatible else "libx264", "-c:a", "copy" if compatible else "aac",
                         "-movflags", "+faststart", str(work / "ready.mp4")], work, pulse, timeout, 76, 80)
            audio_source = work / "ready.mp4"
        elif mode == "audio":
            pulse(5, "下载音频轨道")
            run_command(command + ["--newline", "--progress", "--max-filesize", f"{settings.max_file_mb}M",
                        "-f", "ba[ext=m4a]/ba", "-o", "source_audio.%(ext)s", url],
                        work, pulse, timeout, 5, 80)
            candidates = [path for path in work.glob("source_audio.*")
                          if path.suffix.lower() in {".m4a", ".webm", ".opus", ".ogg", ".mp3", ".aac"}]
            if len(candidates) != 1:
                raise MediaError("未生成可用音轨，可能没有独立音频格式或超过大小限制")
            audio_source = candidates[0]
            if audio_source.stat().st_size > settings.max_file_mb * 1024**2:
                raise MediaError("音频轨道超过配置的大小限制")
        else:
            raise MediaError("不支持的下载模式")
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_name", "-of", "json", str(audio_source)],
                               capture_output=True, timeout=30, check=True)
        audio_streams = json.loads(probe.stdout).get("streams", [])
        if not audio_streams:
            raise MediaError("下载结果没有可用音轨")
        copy_audio = audio_streams[0].get("codec_name") == "aac"
        audio_processing = "stream_copy" if copy_audio else "transcoded"
        pulse(81, "保存原始 AAC 音轨" if copy_audio else "转换为兼容音频（单线程）")
        codec_args = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "96k", "-threads", "1"]
        run_command(["ffmpeg", "-nostdin", "-y", "-i", str(audio_source), "-map", "0:a:0", "-vn",
                     *codec_args, "-movflags", "+faststart", str(work / "audio.m4a")], work, pulse, timeout, 81, 87)
        pulse(88, "获取字幕文稿")
        transcript, warning = None, None
        language = episode["language"]
        available = info.get("subtitles", {})
        automatic = info.get("automatic_captions", {})
        matches = lambda mapping: sorted((k for k in mapping if k == language or k.startswith(language + "-")),
                                         key=lambda k: (k != language, k))
        manual_langs, auto_langs = matches(available), matches(automatic)
        selected = (manual_langs or auto_langs or [None])[0]
        if selected:
            try:
                run_command(command + ["--skip-download", "--write-subs" if manual_langs else "--write-auto-subs",
                            "--sub-langs", selected, "--sub-format", "json3", "-o", "caption.%(ext)s", url],
                            work, pulse, min(timeout, 300), 88, 95)
                files = list(work.glob("caption.*.json3"))
                segments = parse_json3(files[0]) if files else []
                if segments:
                    # Do not silently overwrite a user-supplied transcript on re-download.
                    if episode["transcript_source"] != "manual":
                        transcript = {"segments": segments, "language": language,
                                      "source": "youtube_manual" if manual_langs else "youtube_auto"}
                else:
                    warning = "字幕文件没有有效文字，可手动导入或启动语音转写"
            except (MediaError, ValueError, TypeError, KeyError, OSError):
                warning = "字幕获取失败；媒体已保留，可手动导入或启动语音转写"
        else:
            warning = "没有找到对应语言字幕，可手动导入或启动语音转写"
        pulse(97, "保存媒体")
        if mode == "video" and (work / "ready.mp4").stat().st_size > settings.max_file_mb * 1024**2:
            raise MediaError("优化后的 MP4 超过配置的大小限制")
        if (work / "audio.m4a").stat().st_size > settings.max_file_mb * 1024**2:
            raise MediaError("M4A 超过配置的大小限制")
        # Immutable attempt-specific paths prevent an old/cancelled worker from replacing valid media.
        destination = settings.data_dir / "media" / episode["id"] / work.name
        destination.mkdir(parents=True, exist_ok=True)
        if mode == "video":
            shutil.move(str(work / "ready.mp4"), destination / "video.mp4")
        shutil.move(str(work / "audio.m4a"), destination / "audio.m4a")
        updates = {
            "title": info.get("title", episode["title"]), "channel_id": info.get("channel_id") or "",
            "channel_title": info.get("channel") or info.get("uploader") or "",
            "description": info.get("description") or "", "duration": duration,
            "thumbnail": info.get("thumbnail") or "",
            "audio_file": str((destination / "audio.m4a").relative_to(settings.data_dir)),
            "transcript_warning": warning,
        }
        if mode == "video":
            updates["video_file"] = str((destination / "video.mp4").relative_to(settings.data_dir))
        if transcript:
            updates["transcript_source"] = transcript["source"]
        return {"mode": mode, "audio_processing": audio_processing,
                "video_available": mode == "video" or bool(episode["video_file"]), "audio_available": True, "transcript_available": bool(transcript) or episode["transcript_source"] != "none",
                "warning": warning}, updates, transcript


def transcribe(db, job, pulse):
    if not db.settings.whisper_enabled:
        raise MediaError("本地语音转写未启用，请配置 EARSPACE_WHISPER_ENABLED")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise MediaError("请先安装 earspace[transcription] 可选依赖") from None
    episode = db.episode(job["episode_id"])
    if not episode["audio_file"]:
        raise MediaError("请先完成媒体下载")
    pulse(1, "加载语音模型（首次需要下载）")
    model = WhisperModel(db.settings.whisper_model, device="cpu", compute_type="int8", cpu_threads=4)
    segments, _ = model.transcribe(str(db.settings.data_dir / episode["audio_file"]),
                                   language=episode["language"], vad_filter=True, beam_size=3)
    result = []
    for segment in segments:
        if segment.text.strip():
            result.append({"start": segment.start, "end": segment.end, "text": segment.text.strip()})
        pulse(min(99, segment.end / max(1, episode["duration"]) * 100), "语音转写中")
    if not result:
        raise MediaError("没有识别到有效语音")
    transcript = {"segments": result, "source": "whisper", "language": episode["language"]}
    return {"segments": len(result)}, {"transcript_source": "whisper", "transcript_warning": None}, transcript
