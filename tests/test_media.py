import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.jobs import LostLease, claim, enqueue, finish
from app.media import MediaError, download, run_command
from app.transcripts import parse_json3


def test_json3_duplicate_cues(tmp_path):
    path = tmp_path / "caption.json3"
    path.write_text(json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "Hello &amp; world"}]},
        {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "Hello &amp; world"}]},
        {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "Next sentence"}]},
        {"tStartMs": 3000, "segs": [{"utf8": "\n"}]}]}), encoding="utf-8")
    result = parse_json3(path)
    assert len(result) == 2
    assert result[0] == {"start": 0, "end": 2, "text": "Hello & world"}


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
def test_media_pipeline_with_real_ffmpeg(db, seed, tmp_path, monkeypatch):
    """Only YouTube transport is simulated; MP4/audio processing uses real FFmpeg."""
    from app import media
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


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Requires FFmpeg")
@pytest.mark.parametrize("existing_video", [False, True])
def test_audio_only_download_uses_audio_track_and_preserves_video(db, seed, tmp_path, monkeypatch, existing_video):
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
    fixture = tmp_path / "fixture.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", str(fixture)], capture_output=True, check=True, timeout=30)
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
            shutil.copy(fixture, directory / "source_audio.m4a")
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
