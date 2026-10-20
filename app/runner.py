"""Internal isolated job entry point; launched by app.worker."""
import sys

from .config import Settings
from .db import Database
from .jobs import LostLease, fail, finish, heartbeat
from .media import MediaError, download, transcribe
from .youtube import ProviderError, YouTube


def execute(db, job, owner):
    def pulse(progress=None, stage=None):
        heartbeat(db, job["id"], owner, progress, stage)
    try:
        if job["kind"] == "refresh":
            finish(db, job, owner, YouTube(db).refresh(pulse))
        elif job["kind"] in ("download", "transcribe"):
            result, updates, transcript = (download if job["kind"] == "download" else transcribe)(db, job, pulse)
            finish(db, job, owner, result, updates, transcript)
        else:
            fail(db, job, owner, "未知任务类型", retry=False)
    except LostLease:
        pass
    except MediaError as exc:
        fail(db, job, owner, str(exc), retry=exc.retry)
    except ProviderError as exc:
        fail(db, job, owner, str(exc))
    except Exception:
        # Do not leak provider keys/absolute paths through raw exception strings or tracebacks.
        fail(db, job, owner, "任务发生内部错误，请检查依赖、文件权限和配置")


def main():
    job_id, owner = sys.argv[1:3]
    db = Database(Settings())
    with db.connect() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND status='running'", (job_id, owner)).fetchone()
    if row:
        execute(db, dict(row), owner)


if __name__ == "__main__":
    main()
