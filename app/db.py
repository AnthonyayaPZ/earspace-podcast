import json
import sqlite3
import time
from contextlib import contextmanager


DEFAULT_PROFILE = {
    "interests": ["科技", "商业", "心理学"], "preferred_channels": [],
    "english_level": "intermediate", "session_minutes": 45,
    "discovery_ratio": 0.2,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, channel_id TEXT NOT NULL DEFAULT '',
 channel_title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
 language TEXT NOT NULL CHECK(language IN ('zh','en')), language_source TEXT NOT NULL DEFAULT 'user',
 duration REAL NOT NULL DEFAULT 0, thumbnail TEXT NOT NULL DEFAULT '',
 published_at TEXT NOT NULL DEFAULT '', topics TEXT NOT NULL DEFAULT '[]',
 library INTEGER NOT NULL DEFAULT 0, video_file TEXT, audio_file TEXT,
 transcript_source TEXT NOT NULL DEFAULT 'none', transcript_warning TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_language ON episodes(language, library);
CREATE TABLE IF NOT EXISTS transcripts (
 episode_id TEXT PRIMARY KEY REFERENCES episodes(id), segments TEXT NOT NULL,
 source TEXT NOT NULL, language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
 episode_id TEXT NOT NULL REFERENCES episodes(id), kind TEXT NOT NULL,
 value TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(episode_id,kind)
);
CREATE TABLE IF NOT EXISTS progress (
 episode_id TEXT PRIMARY KEY REFERENCES episodes(id), position REAL NOT NULL,
 completed INTEGER NOT NULL, playback_rate REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, episode_id TEXT REFERENCES episodes(id),
 payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
 progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '等待处理',
 attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
 lease_until REAL, owner TEXT, error TEXT, result TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(kind, COALESCE(episode_id,''))
 WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_media ON jobs(episode_id)
 WHERE kind IN ('download','transcribe') AND status IN ('queued','running');
CREATE INDEX IF NOT EXISTS jobs_poll ON jobs(status,available_at);
CREATE TABLE IF NOT EXISTS search_cache (
 cache_key TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
 id TEXT PRIMARY KEY, heartbeat REAL NOT NULL
);
"""


class Database:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connect(self, immediate=False):
        conn = sqlite3.connect(self.settings.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            version = c.execute("SELECT version FROM schema_version").fetchone()[0]
            if version != 1:
                raise RuntimeError("Unsupported database schema; back up data before upgrading")
            c.execute("INSERT OR IGNORE INTO profile VALUES (1,?)", (json.dumps(DEFAULT_PROFILE),))

    def profile(self):
        with self.connect() as c:
            return json.loads(c.execute("SELECT data FROM profile WHERE id=1").fetchone()[0])

    def episode(self, video_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM episodes WHERE id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    def upsert_episode(self, item):
        now = time.time()
        fields = ("id", "title", "channel_id", "channel_title", "description", "language",
                  "language_source", "duration", "thumbnail", "published_at", "topics")
        defaults = {"channel_id": "", "channel_title": "", "description": "", "duration": 0,
                    "thumbnail": "", "published_at": "", "topics": [], "language_source": "user"}
        values = [json.dumps(item.get(f, []), ensure_ascii=False) if f == "topics"
                  else item.get(f, defaults.get(f)) for f in fields]
        # Discovery never overwrites a user's language choice or existing files.
        updates = ",".join(f"{f}=excluded.{f}" for f in fields if f not in ("id", "language", "language_source"))
        with self.connect() as c:
            existing = c.execute("SELECT topics FROM episodes WHERE id=?", (item["id"],)).fetchone()
            if existing:
                values[-1] = json.dumps(list(dict.fromkeys(json.loads(existing[0]) + item.get("topics", []))), ensure_ascii=False)
            c.execute(f"INSERT INTO episodes ({','.join(fields)},created_at,updated_at) "
                      f"VALUES ({','.join('?' for _ in range(len(fields)+2))}) "
                      f"ON CONFLICT(id) DO UPDATE SET {updates},updated_at=excluded.updated_at",
                      (*values, now, now))


def public_episode(row):
    item = dict(row)
    item["topics"] = json.loads(item["topics"])
    item["library"] = bool(item["library"])
    item["youtube_url"] = f"https://www.youtube.com/watch?v={item['id']}"
    for media in ("video", "audio"):
        item[f"{media}_available"] = bool(item.pop(f"{media}_file", None))
    return item
