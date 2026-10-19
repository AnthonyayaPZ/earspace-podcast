import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field, field_validator

Language = Literal["zh", "en"]
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_id(value: str) -> str:
    if VIDEO_ID.fullmatch(value):
        return value
    p = urlparse(value)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        raise ValueError("仅接受 HTTPS YouTube 视频链接或 11 位视频 ID")
    host = (p.hostname or "").lower()
    if host == "youtu.be":
        candidate = p.path.strip("/")
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if p.path == "/watch":
            candidate = parse_qs(p.query).get("v", [""])[0]
        elif p.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = p.path.split("/")[2]
        else:
            candidate = ""
    else:
        candidate = ""
    if not VIDEO_ID.fullmatch(candidate):
        raise ValueError("需要单个 YouTube 视频链接，不支持任意网址或播放列表")
    return candidate


class Profile(BaseModel):
    interests: list[str] = Field(default_factory=lambda: ["科技", "商业", "心理学"], min_length=1, max_length=12)
    preferred_channels: list[str] = Field(default_factory=list, max_length=30)
    english_level: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    session_minutes: int = Field(default=45, ge=5, le=240)
    discovery_ratio: float = Field(default=0.2, ge=0, le=0.5, allow_inf_nan=False)

    @field_validator("interests")
    @classmethod
    def valid_interests(cls, values):
        values = list(dict.fromkeys(s.strip() for s in values))
        if any(not s or len(s) > 40 for s in values):
            raise ValueError("兴趣标签长度应为 1–40 个字符")
        return values

    @field_validator("preferred_channels")
    @classmethod
    def valid_channels(cls, values):
        if any(not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", s) for s in values):
            raise ValueError("请使用 UC 开头的 24 位 YouTube 频道 ID")
        return list(dict.fromkeys(values))


class ImportRequest(BaseModel):
    url: str = Field(max_length=2048)
    language: Language
    download: bool = True
    mode: Literal["audio", "video"] = "audio"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        youtube_id(value)
        return value


class FeedbackRequest(BaseModel):
    kind: Literal["reaction", "difficulty"]
    value: Literal["like", "dislike", "too_easy", "just_right", "too_hard"]

    @field_validator("value")
    @classmethod
    def compatible(cls, value, info):
        choices = {"reaction": {"like", "dislike"}, "difficulty": {"too_easy", "just_right", "too_hard"}}
        if info.data.get("kind") in choices and value not in choices[info.data["kind"]]:
            raise ValueError("反馈类型与取值不匹配")
        return value


class ProgressRequest(BaseModel):
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)
    completed: bool = False
    playback_rate: float = Field(default=1, ge=0.5, le=3, allow_inf_nan=False)


class Segment(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=10000)


class TranscriptRequest(BaseModel):
    language: Language
    segments: list[Segment] = Field(min_length=1, max_length=20000)

    @field_validator("segments")
    @classmethod
    def ordered(cls, segments):
        last = -1
        for s in segments:
            if s.end <= s.start or s.start < last or not s.text.strip():
                raise ValueError("文稿必须按开始时间排序，结束时间晚于开始时间，且包含有效文字")
            last = s.start
        return segments
