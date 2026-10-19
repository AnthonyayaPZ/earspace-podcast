import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, api_key="test-secret-" + "a" * 32, youtube_key="",
                    cors_origins=("http://localhost:5173",), min_free_mb=0)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = f"Bearer {settings.api_key}"
        yield c


@pytest.fixture
def db(client):
    return client.app.state.db


@pytest.fixture
def seed(db):
    def add(video_id="abcdefghijk", language="en", **kwargs):
        item = {"id": video_id, "title": "科技 podcast interview", "language": language,
                "duration": 2700, "channel_id": "UC" + video_id + "0"*11,
                "channel_title": "Test podcast", "topics": ["科技"]}
        item.update(kwargs)
        db.upsert_episode(item)
        return video_id
    return add
