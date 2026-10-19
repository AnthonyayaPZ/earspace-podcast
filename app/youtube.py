"""YouTube metadata discovery. No scraping of personalized YouTube home feeds."""
import html
import json
import re
import time

import httpx

ENGLISH_TOPICS = {"科技": "technology", "商业": "business", "心理学": "psychology", "人工智能": "artificial intelligence",
                  "AI": "artificial intelligence", "历史": "history", "经济": "economics", "金融": "finance",
                  "健康": "health", "科学": "science", "哲学": "philosophy", "创业": "entrepreneurship",
                  "人文": "humanities", "投资": "investing", "教育": "education", "运动": "fitness"}


class ProviderError(Exception):
    pass


def iso_duration(value):
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        return 0
    d, h, m, s = (float(n or 0) for n in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


class YouTube:
    def __init__(self, db, client=None):
        self.db = db
        self.settings = db.settings
        self.client = client

    def request(self, endpoint, **params):
        if not self.settings.youtube_key:
            raise ProviderError("未配置 YOUTUBE_API_KEY；请配置后刷新推荐")
        key = endpoint + json.dumps(params, sort_keys=True)
        with self.db.connect() as c:
            row = c.execute("SELECT data FROM search_cache WHERE cache_key=? AND expires_at>?",
                            (key, time.time())).fetchone()
        if row:
            return json.loads(row[0])
        params["key"] = self.settings.youtube_key
        # Never include request URLs in exceptions: they contain the provider secret.
        with httpx.Client(timeout=30, follow_redirects=False) if self.client is None else _borrow(self.client) as client:
            for attempt in range(3):
                try:
                    response = client.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params)
                except httpx.HTTPError:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    raise ProviderError("YouTube 网络连接失败，请检查服务器出站网络") from None
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        raise ProviderError("YouTube 返回了无效响应") from None
                    with self.db.connect() as c:
                        c.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?)",
                                  (key, json.dumps(data), time.time() + self.settings.search_ttl))
                    return data
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code in (400, 401, 403):
                    raise ProviderError("YouTube API 请求被拒绝，请检查密钥、API 启用状态和配额")
                raise ProviderError(f"YouTube 服务暂不可用（HTTP {response.status_code}）")

    def videos(self, ids, fallback_language, topics=None):
        if not ids:
            return []
        data = self.request("videos", part="snippet,contentDetails,status", id=",".join(dict.fromkeys(ids)))
        items = []
        for entry in data.get("items", []):
            s = entry["snippet"]
            if s.get("liveBroadcastContent", "none") != "none":
                continue
            duration = iso_duration(entry.get("contentDetails", {}).get("duration", ""))
            if duration <= 0 or duration > self.settings.max_duration:
                continue
            declared = s.get("defaultAudioLanguage") or s.get("defaultLanguage", "")
            if declared:
                language = "zh" if declared.startswith("zh") else "en" if declared.startswith("en") else None
                source = "youtube_metadata"
            else:
                # A weak inference is exposed as such, never as verified speech language.
                chinese = len(re.findall(r"[\u3400-\u9fff]", s.get("title", "")))
                language = "zh" if chinese >= 2 else fallback_language
                source = "estimated_from_search"
            if language not in ("zh", "en"):
                continue
            thumbnails = s.get("thumbnails", {})
            thumbnail = thumbnails.get("high", thumbnails.get("default", {})).get("url", "")
            item = dict(id=entry["id"], title=html.unescape(s["title"]),
                        channel_id=s["channelId"], channel_title=html.unescape(s["channelTitle"]),
                        description=html.unescape(s.get("description", "")), language=language,
                        language_source=source, duration=duration, thumbnail=thumbnail,
                        published_at=s.get("publishedAt", ""), topics=(topics or {}).get(entry["id"], []))
            self.db.upsert_episode(item)
            items.append(item)
        return items

    def refresh(self, progress=lambda *args: None):
        profile = self.db.profile()
        interests = profile["interests"]
        # Rotate searches daily instead of issuing a query for every interest on every visit.
        start = int(time.time() // 86400) % len(interests)
        selected = (interests + interests)[start:start + min(3, len(interests))]
        found, warnings = set(), []
        requests = [(lang, topic) for lang in ("zh", "en") for topic in selected]
        for index, (language, topic) in enumerate(requests):
            progress(10 + 60 * index / len(requests), f"发现{language}播客：{topic}")
            # Broad content query; title/description relevance is assessed again at ranking.
            search_topic = ENGLISH_TOPICS.get(topic, topic) if language == "en" else topic
            query = f"{search_topic} {'中文 播客 访谈' if language == 'zh' else 'podcast interview'}"
            try:
                data = self.request("search", part="snippet", type="video", q=query,
                                    relevanceLanguage="zh-Hans" if language == "zh" else "en",
                                    videoDuration="long", maxResults=25)
                ids = [x["id"]["videoId"] for x in data.get("items", []) if x.get("id", {}).get("videoId")]
                items = self.videos(ids, language, {i: [topic] for i in ids})
                found.update(x["id"] for x in items)
            except ProviderError as exc:
                warnings.append(str(exc))
        channels = profile["preferred_channels"]
        if channels:
            offset = int(time.time() // 86400) % len(channels)
            selected_channels = (channels + channels)[offset:offset + min(5, len(channels))]
            try:
                info = self.request("channels", part="contentDetails", id=",".join(selected_channels))
                for channel in info.get("items", []):
                    playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
                    uploads = self.request("playlistItems", part="contentDetails", playlistId=playlist, maxResults=15)
                    ids = [x["contentDetails"]["videoId"] for x in uploads.get("items", [])]
                    found.update(x["id"] for x in self.videos(ids, "en"))
            except ProviderError as exc:
                warnings.append(str(exc))
        if not found and warnings:
            raise ProviderError(warnings[0])
        # Keep stored API metadata within a bounded lifetime. Library entries are user-retained.
        with self.db.connect() as c:
            cutoff = time.time() - 29 * 86400
            c.execute("DELETE FROM search_cache WHERE expires_at<?", (time.time(),))
            c.execute("""DELETE FROM episodes WHERE library=0 AND updated_at<?
                AND id NOT IN (SELECT episode_id FROM jobs WHERE episode_id IS NOT NULL)
                AND id NOT IN (SELECT episode_id FROM feedback)
                AND id NOT IN (SELECT episode_id FROM progress)
                AND id NOT IN (SELECT episode_id FROM transcripts)""", (cutoff,))
        return {"discovered": len(found), "warnings": list(dict.fromkeys(warnings)),
                "ranking": "explainable_rules_v1", "refreshed_at": time.time()}


class _borrow:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *args):
        pass
