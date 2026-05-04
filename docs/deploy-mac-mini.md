# Hermes Agent 部署指南 — Mac mini (Apple Silicon)

本文档用于在一台全新的 Mac mini (Apple Silicon / arm64) 上，通过 Docker 部署 Hermes Agent Gateway 并接入飞书。

镜像已预构建并推送至阿里云 ACR，部署时**无需克隆仓库、无需本地编译**。

---

## 1. 前置条件

目标 Mac mini 需要：

- macOS（Apple Silicon / arm64）
- Docker Desktop 或 OrbStack（任选一个）
- 能访问阿里云 ACR（`modelbest-registry.cn-beijing.cr.aliyuncs.com`）
- 能访问飞书开放平台（`open.feishu.cn`）
- 至少一个 LLM provider 的 API key

验证 Docker 可用：

```bash
docker --version
docker compose version
```

如果未安装 Docker，推荐安装 OrbStack（macOS 上更轻量）：

```bash
brew install orbstack
```

---

## 2. 登录阿里云 ACR 并拉取镜像

```bash
docker login modelbest-registry.cn-beijing.cr.aliyuncs.com
```

输入阿里云 ACR 的用户名和密码。

拉取预构建的 arm64 镜像：

```bash
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-arm64
```

给镜像打一个短名 tag，方便后续使用：

```bash
docker tag modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-arm64 hermes-agent:latest
```

验证镜像：

```bash
docker run --rm hermes-agent:latest --version
```

期望输出包含：

```text
Hermes Agent v0.12.0
```

---

## 3. 初始化数据目录

所有持久化数据（配置、密钥、会话、日志、技能）存放在宿主机 `~/.hermes/`，容器内映射为 `/opt/data`。

```bash
mkdir -p ~/.hermes
```

首次启动时，容器 entrypoint 会自动生成以下文件：

```text
~/.hermes/.env          # API 密钥
~/.hermes/config.yaml   # 配置
~/.hermes/SOUL.md       # Agent 人格文件
~/.hermes/skills/       # 内置技能
~/.hermes/sessions/     # 会话历史
~/.hermes/logs/         # 运行日志
```

---

## 4. 创建 docker-compose.yml

在任意工作目录（例如 `~/hermes-deploy/`）创建 `docker-compose.yml`：

```bash
mkdir -p ~/hermes-deploy && cd ~/hermes-deploy
```

写入以下内容：

```yaml
services:
  gateway:
    image: hermes-agent:latest
    container_name: hermes
    restart: unless-stopped
    network_mode: host
    volumes:
      - ~/.hermes:/opt/data
    environment:
      - HERMES_UID=${HERMES_UID:-501}
      - HERMES_GID=${HERMES_GID:-20}
    command: ["gateway", "run"]

  dashboard:
    image: hermes-agent:latest
    container_name: hermes-dashboard
    restart: unless-stopped
    network_mode: host
    depends_on:
      - gateway
    volumes:
      - ~/.hermes:/opt/data
    environment:
      - HERMES_UID=${HERMES_UID:-501}
      - HERMES_GID=${HERMES_GID:-20}
    command: ["dashboard", "--host", "127.0.0.1", "--no-open"]
```

> 说明：macOS 默认用户 UID 是 501、GID 是 20（staff）。如果不确定，执行 `id -u` 和 `id -g` 获取。

---

## 5. 配置 LLM Provider

### 方式 A：交互式 setup（推荐首次使用）

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) \
  docker compose run --rm gateway setup
```

按向导选择模型供应商（如 OpenRouter / Anthropic / GLM / Google 等），填入 API key。

### 方式 B：直接编辑 .env

```bash
nano ~/.hermes/.env
```

写入你的 LLM provider key，例如：

```bash
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
```

如需修改默认模型，编辑 `~/.hermes/config.yaml` 中 `model.default` 字段。

---

## 6. 配置飞书

### 6.1 在飞书开放平台创建应用

1. 访问 https://open.feishu.cn/（国际版：https://open.larksuite.com/）
2. 创建一个企业自建应用
3. 启用 **Bot** 能力
4. 记录 `App ID` 和 `App Secret`
5. 在「事件订阅」中选择 **使用长连接接收事件**（即 WebSocket 模式）
6. 添加以下事件订阅权限（按实际需要调整）：
   - `im:message`（接收消息）
   - `im:message:send_as_bot`（以机器人身份发消息）
   - `im:resource`（读取图片/文件，如需要）

### 6.2 写入飞书凭据

编辑 `~/.hermes/.env`，追加：

```bash
# 飞书应用凭证
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx

# 域名：feishu（国内）或 lark（国际版）
FEISHU_DOMAIN=feishu

# 连接模式：websocket（推荐，无需公网回调）
FEISHU_CONNECTION_MODE=websocket

# DM 访问控制（二选一）
# 方式 1：仅允许指定用户（推荐，填飞书 open_id）
FEISHU_ALLOW_ALL_USERS=false
FEISHU_ALLOWED_USERS=ou_xxxxxxxx

# 方式 2：允许所有用户（不建议生产环境）
# FEISHU_ALLOW_ALL_USERS=true

# 群聊策略：open = 允许群聊（需 @机器人）；disabled = 关闭群聊
FEISHU_GROUP_POLICY=open
```

也可以使用交互式向导配置飞书：

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) \
  docker compose run --rm gateway gateway setup
```

选择 **Feishu / Lark**，按提示操作。

---

## 7. 启动服务

```bash
cd ~/hermes-deploy

HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

查看启动日志：

```bash
docker compose logs -f gateway
```

看到以下日志说明飞书连接成功：

```text
[Feishu] Connected in websocket mode (feishu)
```

---

## 8. 验证

### 8.1 容器状态

```bash
docker compose ps
```

期望 `hermes` 和 `hermes-dashboard` 都是 `running`。

### 8.2 Hermes 诊断

```bash
docker compose exec gateway hermes doctor
docker compose exec gateway hermes status
```

### 8.3 飞书验证

1. 在飞书中找到你创建的机器人，直接发一条私聊消息。
2. 如果选择了 pairing 模式，首次私聊会收到配对码，需在宿主机批准：

```bash
docker compose exec gateway hermes pairing list
docker compose exec gateway hermes pairing approve feishu <配对码>
```

3. 群聊中需要先把机器人拉入群，然后 `@机器人` 发消息。

### 8.4 Dashboard（可选）

本机浏览器访问：

```text
http://127.0.0.1:9119
```

如果从其他机器远程访问，使用 SSH 隧道：

```bash
ssh -L 9119:127.0.0.1:9119 user@mac-mini-ip
```

---

## 9. 常用运维命令

```bash
# 查看日志
docker compose logs -f gateway

# 查看 Hermes 日志文件
docker compose exec gateway hermes logs --level info

# 重启 gateway
docker compose restart gateway

# 停止所有服务
docker compose down

# 进入容器交互
docker compose exec gateway bash

# 在容器内运行 Hermes CLI
docker compose exec gateway hermes

# 备份数据
tar -czf hermes-backup-$(date +%Y%m%d).tar.gz -C ~ .hermes
```

---

## 10. 升级镜像

当有新版镜像推送到 ACR 后：

```bash
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:<新版本>
docker tag modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:<新版本> hermes-agent:latest

cd ~/hermes-deploy
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

数据目录 `~/.hermes/` 不受影响，配置和会话自动保留。

---

## 11. 安全注意事项

- **不要**设置 `GATEWAY_ALLOW_ALL_USERS=true`，除非你清楚后果。
- **不要**把 Dashboard 暴露到公网（它能操作 API 密钥和 Agent 配置）。
- **不要**同时运行两个 gateway 容器挂载同一个 `~/.hermes`。
- `~/.hermes/.env` 包含明文密钥，确保宿主机文件权限合理（`chmod 600 ~/.hermes/.env`）。
- 同一个 `FEISHU_APP_ID` 同时只能被一个 Hermes 实例使用（有 scoped lock 保护）。

---

## 附：最小快速部署命令汇总

```bash
# 1. 拉取镜像
docker login modelbest-registry.cn-beijing.cr.aliyuncs.com
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-arm64
docker tag modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-arm64 hermes-agent:latest

# 2. 初始化
mkdir -p ~/.hermes ~/hermes-deploy && cd ~/hermes-deploy
# （手动创建 docker-compose.yml，见第 4 节）

# 3. 交互式配置 LLM + 飞书
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose run --rm gateway setup
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose run --rm gateway gateway setup

# 4. 启动
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d

# 5. 查看日志
docker compose logs -f gateway
```
