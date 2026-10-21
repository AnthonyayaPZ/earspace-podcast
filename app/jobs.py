import json
import time
import uuid

from .transcripts import save_transcript


class LostLease(Exception):
    pass


class QueueConflict(Exception):
    pass


def enqueue(db, kind, episode_id=None, payload=None):
    now = time.time()
    with db.connect(immediate=True) as c:
        row = c.execute("SELECT * FROM jobs WHERE kind=? AND episode_id IS ? AND status IN ('queued','running')",
                        (kind, episode_id)).fetchone()
        if row:
            if kind == "download" and json.loads(row["payload"]).get("mode", "video") != (payload or {}).get("mode", "video"):
                raise QueueConflict("该节目已有另一种下载模式的任务，请完成或取消后再切换模式")
            return public_job(row)
        if kind in ("download", "transcribe"):
            other = c.execute("SELECT 1 FROM jobs WHERE episode_id=? AND kind IN ('download','transcribe') AND status IN ('queued','running')",
                              (episode_id,)).fetchone()
            if other:
                raise QueueConflict("此节目已有下载或转写任务，请完成或取消后再操作")
        job_id = uuid.uuid4().hex
        c.execute("""INSERT INTO jobs (id,kind,episode_id,payload,available_at,created_at,updated_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (job_id, kind, episode_id, json.dumps(payload or {}), now, now, now))
        return public_job(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def public_job(row):
    item = dict(row)
    if item["kind"] == "download":
        item["media_mode"] = json.loads(item["payload"]).get("mode", "video")
    for field in ("owner", "lease_until", "payload"):
        item.pop(field, None)
    item["result"] = json.loads(item["result"]) if item["result"] else None
    return item


def claim(db, owner):
    now = time.time()
    with db.connect(immediate=True) as c:
        c.execute("INSERT OR REPLACE INTO workers VALUES (?,?)", (owner, now))
        c.execute("DELETE FROM workers WHERE heartbeat<?", (now-86400,))
        c.execute("""UPDATE jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                     error='Worker 中断，任务租约已过期', stage='等待恢复', owner=NULL, lease_until=NULL,
                     available_at=?,updated_at=? WHERE status='running' AND lease_until<?""",
                  (db.settings.max_attempts, now, now, now))
        row = c.execute("SELECT * FROM jobs WHERE status='queued' AND available_at<=? ORDER BY created_at LIMIT 1",
                        (now,)).fetchone()
        if not row:
            return None
        c.execute("""UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1,
                     error=NULL,stage='开始处理',progress=0,updated_at=? WHERE id=?""",
                  (owner, now + 120, now, row["id"]))
        return dict(c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())


def heartbeat(db, job_id, owner, progress=None, stage=None):
    now = time.time()
    with db.connect() as c:
        result = c.execute("""UPDATE jobs SET lease_until=?,updated_at=?,progress=COALESCE(?,progress),
                              stage=COALESCE(?,stage) WHERE id=? AND owner=? AND status='running'""",
                           (now+120, now, progress, stage, job_id, owner))
        if not result.rowcount:
            raise LostLease("任务已取消或由其他 Worker 接管")
        c.execute("INSERT OR REPLACE INTO workers VALUES (?,?)", (owner, now))


def fail(db, job, owner, message, retry=True):
    now = time.time()
    status = "queued" if retry and job["attempts"] < db.settings.max_attempts else "failed"
    with db.connect() as c:
        c.execute("""UPDATE jobs SET status=?,error=?,stage=?,owner=NULL,lease_until=NULL,
                     available_at=?,updated_at=? WHERE id=? AND owner=? AND status='running'""",
                  (status, message[:1500], "等待重试" if status == "queued" else "处理失败",
                   now + 30 * job["attempts"], now, job["id"], owner))


def finish(db, job, owner, result, episode_updates=None, transcript=None):
    now = time.time()
    with db.connect(immediate=True) as c:
        changed = c.execute("""UPDATE jobs SET status='succeeded',progress=100,stage='完成',result=?,
                              updated_at=?,owner=NULL,lease_until=NULL WHERE id=? AND owner=? AND status='running'""",
                            (json.dumps(result, ensure_ascii=False), now, job["id"], owner))
        if not changed.rowcount:
            raise LostLease()
        if episode_updates:
            # Internal allowlist, never interpolate caller-supplied field names.
            allowed = {"title", "channel_title", "channel_id", "duration", "description", "thumbnail",
                       "video_file", "audio_file", "transcript_source", "transcript_warning"}
            values = {k: v for k, v in episode_updates.items() if k in allowed}
            if values:
                c.execute("UPDATE episodes SET " + ",".join(f"{k}=?" for k in values) + ",updated_at=? WHERE id=?",
                          (*values.values(), now, job["episode_id"]))
        if transcript:
            save_transcript(c, job["episode_id"], transcript)


def cancel(db, job_id):
    with db.connect(immediate=True) as c:
        c.execute("""UPDATE jobs SET status='cancelled',stage='已取消',owner=NULL,lease_until=NULL,updated_at=?
                     WHERE id=? AND status IN ('queued','running')""", (time.time(), job_id))
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return public_job(row) if row else None
