"""Report orphan artifacts by default; --prune removes only old, unreferenced artifacts."""
import argparse
import json
import shutil
import time

from .config import Settings
from .db import Database


def collect(db, prune=False, minimum_age=86400):
    root = db.settings.data_dir.resolve()
    found = []
    # Fence concurrent job publication/claims while collecting and deleting.
    with db.connect(immediate=True) as c:
        references = set()
        for row in c.execute("SELECT video_file,audio_file FROM episodes"):
            references.update((root / value).resolve() for value in row if value)
        active = {r[0] for r in c.execute("SELECT id FROM jobs WHERE status IN ('queued','running')")}
        cutoff = time.time() - minimum_age
        media_root = root / "media"
        if media_root.is_dir() and not media_root.is_symlink():
            for file in media_root.rglob("*"):
                if file.is_symlink() or not file.is_file():
                    continue
                resolved = file.resolve()
                if not resolved.is_relative_to(media_root.resolve()) or resolved in references:
                    continue
                if any(part.startswith(job_id + "-") for part in file.parts for job_id in active):
                    continue
                if file.stat().st_mtime < cutoff:
                    found.append({"path": str(file.relative_to(root)), "bytes": file.stat().st_size})
                    if prune:
                        file.unlink()
        work_root = root / "work"
        if work_root.is_dir() and not work_root.is_symlink():
            for folder in work_root.iterdir():
                if folder.is_symlink() or not folder.is_dir() or not folder.resolve().is_relative_to(work_root.resolve()):
                    continue
                if any(folder.name.startswith(job_id + "-") for job_id in active):
                    continue
                if folder.stat().st_mtime < cutoff:
                    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink())
                    found.append({"path": str(folder.relative_to(root)), "bytes": size})
                    if prune:
                        shutil.rmtree(folder)
    return {"pruned": prune, "bytes": sum(x["bytes"] for x in found), "artifacts": found}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prune", action="store_true", help="Remove old unreferenced files (default: report only)")
    args = parser.parse_args()
    settings = Settings()
    settings.validate()
    db = Database(settings)
    db.initialize()
    print(json.dumps(collect(db, args.prune), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
