# 升级指南：群聊审批卡片屏蔽（group_mode）

> 适用场景：已按 `deploy-mac-mini.md` 部署，希望在群聊中启用工具能力但不弹出审批卡片。

---

## 背景

之前群聊中禁用了所有工具（`group_disabled_toolsets`），导致 skills 无法执行实际操作。

本次升级新增了 `approvals.group_mode` 配置项。开启后，群聊中遇到需人工审批的危险命令会**自动静默拒绝**，而非弹出包含原始命令的审批卡片。配合 `audit`（独立审计）和硬编码黑名单，可以安全地在群聊中开放工具。

---

## 操作步骤

### 1. 拉取新镜像

```bash
docker pull modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:latest
docker tag modelbest-registry.cn-beijing.cr.aliyuncs.com/openbmb/hermes-agent:latest hermes-agent:latest
```

### 2. 修改 `~/.hermes/config.yaml`

#### 2.1 添加 `group_mode`（必做）

在 `approvals` 块中加一行：

```yaml
approvals:
  mode: smart
  timeout: 30
  cron_mode: deny
  group_mode: deny    # 群聊中自动拒绝 ESCALATE，不弹审批卡片
```

#### 2.2 放开群聊工具面（可选）

如果希望群聊中 skills 能调用 terminal、搜索、文件读写等工具，将 `group_disabled_toolsets` 从全禁改为只禁高风险的：

```yaml
agent:
  disabled_toolsets: []
  group_disabled_toolsets:
    - delegation
    - cronjob
```

> 移除了 `terminal`、`file`、`web`、`browser`、`code_execution`、`session_search`、`image_gen`、`tts` 的禁用。这些工具在群聊中的安全保障为：
>
> | 防护层 | 作用 |
> |--------|------|
> | `approvals.group_mode: deny` | ESCALATE 命令自动拒绝，不弹卡片 |
> | `approvals.mode: smart` | 明确安全的命令自动放行，明确危险的自动拒绝 |
> | 硬编码黑名单 | `rm -rf /` 等永远被拦截 |
> | `audit.enabled: true` | 所有工具调用 + 回复经独立 LLM 审查 |
> | `security.redact_secrets: true` | 工具输出中的密钥被脱敏 |

如果暂时不想放开工具面，**只做 2.1 就够了**——`group_mode: deny` 会在将来放开时自动生效。

### 3. 重启服务

```bash
cd ~/hermes-deploy
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

### 4. 验证

```bash
# 检查日志正常启动
docker compose logs --tail=100 gateway | grep -E "(Feishu|ERROR|WARN)"
```

在群聊中测试：

| 测试项 | 操作 | 期望结果 |
|--------|------|----------|
| 正常对话 | @机器人 提问 | 正常回复 |
| skill 调用 | @机器人 触发一个需要工具的 skill | 能正常执行（如果已做 2.2） |
| 审批卡片 | 诱导执行 `curl ... \| python3` 类命令 | **不弹卡片**，agent 静默收到 BLOCKED 后换方式 |
| 审计拦截 | 诱导泄露内部信息 | 回复被替换为 block_message |

---

## 不需要修改的部分

- `docker-compose.yml` — 无变化
- `~/.hermes/.env` — 无变化
- 飞书应用配置 — 无变化
- `SOUL.md` — 无变化
- `customer-support-guard` 插件 — 无变化

---

## 回滚

如果需要恢复到之前的行为：

1. 从 `config.yaml` 中删除 `group_mode: deny`（或改为 `group_mode: escalate`）
2. 恢复 `group_disabled_toolsets` 的完整禁用列表
3. `docker compose restart gateway`
