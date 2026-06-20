"""Feishu/Lark IM read tools -- read chat messages via the local ``lark-cli``.

These tools wrap the locally-installed ``lark-cli`` (the same binary backing
the lark-* agent skills) with **user identity** (``--as user``) and expose a
**read-only** surface only: list chats, read a chat's message history, search
chats, search messages across chats, and batch-fetch messages by ID.

Design constraints (see plan "引入飞书 CLI 读取聊天页信息"):

* Read-only by construction. We deliberately wrap only read shortcuts and
  never ``+messages-send`` / ``+messages-reply`` / ``+chat-create`` / etc., so
  the model has no tool that can send/modify anything on Feishu.
* User identity. ``--as user`` is hard-coded on every invocation so the tools
  can see every chat the authorizing user can see (not just bot-member chats),
  and the user-only ``+messages-search`` works.
* No credential management. ``lark-cli`` owns the OAuth user_access_token cache
  and refresh; these tools never read or print secrets.

Prerequisite: ``lark-cli config init`` plus a one-time
``lark-cli auth login --scope "im:message:readonly im:chat:read"``. When a
permission/scope error comes back, the CLI's ``console_url`` / hint is passed
through verbatim so the caller can fix authorization.
"""

import json
import logging
import shutil
import subprocess

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_LARK_CLI = "lark-cli"
# Generous timeout: ``+messages-search --page-all`` can walk up to 40 pages.
_DEFAULT_TIMEOUT = 180


def _check_lark_cli() -> bool:
    """Return True when the ``lark-cli`` binary is on PATH."""
    return shutil.which(_LARK_CLI) is not None


def _append(args: list, flag: str, value) -> None:
    """Append ``--flag value`` to *args* when *value* is a non-empty scalar."""
    if value is None:
        return
    text = str(value).strip()
    if text == "":
        return
    args.append(flag)
    args.append(text)


def _append_bool(args: list, flag: str, value) -> None:
    """Append a bare ``--flag`` when *value* is truthy."""
    if value:
        args.append(flag)


def _run_lark_im(verb: str, extra_args: list) -> str:
    """Run ``lark-cli im <verb> --as user --format json [extra]`` (read-only).

    Returns a JSON string suitable for a tool result. On any failure the
    ``lark-cli`` stderr/stdout (which may carry a structured error envelope
    with ``console_url`` / ``permission_violations``) is surfaced verbatim.
    """
    if not _check_lark_cli():
        return tool_error(
            "lark-cli not found on PATH. Install it and run `lark-cli config init` "
            "plus `lark-cli auth login --scope \"im:message:readonly im:chat:read\"`."
        )

    argv = [_LARK_CLI, "im", verb, "--as", "user", "--format", "json", *extra_args]

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"lark-cli im {verb} timed out after {_DEFAULT_TIMEOUT}s")
    except OSError as e:
        return tool_error(f"Failed to run lark-cli: {e}")

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        # Prefer a structured error envelope from stderr/stdout when present so
        # console_url / permission_violations reach the caller intact.
        for blob in (stderr, stdout):
            if not blob:
                continue
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                continue
            return tool_error(
                f"lark-cli im {verb} failed (exit {proc.returncode})",
                detail=parsed,
            )
        message = stderr or stdout or "unknown error"
        return tool_error(
            f"lark-cli im {verb} failed (exit {proc.returncode}): {message}"
        )

    if not stdout:
        return tool_result(success=True, result=None, note="empty output")

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        # Non-JSON success output (shouldn't happen with --format json) -- return raw.
        return tool_result(success=True, raw=stdout)

    # NOTE: tool_result() treats a `data=` kwarg as its positional payload, so
    # the result is returned under `result` rather than `data`.
    return tool_result(success=True, result=parsed)


# ---------------------------------------------------------------------------
# feishu_im_chat_list
# ---------------------------------------------------------------------------

FEISHU_IM_CHAT_LIST_SCHEMA = {
    "name": "feishu_im_chat_list",
    "description": (
        "List Feishu/Lark chats the authorizing user belongs to (read-only). "
        "Defaults to group chats; pass types='p2p,group' to include direct chats. "
        "Returns chat_id, name, owner and chat_mode. Paginate with page_token "
        "when has_more is true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "types": {
                "type": "string",
                "description": "Comma-separated chat types to include: 'group', 'p2p', or 'p2p,group'. Omit for groups only.",
            },
            "sort": {
                "type": "string",
                "enum": ["create_time", "active_time"],
                "description": "Result ordering. 'active_time' = most recently active first.",
            },
            "page_size": {
                "type": "integer",
                "description": "Results per page (1-100, default 20).",
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token from a previous response.",
            },
            "exclude_muted": {
                "type": "boolean",
                "description": "Drop chats the user has muted (do-not-disturb).",
            },
        },
        "required": [],
    },
}


def _handle_chat_list(args: dict, **kwargs) -> str:
    extra: list = []
    _append(extra, "--types", args.get("types"))
    _append(extra, "--sort", args.get("sort"))
    _append(extra, "--page-size", args.get("page_size"))
    _append(extra, "--page-token", args.get("page_token"))
    _append_bool(extra, "--exclude-muted", args.get("exclude_muted"))
    return _run_lark_im("+chat-list", extra)


# ---------------------------------------------------------------------------
# feishu_im_chat_messages
# ---------------------------------------------------------------------------

FEISHU_IM_CHAT_MESSAGES_SCHEMA = {
    "name": "feishu_im_chat_messages",
    "description": (
        "Read the message history of a single Feishu/Lark chat (read-only) -- the "
        "primary way to read an entire group's messages. Provide chat_id (oc_xxx) "
        "for a group, or user_id (ou_xxx) for a direct chat. Supports a creation-time "
        "range (start/end) and order. Page size max is 50; when has_more is true, "
        "pass page_token to continue and repeat until has_more is false to read the "
        "full history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Group/conversation chat_id (oc_xxx). Mutually exclusive with user_id.",
            },
            "user_id": {
                "type": "string",
                "description": "Other user's open_id (ou_xxx) for a direct chat; the p2p chat_id is resolved automatically.",
            },
            "start": {
                "type": "string",
                "description": "Start time, ISO 8601 or date-only (e.g. '2026-03-10' or '2026-03-10T00:00:00+08:00').",
            },
            "end": {
                "type": "string",
                "description": "End time, ISO 8601 or date-only.",
            },
            "order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort by creation time (default desc).",
            },
            "page_size": {
                "type": "integer",
                "description": "Results per page (max 50, default 50).",
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token from a previous response.",
            },
        },
        "required": [],
    },
}


def _handle_chat_messages(args: dict, **kwargs) -> str:
    chat_id = (args.get("chat_id") or "").strip()
    user_id = (args.get("user_id") or "").strip()
    if not chat_id and not user_id:
        return tool_error("Provide either chat_id (oc_xxx) or user_id (ou_xxx)")
    if chat_id and user_id:
        return tool_error("chat_id and user_id are mutually exclusive; provide only one")

    extra: list = []
    _append(extra, "--chat-id", chat_id)
    _append(extra, "--user-id", user_id)
    _append(extra, "--start", args.get("start"))
    _append(extra, "--end", args.get("end"))
    _append(extra, "--order", args.get("order"))
    _append(extra, "--page-size", args.get("page_size"))
    _append(extra, "--page-token", args.get("page_token"))
    return _run_lark_im("+chat-messages-list", extra)


# ---------------------------------------------------------------------------
# feishu_im_chat_search
# ---------------------------------------------------------------------------

FEISHU_IM_CHAT_SEARCH_SCHEMA = {
    "name": "feishu_im_chat_search",
    "description": (
        "Search Feishu/Lark group chats visible to the user by keyword and/or member "
        "(read-only). Use this to resolve a chat_id from a group name before reading "
        "its messages. At least one of query or member_ids is required."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword (max 64 chars). Matches chat names and member names; supports pinyin/prefix.",
            },
            "member_ids": {
                "type": "string",
                "description": "Comma-separated member open_ids (ou_xxx), up to 50. Usable alone or with query.",
            },
            "search_types": {
                "type": "string",
                "description": "Comma-separated visibility filter: private, external, public_joined, public_not_joined.",
            },
            "chat_modes": {
                "type": "string",
                "description": "Comma-separated chat modes: group, topic.",
            },
            "page_size": {
                "type": "integer",
                "description": "Results per page (1-100, default 20).",
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token from a previous response.",
            },
        },
        "required": [],
    },
}


def _handle_chat_search(args: dict, **kwargs) -> str:
    query = (args.get("query") or "").strip()
    member_ids = (args.get("member_ids") or "").strip()
    if not query and not member_ids:
        return tool_error("At least one of query or member_ids is required")

    extra: list = []
    _append(extra, "--query", query)
    _append(extra, "--member-ids", member_ids)
    _append(extra, "--search-types", args.get("search_types"))
    _append(extra, "--chat-modes", args.get("chat_modes"))
    _append(extra, "--page-size", args.get("page_size"))
    _append(extra, "--page-token", args.get("page_token"))
    return _run_lark_im("+chat-search", extra)


# ---------------------------------------------------------------------------
# feishu_im_messages_search
# ---------------------------------------------------------------------------

FEISHU_IM_MESSAGES_SEARCH_SCHEMA = {
    "name": "feishu_im_messages_search",
    "description": (
        "Search Feishu/Lark messages across conversations the user can see (read-only, "
        "user identity only). Filter by keyword, chat_id, sender, chat_type and a time "
        "range. Use page_all to walk every page automatically (best for summaries)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keyword. May be empty when other filters are provided.",
            },
            "chat_id": {
                "type": "string",
                "description": "Restrict to chat IDs, comma-separated (oc_xxx,oc_yyy).",
            },
            "sender": {
                "type": "string",
                "description": "Sender open_ids, comma-separated (ou_xxx).",
            },
            "chat_type": {
                "type": "string",
                "enum": ["group", "p2p"],
                "description": "Restrict to group or p2p conversations.",
            },
            "start": {
                "type": "string",
                "description": "Start time with timezone offset, e.g. '2026-03-24T00:00:00+08:00'.",
            },
            "end": {
                "type": "string",
                "description": "End time with timezone offset, e.g. '2026-03-25T23:59:59+08:00'.",
            },
            "page_size": {
                "type": "integer",
                "description": "Page size (1-50, default 20).",
            },
            "page_all": {
                "type": "boolean",
                "description": "Auto-paginate through all result pages (up to 40).",
            },
            "page_limit": {
                "type": "integer",
                "description": "Max pages to fetch when auto-paginating (default 20, max 40).",
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token to continue from a previous response.",
            },
        },
        "required": [],
    },
}


def _handle_messages_search(args: dict, **kwargs) -> str:
    extra: list = []
    _append(extra, "--query", args.get("query"))
    _append(extra, "--chat-id", args.get("chat_id"))
    _append(extra, "--sender", args.get("sender"))
    _append(extra, "--chat-type", args.get("chat_type"))
    _append(extra, "--start", args.get("start"))
    _append(extra, "--end", args.get("end"))
    _append(extra, "--page-size", args.get("page_size"))
    _append(extra, "--page-limit", args.get("page_limit"))
    _append(extra, "--page-token", args.get("page_token"))
    _append_bool(extra, "--page-all", args.get("page_all"))
    return _run_lark_im("+messages-search", extra)


# ---------------------------------------------------------------------------
# feishu_im_messages_get
# ---------------------------------------------------------------------------

FEISHU_IM_MESSAGES_GET_SCHEMA = {
    "name": "feishu_im_messages_get",
    "description": (
        "Batch fetch full Feishu/Lark message content by message IDs (read-only). "
        "Provide up to 50 comma-separated message IDs (om_xxx)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "string",
                "description": "Comma-separated message IDs (om_xxx), at least one and at most 50.",
            },
        },
        "required": ["message_ids"],
    },
}


def _handle_messages_get(args: dict, **kwargs) -> str:
    message_ids = (args.get("message_ids") or "").strip()
    if not message_ids:
        return tool_error("message_ids is required (comma-separated om_xxx)")
    extra = ["--message-ids", message_ids]
    return _run_lark_im("+messages-mget", extra)


# ---------------------------------------------------------------------------
# Registration (read-only tools only)
# ---------------------------------------------------------------------------

_TOOLSET = "feishu_im"

registry.register(
    name="feishu_im_chat_list",
    toolset=_TOOLSET,
    schema=FEISHU_IM_CHAT_LIST_SCHEMA,
    handler=_handle_chat_list,
    check_fn=_check_lark_cli,
    requires_env=[],
    is_async=False,
    description="List Feishu/Lark chats (read-only)",
    emoji="\U0001f4ac",
)

registry.register(
    name="feishu_im_chat_messages",
    toolset=_TOOLSET,
    schema=FEISHU_IM_CHAT_MESSAGES_SCHEMA,
    handler=_handle_chat_messages,
    check_fn=_check_lark_cli,
    requires_env=[],
    is_async=False,
    description="Read a Feishu/Lark chat's messages (read-only)",
    emoji="\U0001f4ac",
)

registry.register(
    name="feishu_im_chat_search",
    toolset=_TOOLSET,
    schema=FEISHU_IM_CHAT_SEARCH_SCHEMA,
    handler=_handle_chat_search,
    check_fn=_check_lark_cli,
    requires_env=[],
    is_async=False,
    description="Search Feishu/Lark chats (read-only)",
    emoji="\U0001f50d",
)

registry.register(
    name="feishu_im_messages_search",
    toolset=_TOOLSET,
    schema=FEISHU_IM_MESSAGES_SEARCH_SCHEMA,
    handler=_handle_messages_search,
    check_fn=_check_lark_cli,
    requires_env=[],
    is_async=False,
    description="Search Feishu/Lark messages across chats (read-only)",
    emoji="\U0001f50d",
)

registry.register(
    name="feishu_im_messages_get",
    toolset=_TOOLSET,
    schema=FEISHU_IM_MESSAGES_GET_SCHEMA,
    handler=_handle_messages_get,
    check_fn=_check_lark_cli,
    requires_env=[],
    is_async=False,
    description="Batch fetch Feishu/Lark messages by ID (read-only)",
    emoji="\U0001f4ac",
)
