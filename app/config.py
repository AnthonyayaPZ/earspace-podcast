import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("EARSPACE_DATA_DIR", "data")).resolve())
    api_key: str = field(default_factory=lambda: os.getenv("EARSPACE_API_KEY", ""))
    youtube_key: str = field(default_factory=lambda: os.getenv("YOUTUBE_API_KEY", ""))
    youtube_cookies_file: str = field(default_factory=lambda: os.getenv("EARSPACE_YOUTUBE_COOKIES_FILE", "").strip())
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.getenv("EARSPACE_CORS_ORIGINS", "").split(",") if s.strip()))
    search_ttl: int = field(default_factory=lambda: int(os.getenv("EARSPACE_SEARCH_TTL_SECONDS", "21600")))
    job_timeout: int = field(default_factory=lambda: int(os.getenv("EARSPACE_JOB_TIMEOUT_SECONDS", "7200")))
    max_attempts: int = field(default_factory=lambda: int(os.getenv("EARSPACE_JOB_MAX_ATTEMPTS", "2")))
    max_duration: int = field(default_factory=lambda: int(os.getenv("EARSPACE_MAX_DURATION_SECONDS", "14400")))
    max_file_mb: int = field(default_factory=lambda: int(os.getenv("EARSPACE_MAX_FILE_MB", "2048")))
    min_free_mb: int = field(default_factory=lambda: int(os.getenv("EARSPACE_MIN_FREE_MB", "2048")))
    whisper_enabled: bool = field(default_factory=lambda: os.getenv("EARSPACE_WHISPER_ENABLED", "false").lower() == "true")
    whisper_model: str = field(default_factory=lambda: os.getenv("EARSPACE_WHISPER_MODEL", "small"))

    def validate(self):
        if len(self.api_key) < 24 or self.api_key.startswith("replace-"):
            raise RuntimeError("Set EARSPACE_API_KEY to a random secret of at least 24 characters.")
        if "*" in self.cors_origins:
            raise RuntimeError("Configure explicit CORS origins instead of '*'.")
        for name in ("search_ttl", "job_timeout", "max_attempts", "max_duration", "max_file_mb"):
            if getattr(self, name) <= 0:
                raise RuntimeError(f"{name} must be positive")

    @property
    def db_path(self):
        return self.data_dir / "earspace.sqlite3"
