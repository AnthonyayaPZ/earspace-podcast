import os
import time

from app.jobs import enqueue
from app.maintenance import collect


def test_pruning_preserves_references_recent_files_and_active_work(db, seed):
    video_id = seed()
    root = db.settings.data_dir
    media = root / "media" / video_id / "old-attempt"
    media.mkdir(parents=True)
    kept = media / "video.mp4"
    kept.write_bytes(b"referenced")
    orphan = media / "audio.m4a"
    orphan.write_bytes(b"unused")
    recent = media / "new.mp4"
    recent.write_bytes(b"recent")
    old = time.time()-2*86400
    os.utime(kept, (old, old)); os.utime(orphan, (old, old))
    with db.connect() as c:
        c.execute("UPDATE episodes SET video_file=? WHERE id=?", (str(kept.relative_to(root)), video_id))
    job = enqueue(db, "download", video_id)
    active = root / "work" / (job["id"] + "-attempt")
    active.mkdir(parents=True)
    (active / "part").write_bytes(b"in use")
    os.utime(active, (old, old))
    abandoned = root / "work" / "abandoned"
    abandoned.mkdir(); (abandoned / "part").write_bytes(b"old")
    os.utime(abandoned, (old, old))
    report = collect(db)
    assert len(report["artifacts"]) == 2
    assert orphan.exists()
    collect(db, prune=True)
    assert not orphan.exists() and not abandoned.exists()
    assert kept.exists() and recent.exists() and active.exists()
