---
sidebar_position: 4
title: "Independent Auditor"
description: "Independent safety auditor — an auxiliary LLM that reviews tool calls, shell commands, clarifying questions, and final replies before they reach the user or the host system"
---

# Independent Auditor

The independent auditor is an optional safety layer that sits between the main agent and its side effects. When enabled, every LLM-initiated tool call, terminal command, clarifying question, and final reply is reviewed by a **separate** auxiliary model before it is executed or delivered. The auditor emits a structured JSON verdict — allow or block — and the agent loop acts on it automatically.

The auditor is designed for **community deployments** (Telegram groups, Discord servers, Slack workspaces) where the agent operates on behalf of a company and must not leak secrets, damage infrastructure, or produce harmful content. It is also useful for any environment where you want defense-in-depth beyond the built-in dangerous-command approval system.

## Key design principles

- **Independent reasoning chain.** The auditor runs as a separate `call_llm(task="audit")` request so the same model that generated the action is never asked to grade it.
- **Default off, fail closed when on.** `audit.enabled` defaults to `false` so existing users pay no extra latency or cost. When enabled, `failure_mode` defaults to `block` — if the auditor is unreachable, the action is refused.
- **Minimal data exposure.** The auditor sees redacted tool arguments and a short conversation tail — never raw secrets or the full message history.
- **Profile-safe logging.** Verdicts are written to `~/.hermes/logs/audit.log` via a dedicated logger.

## Quick start

Add the following to your `~/.hermes/config.yaml`:

```yaml
audit:
  enabled: true
```

That's it. All four review channels (tools, commands, questions, replies) are on by default. The auditor model defaults to `auxiliary.audit.provider: auto`, which reuses whichever provider and model you already have configured for your main chat.

:::tip Recommended: use a separate model
For production community deployments, pin the auditor to a cheap, fast model so it doesn't share the same reasoning chain as the main agent:

```yaml
auxiliary:
  audit:
    provider: openrouter
    model: google/gemini-2.5-flash-preview
    timeout: 15
```
:::

## Configuration reference

### `audit` section

```yaml
audit:
  # Master switch. When false, all audit checks are skipped.
  enabled: false

  # Per-channel switches. Each can be toggled independently.
  audit_tools: true       # Review tool calls before dispatch
  audit_commands: true    # Review shell commands before execution
  audit_questions: true   # Review clarify questions before showing to user
  audit_replies: true     # Review final responses before delivery

  # Failure mode when the auditor itself errors, times out, or has no provider.
  #   block — refuse the action (default; safe for community deployments)
  #   allow — let the action through but emit a WARNING in audit.log
  failure_mode: block

  # Free-form policy text appended to the auditor's system prompt.
  # Use this to encode your company's specific rules.
  policy: ""

  # Message shown to the user when an action or reply is blocked.
  block_message: >-
    This action was blocked by the safety auditor.
    Please rephrase the request or contact an administrator.

  # Whether to log allowed actions (not just blocks).
  # Off by default to keep the audit log focused on incidents.
  log_allowed: false

  # Number of recent conversation messages sent to the auditor for context.
  # Higher values give better context but increase token usage.
  context_tail_messages: 4

  # Per-call timeout for the auditor LLM (seconds).
  # Falls back to auxiliary.audit.timeout when unset.
  request_timeout: 0

  # Maximum tokens the auditor model may generate per verdict (default 400).
  # The auditor only returns a small JSON object, so 400 is usually enough.
  # Raise this if you use a verbose model that adds preamble before the JSON.
  max_tokens: 400
```

### `auxiliary.audit` section

The auditor model is configured alongside other auxiliary tasks:

```yaml
auxiliary:
  audit:
    provider: auto       # auto | openrouter | nous | anthropic | custom | ...
    model: ""            # e.g. "google/gemini-2.5-flash-preview"
    base_url: ""         # For custom endpoints
    api_key: ""          # For custom endpoints
    timeout: 30          # Request timeout in seconds
```

When `provider` is `auto` (default), the auditor reuses your main chat model — no additional API key needed. Override it when you want a separate, cheaper model for auditing.

## How it works

### Review channels

| Channel | Entry point | What is reviewed |
|---------|-------------|-----------------|
| **tools** | Every `tool_call` before dispatch | Tool name + arguments (JSON) |
| **commands** | Shell commands before execution | Command string, backend type, working directory |
| **questions** | `clarify` tool before showing to user | Question text + choices |
| **replies** | Final assistant response before delivery | Complete response text |

### What the auditor blocks

The auditor's system prompt instructs it to block actions that could:

1. **Damage the host machine**, network, or any external system
2. **Exfiltrate secrets** (tokens, keys, credentials) or private data
3. **Expose internal/confidential** company information
4. **Harm the company's reputation** (insults, slander, off-policy claims)
5. **Produce unsafe, illegal, or policy-violating** content
6. **Circumvent the authorized tool surface** or the auditor itself

Benign developer operations (reading files, running tests, asking questions) are explicitly preferred for ALLOW.

### Custom policy

The `audit.policy` field lets you inject company-specific rules into the auditor's system prompt. For example:

```yaml
audit:
  enabled: true
  policy: |
    - Never mention competitor products by name.
    - Do not discuss unannounced features or roadmap items.
    - All code snippets shared with users must exclude internal module paths.
    - Community interactions must be in a professional, friendly tone.
```

The policy text is appended verbatim to the auditor's system prompt, so write it as instructions addressed to the auditor.

### What happens when an action is blocked

**Tool calls and commands:** The blocked tool is not executed. Instead, an error JSON is returned to the LLM as the tool result:

```json
{"error": "This action was blocked by the safety auditor. Suggestion: ..."}
```

The agent loop **continues normally** — the LLM sees the error, can self-correct, and may retry with a safer approach. Other tool calls in the same batch are unaffected.

**Final replies:** The original response text is replaced with the configured `block_message`. If streaming is enabled, buffered text is discarded and only the block message is delivered to the user. The conversation turn ends, but the agent session remains active for the next user message.

**Clarify questions:** The question is not shown to the user. The clarify tool returns an error to the LLM.

### Verdict caching

A single tool call may pass through multiple audit checkpoints (e.g. the `run_agent` fan-out and `terminal_tool`'s own guard). To avoid duplicate auditor calls on the same payload, verdicts are cached in memory by `(channel, dedup_key)` for up to 256 entries per session.

### Data redaction

Before sending data to the auditor, tool arguments and conversation content are passed through the secret redaction engine (`agent.redact`). The auditor never sees raw API keys, tokens, or passwords — only masked placeholders.

## Audit log

When audit is enabled, verdicts are written to `~/.hermes/logs/audit.log` as structured JSON lines:

```json
{"channel":"command","verdict":"block","source":"auditor","risk_level":"high","categories":["exfiltrate_secrets"],"reason":"Command pipes environment variables containing API keys to an external server","summary":"env=local cmd_len=89","env_type":"local","session_id":"abc123"}
```

| Field | Description |
|-------|-------------|
| `channel` | `tool`, `command`, `question`, or `reply` |
| `verdict` | `allow` or `block` |
| `source` | `auditor`, `failure_block`, `failure_allow`, `disabled`, or `cache` |
| `risk_level` | `none`, `low`, `medium`, or `high` |
| `categories` | Short tags (e.g. `["pii", "exfiltrate_secrets"]`) |
| `reason` | One-sentence explanation from the auditor |

By default, only blocks and failures are logged. Set `audit.log_allowed: true` to also log allowed actions.

## Failure handling

When the auditor itself fails (provider timeout, no API key, crash), the behavior depends on `failure_mode`:

| `failure_mode` | Behavior | Use case |
|----------------|----------|----------|
| `block` (default) | Action is refused, WARNING logged | Community deployments where safety > availability |
| `allow` | Action proceeds, WARNING logged | Development environments where you don't want a flaky auditor to block work |

In both modes, a WARNING entry is written to `audit.log` and `errors.log` so operators can detect a degraded auditor.

## Interaction with other safety layers

The auditor is one of several safety layers in Hermes. They run in this order for tool calls:

1. **Independent auditor** (this feature) — blocks unsafe actions before any execution
2. **Plugin pre_tool_call hooks** — plugins can inspect and block tool calls
3. **Tool-loop guardrails** — detect and halt repetitive/looping tool patterns
4. **Dangerous command approval** — human-in-the-loop for destructive terminal commands

The auditor runs first. If it blocks a call, the subsequent layers never see it. This ensures that an unsafe action cannot reach any handler.

## Examples

### Minimal setup (reuse main model)

```yaml
audit:
  enabled: true
```

### Production community deployment

```yaml
auxiliary:
  audit:
    provider: openrouter
    model: google/gemini-2.5-flash-preview
    timeout: 15

audit:
  enabled: true
  failure_mode: block
  policy: |
    You are auditing a public Telegram community bot for Acme Corp.
    Block any content that mentions internal codenames, unreleased products,
    or employee names. Block insults, profanity, and off-topic political content.
  block_message: "I can't process that request right now. Please try rephrasing, or reach out to a team admin for help."
  log_allowed: true
  context_tail_messages: 6
```

### Development / testing (fail open)

```yaml
audit:
  enabled: true
  failure_mode: allow
  audit_replies: false    # Don't gate final responses during dev
```
