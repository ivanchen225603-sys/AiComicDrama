# AI 漫剧工作台

中文 | [English](README.en.md)

[![CI](https://github.com/ivanchen225603-sys/AiComicDrama/actions/workflows/ci.yml/badge.svg)](https://github.com/ivanchen225603-sys/AiComicDrama/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue) ![License](https://img.shields.io/badge/license-Apache--2.0-green)

从一句话创意到可下载成片的 AI 漫剧创作工作台：写剧本、定角色、拆分镜、生成画面与视频、配音字幕、剪辑导出，一站完成。

![AI 漫剧工作台演示](screenshots/demo.gif)

| 项目总览 | 分镜 | 画面生成 |
|---|---|---|
| ![项目总览](screenshots/overview.png) | ![分镜](screenshots/storyboard.png) | ![画面生成](screenshots/generate.png) |
| **剪辑** | **配音与字幕** | **模型设置** |
| ![剪辑](screenshots/timeline.png) | ![配音与字幕](screenshots/audio.png) | ![模型设置](screenshots/models.png) |

截图中的画面与配音是未填写任何密钥时的本机示例素材。

## 功能

| 步骤 | 能力 |
|---|---|
| 写故事 / 定剧本 | 一句话生成剧本、小说改编、剧本诊断、版本对比与定稿 |
| 角色与场景 | 从剧本提取角色、场景、道具；生成形象图或上传参考图作为锚点 |
| 分镜 | AI 拆分镜头，逐镜编辑、锁定、排序 |
| 画面生成 | 批量生成关键帧（支持多候选）、批量图生视频、历史版本回退、生成进度与预计等待时间 |
| 配音与字幕 | 逐句配音按字幕时间轴对齐，背景音乐混音 |
| 剪辑与导出 | 时间线、本机 FFmpeg 合成成片（软字幕 / 硬字幕、多分辨率）、审片清单、导出与下载 |
| 运营 | 账号注册登录、积分与账单、用户自设使用上限、账本概览与对账报表、内容审核接口 |

**不填任何密钥即可完整体验**：画面、视频、配音使用本机生成的示例素材，全流程可走通、可导出可播放的示例成片。

**接入真实模型**：在页面「模型设置」里填写自己的密钥即可，不需要改代码或重启服务。

- **Liblib（必填才能出真实画面）**：AccessKey / SecretKey，在 [Liblib 开放平台](https://www.liblib.art/apis) 获取。填写后形象图、关键帧走星流文生图，视频走可灵图生视频，消耗的是你自己 Liblib 账户的积分。
- **文本模型（可选）**：任意 OpenAI 兼容接口（如 DeepSeek、通义千问），填写接口地址、模型名称与 API Key。不填时剧本生成、诊断与分镜使用内置规则。
- **配音（可选）**：任意 OpenAI 兼容的语音合成接口（`/audio/speech`，如 OpenAI、硅基流动），填写接口地址、模型、音色与 API Key；多个音色用逗号分隔，按角色自动分配。不填时成片只包含字幕和背景音乐。
- 使用自己的密钥时，生成费用记在你自己的平台账户上，本系统不再扣积分。
- 页面提供「检测连接」，只调用不产生费用的接口。
- 密钥按账号保存在服务端数据库，接口只返回打码后的视图，不会出现在页面数据、导出文件或日志中。

## 快速开始

### 用 Docker 一键启动

```bash
docker compose up -d --build
```

浏览器打开 <http://127.0.0.1:8001/>，首次进入先注册一个账号，然后在「模型设置」里填写密钥。镜像已包含 FFmpeg 与中文字体，数据保存在 `comic_state` 数据卷。

### 本机运行

需要 Python 3.11–3.13；成片合成需要本机安装 FFmpeg。

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .\.venv\Scripts\activate
python -m pip install -r requirements.lock
python -m pip install --no-deps .
python -m comicdrama
```

浏览器打开 <http://127.0.0.1:8001/>，本机访问无需登录。接口文档：<http://127.0.0.1:8001/docs>。一个数据目录只运行一个服务进程。

## 部署到公网服务器

| 环境变量 | 说明 |
|---|---|
| `COMIC_REQUIRE_LOGIN=true` | 访客必须注册 / 登录自己的账号。经 Nginx 反向代理部署时**必须打开**，否则所有访客会共用同一个本机账号 |
| `COMIC_ALLOWED_HOSTS='["你的域名"]'` | 允许用该域名访问（默认只允许本机地址） |
| `COMIC_DRAMA_PUBLIC_BASE_URL=https://你的域名` | 视频生成需要：Liblib 通过这个地址拉取首帧图片（服务器签发带有效期的免登录链接） |
| `COMIC_CREDIT_ADMINS='["账号ID"]'` | 可以给账号加积分、处理待复核任务的管理员 |

全部配置见 [.env.example](.env.example)。成片合成需要服务器安装 FFmpeg 与中文字体。

## 项目结构

```text
comicdrama/
  app.py            应用装配：鉴权、中间件、路由、静态页
  config.py         配置（COMIC_ 前缀环境变量）
  service.py        业务动作、积分预占与结算
  store.py          SQLite 存储
  credentials.py    用户自填密钥（打码保存、连接检测）
  providers/        生成能力：示例素材 / 真实模型 / 按账号混合
  liblib/           Liblib 开放平台客户端（签名、提交、轮询、取回素材）
  compose.py        FFmpeg 成片合成
  web/              前端页面（原生 JavaScript，无构建步骤）
tests/              Python 与前端测试
```

## 质量检查

每次提交由 [GitHub Actions](.github/workflows/ci.yml) 在 Linux 上自动运行：ruff 代码检查与格式检查、mypy 类型检查、Python 测试（覆盖率不低于 85%，含真实 FFmpeg 成片合成）、前端测试，以及 Docker 镜像构建与启动检查。本地一次跑完：

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy \n  && python -m pytest --cov=comicdrama --cov-fail-under=85 && node --test tests/test_ui_state.mjs
```

## 开发

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest
node --test tests/test_ui_state.mjs
python -m ruff check . && python -m ruff format --check . && python -m mypy
```

## 许可证

[Apache License 2.0](LICENSE)。生成内容须遵守所接入模型平台的服务条款，以及当地关于 AI 生成内容标识的规定。
