# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Hermes Telegram Downloader —— 基于 Pyrogram 的 Telegram 媒体下载 / 转发 / 监听工具，带任务持久化、崩溃恢复和 Flask WebUI。本项目是 [tangyoha/telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader) v2.2.6 的深度 fork；fork 自身的改动（任务持久化、pending 队列、重写的 WebUI、非阻塞 FLOOD_WAIT 处理）记录在 `README.md` 底部的「相比上游的改动」一节。README.md / README_CN.md 是权威的功能文档，比一般项目详细得多 —— 做较大改动前请先阅读。

## 常用命令

```bash
# 本地 WebUI 开发 —— 无需 Telegram 账号，run_local.py 提供 mock 数据
python run_local.py                    # http://localhost:5000

# 完整运行（需要填好 config.yaml 真实凭证，首次运行需交互式登录 TG）
python media_downloader.py

# Docker（生产）
docker-compose run --rm hermes-telegram-downloader   # 首次前台运行，用于登录 TG
docker-compose up -d                                 # 之后后台运行
docker-compose logs -f
```

**没有测试套件、linter 或构建步骤。** `pip install -r requirements.txt` 安装依赖 —— 注意 Pyrogram 是从 GitHub zip 拉取的定制 fork（`tangyoha/pyrogram` 的 `patch` 分支），并非 PyPI 上的 Pyrogram。

**前端开发**：`run_local.py` 是最快的迭代方式。它是一个独立的 Flask 应用，用硬编码的 mock 数据渲染真实的 `module/templates/index.html` 和 `module/static/`，完全不碰 Pyrogram 或 `module/web.py`。它的 mock JSON 结构与真实 API 响应保持一致 —— 修改 API 契约时记得同步更新。

## 架构

单进程、单 asyncio 事件循环（`app.loop`），外加一个后台线程运行 Web 服务器。

**入口**：`media_downloader.py` 的 `main()`。它创建 Pyrogram 用户客户端、启动事件循环、在循环运行**之后**才创建全局 `asyncio.Queue`（在 import 时创建会绑定到错误的 loop，导致 worker 静默地永远收不到任务）、启动 `max_download_task` 个 worker 协程、启动 bot，然后进入 `run_until_all_task_finish()`。

**两个 Pyrogram 客户端**：
- 用户客户端 `"media_downloader"` —— 负责实际下载；`/listen_forward` 和 config 频道的实时监听 handler 注册在**这个**客户端上。
- Bot 客户端（`module/bot.py` 的 `DownloadBot`）—— 处理 `/download`、`/forward` 等命令。仅在设置了 `bot_token` 时启动。

**全局单例**（import 时的模块级状态，各处引用）：
- `app` = `Application`（`module/app.py`）—— 配置、loop、`chat_download_config`、客户端引用。核心枢纽。
- `queue`（`media_downloader.py`）—— 喂给 worker 的 `(message, node)` 元组。
- `module/download_stat.py` 里的模块级字典 —— `_download_result`、失败列表、速度、暂停标志。这是 WebUI 读取的实时内存状态。

**TaskNode**（`module/app.py`）是每条命令的工作单元（一个频道 + 消息 ID 范围 + 过滤器 + 进度计数器）。Bot handler 构建 TaskNode、持久化它，然后针对它逐条把消息入队。

### 任务生命周期与持久化

`log/` 下三个 JSON 文件（见 `module/task_store.py` 和 `module/download_stat.py`），原子写入：
- `bot_tasks.json` —— 活跃 / pending 任务（用于崩溃恢复）
- `task_counter.json` —— `MMDD-序号` 任务 ID 计数器（每日重置，持久化）
- `download_history.json` —— WebUI 显示的完成 + 失败记录

流程：命令 → `save_task()`（持久化）→ `add_download_task()`（入队）→ `worker()` → `download_task()` → `download_media()` → 进度回调更新 `_download_result` → `complete_task()` 从持久化移除 + `save_downloads()` 写入历史。失败进入 `add_failed_download()`，可从 WebUI 重试。

### Pending 消费者（fork 的核心机制）

重启时未完成任务**不会**直接恢复 —— 它们全部重置为 `pending` 状态。`_pending_consumer_loop()`（`module/bot.py`，每 5 秒）通过 `_consume_one_pending()` 最多把 `max_download_task` 个 pending 任务喂入 worker 队列。这样限制了重启节奏，避免 100+ 个恢复的任务同时调用 `get_messages` 引发 FLOOD_WAIT 风暴。`queued` 状态 = 已交给 worker；`pending` = 仍在等待。

### FLOOD_WAIT 处理

全程非阻塞：用冷却时间戳而非 `sleep`，因此单个被限速的任务永不卡死整个队列。冷却存在于全局（pending 消费者）、单文件（下载）、单 node（消息编辑）三个层级。

### Web 服务器

`module/web.py` 的 `init_web()` 在 `threading.Thread` 里启动 Flask（不在 asyncio loop 上）。路由读取共享的内存字典 / 持久化 JSON，并通过 `download_stat.py` / `task_store.py` 改变任务状态。完整路由列表见 README.md 的「Web API」。

### 值得注意的 monkeypatch 与坑

- `main()` 在创建客户端前把 `pyrogram...TCP.TIMEOUT` 从默认 10s 改为 `900` —— 防止 TG 限速时的重连风暴。**保留它。**
- 上面提到的「queue 在 loop 启动后创建」是关键约束，不要把 queue 创建移到 import 时。
- 配置文件是 `config.yaml`（从 `config.yaml.example` 复制）；运行时可变数据（如 `ids_to_retry`）放在 `data.yaml`。

### 模块地图

| 文件 | 职责 |
|------|------|
| `media_downloader.py` | 入口、worker 池、`download_media()` 核心 |
| `module/app.py` | `Application` 单例、`TaskNode`、枚举、配置访问 |
| `module/bot.py` | Bot 命令 handler、pending 消费者、监听/转发逻辑 |
| `module/task_store.py` | 任务持久化 + 崩溃恢复（JSON、原子、线程安全） |
| `module/download_stat.py` | 实时下载状态、速度、失败/完成历史、暂停/恢复 |
| `module/web.py` | Flask 应用 + 所有 HTTP 路由（在独立线程运行） |
| `module/pyrogram_extension.py` | Pyrogram 辅助/补丁（最大的支撑模块） |
| `module/filter.py` | 下载过滤表达式解析（基于 ply） |
| `module/get_chat_history_v2.py`、`module/send_media_group_v2.py` | 打补丁后的 Pyrogram 行为 |
| `module/cloud_drive.py` | 下载后可选的 rclone 上传 |
| `module/language.py` | i18n 字符串（ZH/EN/RU/UA） |
| `utils/` | 格式化、日志（loguru，`tdl.log` + `download.log`）、平台辅助 |
