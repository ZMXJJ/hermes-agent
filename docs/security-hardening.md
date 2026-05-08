# Hermes Agent 安全加固操作文档

> 适用场景：已经按 [`deploy-mac-mini.md`](deploy-mac-mini.md) 完成基础部署的 Mac mini 设备。
> 本文档**只涉及安全相关配置**，不重复部署流程。所有操作都在宿主机 `~/.hermes/` 下完成，不改代码、不重建镜像。

---

## 0. 操作前备份

**必做。** 所有改动都在 `~/.hermes/` 内，先打包当前快照：

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/hermes-backups
tar -czf ~/hermes-backups/hermes-pre-security-${ts}.tar.gz -C ~ .hermes
echo "备份完成：~/hermes-backups/hermes-pre-security-${ts}.tar.gz"
```

---

## 1. 命令审批：`approvals`

控制 agent 执行危险 shell 命令时的审批行为。

### 1.1 推荐配置

在 `~/.hermes/config.yaml` 中设置：

```yaml
approvals:
  mode: smart
  timeout: 30
  cron_mode: deny
  group_mode: deny    # 群聊中不弹审批卡片，自动拒绝需人工审批的命令
```

### 1.2 三种模式对比

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `manual` | 每次危险命令都弹审批提示，等用户手动批准 | CLI 交互式使用 |
| **`smart`（推荐）** | 辅助 LLM 自动评估风险：低风险自动放行、高风险自动拒绝、不确定的交人工 | 飞书/gateway 部署（无人值守时不会卡住） |
| `off` | 跳过所有审批，等同 `--yolo` | 仅限容器内/CI 等完全隔离环境 |

### 1.3 smart 模式的三种判定

```
命令被模式匹配标记为"危险"
        │
        ▼
  辅助 LLM 评估真实风险
        │
   ┌────┼────────┐
   ▼    ▼        ▼
APPROVE  DENY   ESCALATE
   │      │        │
   ▼      ▼        ▼
自动放行  直接拒绝  交给用户审批
（记住本  （不执行） （无人响应→
 session          超时拒绝）
 同类放行）
```

- `pip install torch`、`git status` → **APPROVE**（自动放行）
- `rm -rf /var/log/*` → **DENY**（直接拒绝）
- 复杂的 `curl | python` 管道 → **ESCALATE**（交人工，超时后 fail-closed 拒绝）

### 1.4 cron_mode 说明

定时任务中遇到危险命令的处理策略：

| 值 | 行为 |
|---|---|
| `deny`（默认，推荐） | 拒绝执行，让 agent 想其他办法 |
| `approve` | 自动放行（仅限完全可信的自动化流水线） |

### 1.5 group_mode 说明

群聊/论坛中遇到需要人工审批的命令的处理策略：

| 值 | 行为 |
|---|---|
| `deny`（默认，推荐） | 自动拒绝，不弹出审批卡片。避免在群聊中暴露原始命令内容 |
| `escalate` | 正常弹出审批卡片（仅限所有群成员都是可信管理员的场景） |

> **为什么需要 group_mode？** 当 `approvals.mode: smart` 遇到无法自动判定的命令时，会 ESCALATE 到人工审批——在群聊中这意味着一张包含原始命令的审批卡片对所有群成员可见。`group_mode: deny` 把这个 ESCALATE 在群聊中自动转为 DENY，agent 收到 "BLOCKED" 后会尝试其他方法，整个过程对群成员不可见。

### 1.6 硬编码黑名单（始终生效）

无论 `approvals.mode` 设为什么，以下命令**永远被拒绝**，没有任何覆盖方式：

- `rm -rf /` 及变体
- `:(){ :|:& };:` 等 fork bomb
- `mkfs.*` 对已挂载根设备
- `dd if=/dev/zero of=/dev/sd*`
- 向 `sh` 管道传入不可信 URL

---

## 2. 独立审计：`audit`

在 agent 的每个动作（工具调用、shell 命令、对用户的提问、最终回复）执行前，由一个**独立的辅助 LLM** 做安全审查。

### 2.1 推荐配置

```yaml
auxiliary:
  audit:
    provider: auto          # 复用主模型，或改为 openrouter 等独立 provider
    model: ""               # 留空用默认，或指定便宜快速模型如 google/gemini-2.5-flash-preview
    timeout: 15

audit:
  enabled: true
  audit_tools: true         # 审查工具调用
  audit_commands: true      # 审查 shell 命令
  audit_questions: true     # 审查 clarify 提问
  audit_replies: true       # 审查最终回复
  failure_mode: block       # 审计器不可用时拒绝动作（fail-closed）
  policy: |
    社群运营守则：
    - 不得泄露内部机密信息（融资、内部代号、未发布产品）
    - 不得对竞品进行贬损性评价
    - 不得对任何用户进行人身攻击或歧视性言论
    - 保持专业友善的沟通态度
  block_message: "抱歉，我无法处理这个请求。请换个方式描述，或联系管理员。"
  log_allowed: false        # 生产环境只记录 block，减少日志量
  context_tail_messages: 4  # 发给审计器的对话上下文条数
  request_timeout: 15       # 审计调用超时秒数
```

### 2.2 审计覆盖的四个通道

| 通道 | 审查对象 | 被 block 后的行为 |
|------|---------|------------------|
| `tools` | 工具名 + 参数 JSON | 返回 error 给 LLM，LLM 可自行调整重试 |
| `commands` | Shell 命令 + 后端类型 | 同上 |
| `questions` | 对用户的澄清提问 | 不展示给用户 |
| `replies` | 最终回复文本 | 替换为 `block_message`，用户只看到安全提示 |

### 2.3 与 approvals 的区别

| 维度 | `approvals`（命令审批） | `audit`（独立审计） |
|------|----------------------|-------------------|
| 覆盖范围 | 仅 shell 命令 | 所有工具调用 + 命令 + 提问 + 回复 |
| 判断方式 | 模式匹配 + LLM（smart 模式） | 完全由独立 LLM 判断 |
| 可自定义策略 | 无 | 支持 `policy` 自定义规则 |
| 数据处理 | 看到原始命令 | 看到脱敏后的数据 |
| 互相关系 | 先于 audit 执行（hardline blocklist） | 在 approvals 之后作为第二层保护 |

**建议两者同时开启**，形成多层防护。

### 2.4 审计日志

开启审计后，所有判定写入 `~/.hermes/logs/audit.log`：

```bash
# 查看最近的 block 记录
docker compose exec gateway cat /opt/data/logs/audit.log | grep '"verdict":"block"' | tail -20
```

---

## 3. 密钥脱敏：`security.redact_secrets`

防止 API key、token 等敏感信息进入模型上下文和日志。

### 3.1 推荐配置

```yaml
security:
  redact_secrets: true
```

### 3.2 效果

开启后，工具输出（terminal stdout、read_file 结果、web 内容）中看起来像 API key、token、password 的字符串会被替换为 `[REDACTED]`，**然后才**进入模型上下文和日志。

---

## 4. 飞书访问控制

### 4.1 DM 访问控制

```bash
# ~/.hermes/.env

# 方式 1（推荐）：仅允许指定用户
FEISHU_ALLOW_ALL_USERS=false
FEISHU_ALLOWED_USERS=ou_xxxxxxxx,ou_yyyyyyyy

# 方式 2：DM 配对模式（首次私聊需管理员批准配对码）
# 不设 FEISHU_ALLOWED_USERS，未知用户会收到配对码
```

**永远不要在对外部署中设置 `FEISHU_ALLOW_ALL_USERS=true`。**

### 4.2 群聊策略

```bash
# ~/.hermes/.env
FEISHU_GROUP_POLICY=open              # 允许群聊（需 @机器人）
FEISHU_REQUIRE_MENTION=true           # 强制 @才响应（必须开启）
```

或在 `config.yaml` 中：

```yaml
feishu:
  require_mention: true
  global_policy: open
```

### 4.3 全局 gateway 访问控制

```bash
# ~/.hermes/.env
# 永远不要设这个为 true：
# GATEWAY_ALLOW_ALL_USERS=true
```

---

## 5. 工具面收窄

对外部署时，禁用不需要暴露给外部用户的工具，减少攻击面。

### 5.1 群聊专用禁用（推荐，私聊保留管理能力）

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

### 5.2 全局禁用（如果镜像不支持 `group_disabled_toolsets`）

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

### 5.3 效果

- 群聊中模型无法调用 terminal、文件读写、浏览器等工具
- 不会出现 `Command Approval Required` 审批卡片
- 模型不会被诱导执行任意 shell 命令

---

## 6. 显示信息收敛

防止内部状态信息在飞书中泄露给外部用户。

```yaml
display:
  # 系统提示（如 "Gateway shutting down"）自动删除延迟
  ephemeral_system_ttl: 5
  # 飞书平台不展示工具执行进度
  platforms:
    feishu:
      tool_progress: "off"
```

---

## 7. 身份隐藏

通过 SOUL.md 替换 agent 默认身份，防止暴露底层框架信息。

### 7.1 覆盖 SOUL.md

参见 [`feishu-customer-support-rollout.md`](feishu-customer-support-rollout.md) 第 2 节的完整 SOUL.md 内容。核心要点：

- 定义对外身份名（如"模型支持小助手"）
- 强约束：被问到底层模型/框架时一律用固定话术回应
- 不复述、不承认、不否认具体厂商

### 7.2 追加 agent.system_prompt 防泄漏规则

参见 [`feishu-customer-support-rollout.md`](feishu-customer-support-rollout.md) 第 3.2 节。作为 SOUL.md 的第二层防护。

---

## 8. 斜杠命令拦截

参见 [`feishu-customer-support-rollout.md`](feishu-customer-support-rollout.md) 第 5 节。通过 `customer-support-guard` 插件，把 `/help`、`/status` 等内置命令统一改写为打招呼话术，防止暴露命令列表。

需要在 config.yaml 中注册：

```yaml
plugins:
  enabled:
    - customer-support-guard
```

---

## 9. 文件权限

```bash
# .env 包含明文密钥，限制权限
chmod 600 ~/.hermes/.env

# 整个数据目录确保当前用户所有
sudo chown -R "$(id -u):$(id -g)" ~/.hermes
```

---

## 10. 完整推荐配置汇总

以下是所有安全相关配置合并后的 `config.yaml` 片段。将其与你现有配置合并（注意不要重复创建同名顶层 key）：

```yaml
# ── 命令审批 ────────────────────────────────────────────────
approvals:
  mode: smart
  timeout: 30
  cron_mode: deny
  group_mode: deny

# ── 独立审计 ────────────────────────────────────────────────
audit:
  enabled: true
  audit_tools: true
  audit_commands: true
  audit_questions: true
  audit_replies: true
  failure_mode: block
  policy: |
    社群运营守则：
    - 不得泄露内部机密信息
    - 不得对竞品进行贬损性评价
    - 不得对用户进行人身攻击或歧视性言论
    - 保持专业友善的沟通态度
  block_message: "抱歉，我无法处理这个请求。请换个方式描述，或联系管理员。"
  log_allowed: false
  context_tail_messages: 4
  request_timeout: 15

# ── 审计模型 ────────────────────────────────────────────────
auxiliary:
  audit:
    provider: auto
    model: ""
    timeout: 15

# ── 密钥脱敏 ────────────────────────────────────────────────
security:
  redact_secrets: true

# ── 工具面收窄 ──────────────────────────────────────────────
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
  # 子 agent 不自动批准危险命令
  subagent_auto_approve: false

# ── 显示控制 ────────────────────────────────────────────────
display:
  ephemeral_system_ttl: 5
  platforms:
    feishu:
      tool_progress: "off"

# ── 飞书 ────────────────────────────────────────────────────
feishu:
  require_mention: true
  global_policy: open

# ── 插件 ────────────────────────────────────────────────────
plugins:
  enabled:
    - customer-support-guard
```

---

## 11. 应用配置并验证

### 11.1 校验 YAML

```bash
docker compose exec gateway python3 -c \
  'import yaml; yaml.safe_load(open("/opt/data/config.yaml")); print("OK")'
```

### 11.2 重启 gateway

```bash
cd ~/hermes-deploy
docker compose restart gateway
```

### 11.3 检查日志

```bash
docker compose logs --tail=200 gateway | grep -E "(audit|plugin|customer-support|approvals|smart|Feishu|ERROR|WARN)"
```

期望关键日志：

```
... customer-support-guard registered pre_gateway_dispatch hook
... [Feishu] Connected in websocket mode (feishu)
```

### 11.4 验证清单

| 测试项 | 操作 | 期望结果 |
|--------|------|----------|
| 身份隐藏 | 私聊发"你是什么模型？" | 回客服话术，不暴露 Hermes/Nous/GPT 等 |
| 斜杠拦截 | 私聊发 `/help` | 回打招呼话术，不出现命令列表 |
| 群聊 @ | 群里不 @ 发消息 | 机器人不响应 |
| 群聊 @ | 群里 @机器人 发消息 | 正常回应 |
| 工具收窄 | 群聊诱导执行命令 | 不出现审批卡片，不执行命令 |
| 工具进度 | 群聊正常提问 | 不显示 `session_search` 等工具进度 |
| 审计拦截 | 私聊诱导泄露内部信息 | 回复被替换为 block_message |

---

## 12. 回滚

```bash
docker compose down
tar -xzf ~/hermes-backups/hermes-pre-security-<ts>.tar.gz -C ~
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

---

## 附：安全层级总览

从外到内，Hermes 的安全防护层级为：

```
请求进入
    │
    ▼
① 用户授权（allowlist / pairing / allow_all）
    │ 未授权 → 拒绝
    ▼
② 斜杠命令拦截（customer-support-guard 插件）
    │ /help 等 → 改写为打招呼话术
    ▼
③ 工具面收窄（group_disabled_toolsets）
    │ 群聊中 terminal/browser 等被禁用
    ▼
④ 硬编码黑名单（UNRECOVERABLE_BLOCKLIST）
    │ rm -rf / 等 → 永远拒绝，无法覆盖
    ▼
⑤ 命令审批（approvals.mode: smart）
    │ 危险命令 → LLM 评估 → 放行/拒绝/交人工
    ▼
⑥ 独立审计（audit.enabled: true）
    │ 工具调用/命令/提问/回复 → 独立 LLM 审查
    ▼
⑦ 密钥脱敏（security.redact_secrets: true）
    │ 工具输出中的密钥 → [REDACTED]
    ▼
⑧ 身份隐藏（SOUL.md + agent.system_prompt）
    │ 最终回复中的底层信息 → 客服话术替代
    ▼
⑨ 显示收敛（display.platforms.feishu.tool_progress: off）
    │ 工具进度/系统提示 → 不展示或自动删除
    ▼
回复送达用户
```
