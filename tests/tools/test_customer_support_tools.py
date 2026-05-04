"""Tests for customer_support_tools -- registration, whitelist, truncation."""

import importlib
import json
import os
import unittest
from unittest.mock import patch, MagicMock

from tools.registry import registry

# Trigger tool discovery
importlib.import_module("tools.customer_support_tools")

from tools.customer_support_tools import (
    _truncate,
    _handle_cs_github_search_issues,
    _handle_cs_hf_search_discussions,
    _load_cs_config,
)


# ---------------------------------------------------------------------------
# Registration & schema
# ---------------------------------------------------------------------------

class TestCSToolRegistration(unittest.TestCase):
    """Verify customer_support tools are registered with valid schemas."""

    EXPECTED_TOOLS = {
        "cs_feishu_search_docs": "customer_support",
        "cs_feishu_read_doc": "customer_support",
        "cs_github_search_issues": "customer_support",
        "cs_hf_search_discussions": "customer_support",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            self.assertIsNotNone(entry, f"{tool_name} not registered")
            self.assertEqual(entry.toolset, toolset)

    def test_schemas_have_required_fields(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            schema = entry.schema
            self.assertIn("name", schema)
            self.assertEqual(schema["name"], tool_name)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_handlers_are_callable(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            self.assertTrue(callable(entry.handler))

    def test_github_schema_has_query_param(self):
        entry = registry.get_entry("cs_github_search_issues")
        props = entry.schema["parameters"]["properties"]
        self.assertIn("query", props)
        self.assertIn("repo", props)

    def test_hf_schema_has_repo_param(self):
        entry = registry.get_entry("cs_hf_search_discussions")
        props = entry.schema["parameters"]["properties"]
        self.assertIn("repo", props)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation(unittest.TestCase):

    def test_short_text_unchanged(self):
        text = "hello world"
        self.assertEqual(_truncate(text, 100), text)

    def test_long_text_truncated(self):
        text = "x" * 200
        result = _truncate(text, 50)
        self.assertTrue(result.startswith("x" * 50))
        self.assertIn("truncated", result)
        self.assertIn("150 chars omitted", result)

    def test_exact_limit_unchanged(self):
        text = "a" * 100
        self.assertEqual(_truncate(text, 100), text)


# ---------------------------------------------------------------------------
# Whitelist enforcement
# ---------------------------------------------------------------------------

class TestGitHubWhitelist(unittest.TestCase):

    def _make_cs_config(self, repos):
        return {"github": {"allowed_repos": repos}}

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_unlisted_repo(self, mock_config):
        mock_config.return_value = self._make_cs_config(["OpenBMB/MiniCPM"])
        result = json.loads(_handle_cs_github_search_issues({
            "query": "OOM error",
            "repo": "evil/repo",
        }))
        self.assertIn("error", result)
        self.assertIn("not in the whitelist", result["error"])

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_empty_whitelist(self, mock_config):
        mock_config.return_value = self._make_cs_config([])
        result = json.loads(_handle_cs_github_search_issues({
            "query": "OOM error",
        }))
        self.assertIn("error", result)
        self.assertIn("No GitHub repos configured", result["error"])

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_empty_query(self, mock_config):
        mock_config.return_value = self._make_cs_config(["OpenBMB/MiniCPM"])
        result = json.loads(_handle_cs_github_search_issues({"query": ""}))
        self.assertIn("error", result)
        self.assertIn("query is required", result["error"])


class TestHFWhitelist(unittest.TestCase):

    def _make_cs_config(self, repos):
        return {"huggingface": {"allowed_repos": repos}}

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_unlisted_repo(self, mock_config):
        mock_config.return_value = self._make_cs_config(["openbmb/MiniCPM-V"])
        result = json.loads(_handle_cs_hf_search_discussions({
            "repo": "evil/model",
        }))
        self.assertIn("error", result)
        self.assertIn("not in the whitelist", result["error"])

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_empty_whitelist(self, mock_config):
        mock_config.return_value = self._make_cs_config([])
        result = json.loads(_handle_cs_hf_search_discussions({
            "repo": "openbmb/MiniCPM-V",
        }))
        self.assertIn("error", result)
        self.assertIn("No HF repos configured", result["error"])

    @patch("tools.customer_support_tools._load_cs_config")
    def test_rejects_missing_repo(self, mock_config):
        mock_config.return_value = self._make_cs_config(["openbmb/MiniCPM-V"])
        result = json.loads(_handle_cs_hf_search_discussions({"repo": ""}))
        self.assertIn("error", result)
        self.assertIn("repo is required", result["error"])


# ---------------------------------------------------------------------------
# Toolset resolution
# ---------------------------------------------------------------------------

class TestToolsetResolution(unittest.TestCase):

    def test_customer_support_toolset_resolves(self):
        from toolsets import resolve_toolset
        tools = resolve_toolset("customer_support")
        self.assertIn("cs_feishu_search_docs", tools)
        self.assertIn("cs_feishu_read_doc", tools)
        self.assertIn("cs_github_search_issues", tools)
        self.assertIn("cs_hf_search_discussions", tools)

    def test_customer_support_not_in_core_tools(self):
        from toolsets import _HERMES_CORE_TOOLS
        for tool in ["cs_feishu_search_docs", "cs_feishu_read_doc",
                      "cs_github_search_issues", "cs_hf_search_discussions"]:
            self.assertNotIn(tool, _HERMES_CORE_TOOLS)


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

class TestDefaultConfig(unittest.TestCase):

    def test_customer_support_in_default_config(self):
        from hermes_cli.config import DEFAULT_CONFIG
        cs = DEFAULT_CONFIG.get("customer_support")
        self.assertIsNotNone(cs, "customer_support missing from DEFAULT_CONFIG")
        self.assertIn("max_results", cs)
        self.assertIn("max_content_chars", cs)
        self.assertIn("feishu", cs)
        self.assertIn("github", cs)
        self.assertIn("huggingface", cs)
        self.assertFalse(cs["feishu"]["enabled"])
        self.assertEqual(cs["github"]["allowed_repos"], [])
        self.assertEqual(cs["huggingface"]["allowed_repos"], [])


if __name__ == "__main__":
    unittest.main()
