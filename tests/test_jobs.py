import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.jobs import LostLease, QueueConflict, cancel, claim, enqueue, fail, finish, heartbeat
from app.worker import consume


def test_queue_deduplication_is_atomic(db, seed):
    video_id = seed()
    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(executor.map(lambda _: enqueue(db, "download", video_id), range(16)))
    assert len({j["id"] for j in jobs}) == 1


def test_download_and_transcription_are_mutually_exclusive(db, seed):
    video_id = seed()
    enqueue(db, "download", video_id)
    with pytest.raises(QueueConflict):
        enqueue(db, "transcribe", video_id)


def test_claim_lease_recovery_and_old_owner_fencing(db, seed):
    video_id = seed()
    job = enqueue(db, "download", video_id)
    first = claim(db, "worker-a")
    assert claim(db, "worker-b") is None
    with db.connect() as c:
        c.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time()-1, job["id"]))
    second = claim(db, "worker-b")
    assert second["attempts"] == 2
    with pytest.raises(LostLease):
        finish(db, first, "worker-a", {}, {"title": "stale"})
    finish(db, second, "worker-b", {"ok": True}, {"title": "fresh"})
    assert db.episode(video_id)["title"] == "fresh"


def test_retry_backoff_and_terminal_failure(db):
    enqueue(db, "refresh")
    first = claim(db, "a")
    fail(db, first, "a", "network failed")
    assert claim(db, "b") is None
    with db.connect() as c:
        c.execute("UPDATE jobs SET available_at=0")
    second = claim(db, "b")
    fail(db, second, "b", "network failed")
    with db.connect() as c:
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "failed"


def test_cancel_fences_running_worker(db):
    queued = enqueue(db, "refresh")
    job = claim(db, "a")
    cancel(db, queued["id"])
    with pytest.raises(LostLease):
        heartbeat(db, job["id"], "a")
    with pytest.raises(LostLease):
        finish(db, job, "a", {"should_not_publish": True})


def test_expired_final_attempt_is_not_requeued(db):
    enqueue(db, "refresh")
    claim(db, "a")
    with db.connect() as c:
        c.execute("UPDATE jobs SET attempts=?,lease_until=0", (db.settings.max_attempts,))
    assert claim(db, "b") is None
    with db.connect() as c:
        assert c.execute("SELECT status FROM jobs").fetchone()[0] == "failed"


def test_actual_worker_subprocess_handles_missing_provider(db, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    monkeypatch.setenv("EARSPACE_API_KEY", db.settings.api_key)
    enqueue(db, "refresh")
    job = claim(db, "test-worker")
    consume(db, job, "test-worker", threading.Event())
    with db.connect() as c:
        row = c.execute("SELECT * FROM jobs").fetchone()
        assert row["status"] == "queued"
        assert "YOUTUBE_API_KEY" in row["error"]
