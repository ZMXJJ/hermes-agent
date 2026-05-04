"""Customer-support tools -- structured, whitelist-gated query tools.

Provides safe, schema-constrained tools for a public-facing customer-support
bot to search Feishu docs/wiki, GitHub Issues, and Hugging Face discussions
without exposing terminal or arbitrary shell access.

Each tool reads its whitelist and limits from ``customer_support`` in
``~/.hermes/config.yaml``.  Queries targeting repos or spaces not in the
whitelist are rejected.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_cached_cs_config: Optional[Dict[str, Any]] = None
_cached_mtime: float = 0.0


def _load_cs_config() -> Dict[str, Any]:
    """Load the ``customer_support`` section from config.yaml (cached on mtime)."""
    global _cached_cs_config, _cached_mtime
    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
        mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
    except Exception:
        mtime = 0.0
        config_path = None

    with _config_lock:
        if _cached_cs_config is not None and mtime == _cached_mtime:
            return _cached_cs_config

    try:
        from hermes_cli.config import read_raw_config
        raw = read_raw_config()
        cs = raw.get("customer_support") or {}
    except Exception:
        cs = {}

    with _config_lock:
        _cached_cs_config = cs
        _cached_mtime = mtime
    return cs


def _max_results() -> int:
    return int(_load_cs_config().get("max_results", 5))


def _max_content_chars() -> int:
    return int(_load_cs_config().get("max_content_chars", 12000))


def _truncate(text: str, limit: int = 0) -> str:
    limit = limit or _max_content_chars()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated, {len(text) - limit} chars omitted]"


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

def _check_feishu_available() -> bool:
    try:
        import lark_oapi  # noqa: F401
    except ImportError:
        return False
    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return False
    cs = _load_cs_config()
    return bool((cs.get("feishu") or {}).get("enabled", False))


def _check_github_available() -> bool:
    cs = _load_cs_config()
    repos = (cs.get("github") or {}).get("allowed_repos") or []
    return bool(repos) and bool(os.getenv("GITHUB_TOKEN", ""))


def _check_hf_available() -> bool:
    cs = _load_cs_config()
    repos = (cs.get("huggingface") or {}).get("allowed_repos") or []
    return bool(repos)


# ---------------------------------------------------------------------------
# Feishu client builder (standalone, not dependent on gateway adapter)
# ---------------------------------------------------------------------------

_feishu_client_lock = threading.Lock()
_feishu_client: Any = None


def _get_feishu_client():
    global _feishu_client
    with _feishu_client_lock:
        if _feishu_client is not None:
            return _feishu_client
    try:
        import lark_oapi as lark
        domain_name = os.getenv("FEISHU_DOMAIN", "feishu").strip().lower()
        domain = lark.FEISHU_DOMAIN if domain_name != "lark" else lark.LARK_DOMAIN
        client = (
            lark.Client.builder()
            .app_id(os.getenv("FEISHU_APP_ID", ""))
            .app_secret(os.getenv("FEISHU_APP_SECRET", ""))
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        with _feishu_client_lock:
            _feishu_client = client
        return client
    except Exception as exc:
        logger.warning("Failed to build Feishu client for CS tools: %s", exc)
        return None


def _feishu_request(method: str, uri: str, *, paths=None, queries=None, body=None):
    """Execute a Feishu OpenAPI request, return (code, msg, data_dict)."""
    client = _get_feishu_client()
    if client is None:
        return -1, "Feishu client unavailable", {}

    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    http_method = HttpMethod.GET if method == "GET" else HttpMethod.POST

    builder = (
        BaseRequest.builder()
        .http_method(http_method)
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
    )
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)

    response = client.request(builder.build())
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", "")

    data: dict = {}
    raw = getattr(response, "raw", None)
    if raw and hasattr(raw, "content"):
        try:
            data = json.loads(raw.content).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            pass
    if not data:
        resp_data = getattr(response, "data", None)
        if isinstance(resp_data, dict):
            data = resp_data
        elif resp_data and hasattr(resp_data, "__dict__"):
            data = vars(resp_data)
    return code, msg, data


# ===================================================================
# Tool 1: cs_feishu_search_docs
# ===================================================================

_SEARCH_DOCS_URI = "/open-apis/suite/docs-api/search/object"

CS_FEISHU_SEARCH_DOCS_SCHEMA = {
    "name": "cs_feishu_search_docs",
    "description": (
        "Search the company's Feishu knowledge base (docs, wikis, sheets) "
        "by keyword. Returns titles, URLs, and brief snippets. Use this "
        "when you need to find internal documentation to answer a user's "
        "model-related question."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (Chinese or English).",
            },
            "count": {
                "type": "integer",
                "description": "Max results to return (1-20, default from config).",
            },
        },
        "required": ["query"],
    },
}


def _handle_cs_feishu_search_docs(args: dict, **kwargs) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")

    count = min(int(args.get("count") or _max_results()), 20)

    code, msg, data = _feishu_request(
        "POST", _SEARCH_DOCS_URI,
        body={
            "search_key": query,
            "count": count,
            "offset": 0,
            "owner_id_type": "open_id",
            "docs_token_list": [],
        },
    )
    if code != 0:
        return tool_error(f"Feishu search failed: code={code} msg={msg}")

    docs_entities = data.get("docs_entities") or data.get("items") or []
    results: List[dict] = []
    for doc in docs_entities[:count]:
        entry: dict = {}
        if isinstance(doc, dict):
            entry = {
                "title": doc.get("title", ""),
                "doc_token": doc.get("docs_token", "") or doc.get("token", ""),
                "doc_type": doc.get("docs_type", "") or doc.get("type", ""),
                "url": doc.get("url", ""),
                "owner": doc.get("owner", {}).get("name", "") if isinstance(doc.get("owner"), dict) else "",
            }
            preview = doc.get("preview", "") or doc.get("summary", "")
            if preview:
                entry["preview"] = preview[:300]
        else:
            entry["raw"] = str(doc)[:200]
        results.append(entry)

    return tool_result(success=True, count=len(results), results=results)


# ===================================================================
# Tool 2: cs_feishu_read_doc
# ===================================================================

_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"

CS_FEISHU_READ_DOC_SCHEMA = {
    "name": "cs_feishu_read_doc",
    "description": (
        "Read the full text content of a Feishu document by its token. "
        "Use after cs_feishu_search_docs finds a relevant doc. "
        "Content is truncated if it exceeds the configured limit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {
                "type": "string",
                "description": "Document token from search results.",
            },
        },
        "required": ["doc_token"],
    },
}


def _handle_cs_feishu_read_doc(args: dict, **kwargs) -> str:
    doc_token = (args.get("doc_token") or "").strip()
    if not doc_token:
        return tool_error("doc_token is required")

    code, msg, data = _feishu_request(
        "GET", _RAW_CONTENT_URI,
        paths={"document_id": doc_token},
    )
    if code != 0:
        return tool_error(f"Failed to read document: code={code} msg={msg}")

    content = data.get("content", "")
    if not content:
        return tool_error("No content returned from document API")

    return tool_result(success=True, content=_truncate(content))


# ===================================================================
# Tool 3: cs_github_search_issues
# ===================================================================

CS_GITHUB_SEARCH_ISSUES_SCHEMA = {
    "name": "cs_github_search_issues",
    "description": (
        "Search GitHub Issues and Discussions in whitelisted repositories "
        "for error messages, known bugs, or usage questions. Returns "
        "titles, URLs, labels, and body excerpts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (error message, feature name, etc.).",
            },
            "repo": {
                "type": "string",
                "description": "Repository in owner/name format (must be in whitelist). "
                               "If omitted, searches all whitelisted repos.",
            },
            "state": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "description": "Filter by issue state (default: all).",
            },
        },
        "required": ["query"],
    },
}


def _handle_cs_github_search_issues(args: dict, **kwargs) -> str:
    import urllib.request
    import urllib.error

    query = (args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")

    cs = _load_cs_config()
    allowed_repos: List[str] = (cs.get("github") or {}).get("allowed_repos") or []
    if not allowed_repos:
        return tool_error("No GitHub repos configured in customer_support.github.allowed_repos")

    target_repo = (args.get("repo") or "").strip()
    if target_repo:
        if target_repo not in allowed_repos:
            return tool_error(
                f"Repository '{target_repo}' is not in the whitelist. "
                f"Allowed: {', '.join(allowed_repos)}"
            )
        repo_qualifiers = [f"repo:{target_repo}"]
    else:
        repo_qualifiers = [f"repo:{r}" for r in allowed_repos]

    state = (args.get("state") or "all").strip().lower()
    state_qualifier = f"state:{state}" if state in ("open", "closed") else ""

    repo_q = " ".join(repo_qualifiers)
    search_q = f"{query} {repo_q} {state_qualifier} is:issue".strip()
    url = f"https://api.github.com/search/issues?q={quote(search_q)}&per_page={_max_results()}"

    token = os.getenv("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-customer-support",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return tool_error(f"GitHub API error: {exc.code} {exc.reason}")
    except Exception as exc:
        return tool_error(f"GitHub request failed: {exc}")

    items = data.get("items") or []
    results: List[dict] = []
    for item in items[:_max_results()]:
        body_text = (item.get("body") or "")[:500]
        results.append({
            "title": item.get("title", ""),
            "url": item.get("html_url", ""),
            "state": item.get("state", ""),
            "labels": [l.get("name", "") for l in (item.get("labels") or [])],
            "created_at": item.get("created_at", ""),
            "body_excerpt": body_text,
        })

    return tool_result(
        success=True,
        total_count=data.get("total_count", 0),
        count=len(results),
        results=results,
    )


# ===================================================================
# Tool 4: cs_hf_search_discussions
# ===================================================================

CS_HF_SEARCH_DISCUSSIONS_SCHEMA = {
    "name": "cs_hf_search_discussions",
    "description": (
        "Search Hugging Face model repo discussions for known issues, "
        "usage tips, or community Q&A. Only whitelisted repos are allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Model repo in owner/name format (must be in whitelist).",
            },
            "query": {
                "type": "string",
                "description": "Search keywords.",
            },
        },
        "required": ["repo"],
    },
}


def _handle_cs_hf_search_discussions(args: dict, **kwargs) -> str:
    import urllib.request
    import urllib.error

    repo = (args.get("repo") or "").strip()
    query = (args.get("query") or "").strip()

    if not repo:
        return tool_error("repo is required")

    cs = _load_cs_config()
    allowed_repos: List[str] = (cs.get("huggingface") or {}).get("allowed_repos") or []
    if not allowed_repos:
        return tool_error("No HF repos configured in customer_support.huggingface.allowed_repos")
    if repo not in allowed_repos:
        return tool_error(
            f"Repository '{repo}' is not in the whitelist. "
            f"Allowed: {', '.join(allowed_repos)}"
        )

    url = f"https://huggingface.co/api/models/{quote(repo, safe='/')}/discussions?limit={_max_results()}"
    if query:
        url += f"&search={quote(query)}"

    token = os.getenv("HF_TOKEN", "")
    headers = {"User-Agent": "hermes-customer-support"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return tool_error(f"Hugging Face API error: {exc.code} {exc.reason}")
    except Exception as exc:
        return tool_error(f"HF request failed: {exc}")

    discussions = data.get("discussions") or (data if isinstance(data, list) else [])
    results: List[dict] = []
    for d in discussions[:_max_results()]:
        results.append({
            "title": d.get("title", ""),
            "num": d.get("num", ""),
            "status": d.get("status", ""),
            "is_pull_request": d.get("isPullRequest", False),
            "created_at": d.get("createdAt", ""),
            "url": f"https://huggingface.co/{repo}/discussions/{d.get('num', '')}",
        })

    return tool_result(success=True, count=len(results), repo=repo, results=results)


# ===================================================================
# Registration
# ===================================================================

registry.register(
    name="cs_feishu_search_docs",
    toolset="customer_support",
    schema=CS_FEISHU_SEARCH_DOCS_SCHEMA,
    handler=_handle_cs_feishu_search_docs,
    check_fn=_check_feishu_available,
    requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    is_async=False,
    description="Search Feishu knowledge base",
    emoji="\U0001f50d",
    max_result_size_chars=50_000,
)

registry.register(
    name="cs_feishu_read_doc",
    toolset="customer_support",
    schema=CS_FEISHU_READ_DOC_SCHEMA,
    handler=_handle_cs_feishu_read_doc,
    check_fn=_check_feishu_available,
    requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    is_async=False,
    description="Read Feishu document content",
    emoji="\U0001f4c4",
    max_result_size_chars=50_000,
)

registry.register(
    name="cs_github_search_issues",
    toolset="customer_support",
    schema=CS_GITHUB_SEARCH_ISSUES_SCHEMA,
    handler=_handle_cs_github_search_issues,
    check_fn=_check_github_available,
    requires_env=["GITHUB_TOKEN"],
    is_async=False,
    description="Search GitHub Issues (whitelist)",
    emoji="\U0001f41b",
    max_result_size_chars=50_000,
)

registry.register(
    name="cs_hf_search_discussions",
    toolset="customer_support",
    schema=CS_HF_SEARCH_DISCUSSIONS_SCHEMA,
    handler=_handle_cs_hf_search_discussions,
    check_fn=_check_hf_available,
    requires_env=[],
    is_async=False,
    description="Search HuggingFace Discussions (whitelist)",
    emoji="\U0001f917",
    max_result_size_chars=50_000,
)
