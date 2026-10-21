# API 参考与调用示例

以下为 Linux shell，`API_KEY` 为 EARSPACE_API_KEY 的值，`BASE` 按部署修改。不要把真实密钥提交到 Git。

```bash
BASE=http://127.0.0.1:8000
read -rs -p 'Earspace API key: ' API_KEY; echo

curl "$BASE/api/v1/preferences" -H "Authorization: Bearer $API_KEY"

curl -X PUT "$BASE/api/v1/preferences" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"interests":["科技","心理学"],"preferred_channels":[],"english_level":"intermediate","session_minutes":45,"discovery_ratio":0.2}'

curl -X POST "$BASE/api/v1/discovery/refresh" -H "Authorization: Bearer $API_KEY"
# 202 返回 job.id；轮询 /api/v1/jobs/{job_id}，成功后读取推荐
curl "$BASE/api/v1/recommendations?language=en&context=gym" -H "Authorization: Bearer $API_KEY"

curl -X POST "$BASE/api/v1/episodes/import" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=YOURVIDEOID","language":"en","download":true,"mode":"audio"}'
```

`YOURVIDEOID` 请替换成实际的 11 位视频 ID。`download:false` 仅加入待听，信息在后续下载任务中补充。所有导入 URL 都会规范化成单个 YouTube 视频，不执行任意 URL 或整播放列表抓取。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 无鉴权的存活检查 |
| `GET /api/v1/status` | 能力、磁盘与 Worker 状态 |
| `GET/PUT /api/v1/preferences` | 读取／完整保存偏好 |
| `POST /api/v1/discovery/refresh` | 创建内容发现任务 |
| `GET /api/v1/recommendations?language=zh` | 推荐，支持 context、minutes、limit、offset |
| `POST /api/v1/episodes/import` | 导入单视频 |
| `GET /api/v1/episodes` | 待听，支持 language、library、分页 |
| `GET /api/v1/episodes/{id}` | 节目详情 |
| `PUT/DELETE /api/v1/episodes/{id}/library` | 加入／移出待听，不删除文件 |
| `POST /api/v1/episodes/{id}/download?mode=audio` | 默认仅音频与字幕；mode=video 时另生成 MP4 |
| `POST /api/v1/episodes/{id}/transcribe` | 显式创建语音转写任务 |
| `GET /api/v1/jobs` | 任务列表，支持状态筛选、分页 |
| `GET /api/v1/jobs/{id}` | 查询任务与结果 |
| `POST /api/v1/jobs/{id}/cancel` | 取消排队／运行任务 |
| `POST /api/v1/jobs/{id}/retry` | 从失败／取消任务创建新任务 |
| `GET/PUT/DELETE /api/v1/episodes/{id}/feedback` | 喜好和难度反馈、重置 |
| `GET/PUT /api/v1/episodes/{id}/progress` | 续播，position 为秒 |
| `GET /api/v1/listening/continue` | 最近未完成收听 |
| `GET/PUT /api/v1/episodes/{id}/transcript` | 默认获取句子文稿／导入原始时间戳文稿；`?view=raw` 获取原始片段 |
| `POST /api/v1/episodes/{id}/transcript/rebuild` | 从已存原始片段重建句子，不下载媒体、不调用语音 API |
| `GET /api/v1/episodes/{id}/transcript/export?format=vtt` | 导出句子 VTT 或 TXT；`view=raw` 可导出原始片段 |
| `GET /api/v1/episodes/{id}/media/audio` | 带 Bearer 鉴权的音频，支持 Range 和 HEAD |
| `GET /api/v1/episodes/{id}/media/video?download=true` | MP4 下载 |
| `POST /api/v1/episodes/{id}/playback-token?kind=audio` | 获取可用于 HTML audio/video 的短期地址 |

除 `/health`、网页静态资源、OpenAPI 文档和已签名流地址外，业务接口均要求 `Authorization: Bearer <EARSPACE_API_KEY>`。签名绑定视频、媒体类型、到期时间；不能用于其他 API。

反馈：`{"kind":"reaction","value":"like"}` 或 `dislike`；难度：`{"kind":"difficulty","value":"too_easy"}`、`just_right`、`too_hard`。重复提交更新原反馈，不重复计权。

任务响应的 `media_mode` 表明下载模式。导入请求 `mode` 和下载查询参数 `mode` 均默认 `audio`；旧版本没有模式字段的已持久化任务仍按视频模式恢复。同节目不同模式的并发请求返回 409。

成功任务的 `result.audio_processing` 为 `stream_copy`（AAC 直接复制，保留原码率）或 `transcoded`（其他编码以单线程转换为 AAC 96 kbps）。

文稿导入格式（秒）：

```json
{
  "language": "en",
  "segments": [
    {"start": 0, "end": 2.5, "text": "Welcome to the podcast."},
    {"start": 2.5, "end": 6, "text": "Today we explore a new idea."}
  ]
}
```

按开始时间排序，结束时间必须晚于开始时间。网页“导入文稿”接收此 JSON。媒体任务运行时暂不接受手动文稿写入，避免覆盖冲突。

文稿读取默认返回整理后的 `segments`，每段保留 `start/end/text`，另有 `kind`（`sentence` 或 `fragment`）、`boundary`（`punctuation/manual/pause/length/end`）和 `timing`（`estimated/caption_words`）。未加标点的手动导入片段尊重导入者的分段，记为 `manual`；它不代表算法验证了语法完整性。`processing` 包含算法 `version`、`raw_count`、`sentence_count`（所有句子／语段总数）、`fragment_count`、`timing`（另可能为 `mixed`）、`invalid_cues` 和 `rolling_revisions`。

`POST .../transcript/rebuild` 返回同样的数据，外加 `changed`：源文稿、语言、节目时长或算法版本相较缓存是否变化。重复重建不会累加切分或改写原文。没有原始文稿时返回 404。新下载／转写／导入自动使旧缓存失效，首次读取生成新句子视图。重建可与媒体任务并发；发布和重建在 SQLite 事务中串行，避免把旧版本缓存覆盖到新来源上。

旧字幕通常只保存片段时间：句中拆分依照字数比例估算，并不进行音频强制对齐。新下载的 JSON3 如有词级偏移则保留并优先使用；`caption_words` 仍是字幕时间，不保证与音频采样精确对齐。缺失字幕时重建不会凭空生成文字，仍需手动导入或另行启用语音识别。算法细节见 [句子与循环设计](TRANSCRIPTS.md)。
