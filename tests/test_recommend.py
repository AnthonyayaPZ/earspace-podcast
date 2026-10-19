import json

from app.recommend import recommendations, speech_estimate


def test_filters_language_dislikes_and_completed(client, db, seed):
    a = seed("aaaaaaaaaaa", language="en")
    b = seed("bbbbbbbbbbb", language="en")
    seed("ccccccccccc", language="zh")
    seed("ddddddddddd", language="en")
    client.put(f"/api/v1/episodes/{a}/feedback", json={"kind": "reaction", "value": "dislike"})
    client.put(f"/api/v1/episodes/{b}/progress", json={"position": 2700, "completed": True})
    result = recommendations(db, "en")
    assert [x["id"] for x in result["items"]] == ["ddddddddddd"]
    assert result["items"][0]["difficulty"]["level"] == "unknown"
    assert result["items"][0]["audio_suitability"] == "not_assessed"


def test_topic_and_duration_affect_order(db, seed):
    profile = db.profile()
    profile["discovery_ratio"] = 0
    with db.connect() as c:
        c.execute("UPDATE profile SET data=?", (json.dumps(profile),))
    seed("aaaaaaaaaaa", title="AI story", topics=["other"], duration=7200)
    seed("bbbbbbbbbbb", title="科技 podcast", topics=["科技"], duration=2700)
    items = recommendations(db, "en")["items"]
    assert items[0]["id"] == "bbbbbbbbbbb"
    assert any("时长" in r for r in items[0]["reasons"])


def test_positive_feedback_changes_similar_channel_rank(client, db, seed):
    profile = db.profile()
    profile["discovery_ratio"] = 0
    with db.connect() as c:
        c.execute("UPDATE profile SET data=?", (json.dumps(profile),))
    a = seed("aaaaaaaaaaa", topics=[], title="podcast", channel_id="channelA")
    b = seed("bbbbbbbbbbb", topics=[], title="podcast", channel_id="channelB")
    c_id = seed("ccccccccccc", topics=[], title="podcast", channel_id="channelB")
    before = {x["id"]: x["score"] for x in recommendations(db, "en")["items"]}
    client.put(f"/api/v1/episodes/{b}/feedback", json={"kind": "reaction", "value": "like"})
    after = {x["id"]: x["score"] for x in recommendations(db, "en")["items"]}
    assert after[c_id] > before[c_id]
    assert after[a] == before[a]


def test_stable_pagination_and_channel_diversity(db, seed):
    for i in range(20):
        seed(f"{i:011d}", channel_id=f"channel{i//4}")
    full = recommendations(db, "en", limit=50)
    first = recommendations(db, "en", limit=3)
    second = recommendations(db, "en", limit=3, offset=3)
    assert [x["id"] for x in first["items"] + second["items"]] == [x["id"] for x in full["items"][:6]]
    assert len(full["items"]) == 10
    assert any(x["exploration"] for x in full["items"])


def test_difficulty_is_only_a_heuristic():
    assert speech_estimate([])["level"] == "unknown"
    result = speech_estimate([{"start": 0, "end": 60, "text": "hello world " * 50}])
    assert result["level"] == "beginner"
    assert result["words_per_minute"] == 100
    assert result["estimated"] is True


def test_repeated_difficulty_feedback_adjusts_target(db, seed, client):
    a = seed("aaaaaaaaaaa", channel_id="A", topics=[])
    b = seed("bbbbbbbbbbb", channel_id="B", topics=[])
    c_id = seed("ccccccccccc", channel_id="C", topics=[])
    segments = [{"start": 0, "end": 60, "text": "hello world " * 50}]
    with db.connect() as c:
        c.execute("INSERT INTO transcripts VALUES (?,?,?,?)", (c_id, json.dumps(segments), "manual", "en"))
    before = {x["id"]: x["score"] for x in recommendations(db, "en")["items"]}
    for eid in (a, b):
        client.put(f"/api/v1/episodes/{eid}/feedback", json={"kind": "difficulty", "value": "too_hard"})
    after = {x["id"]: x["score"] for x in recommendations(db, "en")["items"]}
    assert after[c_id] - before[c_id] == 20
    assert db.profile()["english_level"] == "intermediate"
