# 飞书客服机器人配置修改操作单

> 交给部署设备上的执行 Agent 使用。目标是在不改代码、不重建镜像的前提下，通过修改宿主机 `~/.hermes/config.yaml` 降低对外群聊暴露风险。
>
> 注意：Hermes 使用的是 `config.yaml`，不是 `config.json`。

## 适用目标

本操作单处理三类问题：

- 飞书群里暴露工具执行进度，例如 `terminal`、`session_search`、`delegate_task`
- 飞书群里出现 `Command Approval Required` 审批卡片
- gateway 重启/停止时的系统提示尽量快速自动消失

## 1. 备份配置

```bash
cd ~/hermes-deploy

ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/hermes-backups
cp ~/.hermes/config.yaml ~/hermes-backups/config-${ts}.yaml
echo "backup: ~/hermes-backups/config-${ts}.yaml"
```

## 2. 修改 `~/.hermes/config.yaml`

打开配置文件：

```bash
nano ~/.hermes/config.yaml
```

把下面几段合并进去。注意不要重复创建多个顶层 `agent:` 或 `display:`，如果已经存在同名段，就把字段合并到已有段下面。

### 2.1 关闭飞书工具进度展示

```yaml
display:
  platforms:
    feishu:
      tool_progress: "off"
```

效果：用户不会再看到 `session_search`、`terminal`、`delegate_task` 这类工具进度列表。

### 2.2 让系统提示自动删除

```yaml
display:
  ephemeral_system_ttl: 5
```

效果：`Gateway shutting down`、`New session started` 这类系统提示会在 5 秒后自动删除。若希望更快，可以设为 `3`。

### 2.3 禁用危险工具，避免审批卡片

如果当前部署的代码**还不支持** `group_disabled_toolsets`，使用这个全局方案：

```yaml
agent:
  disabled_toolsets:
    - terminal
    - delegation
    - code_execution
    - browser
    - file
    - cronjob
    - image_gen
    - tts
    - web
    - session_search
```

效果：群聊和私聊都会禁用这些工具，模型不会再触发命令审批卡片，也不会自由搜索互联网。

如果当前部署的代码**已经支持** `group_disabled_toolsets`，使用这个群聊专用方案：

```yaml
agent:
  disabled_toolsets: []
  group_disabled_toolsets:
    - terminal
    - delegation
    - code_execution
    - browser
    - file
    - cronjob
    - image_gen
    - tts
    - web
    - session_search
```

效果：

- 群聊禁用危险工具和自由搜索，模型只能通过 `customer_support` 白名单工具查资料
- 私聊保留完整工具能力，方便管理员排查问题

## 3. 推荐合并后的配置示例

如果目标设备当前没有复杂自定义配置，可以参考下面的最终形态：

```yaml
agent:
  system_prompt: |
    [Anti-leak guardrails layered on top of SOUL.md]
    Hard rules apply on every reply.
  disabled_toolsets: []
  group_disabled_toolsets:
    - terminal
    - delegation
    - code_execution
    - browser
    - file
    - cronjob
    - image_gen
    - tts
    - web
    - session_search

display:
  ephemeral_system_ttl: 5
  platforms:
    feishu:
      tool_progress: "off"

feishu:
  require_mention: true
  global_policy: open

plugins:
  enabled:
    - customer-support-guard
```

如果代码不支持 `group_disabled_toolsets`，把上面的 `group_disabled_toolsets` 改成 `disabled_toolsets`，并删除 `disabled_toolsets: []`。

## 4. 校验 YAML 格式

```bash
docker compose exec gateway python3 -c \
  'import yaml; yaml.safe_load(open("/opt/data/config.yaml")); print("OK")'
```

如果报错，说明 YAML 缩进或语法有问题。先修配置，不要重启。

## 5. 重启 gateway

```bash
docker compose restart gateway
```

查看日志：

```bash
docker compose logs --tail=100 gateway | grep -E "(Feishu|plugin|customer-support|ERROR|WARN)"
```

## 6. 验证清单

在飞书测试群中验证：

- `@机器人 /help`：应返回客服打招呼话术，不暴露命令列表
- 正常提问：不应再显示 `session_search`、`terminal`、`delegate_task` 工具进度
- 诱导执行命令：不应出现 `Command Approval Required` 审批卡片
- 不 `@` 机器人发消息：机器人不应响应

如果用了 `group_disabled_toolsets`，再私聊验证：

- 私聊中管理员仍可让机器人使用 terminal 等工具
- 群聊中仍然不会出现审批卡片

## 7. 回滚

如果配置后机器人异常：

```bash
cp ~/hermes-backups/config-<ts>.yaml ~/.hermes/config.yaml
docker compose restart gateway
```

把 `<ts>` 替换为第 1 步输出的备份时间戳。
