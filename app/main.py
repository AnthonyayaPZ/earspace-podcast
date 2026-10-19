import hashlib
import hmac
import json
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database, public_episode
from .jobs import QueueConflict, cancel, enqueue, public_job
from .models import FeedbackRequest, ImportRequest, Language, Profile, ProgressRequest, TranscriptRequest, youtube_id
from .recommend import recommendations, speech_estimate
from .transcripts import vtt

security = HTTPBearer(auto_error=False)


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        db.initialize()
        yield

    app = FastAPI(title="Earspace · 双语播客服务", version="0.1.0", lifespan=lifespan,
                  description="单用户自托管 API。先设置偏好，再刷新推荐；后台 Worker 执行发现、下载与可选转写。")
    app.state.db = db
    web = Path(__file__).parent / "web"
    if web.is_dir():
        app.mount("/assets", StaticFiles(directory=web), name="assets")

        @app.get("/", include_in_schema=False)
        def web_home():
            return FileResponse(web / "index.html")

    @app.exception_handler(QueueConflict)
    async def queue_conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "Range"],
                       expose_headers=["Content-Range", "Accept-Ranges", "Content-Disposition"],
                       allow_credentials=False)

    @app.middleware("http")
    async def limit_body(request, call_next):
        # Enforce a real streamed body limit, even without Content-Length.
        if request.method in ("POST", "PUT", "PATCH"):
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    return JSONResponse(status_code=413, content={"detail": "请求体不得超过 8 MB"})
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(
                credentials.credentials.encode(), settings.api_key.encode()):
            raise HTTPException(401, "需要有效的 Bearer API Key", headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(authenticated)]

    def require_episode(video_id):
        if len(video_id) != 11:
            raise HTTPException(404, "节目不存在")
        row = db.episode(video_id)
        if not row:
            raise HTTPException(404, "节目不存在")
        return row

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/v1/status", dependencies=protected, tags=["system"])
    def status():
        with db.connect() as c:
            row = c.execute("SELECT MAX(updated_at) FROM jobs WHERE status='running'").fetchone()
            active = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            online = c.execute("SELECT COUNT(*) FROM workers WHERE heartbeat>?", (time.time()-60,)).fetchone()[0]
        return {"youtube_discovery_configured": bool(settings.youtube_key),
                "ffmpeg_available": bool(shutil.which("ffmpeg")),
                "ffprobe_available": bool(shutil.which("ffprobe")),
                "node_available": bool(shutil.which("node")),
                "whisper_enabled": settings.whisper_enabled,
                "free_disk_mb": shutil.disk_usage(settings.data_dir).free // 1024**2,
                "active_jobs": active, "last_running_job_heartbeat": row[0],
                "workers_online": online, "worker_note": "独立启动 python -m app.worker"}

    @app.get("/api/v1/preferences", dependencies=protected, tags=["preferences"])
    def get_preferences():
        return db.profile()

    @app.put("/api/v1/preferences", dependencies=protected, tags=["preferences"])
    def save_preferences(profile: Profile):
        data = profile.model_dump()
        with db.connect() as c:
            c.execute("UPDATE profile SET data=? WHERE id=1", (json.dumps(data, ensure_ascii=False),))
        return data

    @app.post("/api/v1/discovery/refresh", status_code=202, dependencies=protected, tags=["discovery"])
    def refresh():
        if not settings.youtube_key:
            raise HTTPException(503, "请先配置 YOUTUBE_API_KEY 并启用 YouTube Data API v3")
        return enqueue(db, "refresh")

    @app.get("/api/v1/recommendations", dependencies=protected, tags=["discovery"])
    def get_recommendations(language: Language, limit: int = Query(12, ge=1, le=50),
                            offset: int = Query(0, ge=0, le=10000),
                            context: Literal["gym", "daily", "practice"] = "gym",
                            minutes: int | None = Query(None, ge=5, le=240)):
        return recommendations(db, language, limit, offset, context, minutes)

    @app.post("/api/v1/episodes/import", status_code=202, dependencies=protected, tags=["episodes"])
    def import_episode(body: ImportRequest):
        video_id = youtube_id(body.url)
        existing = db.episode(video_id)
        if existing and existing["language"] != body.language:
            with db.connect() as c:
                active = c.execute("SELECT 1 FROM jobs WHERE episode_id=? AND status IN ('queued','running')",
                                   (video_id,)).fetchone()
            if active:
                raise HTTPException(409, "请先完成或取消该节目的任务，再修改语言")
            # User's explicit language takes precedence over discovery inference.
            with db.connect() as c:
                c.execute("UPDATE episodes SET language=?,language_source='user',updated_at=? WHERE id=?",
                          (body.language, time.time(), video_id))
        if not existing:
            db.upsert_episode({"id": video_id, "title": f"YouTube · {video_id}", "language": body.language})
        with db.connect() as c:
            c.execute("UPDATE episodes SET library=1,language_source='user' WHERE id=?", (video_id,))
        job = enqueue(db, "download", video_id, {"mode": body.mode}) if body.download else None
        return {"episode": public_episode(require_episode(video_id)), "job": job}

    @app.get("/api/v1/episodes", dependencies=protected, tags=["episodes"])
    def list_episodes(language: Language | None = None, library: bool = True,
                      limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        where, args = "library=?", [int(library)]
        if language:
            where += " AND language=?"
            args.append(language)
        with db.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM episodes WHERE {where}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM episodes WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                             (*args, limit, offset)).fetchall()
        return {"items": [public_episode(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/episodes/{video_id}", dependencies=protected, tags=["episodes"])
    def get_episode(video_id: str):
        return public_episode(require_episode(video_id))

    @app.put("/api/v1/episodes/{video_id}/library", dependencies=protected, tags=["episodes"])
    def add_to_library(video_id: str):
        require_episode(video_id)
        with db.connect() as c:
            c.execute("UPDATE episodes SET library=1 WHERE id=?", (video_id,))
        return {"library": True}

    @app.delete("/api/v1/episodes/{video_id}/library", dependencies=protected, tags=["episodes"])
    def remove_from_library(video_id: str):
        require_episode(video_id)
        with db.connect() as c:
            c.execute("UPDATE episodes SET library=0 WHERE id=?", (video_id,))
        return {"library": False, "note": "仅移出待听列表，保留文件和进度"}

    @app.post("/api/v1/episodes/{video_id}/download", status_code=202, dependencies=protected, tags=["jobs"])
    def start_download(video_id: str, mode: Literal["audio", "video"] = "audio"):
        require_episode(video_id)
        with db.connect() as c:
            c.execute("UPDATE episodes SET library=1 WHERE id=?", (video_id,))
        return enqueue(db, "download", video_id, {"mode": mode})

    @app.post("/api/v1/episodes/{video_id}/transcribe", status_code=202, dependencies=protected, tags=["jobs"])
    def start_transcription(video_id: str):
        episode = require_episode(video_id)
        if not settings.whisper_enabled:
            raise HTTPException(503, "转写未启用，请安装可选依赖并配置 EARSPACE_WHISPER_ENABLED=true")
        if not episode["audio_file"]:
            raise HTTPException(409, "请先完成媒体下载")
        return enqueue(db, "transcribe", video_id)

    @app.get("/api/v1/jobs", dependencies=protected, tags=["jobs"])
    def list_jobs(status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None,
                  limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        where, args = ("WHERE status=?", [status]) if status else ("", [])
        with db.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM jobs {where}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM jobs {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                             (*args, limit, offset)).fetchall()
        return {"items": [public_job(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/api/v1/jobs/{job_id}", dependencies=protected, tags=["jobs"])
    def get_job(job_id: str):
        with db.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        return public_job(row)

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=protected, tags=["jobs"])
    def cancel_job(job_id: str):
        row = cancel(db, job_id)
        if row is None:
            raise HTTPException(404, "任务不存在")
        return row

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202, dependencies=protected, tags=["jobs"])
    def retry_job(job_id: str):
        with db.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] not in ("failed", "cancelled"):
            raise HTTPException(409, "仅失败或取消的任务可以重试")
        return enqueue(db, row["kind"], row["episode_id"], json.loads(row["payload"]))

    @app.put("/api/v1/episodes/{video_id}/feedback", dependencies=protected, tags=["listening"])
    def set_feedback(video_id: str, body: FeedbackRequest):
        require_episode(video_id)
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?)",
                      (video_id, body.kind, body.value, time.time()))
        return body

    @app.get("/api/v1/episodes/{video_id}/feedback", dependencies=protected, tags=["listening"])
    def get_feedback(video_id: str):
        require_episode(video_id)
        with db.connect() as c:
            return {r["kind"]: r["value"] for r in c.execute("SELECT kind,value FROM feedback WHERE episode_id=?", (video_id,))}

    @app.delete("/api/v1/episodes/{video_id}/feedback", dependencies=protected, tags=["listening"])
    def clear_feedback(video_id: str):
        require_episode(video_id)
        with db.connect() as c:
            c.execute("DELETE FROM feedback WHERE episode_id=?", (video_id,))
        return {"cleared": True}

    @app.put("/api/v1/episodes/{video_id}/progress", dependencies=protected, tags=["listening"])
    def save_progress(video_id: str, body: ProgressRequest):
        episode = require_episode(video_id)
        if episode["duration"] and body.position > episode["duration"] + 5:
            raise HTTPException(422, "播放位置超出节目时长")
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?,?,?)",
                      (video_id, body.position, int(body.completed), body.playback_rate, time.time()))
        return body

    @app.get("/api/v1/episodes/{video_id}/progress", dependencies=protected, tags=["listening"])
    def get_progress(video_id: str):
        require_episode(video_id)
        with db.connect() as c:
            row = c.execute("SELECT * FROM progress WHERE episode_id=?", (video_id,)).fetchone()
        return dict(row) if row else {"episode_id": video_id, "position": 0, "completed": False, "playback_rate": 1}

    @app.get("/api/v1/listening/continue", dependencies=protected, tags=["listening"])
    def continue_listening(limit: int = Query(12, ge=1, le=50)):
        with db.connect() as c:
            rows = c.execute("""SELECT e.*,p.position,p.playback_rate,p.updated_at AS last_played_at FROM episodes e
                                JOIN progress p ON e.id=p.episode_id WHERE p.completed=0 AND p.position>0
                                ORDER BY p.updated_at DESC LIMIT ?""", (limit,)).fetchall()
        return {"items": [public_episode(r) for r in rows]}

    @app.put("/api/v1/episodes/{video_id}/transcript", dependencies=protected, tags=["transcripts"])
    def upload_transcript(video_id: str, body: TranscriptRequest):
        require_episode(video_id)
        with db.connect(immediate=True) as c:
            if c.execute("SELECT 1 FROM jobs WHERE episode_id=? AND kind IN ('download','transcribe') AND status IN ('queued','running')",
                         (video_id,)).fetchone():
                raise HTTPException(409, "请等待媒体任务完成或取消后再导入文稿")
            c.execute("INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?)",
                      (video_id, json.dumps([s.model_dump() for s in body.segments], ensure_ascii=False), "manual", body.language))
            c.execute("UPDATE episodes SET transcript_source='manual',transcript_warning=NULL WHERE id=?", (video_id,))
        return {"source": "manual", "segments": len(body.segments)}

    def transcript_data(video_id):
        require_episode(video_id)
        with db.connect() as c:
            row = c.execute("SELECT * FROM transcripts WHERE episode_id=?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "文稿尚未生成，可导入文稿或启动转写")
        return {"episode_id": video_id, "language": row["language"], "source": row["source"],
                "segments": json.loads(row["segments"])}

    @app.get("/api/v1/episodes/{video_id}/transcript", dependencies=protected, tags=["transcripts"])
    def get_transcript(video_id: str):
        result = transcript_data(video_id)
        if result["language"] == "en":
            result["difficulty"] = speech_estimate(result["segments"])
        return result

    @app.get("/api/v1/episodes/{video_id}/transcript/export", dependencies=protected, tags=["transcripts"])
    def export_transcript(video_id: str, format: Literal["vtt", "txt"] = "vtt"):
        data = transcript_data(video_id)
        content = vtt(data["segments"]) if format == "vtt" else "\n".join(s["text"] for s in data["segments"])
        return PlainTextResponse(content, media_type="text/vtt" if format == "vtt" else "text/plain",
                                 headers={"Content-Disposition": f'attachment; filename="{video_id}.{format}"'})

    def media_path(video_id, kind):
        episode = require_episode(video_id)
        relative = episode[f"{kind}_file"]
        if not relative:
            raise HTTPException(409, "媒体尚未准备好，请创建下载任务并查询进度")
        path = (settings.data_dir / relative).resolve()
        if not path.is_relative_to((settings.data_dir / "media").resolve()) or not path.is_file():
            raise HTTPException(404, "媒体文件不存在，请重新下载")
        return path

    @app.post("/api/v1/episodes/{video_id}/playback-token", dependencies=protected, tags=["media"])
    def playback_token(video_id: str, kind: Literal["video", "audio"] = "audio"):
        media_path(video_id, kind)
        expires = int(time.time()) + 12 * 3600
        signature = hmac.new(settings.api_key.encode(), f"{video_id}:{kind}:{expires}".encode(), hashlib.sha256).hexdigest()
        return {"url": f"/api/v1/episodes/{video_id}/stream/{kind}?expires={expires}&signature={signature}",
                "expires_at": expires, "note": "临时播放链接可直接用于 audio/video 标签，请勿公开分享"}

    @app.head("/api/v1/episodes/{video_id}/stream/{kind}", include_in_schema=False)
    @app.get("/api/v1/episodes/{video_id}/stream/{kind}", tags=["media"])
    def stream(video_id: str, kind: Literal["video", "audio"], expires: int,
               signature: str = Query(pattern=r"^[a-f0-9]{64}$")):
        now = int(time.time())
        expected = hmac.new(settings.api_key.encode(), f"{video_id}:{kind}:{expires}".encode(), hashlib.sha256).hexdigest()
        if expires < now or expires > now + 12 * 3600 + 60 or not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "播放链接已过期或无效")
        return FileResponse(media_path(video_id, kind), media_type="video/mp4" if kind == "video" else "audio/mp4")

    @app.head("/api/v1/episodes/{video_id}/media/{kind}", dependencies=protected, include_in_schema=False)
    @app.get("/api/v1/episodes/{video_id}/media/{kind}", dependencies=protected, tags=["media"])
    def get_media(video_id: str, kind: Literal["video", "audio"], download: bool = False):
        return FileResponse(media_path(video_id, kind), media_type="video/mp4" if kind == "video" else "audio/mp4",
                            filename=f"{video_id}.{'mp4' if kind == 'video' else 'm4a'}" if download else None)

    return app


app = create_app()
