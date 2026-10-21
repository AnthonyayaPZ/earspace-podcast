import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import pytest

from app.jobs import LostLease, claim, enqueue, finish
from app.media import MediaError, download, run_command, youtube_command
from app.transcripts import parse_json3


def test_json3_preserves_source_and_repeated_speech(tmp_path):
    path = tmp_path / "caption.json3"
    path.write_text(json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hello &amp; world"}]},
        {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "Hello &amp; world"}]},
        {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "Next sentence"}]},
        {"tStartMs": 3000, "segs": [{"utf8": "\n"}]}]}), encoding="utf-8")
    result = parse_json3(path)
    assert len(result) == 3
    assert result[0] == {"start": 0, "end": 1, "text": "Hello & world"}
    assert result[1]["text"] == result[0]["text"]  # A repeated utterance is not a rolling revision.


def test_subprocess_timeout_and_cancellation(tmp_path):
    start = time.monotonic()
    with pytest.raises(MediaError, match="超时"):
        run_command([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, lambda *_: None, .2)
    assert time.monotonic() - start < 10
    def cancel(*_):
        raise LostLease()
    with pytest.raises(LostLease):
        run_command([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, cancel, 30)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg")
@pytest.mark.parametrize("with_cookies", [False, True])
def test_media_pipeline_with_real_ffmpeg(db, seed, tmp_path, monkeypatch, with_cookies):
    """Only YouTube transport is simulated; MP4/audio processing uses real FFmpeg."""
    from app import media
    cookie_source = tmp_path / "supplied.cookies.txt"
    cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfixture-cookie-secret\n"
    if with_cookies:
        cookie_source.write_text(cookie_text, encoding="utf-8")
        db.settings = replace(db.settings, youtube_cookies_file=str(cookie_source))
    cookie_paths = []
    video_id = seed()
    fixture = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(fixture)],
                   check=True, capture_output=True, timeout=30)
    real_run = media.run_command
    def simulated_youtube(command, directory, pulse, timeout, *args):
        directory = Path(directory)
        if "yt_dlp" not in command:
            return real_run(command, directory, pulse, timeout, *args)
        if with_cookies:
            cookie_path = Path(command[command.index("--cookies") + 1])
            assert cookie_path.parent == directory and cookie_path != cookie_source
            assert "fixture-cookie-secret" in cookie_path.read_text(encoding="utf-8")
            cookie_paths.append(cookie_path)
        else:
            assert "--cookies" not in command
        if "--write-info-json" in command:
            (directory / "source.info.json").write_text(json.dumps({
                "title": "Transport fixture", "duration": 2,
                "channel": "Test", "subtitles": {"en": [{"ext": "json3"}]}}), encoding="utf-8")
        elif "--write-subs" in command:
            (directory / "caption.en.json3").write_text(json.dumps({"events": [
                {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Test speech"}]}]}), encoding="utf-8")
        else:
            shutil.copy(fixture, directory / "video.mp4")
    monkeypatch.setattr(media, "run_command", simulated_youtube)
    enqueue(db, "download", video_id)
    job = claim(db, "media-test")
    result, updates, transcript = download(db, job, lambda *_: None)
    finish(db, job, "media-test", result, updates, transcript)
    row = db.episode(video_id)
    assert row["transcript_source"] == "youtube_manual"
    for key in ("audio_file", "video_file"):
        path = db.settings.data_dir / row[key]
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                               check=True, capture_output=True, timeout=30)
        assert 1.8 < float(json.loads(probe.stdout)["format"]["duration"]) < 2.5
    assert transcript["segments"][0]["text"] == "Test speech"
    if with_cookies:
        assert len(cookie_paths) == 3 and len(set(cookie_paths)) == 1
        assert not cookie_paths[0].exists()
        assert cookie_source.read_text(encoding="utf-8") == cookie_text
        assert not list((db.settings.data_dir / "media").rglob("*cookies*"))
        assert "fixture-cookie-secret" not in json.dumps((result, updates, transcript))


def test_cookies_are_filtered_and_source_is_not_modified(settings, tmp_path):
    source = tmp_path / "browser.cookies.txt"
    original = ("# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tyoutube-secret\n"
                ".example.com\tTRUE\t/\tTRUE\t2147483647\tSESSION\tother-site-secret\n")
    source.write_text(original, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    command = youtube_command(replace(settings, youtube_cookies_file=str(source)), work)
    private = Path(command[command.index("--cookies")+1])
    jar = MozillaCookieJar(str(private))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert [(cookie.domain, cookie.value) for cookie in jar] == [(".youtube.com", "youtube-secret")]
    assert "other-site-secret" not in private.read_text(encoding="utf-8")
    private.write_text("updated session", encoding="utf-8")
    assert source.read_text(encoding="utf-8") == original
    if sys.platform != "win32":
        assert private.stat().st_mode & 0o777 == 0o600


def test_cancelled_attempt_cookies_are_removed_without_touching_a_new_owner(db, tmp_path):
    from app.worker import cleanup_cookie_files
    work = db.settings.data_dir / "work"
    old = work / "job-id-1-old" / ".youtube-cookies.txt"
    current = work / "job-id-2-current" / ".youtube-cookies.txt"
    other = work / "other-job-1-current" / ".youtube-cookies.txt"
    source = tmp_path / "supplied.cookies.txt"
    for path in (old, current, other, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("credential", encoding="utf-8")
    cleanup_cookie_files(db, {"id": "job-id", "attempts": 1})
    assert not old.exists()
    assert all(path.read_text(encoding="utf-8") == "credential" for path in (current, other, source))


@pytest.mark.parametrize("content", [None, "invalid-cookie-secret", "# Netscape HTTP Cookie File\n",
    "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t2147483647\tSESSION\tother-site-secret\n"])
def test_cookie_configuration_errors_are_safe_and_not_retried(settings, tmp_path, content):
    source = tmp_path / "private-credentials.txt"
    if content is not None:
        source.write_text(content, encoding="utf-8")
    with pytest.raises(MediaError) as error:
        youtube_command(replace(settings, youtube_cookies_file=str(source)), tmp_path)
    assert error.value.retry is False
    assert "EARSPACE_YOUTUBE_COOKIES_FILE" in str(error.value)
    assert str(source) not in str(error.value)
    assert "other-site-secret" not in str(error.value)
    assert "invalid-cookie-secret" not in str(error.value)


def test_login_errors_fail_once_without_leaking_provider_output(db, seed, tmp_path, monkeypatch):
    from app import runner
    code = "import sys; print('ERROR: Sign in to confirm you are not a bot; secret=value'); sys.exit(1)"
    with pytest.raises(MediaError) as error:
        run_command([sys.executable, "-c", code], tmp_path, lambda *_: None, 10)
    assert error.value.retry is False
    assert "secret=value" not in str(error.value)
    def login_required(*args):
        raise error.value
    monkeypatch.setattr(runner, "download", login_required)
    enqueue(db, "download", seed(), {"mode": "audio"})
    job = claim(db, "login-test")
    runner.execute(db, job, "login-test")
    with db.connect() as connection:
        row = connection.execute("SELECT status, attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert dict(row) == {"status": "failed", "attempts": 1}


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg")
@pytest.mark.parametrize("existing_video", [False, True])
@pytest.mark.parametrize("input_codec", ["aac", "libopus"])
def test_audio_only_download_uses_audio_track_and_preserves_video(db, seed, tmp_path, monkeypatch, existing_video, input_codec):
    from app import media
    video_id = seed()
    previous_video = None
    if existing_video:
        previous_video = "media/previous/video.mp4"
        path = db.settings.data_dir / previous_video
        path.parent.mkdir(parents=True)
        path.write_bytes(b"existing cached video")
        with db.connect() as c:
            c.execute("UPDATE episodes SET video_file=? WHERE id=?", (previous_video, video_id))
    extension = ".m4a" if input_codec == "aac" else ".webm"
    fixture = tmp_path / ("fixture" + extension)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", input_codec, str(fixture)], capture_output=True, check=True, timeout=30)
    real_run, downloads = media.run_command, []
    def transport(command, directory, pulse, timeout, *args):
        directory = Path(directory)
        if "yt_dlp" not in command:
            return real_run(command, directory, pulse, timeout, *args)
        if "--write-info-json" in command:
            (directory / "source.info.json").write_text(json.dumps({"title": "Audio test", "duration": 2}), encoding="utf-8")
        else:
            downloads.append(command)
            assert command[command.index("-f")+1] == "ba[ext=m4a]/ba"
            assert "--merge-output-format" not in command
            shutil.copy(fixture, directory / ("source_audio" + extension))
    monkeypatch.setattr(media, "run_command", transport)
    enqueue(db, "download", video_id, {"mode": "audio"})
    job = claim(db, "audio-test")
    result, updates, transcript = download(db, job, lambda *_: None)
    finish(db, job, "audio-test", result, updates, transcript)
    row = db.episode(video_id)
    assert len(downloads) == 1
    assert row["video_file"] == previous_video
    assert result["mode"] == "audio"
    assert result["video_available"] is existing_video
    assert "video_file" not in updates
    assert result["warning"] and transcript is None
    audio = db.settings.data_dir / row["audio_file"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(audio)],
                           capture_output=True, check=True, timeout=30)
    streams = json.loads(probe.stdout)["streams"]
    assert len(streams) == 1 and streams[0] == {"codec_name": "aac", "codec_type": "audio"}
    assert result["audio_processing"] == ("stream_copy" if input_codec == "aac" else "transcoded")
    if input_codec == "aac":
        def packet_hashes(path):
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
                                    "-show_entries", "packet=data_hash", "-show_data_hash", "sha256", "-of", "json", str(path)],
                                   capture_output=True, check=True, timeout=30)
            return [p["data_hash"] for p in json.loads(probe.stdout)["packets"]]
        # A stream copy preserves the compressed packets, unlike decode/re-encode.
        assert packet_hashes(fixture) == packet_hashes(audio)
