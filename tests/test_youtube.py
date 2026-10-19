import json
from dataclasses import replace

import httpx
import pytest

from app.db import Database
from app.youtube import ProviderError, YouTube, iso_duration


def test_duration():
    assert iso_duration("PT1H20M30S") == 4830
    assert iso_duration("P1DT1H") == 90000
    assert iso_duration("bad") == 0


def test_provider_caches_requests_and_sanitizes_secret(db):
    db = Database(replace(db.settings, youtube_key="super-secret-provider-key"))
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"items": []})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = YouTube(db, client)
        assert provider.request("search", q="test") == {"items": []}
        provider.request("search", q="test")
    assert len(calls) == 1
    with db.connect() as c:
        assert "super-secret-provider-key" not in c.execute("SELECT cache_key FROM search_cache").fetchone()[0]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(ProviderError) as exc:
            YouTube(db, client).request("search", q="different")
        assert "super-secret-provider-key" not in str(exc.value)


def test_discovery_pipeline_language_and_metadata(db):
    db = Database(replace(db.settings, youtube_key="test-key"))
    calls = []
    def transport(request):
        calls.append(str(request.url.path))
        if request.url.path.endswith("/search"):
            lang = request.url.params["relevanceLanguage"]
            return httpx.Response(200, json={"items": [{"id": {"videoId": "aaaaaaaaaaa" if lang == "en" else "bbbbbbbbbbb"}}]})
        ids = request.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [{"id": vid, "snippet": {
            "title": "Science &amp; life" if vid == "aaaaaaaaaaa" else "科技与生活",
            "channelId": "UC" + "x" * 22, "channelTitle": "Knowledge",
            "defaultAudioLanguage": "en-US" if vid == "aaaaaaaaaaa" else "zh-Hans",
            "publishedAt": "2026-10-01T00:00:00Z"}, "contentDetails": {"duration": "PT45M"}} for vid in ids]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = YouTube(db, client).refresh()
        assert result["discovered"] == 2
        before = len(calls)
        YouTube(db, client).refresh()
        assert len(calls) == before
    assert db.episode("aaaaaaaaaaa")["title"] == "Science & life"
    assert db.episode("bbbbbbbbbbb")["language"] == "zh"
    assert len(json.loads(db.episode("aaaaaaaaaaa")["topics"])) == 3


def test_discovery_preserves_explicit_language_and_downloads(db, seed):
    seed("aaaaaaaaaaa", language="zh")
    db.upsert_episode({"id": "aaaaaaaaaaa", "title": "new metadata", "language": "en",
                       "language_source": "estimated_from_search", "topics": ["历史"]})
    assert db.episode("aaaaaaaaaaa")["language"] == "zh"
    assert json.loads(db.episode("aaaaaaaaaaa")["topics"]) == ["科技", "历史"]
