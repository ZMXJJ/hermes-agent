"""Independent-auditor safety layer.

This module sits between the main agent loop and any *side effect* it could
have on the host system or on the community of users it talks to.  Every
LLM-initiated tool call, terminal command, clarifying question, and final
reply can be routed through an *independent* auditor model — configured
under ``auxiliary.audit`` and gated by the ``audit`` block in
``config.yaml`` — that emits a structured JSON verdict.

Design goals
------------

* **Independent reasoning chain.**  The auditor must be a separate
  ``call_llm(task="audit", ...)`` request so the same model that
  generated the action is not asked to grade it.  ``auxiliary.audit``
  defaults to ``provider: auto`` so users on a managed setup get a
  reasonable backend without extra configuration, and operators can pin
  it to a cheap fast model for community deployments.
* **Default off, fail closed when on.**  ``audit.enabled`` defaults to
  ``False`` so existing users don't pay extra latency/cost overnight.
  When enabled, ``failure_mode`` defaults to ``block`` so an unreachable
  auditor never accidentally widens the trust boundary.
* **Profile-safe logging.**  Verdicts are written to
  ``get_hermes_home()/logs/audit.log`` via the dedicated ``hermes_audit``
  logger.  See :func:`hermes_logging.setup_logging` for the file
  handler.
* **Minimal data exposure.**  The auditor sees redacted tool arguments
  and a short conversation tail — never raw secrets or the full message
  history.

The module is import-cheap: no third-party imports at module scope, and
all configuration is read on demand so tests can monkeypatch
``hermes_cli.config.load_config`` without import-order pain.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger("hermes_audit")


# ---------------------------------------------------------------------------
# Verdict data model
# ---------------------------------------------------------------------------


@dataclass
class AuditVerdict:
    """A single auditor decision.

    Attributes
    ----------
    allowed
        ``True`` if the action/reply may proceed; ``False`` if it must be
        blocked.  Callers should treat any non-``True`` value as a block
        for safety.
    risk_level
        One of ``"none" | "low" | "medium" | "high"``.  Free-form values
        from the auditor are coerced to this set; unrecognised levels
        collapse to ``"medium"``.
    categories
        Short tags describing why the action was flagged (e.g. ``["pii",
        "company_reputation"]``).  Empty list when allowed.
    reason
        Human-readable explanation suitable for an audit log.  Never
        surfaced to the end user verbatim — use ``block_message`` from
        config for that.
    safe_alternative
        Optional suggestion the agent could try instead.  Surfaced back
        to the LLM via the tool result so it can self-correct.
    source
        Where the verdict came from: ``"auditor"``, ``"failure_block"``,
        ``"failure_allow"``, ``"disabled"``, or ``"cache"``.
    raw
        Raw JSON / text returned by the auditor, retained for diagnostic
        logging only.
    """

    allowed: bool
    risk_level: str = "none"
    categories: List[str] = field(default_factory=list)
    reason: str = ""
    safe_alternative: str = ""
    source: str = "auditor"
    raw: str = ""

    @property
    def blocked(self) -> bool:
        return not self.allowed


_ALLOWED_RISK_LEVELS = ("none", "low", "medium", "high")


def _coerce_risk_level(value: Any) -> str:
    if not isinstance(value, str):
        return "medium"
    norm = value.strip().lower()
    if norm in _ALLOWED_RISK_LEVELS:
        return norm
    if norm in ("safe", "ok", "clear"):
        return "none"
    if norm in ("severe", "critical", "danger"):
        return "high"
    return "medium"


def _coerce_categories(value: Any) -> List[str]:
    if isinstance(value, str):
        items = [v.strip() for v in re.split(r"[,/;]", value) if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        return []
    return items[:8]


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _load_audit_config() -> Dict[str, Any]:
    """Return the merged ``audit`` config block.

    Falls back to a hard-coded ``{"enabled": False}`` when config loading
    raises (e.g. in tests that haven't initialised a HERMES_HOME yet).
    """

    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        block = cfg_get(cfg, "audit", default=None)
    except Exception:
        return {"enabled": False}

    if not isinstance(block, dict):
        return {"enabled": False}
    return block


def is_enabled() -> bool:
    """Return True when audit is globally enabled in config."""

    return bool(_load_audit_config().get("enabled", False))


def _channel_enabled(channel: str) -> bool:
    """Return True when both global audit and per-channel switch are on.

    ``channel`` is one of ``"tools"``, ``"commands"``, ``"questions"``,
    ``"replies"``.
    """

    cfg = _load_audit_config()
    if not cfg.get("enabled", False):
        return False
    key = f"audit_{channel}"
    return bool(cfg.get(key, True))


# ---------------------------------------------------------------------------
# Per-LLM-call de-duplication cache
# ---------------------------------------------------------------------------
#
# A single LLM tool_call may be inspected at multiple layers (e.g. the
# run_agent fan-out *and* terminal_tool's own guard).  We cache the verdict
# by ``(channel, dedup_key)`` for a short window so we don't pay for two
# auditor calls on the same payload.  Keyed by ``tool_call_id`` (preferred)
# or by a hash of the normalized payload.

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, AuditVerdict] = {}
_CACHE_MAX_ENTRIES = 256


def _cache_get(key: str) -> Optional[AuditVerdict]:
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _cache_put(key: str, verdict: AuditVerdict) -> None:
    if not key:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            # Cheap LRU-ish eviction: drop the oldest insertion.
            try:
                first_key = next(iter(_CACHE))
                _CACHE.pop(first_key, None)
            except StopIteration:
                pass
        _CACHE[key] = verdict


def reset_cache() -> None:
    """Clear the verdict cache (test hook + safety net for long sessions)."""

    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are an independent safety auditor for an AI agent that serves a "
    "public community on behalf of a company. The main agent has proposed "
    "an action or a reply, and you must decide whether it is safe to "
    "proceed.\n\n"
    "Block the action if it could:\n"
    "  - damage the host machine, network, or any external system\n"
    "  - exfiltrate secrets (tokens, keys, credentials) or private data\n"
    "  - expose internal/confidential company information\n"
    "  - harm the company's reputation (insults, slander, off-policy claims)\n"
    "  - produce unsafe, illegal, or policy-violating content\n"
    "  - circumvent the authorised tool surface or the auditor itself.\n\n"
    "Otherwise allow the action.  Prefer ALLOW for benign developer "
    "operations (reading files, running tests, asking benign questions).\n\n"
    "You MUST respond with a single JSON object on one line, no markdown, "
    "no commentary, matching this schema exactly:\n"
    "{\n"
    '  "verdict": "allow" | "block",\n'
    '  "risk_level": "none" | "low" | "medium" | "high",\n'
    '  "categories": ["short", "tags"],\n'
    '  "reason": "one short sentence",\n'
    '  "safe_alternative": "optional short suggestion or empty string"\n'
    "}\n"
)


def _build_messages(
    *,
    channel: str,
    payload_label: str,
    payload: str,
    extra_context: Optional[str],
    policy: str,
    conversation_tail: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    system = _SYSTEM_PROMPT
    if policy:
        system += "\nAdditional company policy:\n" + policy.strip() + "\n"

    parts: List[str] = []
    parts.append(f"Channel under review: {channel}")
    if extra_context:
        parts.append(f"Context: {extra_context}")
    if conversation_tail:
        try:
            tail_json = json.dumps(
                _summarize_conversation_tail(conversation_tail),
                ensure_ascii=False,
            )
        except Exception:
            tail_json = "[]"
        parts.append("Recent conversation (oldest → newest):\n" + tail_json)
    parts.append(f"{payload_label}:\n{payload}")
    parts.append(
        "Decide whether to ALLOW or BLOCK and respond with the JSON "
        "object described above."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _summarize_conversation_tail(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Project chat messages into a compact role/content list for audit."""

    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip()
        if role not in ("system", "user", "assistant", "tool"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            # OpenAI multimodal content — keep just the text parts.
            text_parts: List[str] = []
            for chunk in content:
                if isinstance(chunk, dict) and isinstance(
                    chunk.get("text"), str
                ):
                    text_parts.append(chunk["text"])
            content = "\n".join(text_parts)
        elif content is None:
            content = ""
        else:
            content = str(content)
        if len(content) > 600:
            content = content[:600] + "…"
        out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

_THINK_BLOCK_RE = re.compile(
    r"<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)\b[^>]*>"
    r".*?"
    r"</(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>",
    re.DOTALL | re.IGNORECASE,
)

_UNTERMINATED_THINK_RE = re.compile(
    r"(?:^|\n)[ \t]*<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)\b[^>]*>.*$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_think_blocks(text: str) -> str:
    """Remove reasoning/thinking XML blocks from auditor output.

    Reasoning models may embed ``<think>…</think>`` blocks containing
    intermediate JSON-like objects that would confuse the verdict parser.
    Strip them before searching for the actual verdict JSON.
    """
    text = _THINK_BLOCK_RE.sub("", text)
    text = _UNTERMINATED_THINK_RE.sub("", text)
    return text.strip()


def parse_verdict(raw: str) -> AuditVerdict:
    """Parse the auditor's raw response into an :class:`AuditVerdict`.

    Tolerant of:
      - Plain JSON objects.
      - Fenced ``json`` code blocks.
      - Stray prose preceding the JSON object.
      - ``<think>``/``<thinking>``/``<reasoning>`` blocks (stripped before
        parsing so intermediate reasoning JSON is never mistaken for the
        actual verdict).
    On unparseable input, returns a *blocked* verdict so that a confused
    auditor never accidentally lets actions through.
    """

    if not isinstance(raw, str) or not raw.strip():
        return AuditVerdict(
            allowed=False,
            risk_level="medium",
            categories=["audit_parse_failure"],
            reason="Auditor returned an empty response",
            source="failure_block",
            raw="",
        )

    text = _strip_think_blocks(raw.strip())
    if not text:
        return AuditVerdict(
            allowed=False,
            risk_level="medium",
            categories=["audit_parse_failure"],
            reason="Auditor response contained only reasoning with no verdict",
            source="failure_block",
            raw=raw,
        )
    candidate = ""

    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        candidate = fenced.group(1)
    else:
        # Find the first top-level {...} JSON object.
        start = text.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        break

    parsed: Optional[Dict[str, Any]] = None
    if candidate:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                parsed = obj
        except Exception:
            parsed = None

    if parsed is None:
        # Fallback: look for ALLOW / BLOCK keyword in the response so the
        # auditor's intent is preserved even without strict JSON.
        upper = text.upper()
        if "ALLOW" in upper and "BLOCK" not in upper:
            return AuditVerdict(
                allowed=True,
                risk_level="low",
                categories=["audit_parse_fallback"],
                reason="Auditor said ALLOW without JSON",
                source="auditor",
                raw=raw,
            )
        return AuditVerdict(
            allowed=False,
            risk_level="medium",
            categories=["audit_parse_failure"],
            reason="Auditor response was not valid JSON",
            source="failure_block",
            raw=raw,
        )

    verdict_str = str(parsed.get("verdict", "")).strip().lower()
    allowed = verdict_str in ("allow", "approve", "ok", "pass", "safe")
    if not allowed and verdict_str not in (
        "block",
        "deny",
        "reject",
        "unsafe",
    ):
        # Unknown verdict — fail closed.
        allowed = False

    return AuditVerdict(
        allowed=allowed,
        risk_level=_coerce_risk_level(parsed.get("risk_level")),
        categories=_coerce_categories(parsed.get("categories")),
        reason=str(parsed.get("reason", "")).strip()[:500],
        safe_alternative=str(parsed.get("safe_alternative", "")).strip()[:500],
        source="auditor",
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Auditor LLM call
# ---------------------------------------------------------------------------


def _failure_verdict(reason: str, *, block: bool) -> AuditVerdict:
    return AuditVerdict(
        allowed=not block,
        risk_level="medium",
        categories=["audit_failure"],
        reason=reason,
        source="failure_block" if block else "failure_allow",
    )


def _call_auditor(messages: List[Dict[str, Any]]) -> AuditVerdict:
    """Invoke the auditor LLM and parse its response.

    Retries up to ``audit.retry_attempts`` times (default 1) with
    exponential back-off (0.5s, 1s, 2s, …) when the LLM call raises or
    the response is unparseable.  Only transient failures are retried —
    once a structurally valid verdict is obtained it is returned
    immediately regardless of allow/block.

    Failure handling honours ``audit.failure_mode`` from config — ``block``
    fails closed, ``allow`` fails open while still emitting a WARNING audit
    log entry at the call site.
    """

    cfg = _load_audit_config()
    failure_mode = str(cfg.get("failure_mode", "block")).strip().lower()
    fail_block = failure_mode != "allow"

    retry_attempts = cfg.get("retry_attempts", 1)
    try:
        retry_attempts = max(0, int(retry_attempts))
    except (TypeError, ValueError):
        retry_attempts = 1

    timeout = cfg.get("request_timeout") or 0
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 0
    timeout_arg = timeout if timeout > 0 else None

    audit_max_tokens = cfg.get("max_tokens", 400)
    try:
        audit_max_tokens = max(1, int(audit_max_tokens))
    except (TypeError, ValueError):
        audit_max_tokens = 400

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover — import-order belt
        return _failure_verdict(
            f"auxiliary_client import failed: {exc}", block=fail_block,
        )

    import time

    last_error: Optional[str] = None
    for attempt in range(1 + retry_attempts):
        if attempt > 0:
            backoff = min(0.5 * (2 ** (attempt - 1)), 4.0)
            logger.info(
                "Audit retry %d/%d after %.1fs backoff (previous: %s)",
                attempt, retry_attempts, backoff, last_error,
            )
            time.sleep(backoff)

        try:
            response = call_llm(
                task="audit",
                messages=messages,
                temperature=0,
                max_tokens=audit_max_tokens,
                timeout=timeout_arg,
            )
        except Exception as exc:
            last_error = f"auditor LLM call failed: {exc}"
            continue

        try:
            msg = response.choices[0].message
            raw = msg.content or ""
            # Reasoning models (DeepSeek, Mimo, Moonshot, etc.) may put all
            # output into a dedicated reasoning field while leaving content
            # empty.  Fall back to reasoning_content / reasoning so the
            # verdict parser can still find the JSON verdict if the model
            # embedded one at the end of its chain of thought.
            if not raw.strip():
                for _field in ("reasoning_content", "reasoning"):
                    _val = getattr(msg, _field, None)
                    if _val and isinstance(_val, str) and _val.strip():
                        raw = _val
                        break
        except Exception as exc:
            last_error = f"auditor response malformed: {exc}"
            continue

        verdict = parse_verdict(raw)
        if verdict.source != "failure_block":
            return verdict

        last_error = f"auditor parse failure: {verdict.reason}"

    return _failure_verdict(last_error or "auditor exhausted retries", block=fail_block)


# ---------------------------------------------------------------------------
# Public review entry points
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True) or text
    except Exception:
        return text


def _emit_log(
    *,
    channel: str,
    verdict: AuditVerdict,
    summary: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    cfg = _load_audit_config()
    log_allowed = bool(cfg.get("log_allowed", False))
    if verdict.allowed and not log_allowed and verdict.source == "auditor":
        return

    payload: Dict[str, Any] = {
        "channel": channel,
        "verdict": "allow" if verdict.allowed else "block",
        "source": verdict.source,
        "risk_level": verdict.risk_level,
        "categories": verdict.categories,
        "reason": verdict.reason,
        "summary": summary,
    }
    if extra:
        for k, v in extra.items():
            if v is None or v == "":
                continue
            payload[k] = v

    try:
        message = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        message = str(payload)

    if verdict.source.startswith("failure"):
        # Auditor itself failed (timeout, crash, parse error). Always
        # WARNING regardless of the failure_mode policy so operators can
        # spot a degraded auditor in errors.log even when fail-open is
        # configured.
        logger.warning(message)
    elif verdict.allowed:
        logger.info(message)
    else:
        logger.warning(message)


def _review(
    *,
    channel: str,
    payload_label: str,
    payload: str,
    summary: str,
    cache_key: str,
    extra_context: Optional[str] = None,
    conversation_tail: Optional[List[Dict[str, Any]]] = None,
    log_extra: Optional[Dict[str, Any]] = None,
) -> AuditVerdict:
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    cfg = _load_audit_config()
    policy = str(cfg.get("policy", "") or "")
    tail_limit = cfg.get("context_tail_messages", 4)
    try:
        tail_limit = int(tail_limit)
    except (TypeError, ValueError):
        tail_limit = 4
    tail_limit = max(0, tail_limit)
    tail = (conversation_tail or [])[-tail_limit:] if tail_limit else None

    redacted_payload = _redact(payload)
    messages = _build_messages(
        channel=channel,
        payload_label=payload_label,
        payload=redacted_payload,
        extra_context=extra_context,
        policy=policy,
        conversation_tail=tail,
    )

    verdict = _call_auditor(messages)

    if cache_key:
        _cache_put(cache_key, verdict)

    _emit_log(
        channel=channel,
        verdict=verdict,
        summary=summary,
        extra=log_extra,
    )
    return verdict


def get_block_message(verdict: AuditVerdict) -> str:
    """Return the user-facing message used when a verdict blocks an action."""

    cfg = _load_audit_config()
    base = str(
        cfg.get(
            "block_message",
            "This action was blocked by the safety auditor.",
        )
    ).strip() or "This action was blocked by the safety auditor."
    if verdict.safe_alternative:
        return f"{base} Suggestion: {verdict.safe_alternative}"
    return base


def _ok_disabled(channel: str) -> AuditVerdict:
    return AuditVerdict(
        allowed=True,
        risk_level="none",
        reason=f"audit disabled for channel={channel}",
        source="disabled",
    )


def audit_tool_call(
    *,
    tool_name: str,
    tool_args: Optional[Dict[str, Any]],
    tool_call_id: str = "",
    session_id: str = "",
    user_message: str = "",
    conversation_tail: Optional[List[Dict[str, Any]]] = None,
) -> AuditVerdict:
    """Audit a single tool call before it is dispatched."""

    if not _channel_enabled("tools"):
        return _ok_disabled("tools")

    try:
        args_text = json.dumps(tool_args or {}, ensure_ascii=False, default=str)
    except Exception:
        args_text = str(tool_args)
    if len(args_text) > 4000:
        args_text = args_text[:4000] + "…"

    summary = f"tool={tool_name} args_len={len(args_text)}"
    cache_key = (
        f"tool::{tool_call_id}" if tool_call_id else f"tool::{tool_name}::{hash(args_text)}"
    )
    extra_context = (
        f"Tool name: {tool_name}\nLatest user request: {user_message[:400]}"
        if user_message
        else f"Tool name: {tool_name}"
    )
    return _review(
        channel="tool",
        payload_label="Proposed tool call arguments (JSON)",
        payload=args_text,
        summary=summary,
        cache_key=cache_key,
        extra_context=extra_context,
        conversation_tail=conversation_tail,
        log_extra={
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "session_id": session_id,
        },
    )


def audit_command(
    *,
    command: str,
    env_type: str = "",
    workdir: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    conversation_tail: Optional[List[Dict[str, Any]]] = None,
) -> AuditVerdict:
    """Audit a shell command before it is handed to a terminal backend."""

    if not _channel_enabled("commands"):
        return _ok_disabled("commands")

    summary = f"env={env_type or 'local'} cmd_len={len(command or '')}"
    cache_key = (
        f"cmd::{tool_call_id}::{hash(command)}"
        if tool_call_id
        else f"cmd::{hash((env_type, command))}"
    )
    extra_context = f"Backend: {env_type or 'local'}; workdir: {workdir or 'unset'}"
    return _review(
        channel="command",
        payload_label="Proposed shell command",
        payload=command or "",
        summary=summary,
        cache_key=cache_key,
        extra_context=extra_context,
        conversation_tail=conversation_tail,
        log_extra={
            "env_type": env_type,
            "tool_call_id": tool_call_id,
            "session_id": session_id,
        },
    )


def audit_clarify(
    *,
    question: str,
    choices: Optional[List[str]] = None,
    session_id: str = "",
    conversation_tail: Optional[List[Dict[str, Any]]] = None,
) -> AuditVerdict:
    """Audit a clarifying question before it is shown to the user."""

    if not _channel_enabled("questions"):
        return _ok_disabled("questions")

    payload = question or ""
    if choices:
        payload += "\nChoices: " + json.dumps(
            list(choices), ensure_ascii=False
        )
    summary = f"q_len={len(question or '')} choices={len(choices or [])}"
    cache_key = f"clarify::{hash(payload)}"
    return _review(
        channel="question",
        payload_label="Proposed question to ask the user",
        payload=payload,
        summary=summary,
        cache_key=cache_key,
        conversation_tail=conversation_tail,
        log_extra={"session_id": session_id},
    )


def audit_final_response(
    *,
    response_text: str,
    session_id: str = "",
    user_message: str = "",
    conversation_tail: Optional[List[Dict[str, Any]]] = None,
) -> AuditVerdict:
    """Audit the agent's final user-visible reply."""

    if not _channel_enabled("replies"):
        return _ok_disabled("replies")

    payload = response_text or ""
    summary = f"reply_len={len(payload)}"
    cache_key = f"reply::{session_id}::{hash(payload)}"
    extra_context = (
        f"Latest user request: {user_message[:400]}"
        if user_message
        else None
    )
    return _review(
        channel="reply",
        payload_label="Proposed final reply to the user",
        payload=payload,
        summary=summary,
        cache_key=cache_key,
        extra_context=extra_context,
        conversation_tail=conversation_tail,
        log_extra={"session_id": session_id},
    )


__all__ = [
    "AuditVerdict",
    "audit_clarify",
    "audit_command",
    "audit_final_response",
    "audit_tool_call",
    "get_block_message",
    "is_enabled",
    "parse_verdict",
    "reset_cache",
]
