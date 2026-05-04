# Hermes Agent Docker + 飞书接入参考文档

本文基于当前仓库的 `Dockerfile`、`docker-compose.yml`、`gateway/platforms/feishu.py` 和 `hermes_cli/gateway.py` 整理，目标是用 Docker 跑起 Hermes Agent Gateway，并把它接入飞书/Lark。

## 1. 方案概览

Hermes 在 Docker 场景里有两层含义：

1. **Agent 本体跑在 Docker 容器里**：本文采用这个方案。Gateway、Dashboard、配置、会话和日志都在容器环境中运行。
2. **Agent 的 terminal tool 使用 Docker 后端**：这是另一种隔离执行命令的方式，不是本文重点。

当前项目的 Docker 运行态约定：

- 镜像代码目录：`/opt/hermes`
- 持久化数据目录：`/opt/data`
- 宿主机默认挂载：`~/.hermes:/opt/data`
- 配置文件：`~/.hermes/config.yaml`
- 密钥文件：`~/.hermes/.env`
- 日志目录：`~/.hermes/logs/`
- Docker entrypoint 会自动创建 `.env`、`config.yaml`、`SOUL.md`、`skills/` 等基础文件。

当前 `Dockerfile` 使用 `uv pip install -e ".[all]"`，因此镜像内已经包含飞书依赖 `lark-oapi`，不需要额外手动安装 `hermes-agent[feishu]`。

## 2. 推荐部署拓扑

推荐使用当前仓库自带的 `docker-compose.yml`：

- `gateway` 服务：运行 `hermes gateway run`
- `dashboard` 服务：运行 `hermes dashboard --host 127.0.0.1 --no-open`
- 两个服务共享同一个 `~/.hermes:/opt/data` 数据目录
- 默认 `network_mode: host`

飞书接入优先使用 **WebSocket 模式**：

- 不需要公网回调 URL
- 不需要暴露容器端口给飞书
- 本项目 setup 默认也推荐 WebSocket

Webhook 模式只建议在你明确需要公网回调、并能正确配置 HTTPS 反代时使用。

## 3. 前置准备

宿主机需要：

- Docker 或 Docker Desktop
- Docker Compose v2
- 能访问飞书开放平台
- 至少一个 LLM provider key，例如 `OPENROUTER_API_KEY`、`ANTHROPIC_API_KEY`、`GLM_API_KEY`、`GOOGLE_API_KEY` 等

检查命令：

```bash
docker --version
docker compose version
```

## 4. 构建镜像

在项目根目录执行：

```bash
cd /Users/huang/CodePrograms/hermes-agent
docker compose build
```

如果只想构建 gateway 使用的镜像，也可以：

```bash
docker build -t hermes-agent .
```

当前镜像会同时构建 Web Dashboard 和 TUI 资源，所以首次构建时间会比较长。

## 5. 初始化持久化目录

```bash
mkdir -p ~/.hermes
```

首次运行容器时，`docker/entrypoint.sh` 会自动生成：

```text
~/.hermes/.env
~/.hermes/config.yaml
~/.hermes/SOUL.md
~/.hermes/logs/
~/.hermes/sessions/
~/.hermes/skills/
```

如果你希望先让容器初始化这些文件，可以执行：

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose run --rm gateway status
```

macOS 上建议传入 `HERMES_UID` / `HERMES_GID`，避免容器写出的文件在宿主机不可编辑。当前 `docker-compose.yml` 已经读取这两个变量：

```bash
export HERMES_UID=$(id -u)
export HERMES_GID=$(id -g)
```

## 6. 配置 LLM Provider

有两种方式。

### 方式 A：运行交互式 setup（推荐）

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose run --rm gateway setup
```

按向导配置模型供应商、默认模型和飞书平台。

### 方式 B：直接编辑 `~/.hermes/.env`

例如使用 OpenRouter：

```bash
OPENROUTER_API_KEY=sk-or-...
```

默认模型等非密钥设置建议写在 `~/.hermes/config.yaml`，不要写进 `.env`。

## 7. 创建/配置飞书应用

进入飞书开放平台：

- 中国飞书：https://open.feishu.cn/
- 国际 Lark：https://open.larksuite.com/

推荐走项目内置向导自动配置：

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose run --rm gateway gateway setup
```

进入平台选择时选择 **Feishu / Lark**。当前项目支持两种配置方式：

1. **扫码自动创建机器人（推荐）**
   - 向导会调用飞书/Lark 注册流程
   - 默认连接模式为 `websocket`
   - 不需要手动填 webhook URL

2. **手动输入 App ID / App Secret**
   - 先在开放平台创建应用
   - 启用 Bot 能力
   - 复制 `App ID` 和 `App Secret`
   - 连接模式优先选 `WebSocket`

如果手动配置，需要确认飞书应用至少启用了：

- Bot 能力
- 事件订阅
- 接收消息相关事件
- 发送消息相关权限
- 如需读取文件/图片/语音，按飞书开放平台提示补齐 IM/文件相关权限

权限和事件名称会随飞书开放平台更新，最终以飞书后台提示为准。

## 8. 飞书相关环境变量

核心变量写入 `~/.hermes/.env`：

```bash
# feishu = 中国飞书；lark = 国际版 Lark
FEISHU_DOMAIN=feishu

# 飞书开放平台应用凭证
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx

# 推荐 websocket；webhook 需要公网 HTTP(S) 回调
FEISHU_CONNECTION_MODE=websocket

# DM 访问控制，生产环境不要开放给所有人
FEISHU_ALLOW_ALL_USERS=false
FEISHU_ALLOWED_USERS=ou_xxxxxxxx,ou_yyyyyyyy

# 群聊策略：open 表示允许群聊，但仍要求 @ 机器人；disabled 表示关闭群聊
FEISHU_GROUP_POLICY=open

# 可选：定时任务、后台通知默认投递群/会话
FEISHU_HOME_CHANNEL=oc_xxxxxxxx
FEISHU_HOME_CHANNEL_NAME=Home
```

Webhook 模式才需要这些：

```bash
FEISHU_CONNECTION_MODE=webhook
FEISHU_WEBHOOK_HOST=0.0.0.0
FEISHU_WEBHOOK_PORT=8765
FEISHU_WEBHOOK_PATH=/feishu/webhook
FEISHU_ENCRYPT_KEY=...
FEISHU_VERIFICATION_TOKEN=...
```

当前适配器默认值：

- `FEISHU_CONNECTION_MODE=websocket`
- `FEISHU_DOMAIN=feishu`
- `FEISHU_GROUP_POLICY=allowlist`（setup 选择群聊后会写成 `open`）
- `FEISHU_WEBHOOK_HOST=127.0.0.1`
- `FEISHU_WEBHOOK_PORT=8765`
- `FEISHU_WEBHOOK_PATH=/feishu/webhook`

## 9. 启动 Gateway

```bash
cd /Users/huang/CodePrograms/hermes-agent
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d gateway
```

查看日志：

```bash
docker compose logs -f gateway
```

看到类似下面日志说明飞书连接成功：

```text
[Feishu] Connected in websocket mode (feishu)
```

查看容器状态：

```bash
docker compose ps
```

进入容器执行 Hermes 命令：

```bash
docker compose exec gateway hermes status
docker compose exec gateway hermes doctor
```

## 10. 启动 Dashboard（可选）

当前 compose 文件默认把 Dashboard 绑定到 `127.0.0.1`，本机访问：

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d dashboard
```

打开：

```text
http://127.0.0.1:9119
```

如果在远程服务器上部署，建议通过 SSH 隧道访问：

```bash
ssh -L 9119:127.0.0.1:9119 user@server
```

不要直接把 dashboard 暴露到公网；它会操作本地 agent 配置和密钥。

## 11. 飞书侧验证

1. 把机器人添加到一个群聊，或直接私聊机器人。
2. 群聊中需要 `@机器人` 后发送消息。
3. 第一次 DM 如果选择了 pairing 模式，未知用户会收到配对码。
4. 在宿主机批准配对：

```bash
docker compose exec gateway hermes pairing list
docker compose exec gateway hermes pairing approve feishu <PAIRING_CODE>
```

如果你使用 allowlist，则需要把发送者的 `open_id` 写进：

```bash
FEISHU_ALLOWED_USERS=ou_xxxxxxxx
```

飞书用户 ID 模型：

- `ou_xxx`：open_id，App 维度，事件里最常见，allowlist 推荐先用它
- `u_xxx`：user_id，租户维度，可能需要额外通讯录权限
- `on_xxx`：union_id，开发者维度，跨应用更稳定

## 12. 常用运维命令

重启：

```bash
docker compose restart gateway
```

停止：

```bash
docker compose down
```

升级镜像后重建并重启：

```bash
git pull
docker compose build
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

查看 Hermes 日志文件：

```bash
ls -la ~/.hermes/logs
docker compose exec gateway hermes logs --level info
```

备份数据：

```bash
tar -czf hermes-backup-$(date +%Y%m%d).tar.gz -C ~ .hermes
```

## 13. Webhook 模式说明

WebSocket 是默认推荐方案。只有以下情况考虑 Webhook：

- 公司网络策略不允许长连接
- 你已经有 HTTPS 域名和反向代理
- 需要统一接入现有公网 webhook 网关

Webhook 模式需要：

1. 修改 `.env`：

```bash
FEISHU_CONNECTION_MODE=webhook
FEISHU_WEBHOOK_HOST=0.0.0.0
FEISHU_WEBHOOK_PORT=8765
FEISHU_WEBHOOK_PATH=/feishu/webhook
FEISHU_ENCRYPT_KEY=...
FEISHU_VERIFICATION_TOKEN=...
```

2. 修改 compose 暴露端口，或者继续用 `network_mode: host`。

3. 用 Nginx/Caddy 把公网 HTTPS 转到：

```text
http://127.0.0.1:8765/feishu/webhook
```

4. 在飞书开放平台事件订阅中填写公网 URL：

```text
https://your-domain.example.com/feishu/webhook
```

## 14. 安全建议

- 生产环境不要设置 `GATEWAY_ALLOW_ALL_USERS=true`。
- 飞书 DM 推荐使用 pairing 或 `FEISHU_ALLOWED_USERS`。
- 群聊虽然 `FEISHU_GROUP_POLICY=open`，但代码仍要求 @ 机器人，避免群消息泛滥触发。
- 不要把 `~/.hermes/.env` 提交到 Git。
- 不要同时运行两个 gateway 容器挂载同一个 `~/.hermes`；会竞争会话、日志和飞书 app lock。
- 同一个 `FEISHU_APP_ID` 同时只能被一个本地 Hermes Gateway 使用；适配器会用 scoped lock 防止重复连接。
- Dashboard 默认只监听 `127.0.0.1`；远程访问用 SSH tunnel 或带认证的反代。

## 15. 常见问题排查

### 15.1 日志提示 `lark-oapi not installed`

说明镜像不是用当前 `Dockerfile` 构建，或没有安装 `.[all]`/`.[feishu]`。

处理：

```bash
docker compose build --no-cache gateway
docker compose up -d gateway
```

### 15.2 日志提示 `FEISHU_APP_ID or FEISHU_APP_SECRET not set`

检查：

```bash
grep FEISHU ~/.hermes/.env
docker compose exec gateway hermes status
```

确保 `.env` 里至少有：

```bash
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
```

### 15.3 群聊中没有响应

检查：

- 机器人是否已加入群聊
- 消息里是否 @ 机器人
- `FEISHU_GROUP_POLICY` 是否为 `open`
- 发送者是否在 `FEISHU_ALLOWED_USERS`（如果你选择 allowlist）
- 飞书应用是否订阅了消息事件

查看日志：

```bash
docker compose logs -f gateway
```

### 15.4 私聊没有响应

检查：

- `FEISHU_ALLOW_ALL_USERS=true` 是否打开，或用户是否通过 pairing/allowlist
- 如果使用 allowlist，确认写入的是当前 app 下的 `ou_xxx`
- 飞书应用是否允许机器人接收私聊消息

### 15.5 WebSocket 一直连不上

检查：

- 飞书开放平台是否启用了 Bot 能力
- App ID / App Secret 是否正确
- `FEISHU_DOMAIN` 是否正确：国内飞书用 `feishu`，国际 Lark 用 `lark`
- 容器是否能访问飞书开放平台：

```bash
docker compose exec gateway python - <<'PY'
import urllib.request
print(urllib.request.urlopen("https://open.feishu.cn", timeout=10).status)
PY
```

### 15.6 文件权限异常

macOS/Linux 上启动 compose 时带上：

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

如果之前已经生成了 root-owned 文件：

```bash
sudo chown -R "$(id -u):$(id -g)" ~/.hermes
```

## 16. 最小可执行流程

如果你只想最快跑通：

```bash
cd /Users/huang/CodePrograms/hermes-agent

mkdir -p ~/.hermes
export HERMES_UID=$(id -u)
export HERMES_GID=$(id -g)

docker compose build
docker compose run --rm gateway setup
docker compose run --rm gateway gateway setup
docker compose up -d gateway
docker compose logs -f gateway
```

然后在飞书里私聊机器人，或把机器人拉进群后 `@机器人` 发送消息。
