import hashlib
import hmac
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import youtube_id


def test_health_and_auth(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/v1/preferences", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/v1/preferences", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/api/v1/preferences").status_code == 200
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["HTTPBearer"]


def test_cookie_status_never_exposes_credential_path(settings):
    path = "/private/server/youtube.cookies.txt"
    with TestClient(create_app(replace(settings, youtube_cookies_file=path))) as client:
        response = client.get("/api/v1/status", headers={"Authorization": f"Bearer {settings.api_key}"})
    assert response.json()["youtube_cookies_configured"] is True
    assert path not in response.text


def test_web_test_client_is_served(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "耳间" in home.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/../config.py").status_code == 404


def test_refuses_unconfigured_secret(tmp_path):
    with pytest.raises(RuntimeError, match="EARSPACE_API_KEY"):
        with TestClient(create_app(Settings(data_dir=tmp_path, api_key=""))):
            pass


@pytest.mark.parametrize("url", ["abcdefghijk", "https://youtu.be/abcdefghijk?t=10",
    "https://www.youtube.com/watch?v=abcdefghijk&list=other", "https://youtube.com/shorts/abcdefghijk",
    "https://m.youtube.com/live/abcdefghijk"])
def test_youtube_canonicalization(url):
    assert youtube_id(url) == "abcdefghijk"


@pytest.mark.parametrize("url", ["http://youtube.com/watch?v=abcdefghijk", "https://youtube.com.evil.test/watch?v=abcdefghijk",
    "https://127.0.0.1/watch?v=abcdefghijk", "file:///etc/passwd", "https://youtube.com/playlist?list=abc",
    "https://youtube.com:8080/watch?v=abcdefghijk", "https://evil@youtube.com/watch?v=abcdefghijk",
    "--exec dangerous", "https://youtube.com/watch?v=short", "https://youtu.be/../../etc/passwd"])
def test_import_rejects_arbitrary_network_targets(client, url):
    assert client.post("/api/v1/episodes/import", json={"url": url, "language": "en"}).status_code == 422


def test_preferences_roundtrip(client):
    profile = {"interests": ["AI", "历史"], "preferred_channels": ["UC" + "a" * 22],
               "english_level": "beginner", "session_minutes": 30, "discovery_ratio": .3}
    assert client.put("/api/v1/preferences", json=profile).json() == profile
    assert client.get("/api/v1/preferences").json() == profile
    profile["interests"] = [" "]
    assert client.put("/api/v1/preferences", json=profile).status_code == 422


def test_discovery_unconfigured_is_explicit(client):
    assert client.post("/api/v1/discovery/refresh").status_code == 503
    body = client.get("/api/v1/recommendations?language=zh").json()
    assert body["items"] == []
    assert body["algorithm"] == "explainable_rules_v1"


def test_import_and_deduplicate_jobs(client):
    data = {"url": "https://youtu.be/abcdefghijk", "language": "en"}
    first = client.post("/api/v1/episodes/import", json=data)
    second = client.post("/api/v1/episodes/import", json=data)
    assert first.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]
    assert client.get("/api/v1/episodes").json()["total"] == 1
    assert "audio_file" not in first.json()["episode"]
    assert client.get("/api/v1/jobs").json()["total"] == 1
    assert first.json()["job"]["media_mode"] == "audio"


def test_download_modes_are_validated_and_conflicting_requests_are_explicit(client, seed):
    video_id = seed()
    base = f"/api/v1/episodes/{video_id}/download"
    assert client.post(base + "?mode=invalid").status_code == 422
    first = client.post(base)
    assert first.status_code == 202 and first.json()["media_mode"] == "audio"
    assert client.post(base + "?mode=video").status_code == 409
    client.post(f"/api/v1/jobs/{first.json()['id']}/cancel")
    video = client.post(base + "?mode=video")
    assert video.status_code == 202 and video.json()["media_mode"] == "video"


def test_video_import_mode_is_preserved_on_retry(client):
    body = {"url": "abcdefghijk", "language": "zh", "mode": "video"}
    response = client.post("/api/v1/episodes/import", json=body)
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["media_mode"] == "video"
    client.post(f"/api/v1/jobs/{job['id']}/cancel")
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").json()["media_mode"] == "video"


def test_cancel_and_retry(client):
    job = client.post("/api/v1/episodes/import", json={"url": "abcdefghijk", "language": "en"}).json()["job"]
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry").json()
    assert retry["id"] != job["id"]
    assert retry["status"] == "queued"
    assert client.get("/api/v1/jobs/missing").status_code == 404


def test_progress_and_continue(client, seed):
    video_id = seed()
    url = f"/api/v1/episodes/{video_id}/progress"
    assert client.get(url).json()["position"] == 0
    assert client.put(url, json={"position": 300, "playback_rate": 1.25}).status_code == 200
    items = client.get("/api/v1/listening/continue").json()["items"]
    assert items[0]["position"] == 300
    assert client.put(url, json={"position": 99999}).status_code == 422
    assert client.put(url, json={"position": 2700, "completed": True}).status_code == 200
    assert client.get("/api/v1/listening/continue").json()["items"] == []


def test_feedback_validation_and_reset(client, seed):
    video_id = seed()
    url = f"/api/v1/episodes/{video_id}/feedback"
    assert client.put(url, json={"kind": "reaction", "value": "too_hard"}).status_code == 422
    assert client.put(url, json={"kind": "reaction", "value": "like"}).status_code == 200
    assert client.get(url).json() == {"reaction": "like"}
    client.delete(url)
    assert client.get(url).json() == {}


def test_manual_transcript_and_exports(client, seed):
    video_id = seed()
    url = f"/api/v1/episodes/{video_id}/transcript"
    assert client.get(url).status_code == 404
    data = {"language": "en", "segments": [{"start": 0, "end": 2.5, "text": "Hello <everyone>"},
                                            {"start": 2.5, "end": 5, "text": "Welcome."}]}
    assert client.put(url, json=data).status_code == 200
    assert client.get(url).json()["source"] == "manual"
    exported = client.get(url + "/export?format=vtt")
    assert "00:00:00.000 --> 00:00:02.500" in exported.text
    assert "&lt;everyone&gt;" in exported.text
    assert client.get(url + "/export?format=txt").text == "Hello <everyone>\nWelcome."
    data["segments"][1]["end"] = 1
    assert client.put(url, json=data).status_code == 422


def test_transcript_conflict_and_disabled_whisper(client):
    client.post("/api/v1/episodes/import", json={"url": "abcdefghijk", "language": "en"})
    data = {"language": "en", "segments": [{"start": 0, "end": 1, "text": "hi"}]}
    assert client.put("/api/v1/episodes/abcdefghijk/transcript", json=data).status_code == 409
    assert client.post("/api/v1/episodes/abcdefghijk/transcribe").status_code == 503


def test_media_auth_ranges_and_signed_links(client, db, seed, settings):
    video_id = seed()
    path = settings.data_dir / "media" / video_id / "audio.m4a"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    with db.connect() as c:
        c.execute("UPDATE episodes SET audio_file=? WHERE id=?", (str(path.relative_to(settings.data_dir)), video_id))
    base = f"/api/v1/episodes/{video_id}"
    response = client.get(base + "/media/audio", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert client.get(base + "/media/audio", headers={"Authorization": ""}).status_code == 401
    assert client.head(base + "/media/audio").status_code == 200
    token = client.post(base + "/playback-token?kind=audio").json()
    assert settings.api_key not in token["url"]
    assert client.get(token["url"], headers={"Authorization": ""}).content == b"0123456789"
    assert client.get(token["url"].replace("/audio?", "/video?")).status_code == 401
    expiry = int(time.time())-1
    sig = hmac.new(settings.api_key.encode(), f"{video_id}:audio:{expiry}".encode(), hashlib.sha256).hexdigest()
    assert client.get(base + f"/stream/audio?expires={expiry}&signature={sig}").status_code == 401
    assert client.get(base + "/media/video").status_code == 409


def test_media_path_cannot_escape_storage(client, db, seed, tmp_path):
    video_id = seed()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with db.connect() as c:
        c.execute("UPDATE episodes SET audio_file=? WHERE id=?", (str(outside), video_id))
    assert client.get(f"/api/v1/episodes/{video_id}/media/audio").status_code == 404


def test_cors(client):
    headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PUT",
               "Access-Control-Request-Headers": "Authorization,Content-Type"}
    result = client.options("/api/v1/preferences", headers=headers)
    assert result.headers["access-control-allow-origin"] == "http://localhost:5173"
    headers["Origin"] = "https://untrusted.test"
    assert "access-control-allow-origin" not in client.options("/api/v1/preferences", headers=headers).headers


def test_actual_body_size_limit(client):
    response = client.put("/api/v1/preferences", content=b"x" * (8 * 1024 * 1024 + 1))
    assert response.status_code == 413
