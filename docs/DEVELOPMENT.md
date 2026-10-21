# 开发、架构与验证

## 本机运行

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.10.4+、FFmpeg／ffprobe、Node.js 22+；后两者需能从 PATH 调用。项目默认 Python 版本由 `.python-version` 固定为 3.12，uv 会查找或自动安装对应解释器，不依赖系统默认的 Python。

```bash
uv sync --locked
uv run --locked scripts/init_env.py
```

编辑 `.env`，然后在两个终端分别启动：

```bash
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

```bash
uv run --locked python -m app.worker
```

Linux、macOS 和 Windows PowerShell 使用相同的 uv 命令，无需手动激活 `.venv`。应用从当前项目根目录读取 `.env`，不会覆盖已设置的环境变量。API 不启动内嵌下载线程；务必运行独立 Worker。打开 <http://127.0.0.1:8000/> 测试，两个终端分别按 Ctrl+C 停止。

`uv sync --locked` 默认安装基础依赖和 dev 组的测试工具，不安装 Whisper。只需运行服务时可使用 `uv sync --locked --no-dev`，相应启动命令也加 `--no-dev`，例如 `uv run --locked --no-dev python -m app.worker`。

只有需要本地语音转写时，才运行 `uv sync --locked --extra transcription` 并设置 `EARSPACE_WHISPER_ENABLED=true`。此时 API 和 Worker 的 `uv run` 命令都加 `--extra transcription`，例如 `uv run --locked --extra transcription python -m app.worker`。依赖会安装到同一个 `.venv`；模型由首次实际转写另行下载。Docker 根据 `EARSPACE_WHISPER_ENABLED` 构建参数选择同一锁文件中的可选依赖。

## 目录与数据流

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 接口、鉴权、同源网页、签名流与 Range 文件 |
| `app/config.py` | 环境配置与启动校验 |
| `app/db.py` | SQLite schema、连接与节目记录 |
| `app/youtube.py` | Google API、搜索／频道发现、查询缓存 |
| `app/recommend.py` | 可解释排序、反馈与文稿难度估计 |
| `app/jobs.py` | 入队、租约、重试、取消、所有权隔离 |
| `app/worker.py` | 队列轮询、任务子进程、心跳与超时控制 |
| `app/runner.py` | 单个任务执行与结果提交 |
| `app/media.py` | 音轨／MP4 下载、FFmpeg、字幕、可选 Whisper |
| `app/transcripts.py` | JSON3 原始字幕、版本化句子整理、重建缓存与 VTT 导出 |
| `app/web/sentence-player.js` | 统一时间查询、单句循环计时与播放器事件处理 |
| `app/maintenance.py` | 未引用媒体和工作目录的只读检查／显式清理 |
| `app/web/` | 无构建的 HTML、CSS 与原生 JavaScript 网页 |

SQLite 开启 WAL、外键与 busy timeout，schema 版本目前为 1。本次兼容升级仅通过 `CREATE TABLE IF NOT EXISTS` 增加可丢弃的 `transcript_sentences` 缓存表，`transcripts` 的四列结构和原始 JSON 保持兼容，不执行原表覆写。升级前使用 SQLite backup API 或停服务后完整备份数据；不要仅复制运行中的主数据库而遗漏 WAL。后续破坏性结构调整需显式迁移。

新下载请求默认音频模式。任务 payload 的 `mode` 值为 `audio` 或 `video`；没有该字段的旧任务恢复为视频模式以兼容历史行为。`public_job` 返回 `media_mode`，不暴露完整内部 payload 或租约 owner。

Worker 定期续租；120 秒没有有效心跳的运行任务会在下一次 claim 时恢复，达到尝试上限则失败。每个任务在单独进程运行，取消／总超时会停止子进程树。同一节目同类型任务去重，下载和转写互斥；不同下载模式的重复请求明确报 409。

可选 Cookie 文件通过 `EARSPACE_YOUTUBE_COOKIES_FILE` 指定。下载子进程从 Netscape 文件中过滤 YouTube 域名 Cookie，将其写入每次任务的临时私有文件；所有 yt-dlp 调用复用该副本。源文件不写入，文件路径与 Cookie 不进入 API 返回或媒体发布目录。明确登录验证和 Cookie 配置错误不自动重试；临时网络错误仍按队列策略重试。配置与 Docker 只读挂载见 [YouTube 接入](YOUTUBE.md#六youtube-登录验证与-cookie-文件)。

媒体输出按每次尝试隔离，成功时在同一数据库事务中提交路径、文稿和任务结果。旧 Worker 不能提交过期结果。单独下载音频会保留以前缓存的 MP4。AAC 使用 stream copy 保留压缩音频包；非 AAC 才单线程转换为 AAC 96 kbps，任务结果记录 `audio_processing`。

## 测试

```bash
uv sync --locked
uv run --locked pytest -q
node --check app/web/app.js
node --check app/web/sentence-player.js
node --test tests/sentence-player.test.cjs
docker compose --env-file .env.example config --quiet
```

测试使用临时数据库与模拟 YouTube 传输，验证鉴权、CORS、请求大小、URL 白名单、签名、Range、反馈排序、模式冲突、并发去重、租约恢复、旧 Worker 隔离、转写／下载互斥与文稿处理。句子测试覆盖中英文标点、缩写／小数、片段合并、句中拆分、重复发言保留、词级偏移、无标点降级、原文保留、重建幂等和并发缓存失效；Node 内建测试使用可控媒体时钟验证循环边界、倍速、拖动、缓冲、末句和音视频切换。媒体集成测试调用真实 FFmpeg／ffprobe 和合成媒体，验证 MP4、AAC 直接复制、Opus 转 AAC 与已有视频保留；AAC 复制前后的逐包 SHA-256 对比用于确认没有重新编码。

缺少 FFmpeg／ffprobe 时对应测试跳过。真实 YouTube 搜索与下载、服务器 IP 限制、可选 Whisper 的模型下载与效果需要在部署环境验收。可选转写并未包含模型性能基准。

GitHub Actions 在 Linux 上跑测试、脚本检查、镜像构建、Compose 启动和鉴权状态探测；不设置真实 YouTube 密钥，不运行真实 YouTube 下载。

## 依赖与发行包

`pyproject.toml` 声明基础依赖、`transcription` 可选依赖和 dev 依赖组；`uv.lock` 是唯一的依赖锁文件，覆盖跨平台运行、开发测试与可选转写依赖，并提交到 Git。不要手动编辑锁文件。新增或更新依赖：

```bash
uv add <package>
uv add --dev <package>
uv lock --upgrade-package yt-dlp
uv sync --locked
```

手动修改 `pyproject.toml` 后运行 `uv lock`。`uv lock --check` 检查锁文件是否匹配；`uv sync --locked` 与 `uv run --locked` 在锁文件过期时直接报错，避免部署时意外解析新版本。更新后测试并重建镜像，尤其 yt-dlp 需要跟随上游提取机制变化。

Docker 与 CI 固定使用 uv 0.10.4；升级 uv 时同时更新 Dockerfile 和 CI 中的版本。镜像分层安装依赖，排除 dev 组，默认排除转写依赖，最终安装为非 editable 项目。运行时通过 PATH 使用 `/app/.venv/bin` 中的 Python 和命令，不在启动时联网同步依赖。Compose 的 Worker、健康检查和维护命令因此仍直接使用 `python`。Docker 使用 `libgomp1` 支持可选 CPU 模型运行。

生成包含代码、文档、配置模板和测试的源码包：

```bash
uv run --locked scripts/package_release.py
```

输出 `artifacts/earspace-server.zip`。脚本使用明确的文件／目录范围，排除真实 `.env`、数据库、媒体、虚拟环境及 Git 元数据；运行前仍应检查自定义文档中没有敏感信息。GitHub 上提交源码，不提交本地产生的发行 ZIP。
