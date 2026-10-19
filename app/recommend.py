import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone

from .db import public_episode


def speech_estimate(segments):
    """Transparent text heuristic, not a CEFR or pronunciation assessment."""
    words = re.findall(r"[a-zA-Z']+", " ".join(s["text"] for s in segments))
    span = max((s["end"] for s in segments), default=0) - min((s["start"] for s in segments), default=0)
    if len(words) < 80 or span < 30:
        return {"level": "unknown", "method": "insufficient_transcript"}
    wpm = round(len(words) / (span / 60))
    long_ratio = sum(len(w) >= 9 for w in words) / len(words)
    level = "advanced" if wpm > 175 or long_ratio > .15 else "beginner" if wpm < 125 and long_ratio < .08 else "intermediate"
    return {"level": level, "words_per_minute": wpm, "method": "transcript_heuristic", "estimated": True}


def recommendations(db, language, limit=12, offset=0, context="gym", minutes=None):
    profile = db.profile()
    target_seconds = (minutes or profile["session_minutes"]) * 60
    with db.connect() as c:
        rows = c.execute("SELECT * FROM episodes WHERE language=? AND duration>0 AND updated_at>?",
                         (language, time.time() - 29 * 86400)).fetchall()
        feedback = {(r["episode_id"], r["kind"]): r["value"] for r in c.execute("SELECT * FROM feedback")}
        completed = {r[0] for r in c.execute("SELECT episode_id FROM progress WHERE completed=1")}
        transcripts = {r[0]: json.loads(r[1]) for r in c.execute("SELECT episode_id,segments FROM transcripts")}
        all_episodes = {r["id"]: dict(r) for r in c.execute("SELECT * FROM episodes")}
    topic_affinity, channel_affinity = Counter(), Counter()
    for (episode_id, kind), value in feedback.items():
        if kind != "reaction" or episode_id not in all_episodes:
            continue
        episode = all_episodes[episode_id]
        weight = 1 if value == "like" else -1
        topic_affinity.update({t: weight for t in json.loads(episode["topics"])})
        channel_affinity[episode["channel_id"]] += weight
    ranked = []
    levels = ["beginner", "intermediate", "advanced"]
    difficulty_votes = Counter(value for (eid, kind), value in feedback.items() if kind == "difficulty"
                               and eid in all_episodes and all_episodes[eid]["language"] == "en")
    adjustment = (1 if difficulty_votes["too_easy"] >= 2 and difficulty_votes["too_easy"] > difficulty_votes["too_hard"]
                  else -1 if difficulty_votes["too_hard"] >= 2 and difficulty_votes["too_hard"] > difficulty_votes["too_easy"] else 0)
    target_level = levels[max(0, min(2, levels.index(profile["english_level"]) + adjustment))]
    day = datetime.now(timezone.utc).date().isoformat()
    for row in rows:
        item = public_episode(row)
        video_id = item["id"]
        if feedback.get((video_id, "reaction")) == "dislike" or video_id in completed:
            continue
        text = (item["title"] + " " + item["description"]).casefold()
        direct = [t for t in profile["interests"] if t.casefold() in text]
        related = [t for t in item["topics"] if t in profile["interests"]]
        reasons, score = [], 10.0
        if direct or related:
            reasons.append("与你的兴趣相关：" + "、".join(direct or related))
            score += min(35, 12 * len(direct) + 5 * len(related))
        if item["channel_id"] in profile["preferred_channels"]:
            score += 22
            reasons.append("来自你关注的频道")
        affinity = sum(topic_affinity[t] for t in item["topics"]) * 3 + channel_affinity[item["channel_id"]] * 4
        score += max(-20, min(20, affinity))
        if affinity > 0:
            reasons.append("与你之前喜欢的内容相似")
        fit = max(0, 1 - abs(item["duration"] - target_seconds) / target_seconds)
        score += 18 * fit
        if fit > .65:
            reasons.append(f"时长适合本次 {round(target_seconds/60)} 分钟收听")
        difficulty = speech_estimate(transcripts.get(video_id, [])) if language == "en" else None
        if difficulty and difficulty["level"] != "unknown":
            if difficulty["level"] == target_level:
                score += 15
                reasons.append("文稿估计的语速与词汇复杂度匹配水平与难度反馈")
            else:
                score -= 5
        # Explicit difficulty feedback also influences similar channel content.
        if language == "en":
            hard = sum(1 for (eid, kind), val in feedback.items() if kind == "difficulty"
                       and val == "too_hard" and eid in all_episodes
                       and all_episodes[eid]["channel_id"] == item["channel_id"])
            score -= min(15, hard * 5)
        if context == "practice" and transcripts.get(video_id):
            score += 12
            reasons.append("已有带时间戳文稿，可逐句练习")
        elif context == "gym":
            # Do not claim audio suitability without actually analyzing the media.
            item["audio_suitability"] = "not_assessed"
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))).days
            score += max(0, 5 * (1 - max(age, 0) / 180))
        except (ValueError, TypeError):
            pass
        random_value = int(hashlib.sha256(f"{day}:{video_id}".encode()).hexdigest()[:8], 16) / 0xffffffff
        item.update(score=round(score, 2), reasons=reasons or ["探索与你常听内容不同的节目"],
                    difficulty=difficulty, exploration=False)
        ranked.append((score, random_value, item))
    ranked.sort(key=lambda x: (-x[0], x[2]["id"]))
    # Keep the ordering independent of requested page size; daily exploration is reproducible.
    exploration = sorted(ranked, key=lambda x: x[1])
    explore_slots = {i for i in range(len(ranked)) if profile["discovery_ratio"] > 0
                     and (i + 1) % max(2, round(1 / profile["discovery_ratio"])) == 0}
    chosen, used, per_channel = [], set(), Counter()
    for slot in range(len(ranked)):
        pool = exploration if slot in explore_slots else ranked
        candidates = [x for x in pool if x[2]["id"] not in used and per_channel[x[2]["channel_id"]] < 2]
        if not candidates:
            break
        item = dict(candidates[0][2])
        item["exploration"] = slot in explore_slots
        if item["exploration"]:
            item["reasons"] = ["保留一个探索名额，尝试不同的内容"] + item["reasons"]
        chosen.append(item)
        used.add(item["id"])
        per_channel[item["channel_id"]] += 1
    return {"items": chosen[offset:offset+limit], "total": len(chosen), "offset": offset,
            "limit": limit, "algorithm": "explainable_rules_v1",
            "note": "按兴趣、反馈、时长与频道排序；英文难度仅在有足够文稿时估计，不是 CEFR 评测。"}
