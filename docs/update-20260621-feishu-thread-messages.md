# 更新说明 — 2026-06-21：飞书话题消息读取

本次更新为飞书 Gateway 新增了**话题（Thread）消息读取**工具，使 Agent 在话题群中被 @ 时能主动拉取该话题的完整历史消息。

---

## 1. 变更内容

### 1.1 新增工具：`feishu_im_thread_messages`

- 封装 `lark-cli im +threads-messages-list` 命令
- Agent 可通过 `thread_id`（`omt_xxx`）或消息 ID（`om_xxx`）读取话题内的全部消息历史
- 已注册到 `feishu_im` 和 `hermes-feishu` 工具集，Gateway 场景下自动可用

### 1.2 身份模式变更：默认改为 bot

所有 `feishu_im` 工具的 `lark-cli` 调用身份从 `--as user` 改为 **`--as bot`**（默认）。

| 身份 | 说明 |
|------|------|
| `bot`（默认） | 使用 tenant_access_token，自动管理、不会过期，适合服务端 |
| `user` | 使用 user_access_token，需要 OAuth 授权，可看到更多群聊 |

通过环境变量 `LARK_CLI_IDENTITY` 可切换：

```bash
# 使用 user 身份（需要先 lark-cli auth login）
LARK_CLI_IDENTITY=user
```

---

## 2. 部署步骤

### 2.1 拉取新镜像

```bash
# 登录 ACR（如果凭据已缓存则跳过）
docker login modelbest-registry.cn-beijing.cr.aliyuncs.com

# 拉取新镜像（build02 已内置 lark-cli）
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-supercpm-20260621-build02

# 更新 latest 标签
docker tag \
  modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:0.12.0-supercpm-20260621-build02 \
  hermes-agent:latest
```

### 2.2 lark-cli 已内置，无需手动安装

从 `build02` 起，`lark-cli`（`@larksuite/cli`）已在 Dockerfile 中通过 `npm install -g` 预装到镜像里。**不再需要** `docker exec npm install` 等手动安装步骤。

#### 配置持久化说明

| 项目 | 路径（容器内） | 是否持久化 | 说明 |
|------|----------------|------------|------|
| `lark-cli` 二进制 | `/usr/local/lib/node_modules/@larksuite/cli/` | **否 — 但已内置** | 每次重建镜像自动安装 |
| `lark-cli` 配置 | `/opt/data/.lark-cli/config.json` | **是** | 在 `/opt/data` 挂载卷上，容器重建后保留 |
| `lark-cli` 缓存/日志 | `/opt/data/.lark-cli/cache/`、`logs/` | **是** | 同上 |

> **关键结论**：镜像重建后 `lark-cli` 二进制会随镜像自动恢复，配置（App ID/Secret、auth token）存储在挂载卷 `/opt/data`（宿主机 `~/.hermes`）中不会丢失。**重建镜像不需要重新配置。**

#### 首次初始化（仅新机器需要）

```bash
# 进入容器初始化 lark-cli 配置
docker exec -it hermes lark-cli config init
# 输入 App ID 和 App Secret（与 Hermes 飞书 Gateway 相同的应用凭据）
# 配置保存位置：
#   容器内：/opt/data/.lark-cli/config.json
#   宿主机：~/.hermes/.lark-cli/config.json  （通过卷挂载 ~/.hermes:/opt/data 映射）
```

### 2.3 重启 Gateway

```bash
cd ~/hermes-deploy  # 或你的 docker-compose.yml 所在目录

# 拉取新镜像并重建容器
docker compose pull
docker compose up -d --force-recreate
```

验证新工具可用：

```bash
docker exec hermes hermes tools | grep feishu_im_thread
```

预期输出包含 `feishu_im_thread_messages`。

### 2.4 验证 lark-cli bot 身份

```bash
docker exec hermes lark-cli auth status --format json
```

确认 `bot.status` 为 `ready`。如果显示 `not_configured`，需要执行 `lark-cli config init` 配置 App ID 和 App Secret。

---

## 3. 配置 Agent Prompt（可选但推荐）

为了让 Agent 在话题群场景下主动使用该工具，建议在 `~/.hermes/SOUL.md` 中添加以下引导：

```markdown
## 飞书话题群上下文

当你收到来自飞书话题群的消息时（session 信息中包含 thread_id），对话历史可能
不完整——你只能看到之前 @ 你的消息。如果用户引用了你不了解的讨论内容，或者
你需要了解话题的完整上下文，使用 `feishu_im_thread_messages` 工具传入 thread_id
（omt_xxx 格式）来获取该话题的完整消息历史。
```

---

## 4. 工具参数参考

### `feishu_im_thread_messages`

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `thread` | 是 | string | 话题 ID（`omt_xxx`）或话题内任意消息 ID（`om_xxx`），会自动解析到对应话题 |
| `order` | 否 | string | 排序方式：`asc`（默认，从旧到新）或 `desc` |
| `page_size` | 否 | integer | 每页条数，1-500，默认 50 |
| `page_token` | 否 | string | 翻页 token，从上一次响应中获取 |

### 调用示例

Agent 在话题群中被 @ 后，可以这样调用：

```json
{
  "name": "feishu_im_thread_messages",
  "arguments": {
    "thread": "omt_xxxxxxxxxxxxxxx",
    "order": "asc",
    "page_size": 50
  }
}
```

---

## 5. 环境变量总结

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LARK_CLI_IDENTITY` | `bot` | lark-cli 调用身份（`bot` 或 `user`） |

无需修改 `config.yaml` 或 `.env`。

---

## 6. 回滚

如需回退到上一版本：

```bash
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:<上一版本tag>
docker tag <上一版本镜像> hermes-agent:latest
docker compose up -d --force-recreate
```
