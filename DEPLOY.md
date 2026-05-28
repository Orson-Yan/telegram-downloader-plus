# Telegram Media Downloader - 部署指南

## 前置条件

1. NAS 上已安装 Docker
2. 有 Telegram API 凭证 (api_id + api_hash)
3. 从 https://my.telegram.org 获取

## 第一步: 上传代码到 NAS

```bash
# 在本地 push 到 GitHub
cd D:/Hermes/projects/telegram_media_downloader
git add .
git commit -m "feat: 重写 UI - 现代浅色主题, 任务管理功能"
git push origin main

# 在 NAS 上 clone
ssh Henry@192.168.1.22
cd /volume1/docker
git clone https://github.com/MangoIsIllegal/telegram_media_downloader.git
cd telegram_media_downloader
```

## 第二步: 配置

编辑 `config.yaml`，填入你的 Telegram API 凭证:

```yaml
api_hash: 你的api_hash
api_id: 你的api_id
chat:
  - chat_id: 你要下载的频道/群组ID
    last_read_message_id: 0
file_formats:
  audio:
    - all
  document:
    - all
  video:
    - all
file_path_prefix:
  - chat_title
  - media_datetime
media_types:
  - audio
  - photo
  - video
  - document
  - voice
  - video_note
```

## 第三步: 构建并启动

```bash
# 构建镜像
docker-compose build

# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 首次启动需要登录 Telegram, 查看日志获取验证码
docker-compose logs -f telegram_media_downloader
```

## 第四步: 访问 Web UI

浏览器打开: http://192.168.1.22:5000

无需登录，直接进入控制面板。

## 常用命令

```bash
# 停止
docker-compose down

# 重启
docker-compose restart

# 更新代码后重新构建
git pull
docker-compose build
docker-compose up -d

# 查看容器状态
docker-compose ps
```

## 代理配置

如果需要代理，编辑 docker-compose.yaml 中的 environment 部分:

```yaml
environment:
  - http_proxy=socks5://你的代理IP:端口
  - https_proxy=socks5://你的代理IP:端口
```

## 故障排查

1. **容器无法启动**: 检查 `docker-compose logs`
2. **无法连接 Telegram**: 检查代理配置
3. **下载失败**: 在 Web UI 的 "失败" Tab 查看原因
4. **权限问题**: 确保挂载目录有写入权限
