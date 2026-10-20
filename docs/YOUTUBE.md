# YouTube 接入、检索推荐与音频下载

本项目的处理链路：

```text
你的兴趣、频道与反馈
        ↓
YouTube Data API 搜索／频道上传列表
        ↓
本地候选内容库 → 按偏好排序 → 网页展示推荐理由
        ↓ 你选择一个节目
后台 yt-dlp 仅获取音轨 → FFmpeg 生成 M4A → 播放／下载
        └ 可用字幕 → 时间戳文稿；缺字幕时可选手动导入或 Whisper
```

## 一、配置 Google 的 YouTube API

1. 登录 [Google Cloud Console](https://console.cloud.google.com/)，创建或选择一个项目。
2. 在 **APIs & Services → Library** 中搜索并启用 **YouTube Data API v3**。
3. 在 **APIs & Services → Credentials** 创建 API Key。
4. 编辑此项目服务器上的 `.env`，填写 `YOUTUBE_API_KEY=你的Google密钥`。
5. 建议把 API 限制限定为 YouTube Data API v3。固定出口服务器可进一步使用其出站公网 IP 限制；后端请求不应配置成网页 HTTP referrer 限制。
6. 重建容器实例以加载配置：`docker compose up -d --force-recreate`。

Google 的 [官方入门说明](https://developers.google.com/youtube/data/getting-started) 介绍了启用 API 和创建凭据的步骤。配额与限制请查看该项目的控制台，不在代码中假定固定的免费次数。

本项目只通过 API 搜索公开内容和读取公开元数据，使用 API Key 即可；当前不需要 OAuth 用户登录，也不会读取你个人 YouTube 首页、观看历史或账号订阅。喜欢的频道通过偏好里的 UC 开头频道 ID 提供。私有账号数据的访问与个人订阅同步尚未实现。见 [官方授权说明](https://developers.google.com/youtube/data/guides/authentication)。

**EARSPACE_API_KEY 与 YOUTUBE_API_KEY 不同：** 前者保护本应用接口；后者仅由服务器调用 Google，不能填入网页连接框，也不应发给浏览器。

## 二、配置喜好并发现内容

网页：连接服务 → 兴趣与偏好 → 保存 → 更新推荐 → 处理任务。

兴趣目前由中英文分区共同使用；内置常见中文主题的英文检索词转换。每次刷新最多使用 3 个兴趣、分别执行中英文搜索，按天轮换；喜欢的频道每天最多轮换 5 个，获取上传列表中的最近候选。

- 搜索调用 [`search.list`](https://developers.google.com/youtube/data/reference/rest/v3/search/list)，限定 `type=video`，关注长视频；语言相关性参数不是严格的语音语言过滤。
- 批量调用 `videos.list` 补充时长、频道和语言元数据；过滤直播与超出上限的视频。
- 已填写频道使用 `channels.list` 获取 uploads 播放列表，再通过 `playlistItems.list` 获取内容；没有逐频道反复调用搜索。
- 查询默认缓存 6 小时；反复点击不会对相同查询每次访问外部 API。
- 未声明语言的结果会以搜索／标题弱推断并标注“语言待确认”；可通过链接导入时选择正确分区。

推荐由本地规则排序，不是调用 YouTube 的个人推荐算法，也未调用大模型：兴趣匹配、偏爱频道、显式喜欢／不喜欢、单次时长、较新内容、频道多样性、少量每日稳定探索。已听完和不感兴趣的条目会被排除。英文内容有足够文稿后才能粗估语速／长词比例；未分析时难度为 unknown。

## 三、接口调用示例

以下命令在服务器 Bash 中运行。API Key 通过交互输入，避免直接写进命令历史。需先完成 [部署步骤](DEPLOYMENT.md)。`curl` 使用 `--fail-with-body`，需要较新的 curl；若系统不支持，可改为 `--fail`。

```bash
BASE=http://127.0.0.1:8000
read -rs -p 'Earspace API key: ' API_KEY; echo

curl --fail-with-body -X PUT "$BASE/api/v1/preferences" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"interests":["科技","商业","心理学"],"preferred_channels":[],"english_level":"intermediate","session_minutes":45,"discovery_ratio":0.2}'

curl --fail-with-body -X POST "$BASE/api/v1/discovery/refresh" \
  -H "Authorization: Bearer $API_KEY"
```

上一步返回 `202` 与任务 `id`。把它填进 JOB_ID，再查询：

```bash
JOB_ID=上一步返回的任务id
curl --fail-with-body "$BASE/api/v1/jobs/$JOB_ID" -H "Authorization: Bearer $API_KEY"
```

任务状态为 `queued → running → succeeded`；一般失败先按配置重试，最终为 failed。明确的登录验证和 Cookie 配置错误直接失败，更新配置后手动重试。部分内容发现失败但仍发现了内容时，任务可成功并在 `result.warnings` 中说明。没有 Worker 就不会处理任务。

成功后读取推荐，language 使用 `zh` 或 `en`：

```bash
curl --fail-with-body "$BASE/api/v1/recommendations?language=en&context=gym&minutes=45&limit=12" \
  -H "Authorization: Bearer $API_KEY"
```

每个条目提供 `id`、`youtube_url`、时长、频道、`reasons` 和媒体是否可用。`context` 可取 `gym`、`daily`、`practice`。推荐接口只读本地候选库，不会自动下载或自动触发 Google 搜索。

## 四、下载选中视频的音频部分

**默认 mode=audio，只下载独立音轨，输出 M4A**。yt-dlp 优先选择 M4A 音轨。检测到 AAC 时，FFmpeg 直接复制压缩后的音频包，保留原码率，不重新编码；其他编码才单线程转换为 AAC 96 kbps。此模式不会先下载完整 MP4。如果无独立音频格式，会明确失败，不回退整视频。

任务结果 `audio_processing=stream_copy` 表明直接复制；`transcoded` 表明发生了兼容性转换。FFmpeg 的 [stream copy](https://ffmpeg.org/ffmpeg.html#Streamcopy) 不经过解码与重新编码，计算开销较低。标题和已有字幕的获取不使用本地语音模型；YouTube 的自动字幕也已经由平台生成，本应用只获取现成文字。

网页：对推荐点“准备收听” → 等待音频任务完成 → “开始收听”或“音频 ↓”。也可以在“导入视频”粘贴链接，下载内容选“仅音频 + 文稿”。

### 已在推荐库中的视频

```bash
VIDEO_ID=推荐条目的11位视频id
curl --fail-with-body -X POST "$BASE/api/v1/episodes/$VIDEO_ID/download?mode=audio" \
  -H "Authorization: Bearer $API_KEY"
```

### 直接提供视频链接

```bash
# 将 YOURVIDEOID 替换为实际的 11 位 YouTube 视频 ID
curl --fail-with-body -X POST "$BASE/api/v1/episodes/import" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=YOURVIDEOID","language":"en","download":true,"mode":"audio"}'
```

导入响应中的任务 ID 在 `job.id`；下载接口响应中的任务 ID 直接在 `id`。轮询对应任务，**等 succeeded 后**再下载：

```bash
curl --fail --show-error -o podcast.m4a \
  -H "Authorization: Bearer $API_KEY" \
  "$BASE/api/v1/episodes/$VIDEO_ID/media/audio?download=true"
```

这个 GET 将服务器已经准备好的文件传给当前设备，不会再次访问 YouTube。也可 `docker compose exec api ...` 查看状态或通过网页下载，媒体实体保存在数据卷的 `/data/media/<video_id>/<attempt>/audio.m4a`，不要依赖尝试目录名。

网页播放器使用 `POST /episodes/{id}/playback-token?kind=audio` 获得短期签名地址；音频流支持 Range 请求与拖动进度。没有媒体时返回 409，不返回假文件。已有 MP4 的节目再次下载音频，不会删除已缓存的视频。

需要 MP4 时显式选择 `mode=video`，会准备 MP4 和音频。下载／转写互斥，同节目不同下载模式也不能同时执行；先完成或取消已有任务。

## 五、文稿与反馈

相应语言人工字幕优先，其次自动字幕。字幕下载失败不影响已经完成的音频；任务结果会提示缺字幕。`GET /episodes/{id}/transcript` 获取时间戳，`.../transcript/export?format=txt` 或 `vtt` 导出。文稿导入格式见 [API 参考](API.md)。

可选转写需要安装镜像内 faster-whisper，并设置 `EARSPACE_WHISPER_ENABLED=true` 后重新构建。仅提交 `POST /episodes/{id}/transcribe` 后才运行；模型首次加载需联网下载，转写耗时取决于硬件。

推荐反馈示例：

```bash
curl --fail-with-body -X PUT "$BASE/api/v1/episodes/$VIDEO_ID/feedback" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"kind":"reaction","value":"like"}'
```

`reaction` 还支持 `dislike`；`difficulty` 支持 `too_easy`、`just_right`、`too_hard`。这些反馈保存在本应用，不会给 YouTube 点赞或发送评论。

## 六、YouTube 登录验证与 Cookie 文件

如果推荐检索成功，但下载报告 `Sign in to confirm you're not a bot`，需要分别检查下载进程的网络访问和登录会话。Google API Key 不用于媒体下载。浏览器能播放，不代表后台下载进程已经使用同一个登录会话或网络出口。

1. 在自己的浏览器打开同一个视频；若要求登录或验证，先在浏览器完成。
2. 按 [yt-dlp 官方 YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)，导出 `youtube.com` 域名 Cookie，格式为 Netscape Cookie 文件。浏览器导出工具可参考其 [官方 FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)。官方建议从独立的隐私窗口登录，导出后关闭该窗口，避免会话持续轮换。不要使用同时导出所有网站 Cookie 的命令。
3. 本机建议保存为项目目录下的 `secrets/youtube.cookies.txt`；Cookie 是登录凭据，留在自己的设备，不放进聊天或 GitHub。`secrets/` 和 `*.cookies.txt` 已被 Git、Docker 构建上下文及源码发行包排除。
4. 在 `.env` 设置路径；空值保持匿名下载：

```dotenv
EARSPACE_YOUTUBE_COOKIES_FILE=./secrets/youtube.cookies.txt
```

重启本地 API 和 Worker，然后在网页手动重试失败的下载任务。系统将文件中的 YouTube 域名 Cookie 复制到每次任务的私有工作目录；信息读取、音轨和字幕下载共用此临时副本。源文件可以只读，yt-dlp 更新会话不会改写源文件；Cookie 不会进入任务返回值或已发布的媒体目录，临时副本随任务工作目录清理。

`/api/v1/status` 的 `youtube_cookies_configured` 仅表示已配置路径，不检查 Cookie 是否有效，也不返回路径或内容。读取不到文件、格式不正确或文件中没有 YouTube Cookie 时，下载任务直接失败，并返回不含凭据的配置提示。

### Docker 使用 Cookie

Cookie 通过只读文件挂载提供给 Worker，不打包进镜像。`.env` 中设置服务器宿主机上的**绝对路径**，例如：

```dotenv
EARSPACE_YOUTUBE_COOKIES_FILE=/home/you/earspace-podcast/secrets/youtube.cookies.txt
```

确保文件存在且容器 UID 10001 可读；Linux 上可按实际路径执行 `sudo chown 10001:10001 secrets/youtube.cookies.txt` 和 `sudo chmod 600 secrets/youtube.cookies.txt`。使用附带的覆盖文件启动：

```bash
docker compose -f compose.yaml -f compose.cookies.yaml up -d --build --wait --wait-timeout 180
```

覆盖文件只将 Cookie 挂载给 Worker，并将其容器内配置改为 `/run/secrets/youtube.cookies.txt`。后续更新或重新创建容器时，继续使用这两个 `-f` 参数。

有效 Cookie 不保证所有视频或服务器出口都能下载。若仍失败，检查会话是否过期、浏览器与 Worker 是否使用相同网络出口，再根据具体错误诊断。部分客户端还需要 PO Token；这是另一项要求，当前项目未内置 Token Provider，参考 [yt-dlp PO Token 指南](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)。不要在明确的登录错误下持续自动重试。

## 七、排错与边界

- Google 密钥配置只决定公开内容检索能否使用；yt-dlp 获取音轨不依赖该密钥。
- 网络能访问 Google API，不代表服务器 IP 一定能下载 YouTube 媒体；反过来也一样。
- Google 配额、密钥限制和 API 启用状态会影响发现；YouTube 的登录要求、IP 限制、地区限制和平台变化会影响音轨下载。
- 默认不读取浏览器 Cookie；仅使用你通过 `EARSPACE_YOUTUBE_COOKIES_FILE` 显式指定的 Cookie 文件，不自动完成验证码／登录，也不保证所有公开视频都能下载。
- yt-dlp 需要维护更新；依赖锁定在 uv.lock，可用 `uv lock --upgrade-package yt-dlp` 更新，测试后重建镜像。不要关闭 TLS 验证来处理网络错误。
- 只下载、保存、转写自己有权处理的内容。项目的音轨格式选择和工具参数参考 [yt-dlp 官方文档](https://github.com/yt-dlp/yt-dlp#format-selection)。
