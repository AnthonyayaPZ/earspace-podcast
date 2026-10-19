"""Durable SQLite queue consumer. Run separately from the HTTP service."""
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

import psutil

from .config import Settings
from .db import Database
from .jobs import LostLease, claim, fail, heartbeat

log = logging.getLogger("earspace.worker")


def kill_tree(process):
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        psutil.wait_procs(children + [parent], timeout=5)
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=10)


def consume(db, job, owner, stop):
    # Isolate model loading and third-party tools so cancellation/timeout can stop them too.
    env = os.environ.copy()
    env["EARSPACE_DATA_DIR"] = str(db.settings.data_dir)
    process = subprocess.Popen([sys.executable, "-m", "app.runner", job["id"], owner], env=env,
                               stdin=subprocess.DEVNULL)
    start = time.monotonic()
    try:
        while process.poll() is None:
            if stop.is_set():
                fail(db, job, owner, "Worker 关闭，任务将自动恢复")
                return
            if time.monotonic() - start > db.settings.job_timeout:
                fail(db, job, owner, "任务超过总处理时限，请检查资源或调高任务超时配置")
                return
            heartbeat(db, job["id"], owner)
            stop.wait(2)
        with db.connect() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if row and row["status"] == "running":
            fail(db, job, owner, "任务进程意外退出，将按配置重试")
    except LostLease:
        log.info("Job %s cancelled or lease lost", job["id"])
    finally:
        if process.poll() is None:
            kill_tree(process)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    settings.validate()
    db = Database(settings)
    db.initialize()
    owner = uuid.uuid4().hex
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("Worker ready")
    while not stop.is_set():
        job = claim(db, owner)
        if job:
            log.info("Processing %s (%s), attempt %d", job["id"], job["kind"], job["attempts"])
            consume(db, job, owner, stop)
        else:
            stop.wait(2)


if __name__ == "__main__":
    main()
