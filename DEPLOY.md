# Hermes Telegram Downloader - 部署指南

## 前置条件

1. 服务器/NAS 上已安装 Docker 和 Docker Compose
2. 有 Telegram API 凭证（api_id + api_hash），从 https://my.telegram.org 获取
3. 已创建 Bot Token（通过 @BotFather）

## 第一步：获取代码

```bash
git clone https://github.com/MangoIsIllegal/hermes-telegram-downloader.git
cd hermes-telegram-downloader
```

## 第二步：配置

复制示例配置并填入你的凭证：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`：

```yaml
api_hash: 你的api_hash
api_id: 你的api_id
bot_token: 你的bot_token

media_types:
  - audio
  - document
  - photo
  - video
  - voice
  - animation

file_formats:
  audio:
    - all
  document:
    - all
  video:
    - all

save_path: /app/downloads
max_download_task: 5

web_host: 0.0.0.0
web_port: 5000

allowed_user_ids:
  - 'me'

language: ZH
```

## 第三步：配置 docker-compose.yaml

根据你的实际环境修改 `docker-compose.yaml` 中的挂载路径和代理：

```yaml
services:
  hermes-telegram-downloader:
    build: .
    container_name: hermes-telegram-downloader
    network_mode: bridge
    stdin_open: true
    tty: true
    ports:
      - "5000:5000"
    environment:
      - TZ=Asia/Shanghai
      # 如果需要代理，取消注释并修改为你的代理地址
      # - http_proxy=socks5://172.17.0.1:1080
      # - https_proxy=socks5://172.17.0.1:1080
    volumes:
      # 下载目录（修改为你的实际路径）
      - "/your/download/path:/app/downloads/"
      # 配置文件
      - "./config.yaml:/app/config.yaml"
      - "./data.yaml:/app/data.yaml"
      # 日志
      - "./log:/app/log/"
      # Telegram session
      - "./sessions:/app/sessions"
      # 临时文件
      - "./temp:/app/temp"
    restart: unless-stopped
```

> **注意**：如果你不需要模块热挂载（bind mount 单个 .py 文件），直接 `docker-compose build && docker-compose up -d` 即可，代码在构建时已经 COPY 进镜像。模块热挂载只用于开发调试。

## 第四步：构建并启动

```bash
# 构建镜像
docker-compose build

# 首次启动（前台，用于登录 Telegram）
docker-compose run --rm hermes-telegram-downloader

# 首次启动会要求输入手机号和验证码，按提示完成登录
# 登录成功后 Ctrl+C 退出

# 后台启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

## 第五步：访问 Web UI

浏览器打开 `http://你的服务器IP:5000`

无需登录，直接进入控制面板。

## 常用命令

```bash
# 停止
docker-compose down

# 重启
docker-compose restart

# 更新代码后重新构建
git pull && docker-compose build && docker-compose up -d

# 查看容器状态
docker-compose ps
```

## 数据持久化

| 路径 | 说明 |
|------|------|
| `./downloads/` | 下载的文件 |
| `./config.yaml` | 配置文件 |
| `./data.yaml` | 运行时数据（ids_to_retry 等） |
| `./log/` | 日志 + 任务持久化（bot_tasks.json / task_counter.json / download_history.json） |
| `./sessions/` | Telegram session 文件 |
| `./temp/` | 下载临时文件 |

## 代理配置

如果你的服务器无法直接访问 Telegram，在 `docker-compose.yaml` 的 `environment` 中配置代理：

```yaml
environment:
  - http_proxy=socks5://你的代理IP:端口
  - https_proxy=socks5://你的代理IP:端口
```

或者在 `config.yaml` 中配置 Pyrogram 代理：

```yaml
proxy:
  scheme: socks5
  hostname: 172.17.0.1
  port: 1080
```

## 故障排查

1. **容器无法启动** — 检查 `docker-compose logs`
2. **无法连接 Telegram** — 检查代理配置，确保容器能访问 `149.154.167.50:443`
3. **下载失败** — 在 Web UI 的"失败"Tab 查看具体原因
4. **权限问题** — 确保挂载目录有写入权限
5. **session 丢失** — 确保 `./sessions/` 目录已正确挂载
6. **FLOOD_WAIT** — 频道消息过多触发了 Telegram 限速，等待冷却后自动恢复
