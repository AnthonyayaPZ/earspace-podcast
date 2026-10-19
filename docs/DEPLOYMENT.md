# 服务器部署与运维

推荐 Linux + Docker Engine + Compose 插件。服务由 API 和后台 Worker 组成，SQLite／媒体／模型共用一个持久化数据卷。单用户使用，不需要单独安装数据库或 Node 前端环境。

## 1. 从 GitHub 获取代码

仓库：<https://github.com/AnthonyayaPZ/earspace-podcast>。仓库为私有，需要使用有读取权限的 GitHub 账号。

已配置服务器 SSH Key 的情况：

```bash
git clone git@github.com:AnthonyayaPZ/earspace-podcast.git
cd earspace-podcast
```

也可以使用 HTTPS 克隆；需要身份验证时，使用 GitHub 支持的凭据管理器或 Personal Access Token，不使用 GitHub 登录密码。不要把 token 写入克隆 URL、脚本或 `.env`。部署服务器只需仓库读取权限，不需要写权限。

```bash
git clone https://github.com/AnthonyayaPZ/earspace-podcast.git
cd earspace-podcast
```

检查基础环境：

```bash
docker --version
docker compose version
python3 --version
```

宿主机的 Python 只用来运行初始化脚本；应用在镜像中使用 Python 3.12。建议至少从 2 核、4GB 内存和足够媒体存储空间开始测试。可选语音模型、长视频转码需要更多资源。服务器必须能访问 GitHub、镜像仓库、Python 软件源、Google APIs、YouTube 及其媒体域名。

## 2. 初始化并配置密钥

```bash
python3 scripts/init_env.py
chmod 600 .env
nano .env
```

初始化脚本从 `.env.example` 生成 `.env` 并产生随机 EARSPACE_API_KEY；已有文件不会被覆盖。

| 配置 | 用途 | 首次部署 |
| --- | --- | --- |
| `EARSPACE_API_KEY` | 你访问本应用的密钥，包括网页连接与业务 API | 保留随机生成值 |
| `YOUTUBE_API_KEY` | 后端调用 Google YouTube Data API 的密钥 | 按 [YouTube 接入文档](YOUTUBE.md) 获取并填写 |
| `EARSPACE_CORS_ORIGINS` | 允许的独立前端来源 | 同源网页可留空；不要填 `*` |
| `EARSPACE_SEARCH_TTL_SECONDS` | YouTube 查询缓存秒数 | 默认 21600，即 6 小时 |
| `EARSPACE_JOB_TIMEOUT_SECONDS` | 一次任务的总处理上限 | 默认 7200 秒 |
| `EARSPACE_JOB_MAX_ATTEMPTS` | 自动尝试次数上限 | 默认 2 |
| `EARSPACE_MAX_DURATION_SECONDS` | 接受的视频时长上限 | 默认 14400 秒 |
| `EARSPACE_MAX_FILE_MB` | 单个媒体文件的大小上限 | 默认 2048 MB |
| `EARSPACE_MIN_FREE_MB` | 开始下载前的剩余磁盘门槛 | 默认 2048 MB |
| `EARSPACE_WHISPER_ENABLED` | 是否允许本地 CPU 语音转写 | 先保留 `false` |
| `EARSPACE_WHISPER_MODEL` | 可选 Whisper 模型 | 默认 `small` |

Compose 会将容器内 `EARSPACE_DATA_DIR` 固定为 `/data`，无需修改宿主机路径。两个密钥用途不同：不要把 Google 的密钥输入网页连接框；不要把应用密钥填入 Google Cloud。

## 3. 构建与启动

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
docker compose logs --tail 100 api worker
curl --fail http://127.0.0.1:8000/health
```

`/health` 只反映 API 存活。继续检查 Worker 和依赖：

```bash
docker compose exec -T api python -c 'import os,json,urllib.request; r=urllib.request.Request("http://127.0.0.1:8000/api/v1/status",headers={"Authorization":"Bearer "+os.environ["EARSPACE_API_KEY"]}); print(json.dumps(json.load(urllib.request.urlopen(r)),ensure_ascii=False,indent=2))'
```

确认 `workers_online >= 1`、`ffmpeg_available`、`ffprobe_available`、`node_available` 为真，`youtube_discovery_configured` 在填写密钥后为真。该状态只检查密钥是否配置，不证明密钥有效；创建发现任务才能验证 Google API 授权和配额。

## 4. 从自己的电脑打开网页

默认端口只监听服务器本地 `127.0.0.1:8000`。先通过 SSH 隧道测试：

```bash
# 在自己的电脑运行；替换服务器登录信息
ssh -N -L 8000:127.0.0.1:8000 your-user@your-server
```

保持 SSH 连接，在本机打开 <http://127.0.0.1:8000/>。输入服务器 `.env` 中的 EARSPACE_API_KEY，连接后设置兴趣、切换中英文分区，再点“更新推荐”。任务中可查看内容发现／音频下载进度。

API 交互文档位于 `/docs`，点击 Authorize 输入同一个应用密钥。网页没有单独构建步骤，与 API 同源运行。

需要域名访问时，使用现有 HTTPS 反向代理；`deploy/Caddyfile` 提供宿主机 Caddy 示例，替换域名后转发到 `127.0.0.1:8000`。Caddy 若运行在另一个 Docker 容器，应按其网络设置上游地址，不能直接照搬宿主机回环地址。不要在访问日志中记录签名流 URL 的查询参数。

## 5. 端到端验收

1. 保存兴趣、收听时长、英语水平和可选频道 ID。
2. 点“更新推荐”，等待任务完成，分别查看中文与英文结果。
3. 对已选择的节目点“准备收听”，默认只下载音轨并生成 M4A 和可用字幕。
4. 音频就绪后“开始收听”，再点“音频 ↓”将 M4A 保存到自己的设备。
5. 确认播放进度会保存，文稿可跳转与循环；无字幕时会提示，而不会显示虚构文稿。
6. 若需要视频，在导入时选择“MP4 + 音频 + 文稿”，或在音频节目的待听卡片点击“准备 MP4”。

只处理你有权下载、保存和转写的媒体。YouTube Data API 提供内容检索和元数据，实际音轨由 yt-dlp 获取；详情见 [YouTube 接入](YOUTUBE.md)。

## 6. 更新、停止与备份

更新源码：

```bash
git pull --ff-only
docker compose up -d --build --wait --wait-timeout 180
```

只修改 `.env`：`docker compose up -d --force-recreate`。切换 Whisper 开关还需要重新构建镜像，以安装可选依赖。

`docker compose down` 保留数据卷；不要加 `-v`，除非要明确删除全部数据。升级前先停 API 和 Worker，备份完整命名卷，包括 SQLite、可能的 WAL、媒体及模型。卷挂载位置可通过 `docker volume ls` 与 `docker volume inspect` 查看。保留项目目录名称或显式维持原 Compose 项目名，避免生成另一份数据卷。

SQLite 数据卷应使用本地磁盘，不用 NFS，不跨机器运行 Worker。空闲 Worker 也有心跳；正常关闭后在线状态最多可能滞后约一分钟。

默认不会自动删除已下载内容。移出待听仅修改列表，不回收文件。检查／清理超过 24 小时且未被数据库引用的残留文件：

```bash
docker compose exec api python -m app.maintenance
# 看完只读报告后，按需执行实际清理
docker compose exec api python -m app.maintenance --prune
```

## 常见问题

| 现象 | 排查 |
| --- | --- |
| 构建镜像拉取失败 | Docker Hub、Debian 包源和 Python 软件源网络；查看具体构建日志 |
| `401`／网页无法连接 | 使用 EARSPACE_API_KEY，不能用 Google 密钥；配置修改后重建容器实例 |
| 发现任务返回 `503` | YOUTUBE_API_KEY 为空，或容器未加载新 `.env` |
| 发现任务 Google API 拒绝 | API 未启用、密钥限制不匹配、配额不足；见 YOUTUBE.md |
| 任务一直 queued | Worker 没启动、正在处理别的任务，或重试退避；查 `docker compose logs worker` |
| 任务中途重启 | 租约超时后自动恢复；超过尝试次数后可在网页重新尝试 |
| YouTube 要求登录／bot 验证 | IP／登录／地区访问限制；当前版本不读取 Cookie、不自动绕过验证 |
| 音频可播但文稿为空 | 没有对应字幕；可导入 JSON 或配置可选转写 |
| 仅音频任务无独立音轨 | 当前模式不回退为整视频下载；可显式选择 MP4 模式 |
| `409` 下载模式冲突 | 同节目已有另一种模式的任务，完成或取消后再提交 |
| 测试时域名跨域失败 | 优先用同源网页；独立前端需填写精确 CORS 来源 |

## 验证范围

本地回归测试覆盖 HTTP／鉴权／任务恢复／推荐与真实 FFmpeg 合成媒体处理。GitHub Actions 另外执行 Linux 测试、镜像构建、Compose 启动和 Worker 连通性检查。CI 不使用真实 YouTube 密钥，不对真实 YouTube IP 限制或可选 Whisper 模型效果作保证。
