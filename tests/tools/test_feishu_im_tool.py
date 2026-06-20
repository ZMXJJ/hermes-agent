"""Tests for tools/feishu_im_tool -- read-only Feishu/Lark IM tools over lark-cli.

These are invariant tests (not snapshots): they assert the read-only contract,
that every invocation uses user identity, JSON parsing/error handling behavior,
and availability gating on the lark-cli binary.
"""

import importlib
import json
import subprocess
from types import SimpleNamespace

import pytest

import tools.feishu_im_tool as fim
from tools.registry import registry

# Ensure the module's registrations are present.
importlib.import_module("tools.feishu_im_tool")


_EXPECTED_TOOLS = {
    "feishu_im_chat_list",
    "feishu_im_chat_messages",
    "feishu_im_chat_search",
    "feishu_im_messages_search",
    "feishu_im_messages_get",
}

# Substrings that would indicate a write/mutating command leaked into the toolset.
_WRITE_MARKERS = ("send", "reply", "create", "update", "delete", "remove", "forward")


@pytest.fixture
def capture_run(monkeypatch):
    """Capture argv passed to subprocess.run and return a canned success result."""
    calls = {}

    def fake_which(_name):
        return "/usr/local/bin/lark-cli"

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"data": {"chats": [], "has_more": False}}),
            stderr="",
        )

    monkeypatch.setattr(fim.shutil, "which", fake_which)
    monkeypatch.setattr(fim.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Registration / read-only contract
# ---------------------------------------------------------------------------


def test_all_tools_registered_under_feishu_im():
    for name in _EXPECTED_TOOLS:
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "feishu_im"
        assert callable(entry.handler)


def test_schemas_well_formed():
    for name in _EXPECTED_TOOLS:
        schema = registry.get_entry(name).schema
        assert schema["name"] == name
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"


def test_toolset_contains_no_write_tools():
    names = registry.get_tool_names_for_toolset("feishu_im")
    assert set(names) == _EXPECTED_TOOLS
    for name in names:
        for marker in _WRITE_MARKERS:
            assert marker not in name, f"write-ish tool leaked into feishu_im: {name}"


def test_messages_get_requires_message_ids():
    schema = registry.get_entry("feishu_im_messages_get").schema
    assert schema["parameters"]["required"] == ["message_ids"]


# ---------------------------------------------------------------------------
# argv invariants: user identity + json, read-only verbs only
# ---------------------------------------------------------------------------


def test_chat_list_uses_user_identity_and_json(capture_run):
    fim._handle_chat_list({})
    argv = capture_run["argv"]
    assert argv[:3] == ["lark-cli", "im", "+chat-list"]
    assert "--as" in argv and argv[argv.index("--as") + 1] == "user"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert "--yes" not in argv


def test_no_handler_emits_write_verb(capture_run):
    handlers = [
        (fim._handle_chat_list, {}),
        (fim._handle_chat_messages, {"chat_id": "oc_x"}),
        (fim._handle_chat_search, {"query": "team"}),
        (fim._handle_messages_search, {"query": "hi"}),
        (fim._handle_messages_get, {"message_ids": "om_x"}),
    ]
    for handler, args in handlers:
        capture_run.clear()
        handler(args)
        verb = capture_run["argv"][2]
        assert verb.startswith("+"), verb
        for marker in _WRITE_MARKERS:
            assert marker not in verb, f"unexpected write verb {verb}"
        assert "--as" in capture_run["argv"]
        assert capture_run["argv"][capture_run["argv"].index("--as") + 1] == "user"


def test_chat_messages_passes_filters(capture_run):
    fim._handle_chat_messages(
        {"chat_id": "oc_abc", "start": "2026-03-10", "end": "2026-03-11", "order": "asc", "page_size": 50}
    )
    argv = capture_run["argv"]
    assert "--chat-id" in argv and argv[argv.index("--chat-id") + 1] == "oc_abc"
    assert "--start" in argv and argv[argv.index("--start") + 1] == "2026-03-10"
    assert "--order" in argv and argv[argv.index("--order") + 1] == "asc"
    assert "--page-size" in argv and argv[argv.index("--page-size") + 1] == "50"


def test_messages_search_page_all_is_bare_flag(capture_run):
    fim._handle_messages_search({"query": "release", "page_all": True})
    argv = capture_run["argv"]
    assert "--page-all" in argv
    # bare flag: not followed by a value token
    idx = argv.index("--page-all")
    assert idx == len(argv) - 1 or argv[idx + 1].startswith("--")


def test_empty_args_omit_flags(capture_run):
    fim._handle_chat_list({"types": "", "sort": None})
    argv = capture_run["argv"]
    assert "--types" not in argv
    assert "--sort" not in argv


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_chat_messages_requires_a_target(capture_run):
    out = json.loads(fim._handle_chat_messages({}))
    assert "error" in out


def test_chat_messages_rejects_both_targets(capture_run):
    out = json.loads(fim._handle_chat_messages({"chat_id": "oc_x", "user_id": "ou_y"}))
    assert "error" in out


def test_chat_search_requires_query_or_members(capture_run):
    out = json.loads(fim._handle_chat_search({}))
    assert "error" in out


def test_messages_get_requires_ids(capture_run):
    out = json.loads(fim._handle_messages_get({"message_ids": "  "}))
    assert "error" in out


# ---------------------------------------------------------------------------
# Output / error handling
# ---------------------------------------------------------------------------


def test_success_returns_parsed_data(capture_run):
    out = json.loads(fim._handle_chat_list({}))
    assert out["success"] is True
    assert out["result"]["data"]["has_more"] is False


def test_nonzero_exit_with_json_envelope_passed_through(monkeypatch):
    monkeypatch.setattr(fim.shutil, "which", lambda _n: "/bin/lark-cli")
    envelope = {"ok": False, "error": {"type": "permission", "console_url": "https://x"}}

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=json.dumps(envelope))

    monkeypatch.setattr(fim.subprocess, "run", fake_run)
    out = json.loads(fim._handle_chat_list({}))
    assert "error" in out
    assert out["detail"]["error"]["console_url"] == "https://x"


def test_timeout_returns_error(monkeypatch):
    monkeypatch.setattr(fim.shutil, "which", lambda _n: "/bin/lark-cli")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(fim.subprocess, "run", fake_run)
    out = json.loads(fim._handle_chat_list({}))
    assert "error" in out
    assert "timed out" in out["error"]


def test_check_fn_false_when_cli_missing(monkeypatch):
    monkeypatch.setattr(fim.shutil, "which", lambda _n: None)
    assert fim._check_lark_cli() is False


def test_handler_short_circuits_when_cli_missing(monkeypatch):
    monkeypatch.setattr(fim.shutil, "which", lambda _n: None)

    def boom(*a, **k):
        raise AssertionError("subprocess.run must not be called when cli missing")

    monkeypatch.setattr(fim.subprocess, "run", boom)
    out = json.loads(fim._handle_chat_list({}))
    assert "error" in out
    assert "lark-cli not found" in out["error"]
