from __future__ import annotations

import json
import base64
import io
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import unittest
import unittest.mock

from contextlib import redirect_stderr
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from copy import deepcopy
from pathlib import Path
from typing import Any

# --- safe import shim (do not remove) -------------------------------------
# Every `from volley.* import X` below is wrapped so that if a candidate
# implementation is missing symbol X, the test module still loads. Only the
# tests that actually touch X fail (with a clear ImportError) instead of the
# whole suite collapsing at import time.
import importlib as _importlib

def _codex_unavail(qualname, exc):
    msg = "{} is not implemented in this volley package: {}: {}".format(
        qualname, type(exc).__name__, exc
    )
    class _Unavailable:
        def __call__(self, *a, **k): raise ImportError(msg)
        def __getattr__(self, n): raise ImportError(msg)
        def __iter__(self): raise ImportError(msg)
        def __bool__(self): return False
        def __repr__(self): return "<unavailable {}>".format(qualname)
    _Unavailable.__name__ = qualname.rsplit(".", 1)[-1]
    return _Unavailable()

def _codex_safe_from(modname, *names_with_aliases):
    """names_with_aliases items are either 'Name' or 'Name as Alias'."""
    pairs = []
    for spec in names_with_aliases:
        spec = spec.strip()
        if " as " in spec:
            name, alias = [p.strip() for p in spec.split(" as ")]
        else:
            name = alias = spec
        pairs.append((name, alias))
    try:
        mod = _importlib.import_module(modname)
    except Exception as _e:
        for name, alias in pairs:
            globals()[alias] = _codex_unavail(modname + "." + name, _e)
        return
    for name, alias in pairs:
        try:
            globals()[alias] = getattr(mod, name)
        except AttributeError as _e:
            globals()[alias] = _codex_unavail(modname + "." + name, _e)


def _ansi_terminal_lines(text: str) -> list[str]:
    lines = [""]
    row = 0
    col = 0
    index = 0

    def ensure_row(target: int) -> None:
        while len(lines) <= target:
            lines.append("")

    while index < len(text):
        ch = text[index]
        if ch == "\r":
            col = 0
            index += 1
            continue
        if ch == "\n":
            row += 1
            col = 0
            ensure_row(row)
            index += 1
            continue
        if ch == "\033" and index + 1 < len(text) and text[index + 1] == "[":
            end = index + 2
            while end < len(text) and not text[end].isalpha():
                end += 1
            if end >= len(text):
                break
            params = text[index + 2 : end]
            command = text[end]
            amount = int(params or "1") if (params or "1").isdigit() else 1
            if command == "A":
                row = max(0, row - amount)
            elif command == "B":
                row += amount
                ensure_row(row)
            elif command == "K":
                ensure_row(row)
                lines[row] = ""
                col = 0
            index = end + 1
            continue
        ensure_row(row)
        line = lines[row]
        if col > len(line):
            line += " " * (col - len(line))
        if col == len(line):
            line += ch
        else:
            line = line[:col] + ch + line[col + 1 :]
        lines[row] = line
        col += 1
        index += 1

    while lines and lines[-1] == "":
        lines.pop()
    return lines

def _codex_safe_import(modname, alias):
    try:
        globals()[alias] = _importlib.import_module(modname)
    except Exception as _e:
        globals()[alias] = _codex_unavail(modname, _e)
# --- end safe import shim -------------------------------------------------

# --- subprocess.run default-timeout guard (do not remove) -----------------
# A broken candidate may hang when the test launches `python -m volley ...` as
# a subprocess (e.g. it tries to call OpenAI instead of honoring the fake-
# responses env). Default every subprocess.run() to a generous timeout so a
# single hung test cannot stall the whole suite.
import subprocess as _codex_subprocess
_codex_subprocess_run_orig = _codex_subprocess.run
def _codex_subprocess_run(*args, **kwargs):
    kwargs.setdefault("timeout", 30)

    return _codex_subprocess_run_orig(*args, **kwargs)
_codex_subprocess.run = _codex_subprocess_run
# --- end subprocess.run guard ---------------------------------------------


def _fake_jwt(payload: dict[str, Any]) -> str:
    def encode(part: dict[str, Any]) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"

_codex_safe_import("volley.types", "codex_types")
_codex_safe_from("volley", "VolleyConfig", "VolleySession")
_codex_safe_from("volley.memory", "MemoryStageOneRecord")
_codex_safe_from("volley.memory", "MemoryStateStore")
_codex_safe_from("volley.memory", "MemoryThreadRecord")
_codex_safe_from("volley.memory", "MemoryWorkspaceChange")
_codex_safe_from("volley.memory", "load_memory_rollout")
_codex_safe_from("volley.memory", "memory_rollout_candidates")
_codex_safe_from("volley.memory", "memory_rollout_is_stage1_startup_eligible")
_codex_safe_from("volley.memory", "memory_workspace_diff")
_codex_safe_from("volley.memory", "memory_extensions_root")
_codex_safe_from("volley.memory", "memory_rate_limit_allows_startup")
_codex_safe_from("volley.memory", "prepare_memory_workspace")
_codex_safe_from("volley.memory", "prune_old_extension_resources")
_codex_safe_from("volley.memory", "prune_stage1_records_for_retention")
_codex_safe_from("volley.memory", "reset_memory_workspace_baseline")
_codex_safe_from("volley.memory", "run_memory_stage_one_for_rollout")
_codex_safe_from("volley.memory", "run_memory_startup_once")
_codex_safe_from("volley.memory", "sanitize_response_item_for_memories")
_codex_safe_from("volley.memory", "seed_extension_instructions")
_codex_safe_from("volley.memory", "serialize_filtered_rollout_response_items")
_codex_safe_from("volley.memory", "select_phase2_memory_inputs")
_codex_safe_from("volley.memory", "build_memory_consolidation_config")
_codex_safe_from("volley.memory", "rebuild_raw_memories_file_from_memories")
_codex_safe_from("volley.memory", "render_memory_workspace_diff_file")
_codex_safe_from("volley.memory", "rollout_summaries_dir")
_codex_safe_from("volley.memory", "rollout_summary_file_stem")
_codex_safe_from("volley.memory", "extract_memory_stage_one")
_codex_safe_from("volley.memory", "memory_stage_one_output_schema")
_codex_safe_from("volley.memory", "parse_memory_stage_one_output")
_codex_safe_from("volley.memory", "raw_memories_file")
_codex_safe_from("volley.memory", "run_memory_consolidation_session")
_codex_safe_from("volley.memory", "run_memory_phase2_once")
_codex_safe_from("volley.memory", "run_memory_startup_pipeline_once")
_codex_safe_from("volley.memory", "start_memory_startup_task")
_codex_safe_from("volley.memory", "sync_rollout_summaries_from_memories")
_codex_safe_from("volley.memory", "sync_phase2_workspace_inputs")
_codex_safe_from("volley.memory", "write_current_memory_workspace_diff")
_codex_safe_from("volley.memory", "write_memory_workspace_diff")
_codex_safe_from("volley.model", "ScriptedResponsesModel")
_codex_safe_from("volley.model", "ModelStreamEvent")
_codex_safe_from("volley.model", "OpenAIResponsesModel")
_codex_safe_from("volley.model", "ChatGPTCodexSubscriptionModel")
_codex_safe_from("volley.model", "FallbackModelClient")
_codex_safe_from("volley.model", "GeminiGenerateContentModel")
_codex_safe_from("volley.model", "default_model_client")
_codex_safe_from("volley.model", "collect_stream_response")
_codex_safe_from("volley.model", "load_env_file")
_codex_safe_from("volley.model", "load_gemini_api_key")
_codex_safe_from("volley.model", "_gemini_request_body")
_codex_safe_from("volley.model", "_iter_gemini_stream_events")
_codex_safe_from("volley.auth", "auth_status")
_codex_safe_from("volley.auth", "build_authorize_url")
_codex_safe_from("volley.auth", "chatgpt_codex_base_url")
_codex_safe_from("volley.auth", "chatgpt_backend_base_url")
_codex_safe_from("volley.auth", "fetch_chatgpt_rate_limits")
_codex_safe_from("volley.auth", "parse_chatgpt_rate_limit_payload")
_codex_safe_from("volley.auth", "login_with_api_key")
_codex_safe_from("volley.auth", "load_auth_snapshot")
_codex_safe_from("volley.auth", "save_chatgpt_tokens")
_codex_safe_from("volley.prompts", "build_base_instructions", "build_environment_context", "build_initial_context_items")
_codex_safe_from("volley.prompts", "build_permissions_instructions", "collect_agents_md")
_codex_safe_from("volley.prompts", "verify_asset_hashes", "read_model_catalog_instructions")
_codex_safe_from("volley.prompts", "ASSETS_DIR as _CODEX_ASSETS_DIR")
_codex_safe_from("volley.cli", "_model_provider_for_model")
_codex_safe_from("volley.cli", "_set_session_model")


# Asset comparison table used by optional upstream fixture checks.
_EVAL_UPSTREAM_ASSET_PATHS = {
    "prompts/gpt_5_codex_prompt.md": "volley-rs/core/gpt_5_codex_prompt.md",
    "prompts/gpt_5_2_prompt.md": "volley-rs/core/gpt_5_2_prompt.md",
    "prompts/gpt-5.2-codex_prompt.md": "volley-rs/core/gpt-5.2-codex_prompt.md",
    "prompts/prompt_with_apply_patch_instructions.md": "volley-rs/core/prompt_with_apply_patch_instructions.md",
    "prompts/compact/prompt.md": "volley-rs/core/templates/compact/prompt.md",
    "prompts/compact/summary_prefix.md": "volley-rs/core/templates/compact/summary_prefix.md",
    "prompts/memories/read_path.md": "volley-rs/memories/read/templates/memories/read_path.md",
    "prompts/memories/write/stage_one_system.md": "volley-rs/memories/write/templates/memories/stage_one_system.md",
    "prompts/memories/write/stage_one_input.md": "volley-rs/memories/write/templates/memories/stage_one_input.md",
    "prompts/memories/write/consolidation.md": "volley-rs/memories/write/templates/memories/consolidation.md",
    "prompts/memories/write/extensions/ad_hoc/instructions.md": "volley-rs/memories/write/templates/extensions/ad_hoc/instructions.md",
    "grammars/apply_patch.lark": "volley-rs/core/src/tools/handlers/apply_patch.lark",
}


def _eval_compare_assets_to_upstream() -> dict[str, bool]:
    upstream_env = os.environ.get("CODEX_UPSTREAM_DIR")
    upstream_dir = Path(upstream_env).expanduser() if upstream_env else (
        Path(_CODEX_ASSETS_DIR).parent / "upstream" / "openai-volley"
    )
    result: dict[str, bool] = {}
    for asset_path, upstream_path in _EVAL_UPSTREAM_ASSET_PATHS.items():
        upstream = upstream_dir / upstream_path
        result[asset_path] = (
            upstream.exists()
            and (Path(_CODEX_ASSETS_DIR) / asset_path).read_bytes() == upstream.read_bytes()
        )
    return result
# --- safe import shim (do not remove) -------------------------------------
# Every `from volley.* import X` below is wrapped so that if a candidate
# implementation is missing symbol X, the test module still loads. Only the
# tests that actually touch X fail (with a clear ImportError) instead of the
# whole suite collapsing at import time.
import importlib as _importlib

def _codex_unavail(qualname, exc):
    msg = "{} is not implemented in this volley package: {}: {}".format(
        qualname, type(exc).__name__, exc
    )
    class _Unavailable:
        def __call__(self, *a, **k): raise ImportError(msg)
        def __getattr__(self, n): raise ImportError(msg)
        def __iter__(self): raise ImportError(msg)
        def __bool__(self): return False
        def __repr__(self): return "<unavailable {}>".format(qualname)
    _Unavailable.__name__ = qualname.rsplit(".", 1)[-1]
    return _Unavailable()

def _codex_safe_from(modname, *names_with_aliases):
    """names_with_aliases items are either 'Name' or 'Name as Alias'."""
    pairs = []
    for spec in names_with_aliases:
        spec = spec.strip()
        if " as " in spec:
            name, alias = [p.strip() for p in spec.split(" as ")]
        else:
            name = alias = spec
        pairs.append((name, alias))
    try:
        mod = _importlib.import_module(modname)
    except Exception as _e:
        for name, alias in pairs:
            globals()[alias] = _codex_unavail(modname + "." + name, _e)
        return
    for name, alias in pairs:
        try:
            globals()[alias] = getattr(mod, name)
        except AttributeError as _e:
            globals()[alias] = _codex_unavail(modname + "." + name, _e)

def _codex_safe_import(modname, alias):
    try:
        globals()[alias] = _importlib.import_module(modname)
    except Exception as _e:
        globals()[alias] = _codex_unavail(modname, _e)
# --- end safe import shim -------------------------------------------------

_codex_safe_from("volley.prompts", "build_memory_consolidation_prompt")
_codex_safe_from("volley.prompts", "build_memory_stage_one_input_message")
_codex_safe_from("volley.prompts", "memory_stage_one_rollout_token_limit")
_codex_safe_from("volley.prompts", "memory_stage_one_system_prompt")
_codex_safe_from("volley.state", "build_compacted_history")
_codex_safe_from("volley.state", "build_compaction_summary_text")
_codex_safe_from("volley.state", "parse_command_actions")
_codex_safe_from("volley.state", "prepare_prompt_history")
_codex_safe_from("volley.state", "reconstruct_history_from_rollout")
_codex_safe_from("volley.state", "trim_remote_compaction_history_to_fit_context_window")
_codex_safe_from("volley.state", "parse_memory_citation")
_codex_safe_from("volley.state", "extract_proposed_plan_text")
_codex_safe_from("volley.state", "strip_memory_citations")
_codex_safe_from("volley.state", "strip_proposed_plan_blocks")
_codex_safe_from("volley.state", "summarization_prompt")
_codex_safe_from("volley.state", "VOLLEY_ROLLOUT_ITEM_TYPES")
_codex_safe_import("volley.tools", "codex_tools")
_codex_safe_from("volley.tools", "ToolRuntime")
_codex_safe_from("volley.tools", "ToolResult")
_codex_safe_from("volley.types", "KNOWN_EVENT_TYPES")
_codex_safe_from("volley.types", "PromptRequest")
_codex_safe_from("volley.types", "TERMINAL_TURN_EVENT_TYPES")
_codex_safe_from("volley.core", "LOCAL_COMPACTION_WARNING_MESSAGE")
_codex_safe_from("volley.core", "_provider_supports_remote_compaction")


def message(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


class MultiAgentRoutingModel:
    def __init__(self):
        self.requests: list[PromptRequest] = []

    def create(self, request: PromptRequest):
        self.requests.append(request)
        texts = _request_texts(request)
        if any("child task" in text for text in texts):
            return _model_response(message("child done"))
        outputs = [item for item in request.input if item.get("type") == "function_call_output"]
        if outputs and outputs[-1].get("call_id") == "spawn-1":
            agent_id = json.loads(outputs[-1]["output"])["agent_id"]
            return _model_response(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "wait_agent",
                            "call_id": "wait-1",
                            "arguments": json.dumps({"targets": [agent_id], "timeout_ms": 5000}),
                        }
                    ]
                }
            )
        if outputs and outputs[-1].get("call_id") == "wait-1":
            return _model_response(message("parent saw child"))
        return _model_response(
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": "spawn-1",
                        "arguments": json.dumps({"message": "child task", "agent_type": "worker"}),
                    }
                ]
            }
        )


class RemoteCompactModel:
    def __init__(self, *, compact_output: list[dict], local_responses: list[dict] | None = None, fail_remote: bool = False):
        self.compact_output = compact_output
        self.local = ScriptedResponsesModel(local_responses or [])
        self.fail_remote = fail_remote
        self.compact_requests: list[PromptRequest] = []

    @property
    def requests(self) -> list[PromptRequest]:
        return self.local.requests

    def compact(self, request: PromptRequest, **_: Any) -> list[dict]:
        self.compact_requests.append(request)
        if self.fail_remote:
            raise RuntimeError("compact endpoint unavailable")
        return deepcopy(self.compact_output)

    def stream(self, request: PromptRequest):
        yield from self.local.stream(request)

    def create(self, request: PromptRequest):
        return self.local.create(request)


def _model_response(payload: dict):
    from volley.types import ModelResponse

    return ModelResponse(id="routed", output=list(payload.get("output", [])), raw=dict(payload))


def _stream_message(text: str) -> ModelStreamEvent:
    return ModelStreamEvent(
        "item.completed",
        {
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        },
    )


def _request_texts(request: PromptRequest) -> list[str]:
    texts: list[str] = []
    for item in request.input:
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return texts


def _plain_terminal_output(text: str) -> str:
    import re

    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = text.replace("\r", "\n")
    return text


class CoreCoreTests(unittest.TestCase):
    def test_prompt_assets_match_manifest(self) -> None:
        self.assertTrue(all(verify_asset_hashes().values()))

    def test_core_tool_schema_names(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        expected = (
            [
                "shell_command",
                "update_plan",
                "request_user_input",
                "apply_patch",
                "view_image",
                "spawn_agent",
                "send_input",
                "resume_agent",
                "wait_agent",
                "close_agent",
                "web_search",
            ]
            if sys.platform == "win32"
            else [
                "exec_command",
                "write_stdin",
                "update_plan",
                "request_user_input",
                "apply_patch",
                "view_image",
                "spawn_agent",
                "send_input",
                "resume_agent",
                "wait_agent",
                "close_agent",
                "web_search",
            ]
        )
        self.assertEqual([spec.get("name", spec["type"]) for spec in runtime.specs()], expected)

    def test_turn_loop_has_no_default_iteration_cap_but_can_be_capped_for_debug(self) -> None:
        self.assertIsNone(VolleyConfig(skip_git_repo_check=True, ephemeral=True).max_iterations)

        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "update_plan",
                            "call_id": "plan-1",
                            "arguments": json.dumps(
                                {"plan": [{"step": "keep looping", "status": "in_progress"}]}
                            ),
                        }
                    ]
                }
            ]
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, max_iterations=1),
            model_client=model,
        )

        result = session.run("exercise the optional iteration cap")

        self.assertEqual(len(model.requests), 1)
        self.assertIn(
            "max_iterations exceeded",
            [str(event.payload.get("error")) for event in result.events if event.type == "turn.failed"],
        )

    def test_session_runs_in_non_git_directory_without_skip_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = VolleySession(
                VolleyConfig(cwd=tmp, ephemeral=True),
                model_client=ScriptedResponsesModel([message("ok outside git")]),
            )

            result = session.run("hello")

        self.assertEqual(result.final_message, "ok outside git")
        self.assertFalse([event for event in result.events if event.type == "turn.failed"])

    def test_request_user_input_schema_and_default_unavailable_handler(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        spec = next(spec for spec in runtime.specs() if spec.get("name") == "request_user_input")
        self.assertIn("Plan mode", spec["description"])
        self.assertEqual(spec["parameters"]["required"], ["questions"])

        result = runtime.request_user_input(
            {
                "questions": [
                    {
                        "id": "choice",
                        "header": "Choice",
                        "question": "Pick one.",
                        "options": [{"label": "A (Recommended)", "description": "Choose A."}],
                    }
                ]
            }
        )
        self.assertFalse(result.ok)
        self.assertIn("unavailable in Default mode", result.output)

    def test_request_user_input_provider_returns_upstream_response_shape(self) -> None:
        def provider(questions: list[dict]) -> dict:
            self.assertTrue(questions[0]["isOther"])
            self.assertFalse(questions[0]["isSecret"])
            return {"answers": {"choice": {"answers": ["A"]}}}

        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                collaboration_mode="Plan",
                request_user_input_provider=provider,
            )
        )
        arguments = {
            "questions": [
                {
                    "id": "choice",
                    "header": "Choice",
                    "question": "Pick one.",
                    "options": [{"label": "A (Recommended)", "description": "Choose A."}],
                }
            ]
        }
        result = runtime.request_user_input(arguments)

        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.output), {"answers": {"choice": {"answers": ["A"]}}})
        self.assertNotIn("isOther", arguments["questions"][0])

    def test_request_user_input_is_root_thread_only(self) -> None:
        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                collaboration_mode="Plan",
                agent_depth=1,
                request_user_input_answers={"choice": {"answers": ["A"]}},
            )
        )
        result = runtime.request_user_input(
            {
                "questions": [
                    {
                        "id": "choice",
                        "header": "Choice",
                        "question": "Pick one.",
                        "options": [{"label": "A (Recommended)", "description": "Choose A."}],
                    }
                ]
            }
        )
        self.assertFalse(result.ok)
        self.assertIn("root thread", result.output)

    def test_update_plan_output_and_plan_mode_rejection_match_upstream_handler(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        result = runtime.update_plan({"plan": [{"step": "Inspect", "status": "in_progress"}]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "Plan updated")

        plan_mode = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True, collaboration_mode="Plan"))
        rejected = plan_mode.update_plan({"plan": [{"step": "Inspect", "status": "in_progress"}]})
        self.assertFalse(rejected.ok)
        self.assertIn("not allowed in Plan mode", rejected.output)

    def test_web_search_tool_uses_upstream_hosted_shape(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        web_search = [spec for spec in runtime.specs() if spec.get("type") == "web_search"]
        self.assertEqual(web_search, [{"type": "web_search", "external_web_access": False}])

    def test_web_search_tool_serializes_upstream_config_shape(self) -> None:
        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                web_search_external_web_access=True,
                web_search_filters={"allowed_domains": ["example.com"]},
                web_search_user_location={
                    "type": "approximate",
                    "country": "US",
                    "region": "California",
                    "city": "San Francisco",
                    "timezone": "America/Los_Angeles",
                },
                web_search_context_size="high",
                web_search_content_types=("text", "image"),
            )
        )
        web_search = [spec for spec in runtime.specs() if spec.get("type") == "web_search"]
        self.assertEqual(
            web_search,
            [
                {
                    "type": "web_search",
                    "external_web_access": True,
                    "filters": {"allowed_domains": ["example.com"]},
                    "user_location": {
                        "type": "approximate",
                        "country": "US",
                        "region": "California",
                        "city": "San Francisco",
                        "timezone": "America/Los_Angeles",
                    },
                    "search_context_size": "high",
                    "search_content_types": ["text", "image"],
                }
            ],
        )

    def test_multi_agent_specs_are_visible_but_runtime_is_unavailable(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        names = {spec.get("name", spec["type"]) for spec in runtime.specs()}
        self.assertTrue({"spawn_agent", "send_input", "resume_agent", "wait_agent", "close_agent"} <= names)
        result = runtime.dispatch("spawn_agent", {"message": "inspect this"})
        self.assertFalse(result.ok)
        self.assertIn("multi-agent runtime is not implemented", result.output)

    def test_codex_session_runs_local_multi_agent_spawn_and_wait(self) -> None:
        model = MultiAgentRoutingModel()
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("delegate")

        self.assertEqual(result.final_message, "parent saw child")
        outputs = [item for item in result.history if item.get("type") == "function_call_output"]
        spawn_output = json.loads(next(item["output"] for item in outputs if item["call_id"] == "spawn-1"))
        wait_output = json.loads(next(item["output"] for item in outputs if item["call_id"] == "wait-1"))
        self.assertEqual(spawn_output["nickname"], "worker")
        self.assertEqual(wait_output["status"][spawn_output["agent_id"]], {"completed": "child done"})
        self.assertFalse(wait_output["timed_out"])
        notification_texts = [
            part["text"]
            for item in result.history
            if item.get("type") == "message" and item.get("role") == "user"
            for part in item.get("content", [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        self.assertTrue(any("<subagent_notification>" in text for text in notification_texts))
        self.assertTrue(any('"status":{"completed":"child done"}' in text for text in notification_texts))

    def test_local_multi_agent_send_resume_and_close(self) -> None:
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=ScriptedResponsesModel([message("first child"), message("second child")]),
        )

        spawned = session.tools.spawn_agent({"message": "first"})
        agent_id = json.loads(spawned.output)["agent_id"]
        first_wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 5000}).output)
        self.assertEqual(first_wait["status"][agent_id], {"completed": "first child"})

        sent = session.tools.send_input({"target": agent_id, "message": "second"})
        self.assertTrue(sent.ok)
        second_wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 5000}).output)
        self.assertEqual(second_wait["status"][agent_id], {"completed": "second child"})

        closed = json.loads(session.tools.close_agent({"target": agent_id}).output)
        self.assertEqual(closed["previous_status"], {"completed": "second child"})
        resumed = json.loads(session.tools.resume_agent({"id": agent_id}).output)
        self.assertEqual(resumed["status"], {"completed": "second child"})

    def test_local_multi_agent_interrupts_running_child_for_send_input(self) -> None:
        sleep_command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "sleep-1",
                                "arguments": json.dumps({"cmd": sleep_command, "yield_time_ms": 30000}),
                            }
                        ]
                    },
                    message("second child"),
                ]
            ),
        )

        spawned = session.tools.spawn_agent({"message": "first"})
        agent_id = json.loads(spawned.output)["agent_id"]
        time.sleep(0.2)
        sent = session.tools.send_input({"target": agent_id, "message": "second", "interrupt": True})
        self.assertTrue(sent.ok)
        second_wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 10000}).output)
        self.assertEqual(second_wait["status"][agent_id], {"completed": "second child"})

    def test_local_multi_agent_close_interrupts_running_child(self) -> None:
        sleep_command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "sleep-1",
                                "arguments": json.dumps({"cmd": sleep_command, "yield_time_ms": 30000}),
                            }
                        ]
                    }
                ]
            ),
        )

        spawned = session.tools.spawn_agent({"message": "first"})
        agent_id = json.loads(spawned.output)["agent_id"]
        time.sleep(0.2)
        closed = json.loads(session.tools.close_agent({"target": agent_id}).output)
        self.assertEqual(closed["previous_status"], "running")
        wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 10000}).output)
        self.assertEqual(wait["status"][agent_id], "shutdown")

    def test_model_client_cancel_is_scoped_to_thread_id(self) -> None:
        """Cancelling one session's in-flight responses must not close sibling
        or parent responses sharing the same model client. A bare cancel() with
        no thread id still closes everything (top-level interrupt)."""

        class _FakeResponse:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        model = OpenAIResponsesModel.__new__(OpenAIResponsesModel)
        model._active_responses = []
        model._active_responses_lock = threading.Lock()

        parent, child_a, child_b = _FakeResponse(), _FakeResponse(), _FakeResponse()
        model._register_active_response(parent, "tid-parent")
        model._register_active_response(child_a, "tid-child-a")
        model._register_active_response(child_b, "tid-child-b")

        model.cancel("tid-child-a")
        self.assertTrue(child_a.closed)
        self.assertFalse(parent.closed)
        self.assertFalse(child_b.closed)
        self.assertEqual({owner for owner, _ in model._active_responses}, {"tid-parent", "tid-child-b"})

        model.cancel()
        self.assertTrue(parent.closed)
        self.assertTrue(child_b.closed)
        self.assertEqual(model._active_responses, [])

    def test_local_multi_agent_wait_timeout_returns_empty_statuses(self) -> None:
        import volley.core as core

        old_min = core._MIN_AGENT_WAIT_TIMEOUT_MS
        old_max = core._MAX_AGENT_WAIT_TIMEOUT_MS
        core._MIN_AGENT_WAIT_TIMEOUT_MS = 1
        core._MAX_AGENT_WAIT_TIMEOUT_MS = 10
        try:
            sleep_command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"
            session = VolleySession(
                VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=ScriptedResponsesModel(
                    [
                        {
                            "output": [
                                {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "call_id": "sleep-1",
                                    "arguments": json.dumps({"cmd": sleep_command, "yield_time_ms": 30000}),
                                }
                            ]
                        }
                    ]
                ),
            )
            spawned = session.tools.spawn_agent({"message": "first"})
            agent_id = json.loads(spawned.output)["agent_id"]
            time.sleep(0.2)

            wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 1}).output)

            self.assertEqual(wait, {"status": {}, "timed_out": True})
            session.tools.close_agent({"target": agent_id})
        finally:
            core._MIN_AGENT_WAIT_TIMEOUT_MS = old_min
            core._MAX_AGENT_WAIT_TIMEOUT_MS = old_max

    def test_spawn_agent_fork_context_sanitizes_runtime_items(self) -> None:
        model = ScriptedResponsesModel([message("child")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.state.history = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "parent user"}]},
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": "pwd"}),
            },
            {"type": "function_call_output", "call_id": "call-1", "output": "parent tool output"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "parent final"}]},
        ]

        spawned = session.tools.spawn_agent({"message": "child", "fork_context": True})
        agent_id = json.loads(spawned.output)["agent_id"]
        wait = json.loads(session.tools.wait_agent({"targets": [agent_id], "timeout_ms": 10000}).output)
        self.assertEqual(wait["status"][agent_id], {"completed": "child"})

        child_input_types = [item.get("type") for item in model.requests[0].input]
        self.assertIn("message", child_input_types)
        self.assertNotIn("function_call", child_input_types)
        self.assertNotIn("function_call_output", child_input_types)

    def test_load_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openai.env"
            path.write_text("# comment\nOPENAI_API_KEY='sk-test'\nOTHER=value\n", encoding="utf-8")
            self.assertEqual(load_env_file(path)["OPENAI_API_KEY"], "sk-test")

    def test_load_gemini_api_key_reads_google_env_file(self) -> None:
        old_gemini = os.environ.pop("GEMINI_API_KEY", None)
        old_google = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "google.env"
                path.write_text("GOOGLE_API_KEY='gemini-test-key'\n", encoding="utf-8")

                self.assertEqual(load_gemini_api_key((path,)), "gemini-test-key")
                self.assertEqual(os.environ["GOOGLE_API_KEY"], "gemini-test-key")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            if old_gemini is not None:
                os.environ["GEMINI_API_KEY"] = old_gemini
            if old_google is not None:
                os.environ["GOOGLE_API_KEY"] = old_google

    def test_prompt_request_includes_stable_responses_request_fields(self) -> None:
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[{"type": "web_search", "external_web_access": False}],
            prompt_cache_key="thread-1",
            reasoning={"effort": "medium", "summary": "auto"},
            include=["reasoning.encrypted_content"],
            service_tier="flex",
            client_metadata={"x-codex-installation-id": "install-1"},
        )
        kwargs = request.to_responses_kwargs()
        self.assertEqual(kwargs["tools"], [{"type": "web_search", "external_web_access": False}])
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertFalse(kwargs["store"])
        self.assertTrue(kwargs["stream"])
        self.assertTrue(kwargs["parallel_tool_calls"])
        self.assertEqual(kwargs["prompt_cache_key"], "thread-1")
        self.assertEqual(kwargs["reasoning"], {"effort": "medium", "summary": "auto"})
        self.assertEqual(kwargs["include"], ["reasoning.encrypted_content"])
        self.assertEqual(kwargs["service_tier"], "flex")
        self.assertEqual(kwargs["client_metadata"], {"x-codex-installation-id": "install-1"})

    def test_fast_service_tier_alias_resolves_to_priority_request_value(self) -> None:
        config = VolleyConfig(skip_git_repo_check=True, ephemeral=True, service_tier="fast")
        self.assertEqual(config.resolved_service_tier(), "priority")

        model = ScriptedResponsesModel([message("ok")])
        session = VolleySession(config, model_client=model)
        session.run("hello")

        self.assertEqual(model.requests[0].service_tier, "priority")

    def test_enterprise_like_chatgpt_plan_defaults_to_fast_service_tier(self) -> None:
        config = VolleyConfig(
            skip_git_repo_check=True,
            ephemeral=True,
            service_tier=None,
            account_plan_type="team",
        )
        self.assertEqual(config.resolved_service_tier(), "priority")
        opted_out = VolleyConfig(
            skip_git_repo_check=True,
            ephemeral=True,
            service_tier=None,
            account_plan_type="team",
            fast_default_opt_out=True,
        )
        self.assertIsNone(opted_out.resolved_service_tier())

    def test_prompt_request_compact_payload_matches_upstream_endpoint_shape(self) -> None:
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
            tools=[{"type": "web_search", "external_web_access": False}],
            parallel_tool_calls=True,
            prompt_cache_key="thread-1",
            reasoning={"effort": "medium", "summary": "auto"},
            include=["reasoning.encrypted_content"],
            service_tier="flex",
            verbosity="low",
        )

        payload = request.to_compact_payload()

        self.assertEqual(
            set(payload),
            {
                "model",
                "input",
                "instructions",
                "tools",
                "parallel_tool_calls",
                "reasoning",
                "service_tier",
                "prompt_cache_key",
                "text",
            },
        )
        self.assertNotIn("stream", payload)
        self.assertNotIn("store", payload)
        self.assertNotIn("include", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["text"], {"verbosity": "low"})

    def test_default_model_client_uses_gemini_provider_when_requested(self) -> None:
        old_fake = os.environ.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        try:
            config = VolleyConfig(
                model="gemini-3.5-flash",
                model_provider_id="gemini",
                gemini_base_url="https://gemini.example/v1beta",
                skip_git_repo_check=True,
                ephemeral=True,
            )

            client = default_model_client(config)
        finally:
            if old_fake is not None:
                os.environ["PY_VOLLEY_FAKE_RESPONSES"] = old_fake

        self.assertIsInstance(client, GeminiGenerateContentModel)
        self.assertEqual(client.base_url, "https://gemini.example/v1beta")

    def test_default_model_client_infers_gemini_provider_from_model_catalog(self) -> None:
        old_fake = os.environ.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        try:
            config = VolleyConfig(
                model="gemini-3.5-flash",
                gemini_base_url="https://gemini.example/v1beta",
                skip_git_repo_check=True,
                ephemeral=True,
            )

            client = default_model_client(config)
        finally:
            if old_fake is not None:
                os.environ["PY_VOLLEY_FAKE_RESPONSES"] = old_fake

        self.assertIsInstance(client, GeminiGenerateContentModel)

    def test_gemini_context_and_auto_compact_match_gpt_5_5_window(self) -> None:
        gemini = VolleyConfig(model="gemini-3.5-flash", skip_git_repo_check=True, ephemeral=True)
        gpt_55 = VolleyConfig(model="gpt-5.5", skip_git_repo_check=True, ephemeral=True)

        self.assertEqual(gemini.resolved_model_context_window(), gpt_55.resolved_model_context_window())
        self.assertEqual(gemini.resolved_model_context_window(), 272_000)
        self.assertEqual(gemini.resolved_auto_compact_token_limit(), 244_800)
        self.assertEqual(gpt_55.resolved_auto_compact_token_limit(), 244_800)

    def test_model_slash_switch_rebuilds_gemini_transport(self) -> None:
        old_fake = os.environ.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        try:
            session = VolleySession(
                VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=ScriptedResponsesModel([message("unused")]),
            )

            _set_session_model(session, "gemini-3.5-flash", "none")
        finally:
            if old_fake is not None:
                os.environ["PY_VOLLEY_FAKE_RESPONSES"] = old_fake

        self.assertEqual(_model_provider_for_model("gemini-3.5-flash"), "gemini")
        self.assertEqual(session.config.model_provider_id, "gemini")
        self.assertIsInstance(session.model_client, GeminiGenerateContentModel)

    def test_model_slash_switch_from_gemini_to_openai_rebuilds_openai_transport(self) -> None:
        old_fake = os.environ.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        try:
            session = VolleySession(
                VolleyConfig(
                    model="gemini-3.5-flash",
                    model_provider_id="gemini",
                    auth_mode="api_key",
                    skip_git_repo_check=True,
                    ephemeral=True,
                ),
                model_client=GeminiGenerateContentModel(api_key="gemini-key"),
            )

            _set_session_model(session, "gpt-5.5", "xhigh")
        finally:
            if old_fake is not None:
                os.environ["PY_VOLLEY_FAKE_RESPONSES"] = old_fake

        self.assertEqual(_model_provider_for_model("gpt-5.5"), "openai")
        self.assertEqual(session.config.model_provider_id, "openai")
        self.assertIsInstance(session.model_client, OpenAIResponsesModel)

    def test_model_picker_only_lists_supported_frontdoor_models(self) -> None:
        from volley.cli import _list_known_models

        slugs = [str(entry.get("slug") or "") for entry in _list_known_models()]

        self.assertEqual(slugs, ["gpt-5.5", "gemini-3.5-flash"])
        self.assertNotIn("codex-auto-review", slugs)

    def test_gemini_model_url_and_headers_use_api_key(self) -> None:
        model = GeminiGenerateContentModel(api_key="gemini-key", base_url="https://gemini.example/v1beta/")

        self.assertEqual(
            model._url("gemini-3.5-flash", stream=True),
            "https://gemini.example/v1beta/models/gemini-3.5-flash:streamGenerateContent?alt=sse",
        )
        self.assertEqual(
            model._url("gemini-3.5-flash", stream=False),
            "https://gemini.example/v1beta/models/gemini-3.5-flash:generateContent",
        )
        self.assertEqual(model._headers(accept="application/json")["x-goog-api-key"], "gemini-key")

    def test_gemini_request_body_maps_messages_tools_and_tool_outputs(self) -> None:
        request = PromptRequest(
            model="gemini-3.5-flash",
            instructions="base instructions",
            input=[
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "developer rules"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "pwd"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "workspace",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ],
            tools=[
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Run a command.",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                },
                {"type": "custom", "name": "apply_patch"},
                {"type": "web_search", "external_web_access": False},
            ],
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )

        body = _gemini_request_body(request)

        system_text = [part["text"] for part in body["systemInstruction"]["parts"]]
        self.assertEqual(system_text, ["base instructions", "developer rules"])
        self.assertEqual(body["contents"][0], {"role": "user", "parts": [{"text": "hello"}]})
        function_call = body["contents"][1]["parts"][0]["functionCall"]
        self.assertEqual(function_call, {"name": "exec_command", "args": {"cmd": "pwd"}, "id": "call-1"})
        function_response = body["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual(function_response["name"], "exec_command")
        self.assertEqual(function_response["id"], "call-1")
        self.assertEqual(function_response["response"], {"result": "workspace"})
        self.assertEqual(body["contents"][3]["role"], "model")
        declarations = body["tools"][0]["functionDeclarations"]
        self.assertEqual([declaration["name"] for declaration in declarations], ["exec_command", "apply_patch"])
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseSchema"]["properties"]["ok"]["type"], "boolean")

    def test_gemini_stream_events_map_text_function_calls_and_usage(self) -> None:
        events = list(
            _iter_gemini_stream_events(
                [
                    {
                        "responseId": "resp-1",
                        "candidates": [{"content": {"parts": [{"text": "hi "}]}}],
                    },
                    {
                        "responseId": "resp-1",
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": "there"},
                                        {
                                            "functionCall": {
                                                "id": "fn-1",
                                                "name": "exec_command",
                                                "args": {"cmd": "pwd"},
                                            },
                                            "thoughtSignature": "sig-1",
                                        },
                                        {"text": ""},
                                    ]
                                }
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 7,
                            "candidatesTokenCount": 3,
                            "totalTokenCount": 11,
                            "thoughtsTokenCount": 1,
                        },
                    },
                ]
            )
        )

        completed = [event.payload["item"] for event in events if event.type == "item.completed"]
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[0]["content"][0]["text"], "hi there")
        self.assertEqual(completed[1]["type"], "function_call")
        self.assertEqual(completed[1]["name"], "exec_command")
        self.assertEqual(completed[1]["call_id"], "fn-1")
        self.assertEqual(json.loads(completed[1]["arguments"]), {"cmd": "pwd"})
        self.assertEqual(completed[1]["gemini_thought_signature"], "sig-1")
        token_count = next(event for event in events if event.type == "token_count")
        self.assertEqual(token_count.payload["usage"]["input_tokens"], 7)
        self.assertEqual(token_count.payload["usage"]["output_tokens"], 3)
        response = events[-1]
        self.assertEqual(response.type, "model.response")
        self.assertEqual(response.payload["response"]["id"], "resp-1")

    def test_gemini_provider_uses_prompt_based_compaction(self) -> None:
        config = VolleyConfig(model_provider_id="gemini", skip_git_repo_check=True, ephemeral=True)

        self.assertFalse(_provider_supports_remote_compaction(config))

    def test_openai_responses_model_exposes_remote_compact_endpoint(self) -> None:
        model = OpenAIResponsesModel(api_key="sk-test", base_url="https://example.test/v1")
        self.assertTrue(callable(model.compact))
        self.assertEqual(model._compact_url(), "https://example.test/v1/responses/compact")
        headers = model._compact_headers(
            session_id="session-1",
            thread_id="thread-1",
            installation_id="install-1",
        )
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(headers["x-codex-installation-id"], "install-1")
        self.assertEqual(headers["session-id"], "session-1")
        self.assertEqual(headers["thread-id"], "thread-1")

    def test_api_key_remote_compact_omits_service_tier_like_upstream(self) -> None:
        model = OpenAIResponsesModel(api_key="sk-test", base_url="https://example.test/v1")
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[],
            service_tier="priority",
        )

        payload = model._compact_payload(request)

        self.assertNotIn("service_tier", payload)

    def test_chatgpt_remote_compact_preserves_service_tier_like_upstream(self) -> None:
        access_token = _fake_jwt({"exp": int(time.time()) + 3600})
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": _fake_jwt({}),
                            "access_token": access_token,
                            "refresh_token": "refresh-token",
                            "account_id": "account-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            model = ChatGPTCodexSubscriptionModel(auth_snapshot=load_auth_snapshot(home, mode="chatgpt"))
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[],
            service_tier="priority",
        )

        payload = model._compact_payload(request)

        self.assertEqual(payload["service_tier"], "priority")

    def test_chatgpt_auth_snapshot_reads_official_auth_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            id_token = _fake_jwt(
                {
                    "email": "user@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus",
                        "chatgpt_account_id": "account-from-jwt",
                    },
                }
            )
            access_token = _fake_jwt({"exp": int(time.time()) + 3600})
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": id_token,
                            "access_token": access_token,
                            "refresh_token": "refresh-token",
                            "account_id": "account-from-file",
                        },
                        "last_refresh": "2026-05-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_auth_snapshot(home, mode="chatgpt")

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.is_chatgpt)
        self.assertEqual(snapshot.access_token, access_token)
        self.assertEqual(snapshot.account_id, "account-from-file")
        self.assertEqual(snapshot.email, "user@example.test")
        self.assertEqual(snapshot.plan_type, "plus")
        self.assertFalse(snapshot.needs_proactive_refresh())

    def test_default_model_client_uses_chatgpt_auth_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            access_token = _fake_jwt({"exp": int(time.time()) + 3600})
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": _fake_jwt({}),
                            "access_token": access_token,
                            "refresh_token": "refresh-token",
                            "account_id": "account-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = VolleyConfig(
                auth_mode="chatgpt",
                auth_home=home,
                chatgpt_base_url="https://chatgpt.example/backend-api",
                skip_git_repo_check=True,
                ephemeral=True,
            )

            client = default_model_client(config)

        self.assertIsInstance(client, ChatGPTCodexSubscriptionModel)
        self.assertEqual(client.base_url, "https://chatgpt.example/backend-api/codex")

    def test_default_model_client_auto_uses_api_key_fallback_for_chatgpt_auth(self) -> None:
        old_api_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-fallback"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                access_token = _fake_jwt({"exp": int(time.time()) + 3600})
                (home / "auth.json").write_text(
                    json.dumps(
                        {
                            "auth_mode": "chatgpt",
                            "tokens": {
                                "id_token": _fake_jwt({}),
                                "access_token": access_token,
                                "refresh_token": "refresh-token",
                                "account_id": "account-1",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                config = VolleyConfig(
                    auth_mode="auto",
                    auth_home=home,
                    skip_git_repo_check=True,
                    ephemeral=True,
                )

                client = default_model_client(config)
        finally:
            if old_api_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_api_key

        self.assertIsInstance(client, FallbackModelClient)
        self.assertEqual(client.auth_display_name, "ChatGPT")
        self.assertEqual(client.auth_fallback_display_name, "API key")

    def test_model_client_falls_back_to_api_key_on_chatgpt_budget_limit(self) -> None:
        class _BudgetLimitedChatGPT:
            auth_display_name = "ChatGPT"

            def stream(self, request: PromptRequest):
                raise RuntimeError("budget limit reached for this ChatGPT account")
                yield  # pragma: no cover

            def create(self, request: PromptRequest):
                raise RuntimeError("budget limit reached for this ChatGPT account")

        client = FallbackModelClient(
            primary=_BudgetLimitedChatGPT(),
            fallback=ScriptedResponsesModel([message("api fallback ok")]),
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=client,
        )

        result = session.run("hello")

        self.assertEqual(result.final_message, "api fallback ok")
        self.assertTrue(client.using_fallback)
        self.assertEqual(client.auth_display_name, "API key")
        self.assertTrue(any(event.type == "warning" and "switched to API key" in event.payload.get("message", "") for event in result.events))

    def test_fallback_model_client_retries_chatgpt_on_next_user_turn_and_restores(self) -> None:
        class _RecoveringChatGPT:
            auth_display_name = "ChatGPT"

            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise RuntimeError("budget limit reached for this ChatGPT account")
                yield _stream_message("chatgpt recovered")
                yield ModelStreamEvent("model.response", {"response_id": "chatgpt-ok"})

        primary = _RecoveringChatGPT()
        client = FallbackModelClient(
            primary=primary,
            fallback=ScriptedResponsesModel([message("api fallback ok")]),
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=client,
        )

        first = session.run("first")
        second = session.run("second")

        self.assertEqual(first.final_message, "api fallback ok")
        self.assertTrue(any(event.type == "warning" and "switched to API key" in event.payload.get("message", "") for event in first.events))
        self.assertEqual(second.final_message, "chatgpt recovered")
        self.assertFalse(client.using_fallback)
        self.assertEqual(client.auth_display_name, "ChatGPT")
        self.assertTrue(
            any(
                event.type == "warning"
                and "switched back from API key" in event.payload.get("message", "")
                for event in second.events
            )
        )
        self.assertEqual(len(primary.requests), 2)

    def test_fallback_model_client_retries_chatgpt_only_once_per_user_turn(self) -> None:
        class _StillLimitedChatGPT:
            auth_display_name = "ChatGPT"

            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                self.requests.append(request)
                raise RuntimeError("usage limit still reached for this ChatGPT account")
                yield  # pragma: no cover

        primary = _StillLimitedChatGPT()
        client = FallbackModelClient(
            primary=primary,
            fallback=ScriptedResponsesModel(
                [
                    message("api fallback first"),
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "call-1",
                                "arguments": json.dumps({"cmd": "printf hi"}),
                            }
                        ]
                    },
                    message("api fallback after tool"),
                ]
            ),
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=client,
        )

        first = session.run("first")
        second = session.run("second")

        self.assertEqual(first.final_message, "api fallback first")
        self.assertEqual(second.final_message, "api fallback after tool")
        self.assertTrue(client.using_fallback)
        # First turn: one ChatGPT request fails and switches to API key.
        # Second turn: one recovery probe at the first user-facing request.
        # The tool follow-up request stays on API key and must not probe again.
        self.assertEqual(len(primary.requests), 2)

    def test_chatgpt_model_adds_official_auth_and_session_headers(self) -> None:
        access_token = _fake_jwt({"exp": int(time.time()) + 3600})
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": _fake_jwt({}),
                            "access_token": access_token,
                            "refresh_token": "refresh-token",
                            "account_id": "account-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            client = ChatGPTCodexSubscriptionModel(auth_snapshot=load_auth_snapshot(home, mode="chatgpt"))
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[],
            prompt_cache_key="thread-1",
            session_id="session-1",
            client_metadata={"x-codex-installation-id": "install-1"},
        )

        headers = client._headers(request, accept="application/json")

        self.assertEqual(headers["Authorization"], f"Bearer {access_token}")
        self.assertEqual(headers["ChatGPT-Account-ID"], "account-1")
        self.assertEqual(headers["originator"], "codex_cli_rs")
        self.assertEqual(headers["session-id"], "session-1")
        self.assertEqual(headers["thread-id"], "thread-1")
        self.assertEqual(headers["x-codex-installation-id"], "install-1")

    def test_auth_status_never_returns_token_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": _fake_jwt({}),
                            "access_token": "secret-access-token",
                            "refresh_token": "secret-refresh-token",
                            "account_id": "account-123456",
                        },
                    }
                ),
                encoding="utf-8",
            )
            status = auth_status(home)
        encoded = json.dumps(status, sort_keys=True)
        self.assertNotIn("secret-access-token", encoded)
        self.assertNotIn("secret-refresh-token", encoded)
        self.assertEqual(status["account_id"], "acco...3456")

    def test_chatgpt_backend_base_url_matches_official_usage_path_root(self) -> None:
        self.assertEqual(
            chatgpt_backend_base_url("https://chatgpt.com/backend-api/codex"),
            "https://chatgpt.com/backend-api",
        )
        self.assertEqual(
            chatgpt_backend_base_url("https://example.test"),
            "https://example.test",
        )

    def test_parse_chatgpt_rate_limit_payload_maps_official_usage_shape(self) -> None:
        snapshots = parse_chatgpt_rate_limit_payload(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 42,
                        "limit_window_seconds": 18000,
                        "reset_after_seconds": 120,
                        "reset_at": 1735689720,
                    },
                    "secondary_window": {
                        "used_percent": 5,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 43200,
                        "reset_at": 1735693200,
                    },
                },
                "credits": {
                    "has_credits": True,
                    "unlimited": False,
                    "balance": "37.6",
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "codex_other",
                        "metered_feature": "codex_other",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 88,
                                "limit_window_seconds": 1800,
                                "reset_at": 1735693200,
                            }
                        },
                    }
                ],
            }
        )

        self.assertEqual(len(snapshots), 2)
        primary = snapshots[0]
        self.assertEqual(primary.limit_id, "volley")
        self.assertEqual(primary.plan_type, "pro")
        self.assertEqual(primary.primary.window_minutes, 300)
        self.assertEqual(primary.primary.used_percent, 42.0)
        self.assertEqual(primary.secondary.window_minutes, 10080)
        self.assertEqual(primary.credits.balance, "37.6")
        self.assertEqual(snapshots[1].limit_id, "codex_other")

    def test_fetch_chatgpt_rate_limits_uses_official_backend_endpoint_and_headers(self) -> None:
        from volley import auth as auth_mod

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": _fake_jwt({}),
                            "access_token": _fake_jwt({"exp": int(time.time()) + 3600}),
                            "refresh_token": "refresh-token",
                            "account_id": "account-123",
                        },
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self):
                    return json.dumps(
                        {
                            "plan_type": "plus",
                            "rate_limit": {
                                "primary_window": {
                                    "used_percent": 25,
                                    "limit_window_seconds": 18000,
                                    "reset_at": 1735689720,
                                }
                            },
                        }
                    ).encode("utf-8")

            old_urlopen = auth_mod.urllib.request.urlopen
            try:
                def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
                    calls.append((request, timeout))
                    return _Response()

                auth_mod.urllib.request.urlopen = fake_urlopen
                snapshots = fetch_chatgpt_rate_limits(
                    home,
                    base_url="https://chatgpt.example/backend-api",
                    timeout=7,
                )
            finally:
                auth_mod.urllib.request.urlopen = old_urlopen

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].primary.used_percent, 25.0)
        request, timeout = calls[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.full_url, "https://chatgpt.example/backend-api/wham/usage")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["chatgpt-account-id"], "account-123")
        self.assertIn("authorization", headers)

    def test_save_chatgpt_tokens_writes_official_login_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            id_token = _fake_jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "account-from-jwt",
                    },
                }
            )
            path = save_chatgpt_tokens(
                volley_home=home,
                id_token=id_token,
                access_token=_fake_jwt({"exp": int(time.time()) + 3600}),
                refresh_token="refresh-token",
            )

            raw = json.loads(path.read_text(encoding="utf-8"))
            snapshot = load_auth_snapshot(home, mode="chatgpt")

        self.assertEqual(raw["auth_mode"], "chatgpt")
        self.assertEqual(raw["tokens"]["id_token"], id_token)
        self.assertEqual(raw["tokens"]["refresh_token"], "refresh-token")
        self.assertEqual(raw["tokens"]["account_id"], "account-from-jwt")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.account_id, "account-from-jwt")

    def test_login_with_api_key_writes_official_auth_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = login_with_api_key("  sk-test-login\n", volley_home=home)
            raw = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(raw, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test-login"})

    def test_build_authorize_url_matches_official_login_params(self) -> None:
        url = build_authorize_url(
            issuer="https://auth.example.test",
            client_id="client-1",
            redirect_uri="http://localhost:1455/auth/callback",
            code_challenge="challenge-1",
            state="state-1",
            forced_workspace_ids=["workspace-1"],
        )

        parsed = urllib.parse.urlparse(url)
        params = {key: value[0] for key, value in urllib.parse.parse_qs(parsed.query).items()}

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "auth.example.test")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["client_id"], "client-1")
        self.assertEqual(params["redirect_uri"], "http://localhost:1455/auth/callback")
        self.assertEqual(
            params["scope"],
            "openid profile email offline_access api.connectors.read api.connectors.invoke",
        )
        self.assertEqual(params["code_challenge"], "challenge-1")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["id_token_add_organizations"], "true")
        self.assertEqual(params["codex_cli_simplified_flow"], "true")
        self.assertEqual(params["state"], "state-1")
        self.assertEqual(params["originator"], "codex_cli_rs")
        self.assertEqual(params["allowed_workspace_id"], "workspace-1")

    def test_cli_login_with_api_key_reads_stdin_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["VOLLEY_AUTH_HOME"] = tmp
            proc = subprocess.run(
                [sys.executable, "-m", "volley", "login", "--with-api-key"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                input="sk-secret-login\n",
                text=True,
                capture_output=True,
            )
            raw = json.loads((Path(tmp) / "auth.json").read_text(encoding="utf-8"))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(raw, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-secret-login"})
        self.assertNotIn("sk-secret-login", proc.stdout)
        self.assertNotIn("sk-secret-login", proc.stderr)

    def test_prompt_request_matches_upstream_empty_tools_shape(self) -> None:
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[],
            parallel_tool_calls=False,
        )
        kwargs = request.to_responses_kwargs()
        self.assertEqual(kwargs["tools"], [])
        self.assertEqual(kwargs["tool_choice"], "auto")
        self.assertFalse(kwargs["parallel_tool_calls"])
        self.assertIsNone(kwargs["reasoning"])
        self.assertEqual(kwargs["include"], [])

    def test_prompt_request_sanitizes_response_output_items_for_followup(self) -> None:
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "status": "completed",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "pwd"}),
                },
                {
                    "type": "message",
                    "id": "msg-1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "ok",
                            "annotations": [],
                            "logprobs": [],
                        }
                    ],
                },
                {
                    "type": "web_search_call",
                    "id": "ws-1",
                    "status": "completed",
                    "action": {"type": "search", "query": "Volley"},
                },
            ],
            tools=[],
        )

        kwargs = request.to_responses_kwargs()
        self.assertEqual(
            kwargs["input"][0],
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": "pwd"}),
            },
        )
        self.assertEqual(
            kwargs["input"][1],
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        )
        self.assertEqual(
            kwargs["input"][2],
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "Volley"},
            },
        )

    def test_prompt_request_includes_output_schema_text_controls(self) -> None:
        request = PromptRequest(
            model="gpt-test",
            instructions="base",
            input=[],
            tools=[],
            output_schema={"type": "object"},
            output_schema_strict=True,
        )
        text = request.to_responses_kwargs()["text"]
        self.assertEqual(text["format"]["type"], "json_schema")
        self.assertTrue(text["format"]["strict"])
        self.assertEqual(text["format"]["name"], "codex_output_schema")
        self.assertEqual(text["format"]["schema"], {"type": "object"})

    def test_stream_response_events_are_collected_to_model_response(self) -> None:
        response = collect_stream_response(
            [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "partial"}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-1",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "final"}],
                            }
                        ],
                    },
                },
            ]
        )
        self.assertEqual(response.id, "resp-1")
        self.assertEqual(response.output[0]["content"][0]["text"], "final")

    def test_incomplete_stream_event_records_usage_before_failure(self) -> None:
        from volley.model import iter_model_stream_events

        events = list(
            iter_model_stream_events(
                [
                    {
                        "type": "response.incomplete",
                        "response": {
                            "id": "resp-1",
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 260,
                                "output_tokens_details": {"reasoning_tokens": 256},
                                "total_tokens": 270,
                            },
                        },
                    }
                ]
            )
        )

        self.assertEqual([event.type for event in events], ["token_count", "model.failed"])
        self.assertEqual(events[0].payload["usage"]["output_tokens_details"]["reasoning_tokens"], 256)
        self.assertEqual(events[1].payload["response_id"], "resp-1")

    def test_core_emits_model_stream_lifecycle_events(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "streamed"}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                }
            ]
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )

        result = session.run("hello")

        self.assertEqual(result.final_message, "streamed")
        event_types = [event.type for event in result.events]
        started_index = event_types.index("item.started")
        delta_index = event_types.index("item.delta")
        completed_index = next(
            index for index, event_type in enumerate(event_types) if event_type == "item.completed" and index > delta_index
        )
        token_count_index = event_types.index("token_count")
        model_response_index = event_types.index("model.response")
        self.assertLess(started_index, delta_index)
        self.assertLess(delta_index, completed_index)
        self.assertLess(completed_index, token_count_index)
        self.assertLess(token_count_index, model_response_index)
        token_count = result.events[token_count_index]
        self.assertEqual(token_count.payload["usage"]["total_tokens"], 3)
        self.assertEqual(token_count.payload["info"]["total_token_usage"], 3)

    def test_token_usage_total_accumulates_api_usage_while_last_tracks_context(self) -> None:
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=ScriptedResponsesModel([]))

        session.state.record_token_usage(
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "reasoning_output_tokens": 3}
        )
        session.state.record_token_usage(
            {
                "input_tokens": 4,
                "output_tokens": 1,
                "total_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 2},
            }
        )

        self.assertEqual(session.state.active_context_tokens(), 5)
        self.assertEqual(session.state.session_usage_tokens(), 20)
        self.assertEqual(session.state.session_reasoning_usage_tokens(), 5)
        self.assertEqual(session.state.token_usage_info()["total_token_usage"], 20)
        self.assertEqual(session.state.token_usage_info()["session_reasoning_tokens"], 5)

    def test_active_context_estimate_uses_prompt_visible_tool_output_truncation(self) -> None:
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=ScriptedResponsesModel([]))
        token_limit = session.config.resolved_tool_output_truncation_tokens()
        long_output = "x" * (token_limit * 40)
        session.state.record_token_usage({"input_tokens": 80, "output_tokens": 20, "total_tokens": 100})
        session.state.append_history(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ready"}]}
        )
        session.state.append_history({"type": "function_call_output", "call_id": "call-1", "output": long_output})

        estimated_tokens, is_estimated = session.state.active_context_token_status()

        self.assertTrue(is_estimated)
        self.assertLess(estimated_tokens, token_limit * 2 + 1_000)
        self.assertLess(estimated_tokens, (len(long_output) + 3) // 8)
        self.assertEqual(session.state.history[-1]["output"], long_output)

    def test_recomputed_compaction_context_does_not_increment_api_session_usage(self) -> None:
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=ScriptedResponsesModel([]))
        session.state.record_token_usage({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "after compact"}]}
        )

        session.state.recompute_token_usage_from_history()

        self.assertEqual(session.state.session_usage_tokens(), 15)
        self.assertTrue(session.state.last_token_usage["estimated"])

    def test_session_context_carries_pre_compaction_context_into_new_epoch(self) -> None:
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=ScriptedResponsesModel([]))
        session.state.record_token_usage({"input_tokens": 80, "output_tokens": 20, "total_tokens": 100})

        self.assertEqual(session.state.session_context_token_status(), (100, False))

        pre_compact_session, estimated = session.state.session_context_token_status()
        session.state.start_new_context_epoch(pre_compact_session, estimated=estimated)
        session.state.record_token_usage({"input_tokens": 12, "output_tokens": 8, "total_tokens": 20})

        self.assertEqual(session.state.active_context_token_status(), (20, False))
        self.assertEqual(session.state.session_context_token_status(), (120, False))

    def test_retryable_model_stream_error_retries_sampling_request(self) -> None:
        class FlakyStreamModel:
            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise RuntimeError("stream closed before response.completed")
                yield _stream_message("recovered")
                yield ModelStreamEvent("model.response", {"response_id": "retry-ok"})

        model = FlakyStreamModel()
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_stream_max_retries=1,
                model_stream_retry_base_delay_ms=0,
            ),
            model_client=model,
        )

        result = session.run("hello")

        self.assertEqual(result.final_message, "recovered")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual([event.type for event in result.events].count("stream_error"), 1)
        self.assertNotIn("turn.failed", [event.type for event in result.events])
        self.assertEqual([item.get("role") for item in result.history if item.get("role") == "assistant"], ["assistant"])

    def test_response_failed_model_streaming_error_retries_sampling_request(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "events": [
                        {
                            "type": "response.failed",
                            "response": {
                                "id": "resp-failed",
                                "error": {"code": "model_error", "message": "model streaming failed"},
                            },
                        }
                    ]
                },
                message("recovered after reconnect"),
            ]
        )
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_stream_max_retries=1,
                model_stream_retry_base_delay_ms=0,
            ),
            model_client=model,
        )

        result = session.run("hello")

        self.assertEqual(result.final_message, "recovered after reconnect")
        self.assertEqual(len(model.requests), 2)
        stream_error = next(event for event in result.events if event.type == "stream_error")
        self.assertIn("model streaming failed", stream_error.payload["additional_details"])
        self.assertNotIn("turn.failed", [event.type for event in result.events])

    def test_response_failed_context_window_is_not_retried(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "events": [
                        {
                            "type": "response.failed",
                            "response": {
                                "id": "resp-context",
                                "error": {
                                    "code": "context_length_exceeded",
                                    "message": "Your input exceeds the context window of this model.",
                                },
                            },
                        }
                    ]
                },
                message("should not be used"),
            ]
        )
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_stream_max_retries=1,
                model_stream_retry_base_delay_ms=0,
            ),
            model_client=model,
        )

        with self.assertRaisesRegex(RuntimeError, "context_length_exceeded"):
            session.run("hello")

        self.assertEqual(len(model.requests), 1)

    def test_steer_input_is_drained_before_follow_up_request(self) -> None:
        class SteeringModel:
            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []
                self.session: VolleySession | None = None

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                self.requests.append(request)
                if len(self.requests) == 1:
                    assert self.session is not None
                    turn_id = self.session.steer_input("second steer", expected_turn_id=self.session.state.turn_id)
                    self.assertEqualTurn(turn_id)
                    yield _stream_message("first answer")
                else:
                    yield _stream_message("second answer")

            def assertEqualTurn(self, turn_id: str) -> None:
                assert self.session is not None
                if turn_id != self.session.state.turn_id:
                    raise AssertionError("steer returned the wrong active turn id")

        model = SteeringModel()
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=model)
        model.session = session

        result = session.run("first prompt")

        self.assertEqual(result.final_message, "second answer")
        self.assertEqual(len(model.requests), 2)
        self.assertIn("first prompt", _request_texts(model.requests[0]))
        self.assertNotIn("second steer", _request_texts(model.requests[0]))
        self.assertIn("second steer", _request_texts(model.requests[1]))
        pending_events = [
            event
            for event in result.events
            if event.type == "item.completed" and event.payload.get("pending_input")
        ]
        self.assertEqual(len(pending_events), 1)

    def test_steer_input_requires_active_regular_turn(self) -> None:
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=ScriptedResponsesModel([]))

        with self.assertRaisesRegex(Exception, "no active turn"):
            session.steer_input("too early")

    def test_interrupt_emits_turn_aborted_and_records_model_visible_marker(self) -> None:
        class InterruptingModel:
            def __init__(self) -> None:
                self.session: VolleySession | None = None

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                assert self.session is not None
                self.session.interrupt()
                yield ModelStreamEvent("model.response", {"response_id": "interrupted"})

        model = InterruptingModel()
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=model)
        model.session = session

        events = list(session.stream("stop this"))

        self.assertIn("turn.aborted", [event.type for event in events])
        self.assertNotIn("turn.failed", [event.type for event in events])
        self.assertTrue(any("<turn_aborted>" in text for text in _request_texts(PromptRequest("m", "", session.state.history, []))))
        self.assertTrue(any(item.get("role") == "user" for item in session.state.history))

    def test_interrupt_cancels_model_client_when_available(self) -> None:
        class CancellableModel:
            def __init__(self) -> None:
                self.cancelled = threading.Event()

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                while not self.cancelled.is_set():
                    time.sleep(0.01)
                raise RuntimeError("stream closed")

            def cancel(self) -> None:
                self.cancelled.set()

        model = CancellableModel()
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=model)
        events: list[Any] = []
        thread = threading.Thread(target=lambda: events.extend(session.stream("stop this")), daemon=True)
        thread.start()

        time.sleep(0.05)
        session.interrupt()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(model.cancelled.is_set())
        self.assertIn("turn.aborted", [event.type for event in events])

    def test_hook_provider_injects_user_prompt_context_before_model_request(self) -> None:
        class InspectingModel:
            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []

            def create(self, request: PromptRequest):
                raise AssertionError("stream expected")

            def stream(self, request: PromptRequest):
                self.requests.append(request)
                yield _stream_message("hooked")

        calls: list[dict] = []

        def hook_provider(request: dict) -> dict:
            calls.append(request)
            if request["event"] == "user_prompt_submit":
                return {"additional_contexts": ["extra policy context"]}
            return {}

        model = InspectingModel()
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=model,
        )

        result = session.run("hello")
        texts = _request_texts(model.requests[0])

        self.assertEqual(result.final_message, "hooked")
        self.assertIn("session_start", [call["event"] for call in calls])
        self.assertIn("user_prompt_submit", [call["event"] for call in calls])
        self.assertTrue(any("<hook_context>" in text and "extra policy context" in text for text in texts))
        self.assertIn("hook.started", [event.type for event in result.events])
        self.assertIn("hook.completed", [event.type for event in result.events])

    def test_pre_tool_use_hook_blocks_tool_and_returns_output_to_model(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf should-not-run"}),
                    }
                ]
            },
            message("saw blocked tool"),
        ]

        def hook_provider(request: dict) -> dict:
            if request["event"] == "pre_tool_use":
                return {"should_block": True, "block_reason": "blocked by test hook"}
            return {}

        model = ScriptedResponsesModel(responses)
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=model,
        )

        result = session.run("run command")
        tool_outputs = [item for item in result.history if item.get("type") == "function_call_output"]

        self.assertEqual(result.final_message, "saw blocked tool")
        self.assertEqual(len(tool_outputs), 1)
        self.assertIn("blocked by test hook", tool_outputs[0]["output"])
        self.assertTrue(any(event.type == "hook.completed" for event in result.events))

    def test_pre_tool_use_hook_uses_bash_contract_and_can_rewrite_exec_command(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf original"}),
                    }
                ]
            },
            message("done"),
        ]
        hook_calls: list[dict] = []

        def hook_provider(request: dict) -> dict:
            hook_calls.append(request)
            if request["event"] == "pre_tool_use":
                return {"updated_input": {"command": "printf rewritten"}}
            return {}

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=ScriptedResponsesModel(responses),
        )

        result = session.run("run command")
        pre = next(call for call in hook_calls if call["event"] == "pre_tool_use")
        post = next(call for call in hook_calls if call["event"] == "post_tool_use")
        tool_output = next(item for item in result.history if item.get("type") == "function_call_output")

        self.assertEqual(pre["tool_name"], "Bash")
        self.assertEqual(pre["tool_input"], {"command": "printf original"})
        self.assertEqual(post["tool_name"], "Bash")
        self.assertEqual(post["tool_input"], {"command": "printf rewritten"})
        self.assertEqual(post["tool_response"], "rewritten")
        self.assertIn("rewritten", tool_output["output"])
        self.assertNotIn("original", tool_output["output"])

    def test_apply_patch_hook_uses_command_contract_aliases_and_can_rewrite_patch(self) -> None:
        original_patch = "*** Begin Patch\n*** Add File: a.txt\n+old\n*** End Patch"
        rewritten_patch = "*** Begin Patch\n*** Add File: b.txt\n+new\n*** End Patch"
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "apply_patch",
                        "call_id": "patch-1",
                        "arguments": original_patch,
                    }
                ]
            },
            message("patched"),
        ]
        hook_calls: list[dict] = []

        def hook_provider(request: dict) -> dict:
            hook_calls.append(request)
            if request["event"] == "pre_tool_use":
                return {"updated_input": {"command": rewritten_patch}}
            return {}

        with tempfile.TemporaryDirectory() as temp:
            session = VolleySession(
                VolleyConfig(
                    cwd=Path(temp),
                    skip_git_repo_check=True,
                    ephemeral=True,
                    hook_provider=hook_provider,
                ),
                model_client=ScriptedResponsesModel(responses),
            )
            result = session.run("patch file")

            self.assertFalse((Path(temp) / "a.txt").exists())
            self.assertEqual((Path(temp) / "b.txt").read_text(encoding="utf-8"), "new\n")

        pre = next(call for call in hook_calls if call["event"] == "pre_tool_use")
        post = next(call for call in hook_calls if call["event"] == "post_tool_use")
        self.assertEqual(pre["tool_name"], "apply_patch")
        self.assertEqual(pre["matcher_aliases"], ["Write", "Edit"])
        self.assertEqual(pre["tool_input"], {"command": original_patch})
        self.assertEqual(post["tool_name"], "apply_patch")
        self.assertEqual(post["matcher_aliases"], ["Write", "Edit"])
        self.assertEqual(post["tool_input"], {"command": rewritten_patch})
        self.assertEqual(post["tool_response"], "Success. Updated the following files:\nA b.txt\n")
        self.assertEqual(result.final_message, "patched")

    def test_pre_post_tool_hooks_only_run_for_upstream_handlers_with_payloads(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "update_plan",
                        "call_id": "plan-1",
                        "arguments": json.dumps({"plan": [{"step": "Inspect", "status": "in_progress"}]}),
                    }
                ]
            },
            message("done"),
        ]
        hook_events: list[str] = []

        def hook_provider(request: dict) -> dict:
            if request["event"] in {"pre_tool_use", "post_tool_use"}:
                hook_events.append(request["event"])
            return {}

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=ScriptedResponsesModel(responses),
        )

        result = session.run("plan")

        self.assertEqual(result.final_message, "done")
        self.assertEqual(hook_events, [])

    def test_permission_request_hook_events_are_emitted_during_tool_approval(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-approval",
                        "arguments": json.dumps(
                            {
                                "cmd": f"{sys.executable!r} -c \"print('approved by hook')\"",
                                "sandbox_permissions": "require_escalated",
                                "justification": "Need hook approval",
                            }
                        ),
                    }
                ]
            },
            message("finished"),
        ]
        hook_calls: list[dict] = []

        def hook_provider(request: dict) -> dict:
            hook_calls.append(request)
            if request["event"] == "permission_request":
                return {"decision": "approved_for_session"}
            return {}

        model = ScriptedResponsesModel(responses)
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                approval_policy="on-request",
                hook_provider=hook_provider,
            ),
            model_client=model,
        )

        result = session.run("run approval command")

        permission_hook_events = [
            event for event in result.events if event.type.startswith("hook.") and event.payload.get("name") == "permission_request"
        ]
        self.assertEqual([event.type for event in permission_hook_events], ["hook.started", "hook.completed"])
        self.assertTrue(any(call["event"] == "permission_request" for call in hook_calls))
        self.assertEqual(result.final_message, "finished")
        self.assertLess(
            result.events.index(permission_hook_events[-1]),
            next(index for index, event in enumerate(result.events) if event.type == "tool.completed"),
        )

    def test_core_aggregates_streamed_tool_argument_deltas(self) -> None:
        arguments = json.dumps({"plan": [{"step": "inspect", "status": "in_progress"}]})
        model = ScriptedResponsesModel(
            [
                {
                    "events": [
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "name": "update_plan",
                                "call_id": "plan-1",
                                "arguments": "",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "plan-1",
                            "output_index": 0,
                            "delta": arguments[:20],
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "plan-1",
                            "output_index": 0,
                            "delta": arguments[20:],
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "name": "update_plan",
                                "call_id": "plan-1",
                                "arguments": arguments,
                            },
                        },
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-tool",
                                "output": [
                                    {
                                        "type": "function_call",
                                        "name": "update_plan",
                                        "call_id": "plan-1",
                                        "arguments": arguments,
                                    }
                                ],
                            },
                        },
                    ]
                },
                message("done after streamed tool args"),
            ]
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )

        result = session.run("stream tool args")

        self.assertEqual(result.final_message, "done after streamed tool args")
        deltas = [event for event in result.events if event.type == "item.delta" and event.payload.get("item_id") == "plan-1"]
        self.assertEqual(deltas[-1].payload["aggregate"]["arguments"], arguments)
        completed = next(
            event
            for event in result.events
            if event.type == "item.completed" and event.payload.get("item", {}).get("call_id") == "plan-1"
        )
        self.assertEqual(completed.payload["aggregate"]["arguments"], arguments)

    def test_compaction_assets_and_replacement_history_helper(self) -> None:
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", summarization_prompt())
        summary = build_compaction_summary_text("continue from here")
        self.assertTrue(summary.startswith("Another language model started"))
        history = build_compacted_history([], ["first user", "second user"], summary)
        self.assertEqual([item["role"] for item in history], ["user", "user", "user"])
        self.assertEqual(history[-1]["content"][0]["text"], summary)

    def test_manual_compaction_runs_summary_turn_and_replaces_history(self) -> None:
        model = ScriptedResponsesModel([message("handoff summary")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first task"}]}
        )
        session.state.append_history(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "assistant work"}]}
        )
        session.state.append_history(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": build_compaction_summary_text("old summary")}],
            }
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second task"}]}
        )

        result = session.compact()

        self.assertEqual(result.final_message, "handoff summary")
        request = model.requests[0]
        self.assertEqual(request.tools, [])
        self.assertFalse(request.parallel_tool_calls)
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", request.input[-1]["content"][0]["text"])
        compacted_texts = [item["content"][0]["text"] for item in result.history]
        self.assertEqual(compacted_texts[:-1], ["first task", "second task"])
        self.assertTrue(compacted_texts[-1].startswith("Another language model started"))
        self.assertIn("handoff summary", compacted_texts[-1])
        self.assertTrue(any(event.type == "context_compaction.completed" for event in result.events))
        warning = next(event for event in result.events if event.type == "warning")
        self.assertEqual(warning.payload["message"], LOCAL_COMPACTION_WARNING_MESSAGE)

    def test_manual_compaction_reinjects_initial_context_on_next_turn(self) -> None:
        model = ScriptedResponsesModel([message("handoff summary"), message("next final")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session._initial_context_recorded = True
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first task"}]}
        )

        session.compact()

        self.assertFalse(session._initial_context_recorded)
        result = session.run("next task")

        self.assertEqual(result.final_message, "next final")
        self.assertEqual(result.history[0]["role"], "user")
        self.assertTrue(result.history[0]["content"][0]["text"].startswith("first task"))
        roles = [item.get("role") for item in result.history]
        self.assertIn("developer", roles)
        self.assertTrue(any(
            item.get("role") == "user"
            and item["content"][-1]["text"].startswith("<environment_context>")
            for item in result.history
        ))

    def test_remote_compaction_uses_compact_endpoint_payload_and_replacement_history(self) -> None:
        real_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "kept user"}]}
        remote_summary = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": build_compaction_summary_text("remote summary")}],
        }
        model = RemoteCompactModel(
            compact_output=[
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "stale dev"}]},
                real_user,
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>\nold\n</environment_context>"}],
                },
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "drop"}]},
                {"type": "function_call", "name": "exec_command", "call_id": "call-1", "arguments": "{}"},
                remote_summary,
            ]
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.state.append_history(real_user)

        result = session.compact()

        self.assertEqual(result.final_message, "")
        self.assertEqual(len(model.compact_requests), 1)
        self.assertEqual(model.requests, [])
        request = model.compact_requests[0]
        self.assertTrue(request.tools)
        self.assertTrue(request.parallel_tool_calls)
        self.assertNotIn("CONTEXT CHECKPOINT COMPACTION", json.dumps(request.input, ensure_ascii=False))
        self.assertEqual([item.get("role") for item in result.history], ["user", "user"])
        self.assertEqual(result.history[-1]["content"][0]["text"], remote_summary["content"][0]["text"])
        completed = next(event for event in result.events if event.type == "context_compaction.completed")
        self.assertEqual(completed.payload["implementation"], "responses_compact")
        self.assertTrue(completed.payload["remote_compaction"])
        self.assertEqual(completed.payload["compacted_message"], "")
        self.assertFalse(any(event.type == "warning" for event in result.events))

    def test_remote_compaction_trims_trailing_codex_generated_outputs_to_fit_context_window(self) -> None:
        huge_output = "x" * 20_000
        history = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "real user"}]},
            {"type": "function_call", "name": "exec_command", "call_id": "call-1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": huge_output},
        ]
        config = VolleyConfig(
            skip_git_repo_check=True,
            ephemeral=True,
            model_context_window=1,
        )

        trimmed, deleted = trim_remote_compaction_history_to_fit_context_window(history, config)

        self.assertEqual(deleted, 1)
        self.assertEqual([item["type"] for item in trimmed], ["message", "function_call"])
        self.assertNotIn(huge_output, json.dumps(trimmed))

    def test_remote_compaction_does_not_trim_real_user_tail_when_request_still_too_large(self) -> None:
        history = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * 20_000}]},
        ]
        config = VolleyConfig(
            skip_git_repo_check=True,
            ephemeral=True,
            model_context_window=1,
        )

        trimmed, deleted = trim_remote_compaction_history_to_fit_context_window(history, config)

        self.assertEqual(deleted, 0)
        self.assertEqual(trimmed, history)

    def test_remote_compaction_request_trims_large_trailing_tool_output_before_endpoint(self) -> None:
        huge_output = "x" * 20_000
        remote_summary = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": build_compaction_summary_text("remote summary")}],
        }
        model = RemoteCompactModel(compact_output=[remote_summary])
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_context_window=1,
            ),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "real user"}]}
        )
        session.state.append_history(
            {"type": "function_call", "name": "exec_command", "call_id": "call-1", "arguments": "{}"}
        )
        session.state.append_history(
            {"type": "function_call_output", "call_id": "call-1", "output": huge_output}
        )

        session.compact()

        request_body = json.dumps(model.compact_requests[0].input, ensure_ascii=False)
        self.assertNotIn(huge_output, request_body)
        self.assertIn('"output": "aborted"', request_body)

    def test_remote_compaction_falls_back_to_local_prompt_when_auto_mode_fails(self) -> None:
        model = RemoteCompactModel(compact_output=[], local_responses=[message("local summary")], fail_remote=True)
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}
        )

        result = session.compact()

        self.assertEqual(result.final_message, "local summary")
        self.assertEqual(len(model.compact_requests), 1)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", model.requests[0].input[-1]["content"][0]["text"])
        self.assertIn("stream_error", [event.type for event in result.events])
        completed = next(event for event in result.events if event.type == "context_compaction.completed")
        self.assertEqual(completed.payload["implementation"], "responses")
        self.assertFalse(completed.payload["remote_compaction"])

    def test_remote_compaction_can_be_disabled_for_prompt_summary_comparison(self) -> None:
        model = RemoteCompactModel(compact_output=[], local_responses=[message("local summary")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, remote_compaction="off"),
            model_client=model,
        )

        result = session.compact()

        self.assertEqual(result.final_message, "local summary")
        self.assertEqual(model.compact_requests, [])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0].tools, [])
        self.assertFalse(model.requests[0].parallel_tool_calls)

    def test_recent_context_offload_appends_block_and_offloads_long_output(self) -> None:
        # DIVERGENCE FROM UPSTREAM: opt-in recent-activity block after compaction.
        import json as _json
        import tempfile
        from pathlib import Path as _Path
        from volley.state import build_recent_context_offload

        tmp = _Path(tempfile.mkdtemp())
        config = VolleyConfig(skip_git_repo_check=True, ephemeral=True)
        history = [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Running tests."}]},
            {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": _json.dumps({"command": ["pytest"]})},
            {"type": "function_call_output", "call_id": "c1", "output": "PASS\n" + ("X" * 9000)},
            {"type": "function_call", "name": "apply_patch", "call_id": "c2", "arguments": _json.dumps({"input": "patch"})},
            {"type": "function_call_output", "call_id": "c2", "output": "M foo.py\n" + ("Y" * 9000)},
        ]

        block = build_recent_context_offload(history, 20_000, config, offload_dir=tmp)

        # Header + assistant prose are preserved verbatim.
        self.assertEqual(block[0]["role"], "developer")
        self.assertIn("<recent_activity>", block[0]["content"][0]["text"])
        self.assertTrue(any(b.get("role") == "assistant" and b["content"][0]["text"] == "Running tests." for b in block))

        outputs = {b["call_id"]: b["output"] for b in block if b.get("type") == "function_call_output"}
        # exec_command output: truncated + offloaded to a file referenced inline.
        self.assertIn("Full original saved to", outputs["c1"])
        self.assertEqual(len(list(tmp.glob("*.txt"))), 1)
        # apply_patch (file edit) output: dropped, replaced with a short pointer note.
        self.assertIn("output omitted", outputs["c2"])

    def test_recent_context_offload_does_not_recopy_previous_recent_block(self) -> None:
        from volley.state import build_recent_context_offload

        config = VolleyConfig(skip_git_repo_check=True, ephemeral=True)
        previous_recent = build_recent_context_offload(
            [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old assistant"}]},
                {"type": "function_call", "name": "exec_command", "call_id": "old", "arguments": json.dumps({"cmd": "old"})},
                {"type": "function_call_output", "call_id": "old", "output": "old output"},
            ],
            20_000,
            config,
            offload_dir=None,
        )
        history = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": build_compaction_summary_text("old summary")}],
            },
            *previous_recent,
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new assistant"}]},
            {"type": "function_call", "name": "exec_command", "call_id": "new", "arguments": json.dumps({"cmd": "new"})},
            {"type": "function_call_output", "call_id": "new", "output": "new output"},
        ]

        block = build_recent_context_offload(history, 20_000, config, offload_dir=None)

        serialized = json.dumps(block, ensure_ascii=False)
        self.assertNotIn("old summary", serialized)
        self.assertNotIn("old assistant", serialized)
        self.assertNotIn("old output", serialized)
        self.assertIn("new assistant", serialized)
        self.assertIn("new output", serialized)

    def test_recent_context_offload_default_gates_on_persistence(self) -> None:
        # Auto default: on for persistent sessions, off for ephemeral/throwaway.
        self.assertEqual(
            VolleyConfig(skip_git_repo_check=True, ephemeral=False).resolved_recent_context_offload_tokens(),
            10_000,
        )
        self.assertEqual(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True).resolved_recent_context_offload_tokens(),
            0,
        )
        # Explicit 0 always disables (upstream-identical), even when persistent.
        self.assertEqual(
            VolleyConfig(ephemeral=False, recent_context_offload_tokens=0).resolved_recent_context_offload_tokens(),
            0,
        )
        config = VolleyConfig(skip_git_repo_check=True, ephemeral=True, recent_context_offload_tokens=0)
        session = VolleySession(config, model_client=ScriptedResponsesModel([message("summary")]))
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}
        )
        session.state.append_history(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "did work"}]}
        )
        session.compact()
        # No <recent_activity> block when the feature is off.
        self.assertFalse(
            any("<recent_activity>" in json.dumps(item, ensure_ascii=False) for item in session.state.history)
        )

    def test_resume_after_recent_context_offload_reinjects_initial_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            session_model = ScriptedResponsesModel([message("first answer"), message("handoff summary")])
            session = VolleySession(config, model_client=session_model)

            first = session.run("first")
            session.compact()
            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{first.thread_id}.jsonl"))
            reconstructed = reconstruct_history_from_rollout(rollout_path, config)
            self.assertTrue(
                any("<recent_activity>" in json.dumps(item, ensure_ascii=False) for item in reconstructed.history)
            )
            self.assertFalse(
                any("<environment_context>" in json.dumps(item, ensure_ascii=False) for item in reconstructed.history)
            )

            resumed_model = ScriptedResponsesModel([message("second answer")])
            resumed = VolleySession.resume_from_rollout(
                rollout_path,
                config,
                model_client=resumed_model,
            )
            resumed.run("second")

            request_texts = _request_texts(resumed_model.requests[0])
            self.assertTrue(any("<recent_activity>" in text for text in request_texts))
            self.assertTrue(any("<environment_context>" in text for text in request_texts))

    def test_manual_compaction_runs_pre_and_post_hooks(self) -> None:
        calls: list[dict] = []

        def hook_provider(request: dict) -> dict:
            calls.append(request)
            if request["event"] == "pre_compact":
                return {"additional_contexts": ["pre compact policy"]}
            return {}

        model = ScriptedResponsesModel([message("handoff summary")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first task"}]}
        )

        result = session.compact()

        self.assertEqual(result.final_message, "handoff summary")
        self.assertEqual([call["event"] for call in calls], ["pre_compact", "post_compact"])
        self.assertEqual(calls[0]["trigger"], "manual")
        self.assertEqual(calls[0]["model"], session.config.model)
        self.assertTrue(any("<hook_context>" in text for text in _request_texts(model.requests[0])))
        self.assertIn("hook.started", [event.type for event in result.events])
        self.assertIn("hook.completed", [event.type for event in result.events])

    def test_pre_compact_hook_can_abort_before_model_request(self) -> None:
        def hook_provider(request: dict) -> dict:
            if request["event"] == "pre_compact":
                return {"should_stop": True, "stop_reason": "skip compact"}
            return {}

        model = ScriptedResponsesModel([])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=model,
        )

        events = list(session.stream_compact())

        self.assertEqual(model.requests, [])
        self.assertIn("turn.aborted", [event.type for event in events])
        self.assertNotIn("model.request", [event.type for event in events])

    def test_post_compact_hook_can_abort_after_history_replacement(self) -> None:
        def hook_provider(request: dict) -> dict:
            if request["event"] == "post_compact":
                return {"should_stop": True, "stop_reason": "stop after compact"}
            return {}

        model = ScriptedResponsesModel([message("handoff summary")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, hook_provider=hook_provider),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first task"}]}
        )

        events = list(session.stream_compact())

        event_types = [event.type for event in events]
        self.assertIn("context_compaction.completed", event_types)
        self.assertIn("turn.aborted", event_types)
        self.assertTrue(session.state.history[-1]["content"][0]["text"].startswith("Another language model started"))

    def test_manual_compaction_persists_upstream_compacted_rollout_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel([message("handoff summary")])
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            )
            session.state.append_history(
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first task"}]}
            )
            session.state.append_history(
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "work"}]}
            )

            result = session.compact()

            matches = list((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            self.assertEqual(len(matches), 1)
            records = [json.loads(line) for line in matches[0].read_text(encoding="utf-8").splitlines()]
            compacted = [record["payload"] for record in records if record["type"] == "compacted"]
            self.assertEqual(len(compacted), 1)
            self.assertTrue(compacted[0]["message"].startswith("Another language model started"))
            self.assertIn("handoff summary", compacted[0]["message"])
            self.assertEqual(compacted[0]["replacement_history"], result.history)

    def test_rollout_reconstruction_uses_replacement_history_checkpoint(self) -> None:
        old_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old"}]}
        compact_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary"}]}
        new_assistant = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "after"}]}
        records = [
            {"type": "session_meta", "payload": {"id": "thread-1", "cwd": "/tmp"}},
            {"type": "response_item", "payload": old_user},
            {
                "type": "compacted",
                "payload": {"message": "summary", "replacement_history": [compact_user]},
            },
            {"type": "response_item", "payload": new_assistant},
        ]

        reconstructed = reconstruct_history_from_rollout(records)

        self.assertEqual(reconstructed.history, [compact_user, new_assistant])
        self.assertEqual(reconstructed.session_meta, {"id": "thread-1", "cwd": "/tmp"})
        self.assertFalse(reconstructed.legacy_compaction_without_replacement_history)

    def test_rollout_reconstruction_supports_legacy_compaction_without_replacement_history(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
            },
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "work"}]},
            },
            {"type": "compacted", "payload": {"message": build_compaction_summary_text("legacy summary")}},
        ]

        reconstructed = reconstruct_history_from_rollout(records)

        self.assertTrue(reconstructed.legacy_compaction_without_replacement_history)
        self.assertIsNone(reconstructed.reference_context_item)
        self.assertEqual([item["role"] for item in reconstructed.history], ["user", "user"])
        self.assertEqual(reconstructed.history[0]["content"][0]["text"], "first")
        self.assertIn("legacy summary", reconstructed.history[-1]["content"][0]["text"])

    def test_rollout_reconstruction_extracts_turn_context_and_rollbacks(self) -> None:
        first_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]}
        first_assistant = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "one"}]}
        second_user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]}
        second_assistant = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "two"}]}
        first_turn_context = {"turn_id": "turn-1", "model": "gpt-first", "realtime_active": False}
        second_turn_context = {"turn_id": "turn-2", "model": "gpt-second", "realtime_active": True}
        records = [
            {"type": "turn_context", "payload": first_turn_context},
            {"type": "response_item", "payload": first_user},
            {"type": "response_item", "payload": first_assistant},
            {"type": "turn_context", "payload": second_turn_context},
            {"type": "response_item", "payload": second_user},
            {"type": "response_item", "payload": second_assistant},
            {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        ]

        reconstructed = reconstruct_history_from_rollout(records)

        self.assertEqual(reconstructed.history, [first_user, first_assistant])
        self.assertEqual(reconstructed.previous_turn_settings, {"model": "gpt-first", "realtime_active": False})
        self.assertEqual(reconstructed.reference_context_item, first_turn_context)

    def test_pre_sampling_auto_compaction_uses_prior_token_usage_before_current_prompt(self) -> None:
        model = ScriptedResponsesModel([message("auto summary"), message("final after compact")])
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_auto_compact_token_limit=1,
            ),
            model_client=model,
        )
        session.state.append_history(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "previous turn"}]}
        )
        session.state.record_token_usage({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

        result = session.run("new prompt is not counted before pre-sampling compaction")

        self.assertEqual(result.final_message, "final after compact")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[0].tools, [])
        self.assertFalse(model.requests[0].parallel_tool_calls)
        self.assertTrue(model.requests[1].tools)
        completed = [event for event in result.events if event.type == "context_compaction.completed"]
        self.assertEqual(completed[0].payload["trigger"], "auto")
        self.assertEqual(completed[0].payload["phase"], "pre_sampling")
        self.assertFalse(completed[0].payload["initial_context_injected"])
        self.assertEqual(result.history[0]["role"], "user")
        self.assertTrue(result.history[0]["content"][0]["text"].startswith("Another language model started"))
        self.assertEqual([item["role"] for item in result.history[1:4]], ["developer", "user", "user"])
        self.assertTrue(result.history[2]["content"][-1]["text"].startswith("<environment_context>"))
        self.assertTrue(any(
            item.get("role") == "user"
            and item["content"][0]["text"].startswith("Another language model started")
            for item in result.history
        ))

    def test_current_first_prompt_does_not_trigger_pre_sampling_compaction_by_itself(self) -> None:
        model = ScriptedResponsesModel([message("final without compact")])
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_auto_compact_token_limit=1,
            ),
            model_client=model,
        )

        result = session.run("this prompt is long enough to exceed a tiny local estimate")

        self.assertEqual(result.final_message, "final without compact")
        self.assertEqual(len(model.requests), 1)
        self.assertTrue(model.requests[0].tools)
        self.assertFalse(any(event.type == "context_compaction.completed" for event in result.events))

    def test_mid_turn_auto_compaction_runs_after_tool_followup_usage_crosses_limit(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "update_plan",
                            "call_id": "plan-1",
                            "arguments": json.dumps(
                                {
                                    "plan": [
                                        {"step": "inspect", "status": "completed"},
                                        {"step": "finish", "status": "in_progress"},
                                    ]
                                }
                            ),
                        }
                    ],
                    "usage": {"input_tokens": 900, "output_tokens": 200, "total_tokens": 1100},
                },
                message("mid-turn summary"),
                message("final after mid-turn compact"),
            ]
        )
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_auto_compact_token_limit=1000,
            ),
            model_client=model,
        )

        result = session.run("use a tool before finishing")

        self.assertEqual(result.final_message, "final after mid-turn compact")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(model.requests[1].tools, [])
        completed = [event for event in result.events if event.type == "context_compaction.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].payload["trigger"], "auto")
        self.assertEqual(completed[0].payload["phase"], "mid_turn")
        self.assertTrue(completed[0].payload["initial_context_injected"])
        started_turn_id = next(event.payload["turn_id"] for event in result.events if event.type == "turn.started")
        self.assertEqual(completed[0].payload["turn_id"], started_turn_id)

    def test_mid_turn_compaction_counts_local_tool_output_after_last_model_item(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "exec-compact",
                            "arguments": json.dumps(
                                {"cmd": f"{sys.executable!r} -c \"print('x' * 400)\""}
                            ),
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                },
                message("summary after local output"),
                message("final after local-output compact"),
            ]
        )
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_auto_compact_token_limit=60,
            ),
            model_client=model,
        )

        result = session.run("run a verbose command")

        self.assertEqual(result.final_message, "final after local-output compact")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(model.requests[1].tools, [])
        completed = [event for event in result.events if event.type == "context_compaction.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].payload["phase"], "mid_turn")

    def test_previous_model_downshift_compaction_uses_previous_model_before_sampling(self) -> None:
        previous_catalog = codex_types._MODEL_CATALOG_CACHE
        codex_types._MODEL_CATALOG_CACHE = {
            "huge-test": {
                "slug": "huge-test",
                "context_window": 5000,
                "default_reasoning_summary": "none",
                "supports_parallel_tool_calls": True,
                "input_modalities": ["text"],
            },
            "tiny-test": {
                "slug": "tiny-test",
                "context_window": 1000,
                "default_reasoning_summary": "none",
                "supports_parallel_tool_calls": True,
                "input_modalities": ["text"],
            },
        }
        try:
            model = ScriptedResponsesModel([message("downshift summary"), message("final after downshift")])
            session = VolleySession(
                VolleyConfig(
                    model="tiny-test",
                    skip_git_repo_check=True,
                    ephemeral=True,
                ),
                model_client=model,
            )
            session.state.previous_turn_settings = {"model": "huge-test", "realtime_active": False}
            session.state.record_token_usage({"input_tokens": 950, "output_tokens": 0, "total_tokens": 950})

            result = session.run("continue after switching to the smaller model")

            self.assertEqual(result.final_message, "final after downshift")
            self.assertEqual([request.model for request in model.requests], ["huge-test", "tiny-test"])
            self.assertEqual(model.requests[0].tools, [])
            completed = [event for event in result.events if event.type == "context_compaction.completed"]
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0].payload["reason"], "model_downshift")
            self.assertEqual(completed[0].payload["phase"], "pre_sampling")
            self.assertFalse(completed[0].payload["initial_context_injected"])
        finally:
            codex_types._MODEL_CATALOG_CACHE = previous_catalog

    def test_persistent_pre_turn_auto_compaction_clears_reference_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                    model_auto_compact_token_limit=1,
                ),
                model_client=ScriptedResponsesModel([message("auto summary"), message("final")]),
            )
            session.state.append_history(
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "previous"}]}
            )
            session.state.record_token_usage({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

            result = session.run("force automatic compaction")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            compacted_index = next(index for index, record in enumerate(records) if record["type"] == "compacted")
            self.assertNotEqual(records[compacted_index + 1]["type"], "turn_context")

            reconstructed = reconstruct_history_from_rollout(rollout_path)

            self.assertIsNotNone(reconstructed.reference_context_item)
            self.assertEqual(reconstructed.previous_turn_settings["model"], VolleyConfig().model)
            self.assertTrue(any(
                item.get("role") == "user"
                and item["content"][0]["text"].startswith("Another language model started")
                for item in reconstructed.history
            ))

    def test_base_instructions_exclude_dynamic_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("project-only instruction", encoding="utf-8")
            volley_home = root / "volley-home"
            memories = volley_home / "memories"
            memories.mkdir(parents=True)
            (memories / "memory_summary.md").write_text("Important prior decision.", encoding="utf-8")

            base = build_base_instructions(
                prompt_asset="gpt_5_codex_prompt.md",
                cwd=root,
                sandbox="workspace-write",
                approval_policy="never",
                volley_home=volley_home,
                memory_tool_enabled=True,
                use_memories=True,
            )

            self.assertIn("You are", base)
            self.assertNotIn("project-only instruction", base)
            self.assertNotIn("Important prior decision.", base)
            self.assertNotIn("Approval policy is currently never.", base)

    def test_auto_base_instructions_use_upstream_model_catalog(self) -> None:
        base = build_base_instructions(
            prompt_asset="auto",
            model="gpt-5.5",
            cwd=Path.cwd(),
            sandbox="workspace-write",
            approval_policy="never",
        )

        self.assertEqual(base, read_model_catalog_instructions("gpt-5.5"))
        self.assertIn("Intermediary updates", base)

    def test_initial_context_matches_upstream_fragment_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("root doc", encoding="utf-8")
            nested = root / "workspace" / "crate_a"
            nested.mkdir(parents=True)
            (nested / "AGENTS.md").write_text("crate doc", encoding="utf-8")

            config = VolleyConfig(
                cwd=nested,
                skip_git_repo_check=True,
                ephemeral=True,
                approval_policy="never",
                current_date="2026-05-14",
                timezone="America/New_York",
            )
            items = build_initial_context_items(config)

            self.assertEqual([item["role"] for item in items], ["developer", "user"])
            permissions_text = items[0]["content"][0]["text"]
            self.assertIn("`sandbox_mode` is `workspace-write`", permissions_text)
            self.assertIn("Network access is restricted.", permissions_text)
            self.assertIn(f"The writable root is `{nested.resolve()}`.", permissions_text)

            user_texts = [part["text"] for part in items[1]["content"]]
            self.assertTrue(user_texts[0].startswith(f"# AGENTS.md instructions for {nested.resolve()}"))
            self.assertIn("<INSTRUCTIONS>\nroot doc\n\ncrate doc\n</INSTRUCTIONS>", user_texts[0])
            self.assertTrue(user_texts[1].startswith("<environment_context>"))
            self.assertTrue(user_texts[1].endswith("</environment_context>"))
            self.assertIn(f"  <cwd>{nested.resolve()}</cwd>", user_texts[1])
            self.assertIn("  <shell>", user_texts[1])
            self.assertIn("  <current_date>2026-05-14</current_date>", user_texts[1])
            self.assertIn("  <timezone>America/New_York</timezone>", user_texts[1])
            self.assertNotIn("<os>", user_texts[1])

    def test_session_request_places_initial_context_before_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("be nice", encoding="utf-8")
            model = ScriptedResponsesModel([message("done")])
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    skip_git_repo_check=True,
                    ephemeral=True,
                    current_date="2026-05-14",
                    timezone="America/New_York",
                ),
                model_client=model,
            )

            session.run("hello")

            request = model.requests[0]
            self.assertNotIn("be nice", request.instructions)
            self.assertEqual([item["role"] for item in request.input[:3]], ["developer", "user", "user"])
            self.assertEqual(request.input[2]["content"][0]["text"], "hello")
            self.assertEqual(request.client_metadata, {"x-codex-installation-id": session.state.installation_id})

    def test_session_request_includes_configured_output_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        model = ScriptedResponsesModel([message('{"answer":"done"}')])
        VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                output_schema=schema,
            ),
            model_client=model,
        ).run("hello")

        self.assertEqual(model.requests[0].output_schema, schema)
        self.assertTrue(model.requests[0].output_schema_strict)
        self.assertEqual(model.requests[0].to_responses_kwargs()["text"]["format"]["schema"], schema)

    def test_session_request_includes_local_image_inputs_before_text(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is required for local image input tests")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "input.png"
            Image.new("RGBA", (1, 1), (10, 20, 30, 255)).save(image_path)
            model = ScriptedResponsesModel([message("saw image")])

            VolleySession(
                VolleyConfig(
                    cwd=root,
                    skip_git_repo_check=True,
                    ephemeral=True,
                    input_images=(Path("input.png"),),
                ),
                model_client=model,
            ).run("describe it")

            user_item = model.requests[0].input[-1]
            content = user_item["content"]
            self.assertEqual(content[0], {"type": "input_text", "text": "<image name=[Image #1]>"})
            self.assertEqual(content[1]["type"], "input_image")
            self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
            self.assertEqual(content[1]["detail"], "high")
            self.assertEqual(content[2], {"type": "input_text", "text": "</image>"})
            self.assertEqual(content[3], {"type": "input_text", "text": "describe it"})

    def test_prompt_history_normalizes_call_output_pairs_for_model_request(self) -> None:
        model = ScriptedResponsesModel([message("done")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.state.append_history(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "missing-output",
                "arguments": json.dumps({"cmd": "pwd"}),
            }
        )
        session.state.append_history(
            {"type": "function_call_output", "call_id": "orphan-output", "output": "orphan"}
        )

        session.run("hello")

        request = model.requests[0]
        call_index = next(index for index, item in enumerate(request.input) if item.get("call_id") == "missing-output")
        self.assertEqual(request.input[call_index + 1], {
            "type": "function_call_output",
            "call_id": "missing-output",
            "output": "aborted",
        })
        self.assertFalse(any(item.get("call_id") == "orphan-output" for item in request.input))
        self.assertTrue(any(item.get("call_id") == "orphan-output" for item in session.state.history))

    def test_prepare_prompt_history_strips_images_when_model_does_not_support_images(self) -> None:
        history = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "high"},
                ],
            },
            {
                "type": "function_call",
                "name": "view_image",
                "call_id": "view-1",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "view-1",
                "output": {
                    "content": [
                        {"type": "input_text", "text": "image result"},
                        {"type": "input_image", "image_url": "data:image/png;base64,def", "detail": "high"},
                    ],
                    "success": True,
                },
            },
        ]

        prompt_history = prepare_prompt_history(
            history,
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model_supports_image_input=False,
            ),
        )

        placeholder = "image content omitted because you do not support image input"
        self.assertEqual(prompt_history[0]["content"][1], {"type": "input_text", "text": placeholder})
        self.assertEqual(prompt_history[2]["output"]["content"][1], {"type": "input_text", "text": placeholder})
        self.assertEqual(history[0]["content"][1]["type"], "input_image")

    def test_prepare_prompt_history_preserves_images_for_image_models(self) -> None:
        history = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "high"},
                ],
            }
        ]

        prompt_history = prepare_prompt_history(history, VolleyConfig(skip_git_repo_check=True, ephemeral=True))

        self.assertEqual(prompt_history[0]["content"][1]["type"], "input_image")

    def test_prepare_prompt_history_truncates_long_tool_outputs(self) -> None:
        long_output = "tool output line\n" * 5_000
        history = [
            {"type": "function_call", "name": "exec_command", "call_id": "call-1", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": long_output},
        ]

        prompt_history = prepare_prompt_history(
            history,
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                model="tiny-test-model",
                model_supports_image_input=False,
            ),
        )

        self.assertIn("tokens truncated", prompt_history[1]["output"])
        self.assertLess(len(prompt_history[1]["output"]), len(long_output))

    def test_agents_md_discovery_uses_root_to_cwd_order_and_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("root versioned", encoding="utf-8")
            child = root / "pkg"
            child.mkdir()
            (child / "AGENTS.md").write_text("child versioned", encoding="utf-8")
            (child / "AGENTS.override.md").write_text("child local", encoding="utf-8")

            self.assertEqual(collect_agents_md(child), "root versioned\n\nchild local")

    def test_environment_context_and_permissions_helpers_match_upstream_shape(self) -> None:
        env = build_environment_context(
            Path("/tmp/example"),
            shell="bash",
            current_date="2026-05-14",
            timezone="America/New_York",
        )
        self.assertEqual(
            env,
            "<environment_context>\n"
            "  <cwd>/tmp/example</cwd>\n"
            "  <shell>bash</shell>\n"
            "  <current_date>2026-05-14</current_date>\n"
            "  <timezone>America/New_York</timezone>\n"
            "</environment_context>",
        )
        permissions = build_permissions_instructions(
            cwd=Path("/tmp/example"),
            sandbox="read-only",
            approval_policy="on-failure",
            network_access="enabled",
        )
        self.assertIn("`sandbox_mode` is `read-only`", permissions)
        self.assertIn("Network access is enabled.", permissions)
        self.assertIn("`approval_policy` is `on-failure`", permissions)

    def test_memory_read_instructions_are_feature_gated_in_initial_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            memories = volley_home / "memories"
            memories.mkdir(parents=True)
            (memories / "memory_summary.md").write_text("Important prior decision.", encoding="utf-8")

            disabled = build_initial_context_items(
                VolleyConfig(
                    cwd=root,
                    sandbox="workspace-write",
                    approval_policy="never",
                    volley_home=volley_home,
                    memory_tool_enabled=False,
                    use_memories=True,
                    skip_git_repo_check=True,
                    ephemeral=True,
                )
            )
            enabled = build_initial_context_items(
                VolleyConfig(
                    cwd=root,
                    sandbox="workspace-write",
                    approval_policy="never",
                    volley_home=volley_home,
                    memory_tool_enabled=True,
                    use_memories=True,
                    skip_git_repo_check=True,
                    ephemeral=True,
                )
            )
            disabled_text = "\n".join(part["text"] for item in disabled for part in item["content"])
            enabled_text = "\n".join(part["text"] for item in enabled for part in item["content"])
            self.assertNotIn("## Memory", disabled_text)
            self.assertIn("## Memory", enabled_text)
            self.assertIn("Important prior decision.", enabled_text)

    def test_memory_citation_parser_matches_upstream_shape(self) -> None:
        visible, citations = strip_memory_citations(
            "answer<oai-mem-citation><citation_entries>\n"
            "MEMORY.md:2-4|note=[used repo decision]\n"
            "</citation_entries>\n<rollout_ids>\n019cc2ea-1dff-7902-8d40-c8f6e5d83cc4\n"
            "019cc2ea-1dff-7902-8d40-c8f6e5d83cc4\n</rollout_ids></oai-mem-citation>"
        )
        self.assertEqual(visible, "answer")
        parsed = parse_memory_citation(citations)
        self.assertEqual(
            parsed,
            {
                "entries": [
                    {
                        "path": "MEMORY.md",
                        "line_start": 2,
                        "line_end": 4,
                        "note": "used repo decision",
                    }
                ],
                "rollout_ids": ["019cc2ea-1dff-7902-8d40-c8f6e5d83cc4"],
            },
        )

    def test_proposed_plan_block_parser_matches_upstream_line_shape(self) -> None:
        text = "before\n<proposed_plan>\n- step\n</proposed_plan>\nafter"
        self.assertEqual(strip_proposed_plan_blocks(text), "before\nafter")
        self.assertEqual(extract_proposed_plan_text(text), "- step\n")
        self.assertEqual(strip_proposed_plan_blocks("  <proposed_plan> extra\n"), "  <proposed_plan> extra\n")
        self.assertEqual(strip_proposed_plan_blocks("<proposed_plan>\n- step\n"), "")

    def test_plan_mode_hides_proposed_plan_from_final_message_and_item_text(self) -> None:
        model = ScriptedResponsesModel(
            [
                message(
                    "before\n<proposed_plan>\n- implement it\n</proposed_plan>\nafter"
                )
            ]
        )
        session = VolleySession(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                collaboration_mode="Plan",
            ),
            model_client=model,
        )

        result = session.run("make a plan")

        self.assertEqual(result.final_message, "before\nafter")
        assistant = next(item for item in result.history if item.get("role") == "assistant")
        self.assertEqual(assistant["content"][0]["text"], "before\nafter")
        self.assertEqual(assistant["proposed_plan"], "- implement it\n")

    def test_non_plan_mode_keeps_proposed_plan_text_visible(self) -> None:
        model = ScriptedResponsesModel([message("<proposed_plan>\n- visible\n</proposed_plan>")])
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("show raw")

        self.assertEqual(result.final_message, "<proposed_plan>\n- visible\n</proposed_plan>")

    def test_memory_write_stage_one_prompt_builders_match_upstream_shape(self) -> None:
        self.assertIn("Memory Writing Agent", memory_stage_one_system_prompt())
        self.assertEqual(
            memory_stage_one_rollout_token_limit(
                model_context_window=123_000,
                effective_context_window_percent=95,
            ),
            ((123_000 * 95) // 100) * 70 // 100,
        )
        self.assertEqual(memory_stage_one_rollout_token_limit(model_context_window=None), 150_000)

        contents = f"{'a' * 100}middle{'z' * 100}"
        message_text = build_memory_stage_one_input_message(
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_cwd=Path("/tmp"),
            rollout_contents=contents,
            model_context_window=10,
            effective_context_window_percent=95,
        )

        self.assertIn("rollout_path: /tmp/rollout.jsonl", message_text)
        self.assertIn("rollout_cwd: /tmp", message_text)
        self.assertIn("tokens truncated", message_text)
        self.assertIn("aaaa", message_text)
        self.assertIn("zzzz", message_text)

    def test_memory_write_consolidation_prompt_points_to_diff_and_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memories"
            (memory_root / "extensions").mkdir(parents=True)

            prompt = build_memory_consolidation_prompt(memory_root)

            self.assertIn("Memory workspace diff:", prompt)
            self.assertIn("phase2_workspace_diff.md", prompt)
            self.assertIn(f"Memory extensions (under {memory_root / 'extensions'}/):", prompt)
            self.assertIn("workspace diff shows deleted extension resource files", prompt)

    def test_memory_stage_one_schema_and_extractor_match_upstream_shape(self) -> None:
        schema = memory_stage_one_output_schema()
        self.assertEqual(sorted(schema["required"]), ["raw_memory", "rollout_slug", "rollout_summary"])
        self.assertEqual(schema["properties"]["rollout_slug"]["type"], ["string", "null"])
        self.assertFalse(schema["additionalProperties"])

        output = json.dumps(
            {
                "raw_memory": "Remember this.",
                "rollout_summary": "short summary",
                "rollout_slug": "Memory Work",
            }
        )
        model = ScriptedResponsesModel([message(output)])
        result = extract_memory_stage_one(
            model_client=model,
            rollout_path=Path("/tmp/rollout.jsonl"),
            rollout_cwd=Path("/tmp"),
            rollout_contents='[{"type":"message","role":"user"}]',
            prompt_cache_key="thread-1",
        )

        self.assertEqual(result.raw_memory, "Remember this.")
        self.assertEqual(result.rollout_summary, "short summary")
        self.assertEqual(result.rollout_slug, "Memory Work")
        request = model.requests[0]
        self.assertEqual(request.model, "gpt-5.4-mini")
        self.assertEqual(request.reasoning, {"effort": "low", "summary": "auto"})
        self.assertEqual(request.include, ["reasoning.encrypted_content"])
        self.assertEqual(request.prompt_cache_key, "thread-1")
        self.assertEqual(request.output_schema, schema)
        self.assertIn("rollout_path: /tmp/rollout.jsonl", request.input[0]["content"][0]["text"])

    def test_memory_stage_one_parser_rejects_unknown_fields_and_redacts_secrets(self) -> None:
        with self.assertRaises(ValueError):
            parse_memory_stage_one_output(
                json.dumps(
                    {
                        "raw_memory": "x",
                        "rollout_summary": "y",
                        "rollout_slug": None,
                        "extra": "z",
                    }
                )
            )

        parsed = parse_memory_stage_one_output(
            json.dumps(
                {
                    "raw_memory": "token=sk-1234567890abcdefghijklmnop",
                    "rollout_summary": "api_key=secret1234",
                    "rollout_slug": None,
                }
            )
        )
        self.assertEqual(parsed.raw_memory, "token=[REDACTED_SECRET]")
        self.assertEqual(parsed.rollout_summary, "api_key=[REDACTED_SECRET]")

    def test_memory_rollout_filter_matches_upstream_memory_policy_shape(self) -> None:
        developer = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]}
        agents = {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "# AGENTS.md instructions for /tmp\n\n<INSTRUCTIONS>\nbody\n</INSTRUCTIONS>",
                }
            ],
        }
        skill = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<skill>\nbody\n</skill>"}],
        }
        environment = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>/tmp</cwd>\n</environment_context>"}],
        }
        user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "keep"}]}
        reasoning = {"type": "reasoning", "summary": []}
        tool_call = {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "call-1"}
        web_search = {"type": "web_search_call", "id": "ws-1", "status": "completed"}

        self.assertIsNone(sanitize_response_item_for_memories(developer))
        self.assertIsNone(sanitize_response_item_for_memories(agents))
        self.assertIsNone(sanitize_response_item_for_memories(skill))
        self.assertEqual(sanitize_response_item_for_memories(environment), environment)
        self.assertEqual(sanitize_response_item_for_memories(user), user)
        self.assertIsNone(sanitize_response_item_for_memories(reasoning))
        self.assertEqual(sanitize_response_item_for_memories(tool_call), tool_call)
        self.assertEqual(sanitize_response_item_for_memories(web_search), web_search)

        serialized = serialize_filtered_rollout_response_items(
            [developer, agents, skill, environment, user, reasoning, tool_call, web_search]
        )
        parsed = json.loads(serialized)
        self.assertEqual([item["type"] for item in parsed], ["message", "message", "function_call", "web_search_call"])

    def test_memory_rollout_loader_supports_upstream_and_python_jsonl_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            lines = [
                {
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "meta": {
                            "id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                            "timestamp": "2026-01-01T00:00:00Z",
                            "cwd": "/tmp/upstream",
                        },
                        "git": {"branch": "main"},
                    },
                },
                {
                    "timestamp": "2026-01-01T00:00:01.000Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                },
                {
                    "ts": 1767225602.0,
                    "type": "item.completed",
                    "thread_id": "python-thread",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")

            rollout = load_memory_rollout(path)

            self.assertEqual(rollout.thread_id, "python-thread")
            self.assertEqual(rollout.cwd, Path("/tmp/upstream"))
            self.assertEqual(rollout.git_branch, "main")
            serialized = json.loads(rollout.serialized_contents)
            self.assertEqual([item["role"] for item in serialized], ["user", "assistant"])

    def test_memory_stage1_startup_eligibility_matches_upstream_source_idle_age_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "meta": {
                                        "id": "thread-exec",
                                        "timestamp": "2026-01-01T00:00:00Z",
                                        "cwd": "/tmp/upstream",
                                        "source": "exec",
                                        "memory_mode": "enabled",
                                    }
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "hi"}],
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            exec_rollout = load_memory_rollout(path)

            self.assertEqual(exec_rollout.source, "exec")
            self.assertFalse(
                memory_rollout_is_stage1_startup_eligible(
                    exec_rollout,
                    now=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    min_rollout_idle_hours=0,
                )
            )
            self.assertFalse(
                memory_rollout_is_stage1_startup_eligible(
                    exec_rollout,
                    current_thread_id="thread-exec",
                    allowed_sources=frozenset({"exec"}),
                    now=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    min_rollout_idle_hours=0,
                )
            )
            self.assertTrue(
                memory_rollout_is_stage1_startup_eligible(
                    exec_rollout,
                    allowed_sources=frozenset({"exec"}),
                    now=datetime(2026, 1, 1, 7, tzinfo=timezone.utc),
                )
            )

    def test_memory_stage_one_for_rollout_and_startup_once_use_serialized_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            sessions.mkdir(parents=True)
            rollout_path = sessions / "0194f5a6-89ab-7cde-8123-456789abcdef.jsonl"
            rollout_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "ts": 1767225600.0,
                                "type": "item.completed",
                                "thread_id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "remember useful thing"}],
                                },
                            }
                        )
                    ]
                ),
                encoding="utf-8",
            )
            model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Useful thing.",
                                "rollout_summary": "Remembered useful thing.",
                                "rollout_slug": "Useful Thing",
                            }
                        )
                    ),
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Useful thing.",
                                "rollout_summary": "Remembered useful thing.",
                                "rollout_slug": "Useful Thing",
                            }
                        )
                    ),
                ]
            )

            record = run_memory_stage_one_for_rollout(model_client=model, rollout_path=rollout_path)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.thread_id, "0194f5a6-89ab-7cde-8123-456789abcdef")
            self.assertEqual(record.rollout_slug, "Useful Thing")
            self.assertIn("remember useful thing", model.requests[0].input[0]["content"][0]["text"])

            self.assertEqual(memory_rollout_candidates(volley_home), [rollout_path])
            store = MemoryStateStore(Path(tmp) / "memory-state.sqlite3")
            startup = run_memory_startup_once(
                volley_home=volley_home,
                model_client=model,
                state_store=store,
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
            )
            self.assertEqual(len(startup.records), 1)
            raw_text = raw_memories_file(startup.memory_root).read_text(encoding="utf-8")
            self.assertIn("Useful thing.", raw_text)
            ad_hoc_instructions = memory_extensions_root(startup.memory_root) / "ad_hoc" / "instructions.md"
            self.assertIn("Ad-hoc notes", ad_hoc_instructions.read_text(encoding="utf-8"))
            stored = store.get_stage1_output("0194f5a6-89ab-7cde-8123-456789abcdef")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["raw_memory"], "Useful thing.")
            self.assertEqual(store.get_job("memory_consolidate_global", "global")["status"], "pending")
            self.assertEqual(
                [memory.thread_id for memory in store.get_phase2_input_selection(n=1, max_unused_days=36_500)],
                ["0194f5a6-89ab-7cde-8123-456789abcdef"],
            )
            store.close()

    def test_memory_startup_claims_before_model_and_skips_completed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            sessions.mkdir(parents=True)
            rollout_path = sessions / "0194f5a6-89ab-7cde-8123-456789abcdef.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "type": "item.completed",
                        "thread_id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "remember useful thing"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = MemoryStateStore(Path(tmp) / "memory-state.sqlite3")
            first_model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Useful thing.",
                                "rollout_summary": "Remembered useful thing.",
                                "rollout_slug": "Useful Thing",
                            }
                        )
                    )
                ]
            )
            first = run_memory_startup_once(
                volley_home=volley_home,
                model_client=first_model,
                state_store=store,
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
            )
            self.assertEqual(len(first.records), 1)

            exhausted_model = ScriptedResponsesModel([])
            second = run_memory_startup_once(
                volley_home=volley_home,
                model_client=exhausted_model,
                state_store=store,
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
            )

            self.assertEqual(second.records, [])
            self.assertEqual(len(exhausted_model.requests), 0)
            store.close()

    def test_memory_startup_uses_thread_store_claim_scan_instead_of_recent_file_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            sessions.mkdir(parents=True)
            now = datetime.now(timezone.utc)

            def write_rollout(name: str, *, source: str, updated_at: datetime, text: str) -> Path:
                path = sessions / f"{name}.jsonl"
                path.write_text(
                    "\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "session_meta",
                                    "payload": {
                                        "meta": {
                                            "id": name,
                                            "timestamp": updated_at.isoformat(),
                                            "cwd": str(Path(tmp)),
                                            "source": source,
                                            "memory_mode": "enabled",
                                        }
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "response_item",
                                    "payload": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [{"type": "input_text", "text": text}],
                                    },
                                }
                            ),
                        ]
                    ),
                    encoding="utf-8",
                )
                os.utime(path, (updated_at.timestamp(), updated_at.timestamp()))
                return path

            write_rollout("current-thread", source="cli", updated_at=now - timedelta(hours=1), text="current")
            write_rollout("exec-thread", source="exec", updated_at=now - timedelta(hours=2), text="exec")
            write_rollout("eligible-thread", source="cli", updated_at=now - timedelta(hours=3), text="eligible memory")

            store = MemoryStateStore(Path(tmp) / "memory-state.sqlite3")
            model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Eligible memory.",
                                "rollout_summary": "Remembered eligible.",
                                "rollout_slug": "Eligible",
                            }
                        )
                    )
                ]
            )
            startup = run_memory_startup_once(
                volley_home=volley_home,
                model_client=model,
                state_store=store,
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
                current_thread_id="current-thread",
            )

            self.assertEqual([record.thread_id for record in startup.records], ["eligible-thread"])
            self.assertIn("eligible memory", model.requests[0].input[0]["content"][0]["text"])
            self.assertIsNotNone(store.get_stage1_output("eligible-thread"))
            self.assertIsNone(store.get_stage1_output("exec-thread"))
            store.close()

    def test_session_runs_memory_startup_once_when_feature_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            root.mkdir()
            sessions.mkdir(parents=True)
            rollout_path = sessions / "0194f5a6-89ab-7cde-8123-456789abcdef.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "type": "item.completed",
                        "thread_id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "remember startup"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Startup memory.",
                                "rollout_summary": "Remembered startup.",
                                "rollout_slug": "Startup",
                            }
                        )
                    ),
                    message("main done"),
                ]
            )
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    memory_tool_enabled=True,
                    use_memories=True,
                    memory_min_rollout_idle_hours=0,
                    memory_max_rollout_age_days=36_500,
                    memory_startup_background=False,
                    memory_run_phase2_on_startup=False,
                ),
                model_client=model,
            )
            result = session.run("hello")

            self.assertEqual(result.final_message, "main done")
            self.assertIsNotNone(session.memory_startup_result)
            assert session.memory_startup_result is not None
            self.assertEqual([record.thread_id for record in session.memory_startup_result.records], [
                "0194f5a6-89ab-7cde-8123-456789abcdef"
            ])
            self.assertIn("Startup memory.", raw_memories_file(session.memory_startup_result.memory_root).read_text(encoding="utf-8"))
            self.assertIsNotNone(session.config.memory_state_store)
            session.config.memory_state_store.close()

    def test_memory_rate_limit_guard_matches_upstream_threshold_shape(self) -> None:
        self.assertTrue(memory_rate_limit_allows_startup(None, min_remaining_percent=25))
        self.assertTrue(
            memory_rate_limit_allows_startup(
                {"primary": {"used_percent": 74.9}, "secondary": {"used_percent": 10}},
                min_remaining_percent=25,
            )
        )
        self.assertFalse(
            memory_rate_limit_allows_startup(
                {"primary": {"used_percent": 75.1}, "secondary": {"used_percent": 10}},
                min_remaining_percent=25,
            )
        )
        self.assertFalse(
            memory_rate_limit_allows_startup(
                {"rate_limit_reached_type": "hard", "primary": {"used_percent": 1}},
                min_remaining_percent=0,
            )
        )

    @unittest.skipUnless(shutil.which("git"), "git CLI is required for memory startup pipeline tests")
    def test_memory_startup_pipeline_runs_phase2_after_claimed_stage1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            sessions.mkdir(parents=True)
            rollout_path = sessions / "0194f5a6-89ab-7cde-8123-456789abcdef.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "type": "item.completed",
                        "thread_id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "remember pipeline"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = MemoryStateStore(Path(tmp) / "memory-state.sqlite3")
            model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Pipeline memory.",
                                "rollout_summary": "Remembered pipeline.",
                                "rollout_slug": "Pipeline",
                            }
                        )
                    ),
                    message("phase2 complete"),
                ]
            )

            result = run_memory_startup_pipeline_once(
                volley_home=volley_home,
                model_client=model,
                state_store=store,
                base_config=VolleyConfig(volley_home=volley_home, skip_git_repo_check=True),
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
                run_phase2=True,
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(result.records), 1)
            self.assertIsNotNone(result.phase2_result)
            assert result.phase2_result is not None
            self.assertEqual(result.phase2_result.status, "succeeded")
            self.assertIn("Pipeline memory.", raw_memories_file(result.memory_root).read_text(encoding="utf-8"))
            self.assertFalse((result.memory_root / "phase2_workspace_diff.md").exists())
            store.close()

    def test_memory_startup_background_task_uses_own_state_store_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            sessions = volley_home / "sessions"
            sessions.mkdir(parents=True)
            rollout_path = sessions / "0194f5a6-89ab-7cde-8123-456789abcdef.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "type": "item.completed",
                        "thread_id": "0194f5a6-89ab-7cde-8123-456789abcdef",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "remember background"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            state_store_path = Path(tmp) / "memory-state.sqlite3"
            model = ScriptedResponsesModel(
                [
                    message(
                        json.dumps(
                            {
                                "raw_memory": "Background memory.",
                                "rollout_summary": "Remembered background.",
                                "rollout_slug": "Background",
                            }
                        )
                    )
                ]
            )

            task = start_memory_startup_task(
                volley_home=volley_home,
                model_client=model,
                state_store_path=state_store_path,
                max_rollouts=1,
                max_unused_days=36_500,
                max_rollout_age_days=36_500,
                min_rollout_idle_hours=0,
                run_phase2=False,
            )
            result = task.join(timeout=5)

            self.assertEqual(task.status, "completed")
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.records[0].raw_memory, "Background memory.")
            store = MemoryStateStore(state_store_path)
            self.assertIsNotNone(store.get_stage1_output("0194f5a6-89ab-7cde-8123-456789abcdef"))
            store.close()

    def test_memory_rollout_summary_file_stem_matches_upstream_v7_and_slug_rules(self) -> None:
        fixed = MemoryStageOneRecord(
            thread_id="0194f5a6-89ab-7cde-8123-456789abcdef",
            source_updated_at=datetime.fromtimestamp(123, tz=timezone.utc),
            raw_memory="raw memory",
            rollout_summary="summary",
            rollout_slug=None,
            rollout_path=Path("/tmp/rollout.jsonl"),
            cwd=Path("/tmp/workspace"),
        )
        self.assertEqual(rollout_summary_file_stem(fixed), "2025-02-11T15-35-19-jqmb")

        slugged = MemoryStageOneRecord(
            thread_id=fixed.thread_id,
            source_updated_at=fixed.source_updated_at,
            raw_memory=fixed.raw_memory,
            rollout_summary=fixed.rollout_summary,
            rollout_slug="Unsafe Slug/With Spaces & Symbols + EXTRA_LONG_12345_67890_ABCDE_fghij_klmno",
            rollout_path=fixed.rollout_path,
            cwd=fixed.cwd,
        )
        slug = rollout_summary_file_stem(slugged).removeprefix("2025-02-11T15-35-19-jqmb-")
        self.assertEqual(len(slug), 60)
        self.assertEqual(slug, "unsafe_slug_with_spaces___symbols___extra_long_12345_67890_a")

    def test_memory_storage_rebuilds_raw_memories_and_rollout_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            rollout_summaries_dir(root).mkdir(parents=True)
            stale = rollout_summaries_dir(root) / "stale.md"
            stale.write_text("old", encoding="utf-8")

            record = MemoryStageOneRecord(
                thread_id="0194f5a6-89ab-7cde-8123-456789abcdef",
                source_updated_at=datetime.fromtimestamp(100, tz=timezone.utc),
                raw_memory="raw memory",
                rollout_summary="short summary",
                rollout_slug=None,
                rollout_path=Path("/tmp/rollout-100.jsonl"),
                cwd=Path("/tmp/workspace"),
            )

            sync_rollout_summaries_from_memories(root, [record], 100, max_unused_days=36_500)
            rebuild_raw_memories_file_from_memories(root, [record], 100, max_unused_days=36_500)

            self.assertFalse(stale.exists())
            summary_files = sorted(path.name for path in rollout_summaries_dir(root).iterdir())
            self.assertEqual(summary_files, [f"{rollout_summary_file_stem(record)}.md"])
            raw_memories = raw_memories_file(root).read_text(encoding="utf-8")
            self.assertIn("# Raw Memories", raw_memories)
            self.assertIn("raw memory", raw_memories)
            self.assertIn("cwd: /tmp/workspace", raw_memories)
            self.assertIn("rollout_path: /tmp/rollout-100.jsonl", raw_memories)
            self.assertIn(f"rollout_summary_file: {summary_files[0]}", raw_memories)

    def test_memory_phase2_input_selection_matches_upstream_retention_rules(self) -> None:
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)

        def record(
            thread_id: str,
            days_ago: int,
            *,
            usage_count: int,
            last_usage_days_ago: int | None = None,
            raw_memory: str = "raw memory",
            rollout_summary: str | None = None,
        ) -> MemoryStageOneRecord:
            last_usage = (
                now - timedelta(days=last_usage_days_ago)
                if last_usage_days_ago is not None
                else None
            )
            return MemoryStageOneRecord(
                thread_id=thread_id,
                source_updated_at=now - timedelta(days=days_ago),
                raw_memory=raw_memory,
                rollout_summary=rollout_summary if rollout_summary is not None else f"summary {thread_id}",
                rollout_slug=None,
                rollout_path=Path(f"/tmp/{thread_id}.jsonl"),
                cwd=Path("/tmp/workspace"),
                usage_count=usage_count,
                last_usage=last_usage,
            )

        high_usage = record("b-high", 25, usage_count=4, last_usage_days_ago=20)
        newest = record("c-new", 2, usage_count=1)
        lower_usage = record("a-low", 1, usage_count=0)
        stale_used = record("d-stale-used", 1, usage_count=99, last_usage_days_ago=40)
        empty = record("e-empty", 1, usage_count=99, raw_memory="", rollout_summary="")

        selected = select_phase2_memory_inputs(
            [lower_usage, stale_used, high_usage, newest, empty],
            2,
            max_unused_days=30,
            now=now,
        )

        self.assertEqual([memory.thread_id for memory in selected], ["b-high", "c-new"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            sync_rollout_summaries_from_memories(
                root,
                [lower_usage, stale_used, high_usage, newest, empty],
                2,
                max_unused_days=30,
                now=now,
            )
            rebuild_raw_memories_file_from_memories(
                root,
                [lower_usage, stale_used, high_usage, newest, empty],
                2,
                max_unused_days=30,
                now=now,
            )
            raw_memories = raw_memories_file(root).read_text(encoding="utf-8")
            self.assertIn("## Thread `b-high`", raw_memories)
            self.assertIn("## Thread `c-new`", raw_memories)
            self.assertNotIn("d-stale-used", raw_memories)
            self.assertNotIn("e-empty", raw_memories)

    def test_memory_stage1_retention_prunes_stale_unselected_records_by_batch(self) -> None:
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)

        def record(
            thread_id: str,
            days_ago: int,
            *,
            selected_for_phase2: bool = False,
            last_usage_days_ago: int | None = None,
        ) -> MemoryStageOneRecord:
            last_usage = (
                now - timedelta(days=last_usage_days_ago)
                if last_usage_days_ago is not None
                else None
            )
            return MemoryStageOneRecord(
                thread_id=thread_id,
                source_updated_at=now - timedelta(days=days_ago),
                raw_memory=f"raw {thread_id}",
                rollout_summary=f"summary {thread_id}",
                rollout_slug=None,
                rollout_path=Path(f"/tmp/{thread_id}.jsonl"),
                cwd=Path("/tmp/workspace"),
                last_usage=last_usage,
                selected_for_phase2=selected_for_phase2,
            )

        stale_oldest = record("a-stale-oldest", 60)
        stale_selected = record("b-stale-selected", 80, selected_for_phase2=True)
        fresh_used = record("c-fresh-used", 90, last_usage_days_ago=2)
        stale_newer = record("d-stale-newer", 40)

        kept, pruned = prune_stage1_records_for_retention(
            [stale_newer, fresh_used, stale_selected, stale_oldest],
            max_unused_days=30,
            limit=1,
            now=now,
        )

        self.assertEqual([memory.thread_id for memory in pruned], ["a-stale-oldest"])
        self.assertEqual(
            sorted(memory.thread_id for memory in kept),
            ["b-stale-selected", "c-fresh-used", "d-stale-newer"],
        )

    def test_memory_state_store_stage1_claim_success_usage_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStateStore(Path(tmp) / "memory.sqlite3")
            now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
            source_updated_at = now - timedelta(hours=2)
            store.upsert_thread(
                MemoryThreadRecord(
                    thread_id="thread-a",
                    rollout_path=Path("/tmp/thread-a.jsonl"),
                    cwd=Path("/tmp/workspace-a"),
                    updated_at=source_updated_at,
                    git_branch="main",
                )
            )

            claim = store.try_claim_stage1_job(
                thread_id="thread-a",
                worker_id="worker",
                source_updated_at=source_updated_at,
                lease_seconds=60,
                max_running_jobs=4,
                now=now,
            )
            self.assertEqual(claim.outcome, "claimed")
            assert claim.ownership_token is not None

            self.assertTrue(
                store.mark_stage1_job_succeeded(
                    thread_id="thread-a",
                    ownership_token=claim.ownership_token,
                    source_updated_at=source_updated_at,
                    raw_memory="remember alpha",
                    rollout_summary="summary alpha",
                    rollout_slug="alpha",
                    now=now + timedelta(seconds=1),
                )
            )
            phase2_job = store.get_job("memory_consolidate_global", "global")
            self.assertIsNotNone(phase2_job)
            assert phase2_job is not None
            self.assertEqual(phase2_job["status"], "pending")

            self.assertEqual(store.record_stage1_output_usage(["thread-a"], now=now + timedelta(seconds=2)), 1)
            selected = store.get_phase2_input_selection(n=1, max_unused_days=30, now=now + timedelta(seconds=3))
            self.assertEqual([memory.thread_id for memory in selected], ["thread-a"])
            self.assertEqual(selected[0].usage_count, 1)
            self.assertEqual(selected[0].last_usage, now + timedelta(seconds=2))
            self.assertEqual(selected[0].rollout_path, "/tmp/thread-a.jsonl")
            store.close()

    def test_memory_state_store_stage1_retry_backoff_and_advanced_source_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStateStore(Path(tmp) / "memory.sqlite3")
            now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
            source_updated_at = now - timedelta(hours=2)
            store.upsert_thread(
                MemoryThreadRecord(
                    thread_id="thread-retry",
                    rollout_path=Path("/tmp/thread-retry.jsonl"),
                    cwd=Path("/tmp/workspace"),
                    updated_at=source_updated_at,
                )
            )
            claim = store.try_claim_stage1_job(
                thread_id="thread-retry",
                worker_id="worker",
                source_updated_at=source_updated_at,
                lease_seconds=60,
                max_running_jobs=4,
                now=now,
            )
            assert claim.ownership_token is not None
            self.assertTrue(
                store.mark_stage1_job_failed(
                    thread_id="thread-retry",
                    ownership_token=claim.ownership_token,
                    failure_reason="failed_extract",
                    retry_delay_seconds=600,
                    now=now + timedelta(seconds=1),
                )
            )

            retry = store.try_claim_stage1_job(
                thread_id="thread-retry",
                worker_id="worker",
                source_updated_at=source_updated_at,
                lease_seconds=60,
                max_running_jobs=4,
                now=now + timedelta(seconds=2),
            )
            self.assertEqual(retry.outcome, "skipped_retry_backoff")

            advanced = store.try_claim_stage1_job(
                thread_id="thread-retry",
                worker_id="worker",
                source_updated_at=source_updated_at + timedelta(seconds=1),
                lease_seconds=60,
                max_running_jobs=4,
                now=now + timedelta(seconds=3),
            )
            self.assertEqual(advanced.outcome, "claimed")
            store.close()

    def test_memory_state_store_phase2_claim_heartbeat_success_and_pollution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStateStore(Path(tmp) / "memory.sqlite3")
            now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
            records = [
                ("thread-a", now - timedelta(hours=3), "raw a"),
                ("thread-b", now - timedelta(hours=2), "raw b"),
            ]
            for thread_id, source_updated_at, raw_memory in records:
                store.upsert_thread(
                    MemoryThreadRecord(
                        thread_id=thread_id,
                        rollout_path=Path(f"/tmp/{thread_id}.jsonl"),
                        cwd=Path("/tmp/workspace"),
                        updated_at=source_updated_at,
                    )
                )
                claim = store.try_claim_stage1_job(
                    thread_id=thread_id,
                    worker_id="worker",
                    source_updated_at=source_updated_at,
                    lease_seconds=60,
                    max_running_jobs=4,
                    now=now,
                )
                assert claim.ownership_token is not None
                self.assertTrue(
                    store.mark_stage1_job_succeeded(
                        thread_id=thread_id,
                        ownership_token=claim.ownership_token,
                        source_updated_at=source_updated_at,
                        raw_memory=raw_memory,
                        rollout_summary=f"summary {thread_id}",
                        rollout_slug=None,
                        now=now + timedelta(seconds=1),
                    )
                )

            selected = store.get_phase2_input_selection(n=2, max_unused_days=30, now=now + timedelta(seconds=2))
            phase2_claim = store.try_claim_global_phase2_job(
                worker_id="worker",
                lease_seconds=60,
                now=now + timedelta(seconds=3),
            )
            self.assertEqual(phase2_claim.outcome, "claimed")
            assert phase2_claim.ownership_token is not None
            self.assertTrue(
                store.heartbeat_global_phase2_job(
                    ownership_token=phase2_claim.ownership_token,
                    lease_seconds=60,
                    now=now + timedelta(seconds=4),
                )
            )
            self.assertTrue(
                store.mark_global_phase2_job_succeeded(
                    ownership_token=phase2_claim.ownership_token,
                    completed_watermark=max(record.source_updated_at for record in selected),
                    selected_outputs=[selected[0]],
                    now=now + timedelta(seconds=5),
                )
            )

            selected_row = store.get_stage1_output(selected[0].thread_id)
            unselected_row = store.get_stage1_output(selected[1].thread_id)
            assert selected_row is not None and unselected_row is not None
            self.assertEqual(selected_row["selected_for_phase2"], 1)
            self.assertEqual(unselected_row["selected_for_phase2"], 0)
            self.assertTrue(store.mark_thread_memory_mode_polluted(selected[0].thread_id, now=now + timedelta(seconds=6)))
            self.assertEqual(store.try_claim_global_phase2_job(worker_id="worker", lease_seconds=60, now=now + timedelta(seconds=7)).outcome, "skipped_cooldown")
            store.close()

    @unittest.skipUnless(shutil.which("git"), "git CLI is required for memory phase2 runner tests")
    def test_memory_phase2_once_runs_consolidation_and_marks_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            volley_home = Path(tmp) / "volley-home"
            store = MemoryStateStore(Path(tmp) / "memory.sqlite3")
            now = datetime.now(timezone.utc) - timedelta(hours=1)
            store.upsert_thread(
                MemoryThreadRecord(
                    thread_id="phase2-thread",
                    rollout_path=Path("/tmp/phase2-thread.jsonl"),
                    cwd=Path("/tmp/workspace"),
                    updated_at=now,
                )
            )
            claim = store.try_claim_stage1_job(
                thread_id="phase2-thread",
                worker_id="worker",
                source_updated_at=now,
                lease_seconds=60,
                max_running_jobs=4,
            )
            assert claim.ownership_token is not None
            store.mark_stage1_job_succeeded(
                thread_id="phase2-thread",
                ownership_token=claim.ownership_token,
                source_updated_at=now,
                raw_memory="phase2 raw memory",
                rollout_summary="phase2 summary",
                rollout_slug=None,
            )

            result = run_memory_phase2_once(
                volley_home=volley_home,
                state_store=store,
                base_config=VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=ScriptedResponsesModel([message("phase2 complete")]),
                max_unused_days=30,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.final_message, "phase2 complete")
            self.assertIn("phase2 raw memory", raw_memories_file(result.memory_root).read_text(encoding="utf-8"))
            self.assertFalse((result.memory_root / "phase2_workspace_diff.md").exists())
            job = store.get_job("memory_consolidate_global", "global")
            assert job is not None
            self.assertEqual(job["status"], "done")
            stored = store.get_stage1_output("phase2-thread")
            assert stored is not None
            self.assertEqual(stored["selected_for_phase2"], 1)
            self.assertEqual(
                store.try_claim_global_phase2_job(worker_id="worker", lease_seconds=60).outcome,
                "skipped_cooldown",
            )
            store.close()

    def test_memory_phase2_prunes_old_extension_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memories"
            extensions = memory_extensions_root(memory_root)
            resources = extensions / "chronicle" / "resources"
            resources.mkdir(parents=True)
            (extensions / "chronicle" / "instructions.md").write_text("instructions", encoding="utf-8")

            old_file = resources / "2026-04-06T11-59-59-abcd-10min-old.md"
            cutoff_file = resources / "2026-04-07T12-00-00-abcd-10min-cutoff.md"
            recent_file = resources / "2026-04-08T12-00-00-abcd-10min-recent.md"
            invalid_file = resources / "not-a-timestamp.md"
            for path in [old_file, cutoff_file, recent_file, invalid_file]:
                path.write_text("resource", encoding="utf-8")

            ignored_resources = extensions / "ignored" / "resources"
            ignored_resources.mkdir(parents=True)
            ignored_old_file = ignored_resources / "2026-04-06T11-59-59-abcd-10min-old.md"
            ignored_old_file.write_text("ignored", encoding="utf-8")

            now = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
            prune_old_extension_resources(memory_root, now=now)

            self.assertFalse(old_file.exists())
            self.assertFalse(cutoff_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(invalid_file.exists())
            self.assertTrue(ignored_old_file.exists())

    def test_memory_startup_seeds_ad_hoc_extension_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memories"
            instructions_path = memory_extensions_root(memory_root) / "ad_hoc" / "instructions.md"

            seed_extension_instructions(memory_root)
            self.assertIn("Ad-hoc notes", instructions_path.read_text(encoding="utf-8"))

            instructions_path.write_text("custom instructions", encoding="utf-8")
            seed_extension_instructions(memory_root)
            self.assertEqual(instructions_path.read_text(encoding="utf-8"), "custom instructions")

    def test_memory_phase2_workspace_sync_writes_inputs_and_prunes_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 5, 15, tzinfo=timezone.utc)
            memory_root = Path(tmp) / "memories"
            resources = memory_extensions_root(memory_root) / "ad_hoc" / "resources"
            resources.mkdir(parents=True)
            (resources.parent / "instructions.md").write_text("instructions", encoding="utf-8")
            stale_resource = resources / "2026-05-01T00-00-00-abcd-stale.md"
            stale_resource.write_text("old resource", encoding="utf-8")
            record = MemoryStageOneRecord(
                thread_id="sync-thread",
                source_updated_at=now,
                raw_memory="synced raw memory",
                rollout_summary="synced summary",
                rollout_slug=None,
                rollout_path=Path("/tmp/sync-thread.jsonl"),
                cwd=Path("/tmp/workspace"),
            )

            sync_phase2_workspace_inputs(
                memory_root,
                [record],
                1,
                max_unused_days=30,
                now=now,
            )

            self.assertIn("synced raw memory", raw_memories_file(memory_root).read_text(encoding="utf-8"))
            self.assertEqual(len(list(rollout_summaries_dir(memory_root).glob("*.md"))), 1)
            self.assertFalse(stale_resource.exists())

    def test_memory_workspace_diff_file_matches_upstream_render_shape(self) -> None:
        rendered = render_memory_workspace_diff_file(
            [MemoryWorkspaceChange(status="M", path="MEMORY.md")],
            "a" * (4 * 1024 * 1024 + 128),
        )
        self.assertIn("# Memory Workspace Diff", rendered)
        self.assertIn("- M MEMORY.md", rendered)
        self.assertIn("[workspace diff truncated at 4194304 bytes]", rendered)
        self.assertTrue(rendered.endswith("```\n"))

        with tempfile.TemporaryDirectory() as tmp:
            path = write_memory_workspace_diff(
                Path(tmp),
                [MemoryWorkspaceChange(status="A", path="raw_memories.md")],
                "+raw\n",
            )
            self.assertEqual(path.name, "phase2_workspace_diff.md")
            self.assertIn("- A raw_memories.md", path.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("git"), "git CLI is required for memory workspace baseline tests")
    def test_memory_workspace_git_baseline_diff_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memories"
            root.mkdir()
            (root / "MEMORY.md").write_text("baseline\n", encoding="utf-8")

            prepare_memory_workspace(root)
            changes, diff = memory_workspace_diff(root)
            self.assertEqual(changes, [])
            self.assertEqual(diff, "")

            (root / "MEMORY.md").write_text("changed\n", encoding="utf-8")
            (root / "raw_memories.md").write_text("new raw\n", encoding="utf-8")
            changes, diff = memory_workspace_diff(root)
            self.assertEqual([(change.status, change.path) for change in changes], [("M", "MEMORY.md"), ("A", "raw_memories.md")])
            self.assertIn("MEMORY.md", diff)
            self.assertIn("+new raw", diff)

            diff_path = write_current_memory_workspace_diff(root)
            self.assertEqual(diff_path.name, "phase2_workspace_diff.md")
            self.assertIn("- M MEMORY.md", diff_path.read_text(encoding="utf-8"))

            reset_memory_workspace_baseline(root)
            self.assertFalse(diff_path.exists())
            changes, diff = memory_workspace_diff(root)
            self.assertEqual(changes, [])
            self.assertEqual(diff, "")

    def test_memory_consolidation_session_uses_locked_down_agent_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memories"
            memory_root.mkdir()
            config = build_memory_consolidation_config(
                memory_root=memory_root,
                base_config=VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            )

            self.assertEqual(config.model, "gpt-5.4")
            self.assertEqual(config.cwd, memory_root)
            self.assertEqual(config.session_source, "internal_memory_consolidation")
            self.assertEqual(config.approval_policy, "never")
            self.assertEqual(config.network_access, "restricted")
            self.assertEqual(config.writable_roots, (memory_root,))
            self.assertTrue(config.ephemeral)
            self.assertFalse(config.use_memories)
            self.assertFalse(config.memory_tool_enabled)
            self.assertFalse(config.memory_generate_memories)
            self.assertFalse(config.include_multi_agent_tools)
            self.assertFalse(config.include_web_search_tool)
            self.assertFalse(config.include_request_user_input_tool)
            self.assertEqual(config.model_reasoning_effort, "medium")

            model = ScriptedResponsesModel([message("consolidated")])
            result = run_memory_consolidation_session(
                memory_root=memory_root,
                base_config=VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            )

            self.assertEqual(result.final_message, "consolidated")
            request = model.requests[0]
            self.assertEqual(request.model, "gpt-5.4")
            self.assertEqual(request.reasoning, {"effort": "medium"})
            self.assertEqual(request.verbosity, "low")
            request_text = "\n".join(part["text"] for item in request.input for part in item["content"])
            self.assertIn("Memory Writing Agent: Phase 2", request_text)
            tool_names = {tool.get("name", tool["type"]) for tool in request.tools}
            self.assertNotIn("web_search", tool_names)
            self.assertNotIn("spawn_agent", tool_names)

    def test_assistant_only_final_response(self) -> None:
        model = ScriptedResponsesModel([message("done")])
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        result = session.run("hello")
        self.assertEqual(result.final_message, "done")
        self.assertTrue(any(event.type == "turn.completed" for event in result.events))
        self.assertEqual(model.requests[0].prompt_cache_key, result.thread_id)
        self.assertEqual(model.requests[0].reasoning, {"effort": "xhigh"})
        self.assertEqual(model.requests[0].verbosity, "low")
        self.assertTrue(model.requests[0].parallel_tool_calls)

    def test_non_reasoning_model_does_not_default_reasoning(self) -> None:
        model = ScriptedResponsesModel([message("done")])
        session = VolleySession(
            VolleyConfig(model="gpt-4.1", skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        session.run("hello")
        self.assertIsNone(model.requests[0].reasoning)
        self.assertEqual(model.requests[0].include, [])

    def test_assistant_memory_citation_is_hidden_and_recorded(self) -> None:
        model = ScriptedResponsesModel(
            [
                message(
                    "visible answer<oai-mem-citation><citation_entries>\n"
                    "MEMORY.md:1-3|note=[answer source]\n"
                    "</citation_entries>\n<rollout_ids>\n019cc2ea-1dff-7902-8d40-c8f6e5d83cc4\n"
                    "</rollout_ids></oai-mem-citation>"
                )
            ]
        )
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("use memory")
        self.assertEqual(result.final_message, "visible answer")
        self.assertNotIn("oai-mem-citation", result.history[-1]["content"][0]["text"])
        self.assertEqual(result.memory_citations[0]["entries"][0]["path"], "MEMORY.md")

    def test_assistant_memory_citation_updates_state_store_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            thread_id = "019cc2ea-1dff-7902-8d40-c8f6e5d83cc4"
            now = datetime(2026, 5, 15, tzinfo=timezone.utc)
            store = MemoryStateStore(Path(tmp) / "memory.sqlite3")
            store.upsert_thread(
                MemoryThreadRecord(
                    thread_id=thread_id,
                    rollout_path=Path("/tmp/thread.jsonl"),
                    cwd=Path("/tmp/workspace"),
                    updated_at=now,
                )
            )
            claim = store.try_claim_stage1_job(
                thread_id=thread_id,
                worker_id="worker",
                source_updated_at=now,
                lease_seconds=60,
                max_running_jobs=4,
                now=now,
            )
            assert claim.ownership_token is not None
            store.mark_stage1_job_succeeded(
                thread_id=thread_id,
                ownership_token=claim.ownership_token,
                source_updated_at=now,
                raw_memory="memory",
                rollout_summary="summary",
                rollout_slug=None,
                now=now,
            )
            model = ScriptedResponsesModel(
                [
                    message(
                        "visible<oai-mem-citation><thread_ids>\n"
                        f"{thread_id}\n"
                        "</thread_ids></oai-mem-citation>"
                    )
                ]
            )

            result = VolleySession(
                VolleyConfig(skip_git_repo_check=True, ephemeral=True, memory_state_store=store),
                model_client=model,
            ).run("use memory")

            self.assertEqual(result.final_message, "visible")
            stored = store.get_stage1_output(thread_id)
            assert stored is not None
            self.assertEqual(stored["usage_count"], 1)
            self.assertIsNotNone(stored["last_usage"])
            store.close()

    def test_shell_tool_call_then_final_response(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": json.dumps({"cmd": "printf hi"}),
                        }
                    ]
                },
                message("saw tool output"),
            ]
        )
        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        )
        result = session.run("run a command")
        self.assertEqual(result.final_message, "saw tool output")
        self.assertTrue(any(item.get("type") == "function_call_output" for item in result.history))

    def test_long_running_exec_emits_output_delta_before_completion(self) -> None:
        command = (
            f"{shlex.quote(sys.executable)} -u -c "
            "\"import time; print('stream-start', flush=True); time.sleep(0.2); print('stream-end', flush=True)\""
        )
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": json.dumps({"cmd": command, "yield_time_ms": 1000}),
                        }
                    ]
                },
                message("done"),
            ]
        )

        events = list(
            VolleySession(
                VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).stream("run a streaming command")
        )

        delta_indices = [
            index
            for index, event in enumerate(events)
            if event.type == "exec_command.output_delta"
            and "stream-start" in str(event.payload.get("delta") or "")
        ]
        self.assertTrue(delta_indices, [event.type for event in events])
        started_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "tool.started" and event.payload.get("call_id") == "call-1"
        )
        completed_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "tool.completed" and event.payload.get("call_id") == "call-1"
        )
        self.assertLess(started_index, delta_indices[0])
        self.assertLess(delta_indices[0], completed_index)

    def test_tool_runtime_output_delta_is_drained_after_fast_future_completion(self) -> None:
        class ImmediateDeltaRuntime:
            def __init__(self) -> None:
                self.events: list[dict[str, Any]] = []

            def specs(self) -> list[dict[str, Any]]:
                return [{"type": "function", "name": "exec_command"}]

            def supports_parallel(self, name: str) -> bool:
                return False

            def dispatch(self, name: str, arguments: Any, *, call_id: str | None = None) -> ToolResult:
                self.events.append(
                    {
                        "type": "exec_command.output_delta",
                        "payload": {"call_id": call_id or "", "delta": "fast output\n", "stream": "stdout"},
                    }
                )
                return ToolResult(
                    True,
                    "fast output\n",
                    {"command": "printf fast", "exit_code": 0, "aggregated_output": "fast output\n"},
                )

            def drain_runtime_events(self) -> list[dict[str, Any]]:
                events = list(self.events)
                self.events.clear()
                return events

        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-fast",
                            "arguments": json.dumps({"cmd": "printf fast"}),
                        }
                    ]
                },
                message("done"),
            ]
        )
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=model)
        session.tools = ImmediateDeltaRuntime()  # type: ignore[assignment]

        events = list(session.stream("run a fast command"))
        delta_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "exec_command.output_delta" and event.payload.get("call_id") == "call-fast"
        )
        completed_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "tool.completed" and event.payload.get("call_id") == "call-fast"
        )
        self.assertLess(delta_index, completed_index)
        self.assertEqual(events[delta_index].payload.get("delta"), "fast output\n")

    def test_parallel_tool_calls_dispatch_concurrently_and_drain_in_order(self) -> None:
        class SleepingToolRuntime:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def specs(self) -> list[dict[str, Any]]:
                return [{"type": "function", "name": "slow_a"}, {"type": "function", "name": "slow_b"}]

            def supports_parallel(self, name: str) -> bool:
                return name in {"slow_a", "slow_b"}

            def dispatch(self, name: str, arguments: Any, *, call_id: str | None = None) -> ToolResult:
                self.calls.append(name)
                time.sleep(0.35)
                return ToolResult(True, name, {"name": name, "call_id": call_id})

        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {"type": "function_call", "name": "slow_a", "call_id": "call-a", "arguments": "{}"},
                        {"type": "function_call", "name": "slow_b", "call_id": "call-b", "arguments": "{}"},
                    ]
                },
                message("done"),
            ]
        )
        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True), model_client=model)
        session.tools = SleepingToolRuntime()  # type: ignore[assignment]

        started = time.monotonic()
        result = session.run("run both")
        elapsed = time.monotonic() - started

        self.assertEqual(result.final_message, "done")
        self.assertLess(elapsed, 0.65)
        output_items = [item for item in result.history if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in output_items[-2:]], ["call-a", "call-b"])
        first_completed = next(index for index, event in enumerate(result.events) if event.type == "tool.completed")
        started_before_completion = [event for event in result.events[:first_completed] if event.type == "tool.started"]
        self.assertEqual([event.payload["call_id"] for event in started_before_completion[-2:]], ["call-a", "call-b"])

    def test_parallel_background_exec_sessions_keep_agent_loop_state_isolated(self) -> None:
        command_a = "printf 'A-start\\n'; sleep 0.7; printf 'A-end\\n'"
        command_b = "printf 'B-start\\n'; sleep 0.7; printf 'B-end\\n'"

        class ParallelBackgroundModel:
            def __init__(self) -> None:
                self.requests: list[PromptRequest] = []
                self.session_ids: dict[str, int] = {}
                self.poll_outputs: dict[str, str] = {}

            def create(self, request: PromptRequest):
                self.requests.append(request)
                outputs = {
                    str(item.get("call_id")): str(item.get("output") or "")
                    for item in request.input
                    if item.get("type") == "function_call_output"
                }
                if len(self.requests) == 1:
                    return _model_response(
                        {
                            "output": [
                                {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "call_id": "exec-a",
                                    "arguments": json.dumps(
                                        {
                                            "cmd": command_a,
                                            "yield_time_ms": 250,
                                            "shell": "/bin/sh",
                                            "login": False,
                                        }
                                    ),
                                },
                                {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "call_id": "exec-b",
                                    "arguments": json.dumps(
                                        {
                                            "cmd": command_b,
                                            "yield_time_ms": 250,
                                            "shell": "/bin/sh",
                                            "login": False,
                                        }
                                    ),
                                },
                            ]
                        }
                    )
                if len(self.requests) == 2:
                    for call_id in ("exec-a", "exec-b"):
                        match = re.search(r"Process running with session ID (\d+)", outputs.get(call_id, ""))
                        if match is None:
                            raise AssertionError(f"{call_id} did not return a background session: {outputs!r}")
                        self.session_ids[call_id] = int(match.group(1))
                    if self.session_ids["exec-a"] == self.session_ids["exec-b"]:
                        raise AssertionError(f"parallel exec sessions were aliased: {self.session_ids!r}")
                    return _model_response(
                        {
                            "output": [
                                {
                                    "type": "function_call",
                                    "name": "write_stdin",
                                    "call_id": "poll-b",
                                    "arguments": json.dumps(
                                        {"session_id": self.session_ids["exec-b"], "yield_time_ms": 2000}
                                    ),
                                },
                                {
                                    "type": "function_call",
                                    "name": "write_stdin",
                                    "call_id": "poll-a",
                                    "arguments": json.dumps(
                                        {"session_id": self.session_ids["exec-a"], "yield_time_ms": 2000}
                                    ),
                                },
                            ]
                        }
                    )
                if len(self.requests) == 3:
                    self.poll_outputs = outputs
                    return _model_response(message("done"))
                raise AssertionError("unexpected extra model request")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ParallelBackgroundModel()
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("run parallel background commands")

            self.assertEqual(result.final_message, "done")
            self.assertIn("A-end", model.poll_outputs["poll-a"])
            self.assertNotIn("B-end", model.poll_outputs["poll-a"])
            self.assertIn("B-end", model.poll_outputs["poll-b"])
            self.assertNotIn("A-end", model.poll_outputs["poll-b"])

            completed = {
                event.payload["call_id"]: event.payload
                for event in result.events
                if event.type == "tool.completed"
            }
            self.assertEqual(completed["poll-a"]["metadata"]["event_call_id"], "exec-a")
            self.assertEqual(completed["poll-b"]["metadata"]["event_call_id"], "exec-b")
            self.assertEqual(completed["poll-a"]["metadata"]["command"], command_a)
            self.assertEqual(completed["poll-b"]["metadata"]["command"], command_b)

            deltas = [event.payload for event in result.events if event.type == "exec_command.output_delta"]
            self.assertTrue(any("A-start" in str(delta.get("delta") or "") for delta in deltas))
            self.assertTrue(any("B-start" in str(delta.get("delta") or "") for delta in deltas))
            for delta in deltas:
                text = str(delta.get("delta") or "")
                if "A-" in text:
                    self.assertEqual(delta.get("call_id"), "exec-a")
                if "B-" in text:
                    self.assertEqual(delta.get("call_id"), "exec-b")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            begin_call_ids = [payload["call_id"] for payload in event_msgs if payload["type"] == "exec_command_begin"]
            self.assertEqual(begin_call_ids[-2:], ["exec-a", "exec-b"])
            end_by_call = {
                payload["call_id"]: payload
                for payload in event_msgs
                if payload["type"] == "exec_command_end"
            }
            self.assertIn("exec-a", end_by_call)
            self.assertIn("exec-b", end_by_call)
            self.assertNotIn("poll-a", end_by_call)
            self.assertNotIn("poll-b", end_by_call)
            self.assertEqual(end_by_call["exec-a"]["process_id"], str(model.session_ids["exec-a"]))
            self.assertEqual(end_by_call["exec-b"]["process_id"], str(model.session_ids["exec-b"]))
            self.assertIn("A-start", end_by_call["exec-a"]["aggregated_output"])
            self.assertIn("A-end", end_by_call["exec-a"]["aggregated_output"])
            self.assertNotIn("B-end", end_by_call["exec-a"]["aggregated_output"])
            self.assertIn("B-start", end_by_call["exec-b"]["aggregated_output"])
            self.assertIn("B-end", end_by_call["exec-b"]["aggregated_output"])
            self.assertNotIn("A-end", end_by_call["exec-b"]["aggregated_output"])

    def test_session_interrupt_notifies_tools_before_slow_model_cancel(self) -> None:
        class SlowCancelModel(ScriptedResponsesModel):
            def cancel(self, *_args: Any) -> None:
                time.sleep(1.0)

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=SlowCancelModel([]),
        )
        called = threading.Event()

        def request_interrupt() -> None:
            called.set()

        session.tools.request_interrupt = request_interrupt  # type: ignore[method-assign]

        started = time.monotonic()
        session.interrupt()

        self.assertTrue(called.wait(timeout=0.1), "tool interrupt was blocked behind model cancellation")
        self.assertLess(time.monotonic() - started, 0.2)

    def test_stream_events_are_covered_by_lifecycle_catalog(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call-1",
                            "arguments": json.dumps({"cmd": "printf hi"}),
                        }
                    ]
                },
                message("done"),
            ]
        )
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("run a command")
        event_types = {event.type for event in result.events}
        self.assertTrue(event_types <= KNOWN_EVENT_TYPES, sorted(event_types - KNOWN_EVENT_TYPES))
        self.assertTrue(event_types & TERMINAL_TURN_EVENT_TYPES)

    def test_persistent_rollout_uses_upstream_jsonl_item_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel([message("persisted answer")])
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                    model_auto_compact_token_limit=100_000,
                ),
                model_client=model,
            ).run("persist this")

            matches = list((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            self.assertEqual(len(matches), 1)
            rollout_path = matches[0]
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            record_types = [record["type"] for record in records]

            self.assertEqual(record_types[0], "session_meta")
            self.assertRegex(
                str(rollout_path.relative_to(volley_home)),
                rf"^sessions/\d{{4}}/\d{{2}}/\d{{2}}/rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-{result.thread_id}\.jsonl$",
            )
            self.assertTrue(set(record_types) <= VOLLEY_ROLLOUT_ITEM_TYPES)
            self.assertIn("turn_context", record_types)
            self.assertIn("response_item", record_types)
            self.assertIn("event_msg", record_types)
            self.assertNotIn("item.completed", record_types)
            session_meta = records[0]["payload"]["meta"]
            self.assertEqual(session_meta["id"], result.thread_id)
            self.assertEqual(session_meta["source"], "cli")
            self.assertEqual(session_meta["memory_mode"], "disabled")
            self.assertIn("base_instructions", session_meta)
            self.assertIn("You are Codex", session_meta["base_instructions"]["text"])
            turn_context = next(record["payload"] for record in records if record["type"] == "turn_context")
            self.assertEqual(turn_context["approval_policy"], "never")
            self.assertNotIn("collaboration_mode", turn_context)
            self.assertEqual(turn_context["summary"], "none")
            self.assertFalse(turn_context["realtime_active"])
            self.assertEqual(turn_context["sandbox_policy"]["writable_roots"], [])
            self.assertEqual(turn_context["permission_profile"]["type"], "managed")
            self.assertEqual(turn_context["permission_profile"]["network"], "restricted")
            self.assertEqual(turn_context["file_system_sandbox_policy"]["kind"], "restricted")
            self.assertEqual(turn_context["truncation_policy"], {"mode": "tokens", "limit": 100_000})

            event_msg_types = [record["payload"]["type"] for record in records if record["type"] == "event_msg"]
            self.assertIn("task_started", event_msg_types)
            turn_started = next(
                record["payload"] for record in records
                if record["type"] == "event_msg" and record["payload"]["type"] == "task_started"
            )
            self.assertEqual(turn_started["collaboration_mode_kind"], "default")
            self.assertIn("user_message", event_msg_types)
            self.assertIn("agent_message", event_msg_types)
            self.assertIn("task_complete", event_msg_types)

            rollout = load_memory_rollout(rollout_path)
            serialized = json.loads(rollout.serialized_contents)
            self.assertEqual([item["role"] for item in serialized], ["user", "user", "assistant"])
            self.assertIn("<environment_context>", serialized[0]["content"][0]["text"])
            self.assertEqual(rollout.thread_id, result.thread_id)
            reconstructed = reconstruct_history_from_rollout(rollout_path)
            self.assertEqual([item["role"] for item in reconstructed.history], ["developer", "user", "user", "assistant"])
            self.assertEqual(reconstructed.session_meta["id"], result.thread_id)
            self.assertEqual(reconstructed.previous_turn_settings["model"], VolleyConfig().model)

    def test_persistent_rollout_records_token_count_event_msg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            response = message("counted answer")
            response["usage"] = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}

            result = VolleySession(
                VolleyConfig(cwd=root, volley_home=volley_home, skip_git_repo_check=True, ephemeral=False),
                model_client=ScriptedResponsesModel([response]),
            ).run("persist token count")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            token_count = next(payload for payload in event_msgs if payload["type"] == "token_count")
            self.assertEqual(token_count["info"]["total_token_usage"], 3)
            self.assertEqual(token_count["info"]["last_token_usage"]["total_tokens"], 3)

    def test_resume_from_rollout_restores_token_usage_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            response = message("counted answer")
            response["usage"] = {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
                "output_tokens_details": {"reasoning_tokens": 7},
            }

            first = VolleySession(
                VolleyConfig(cwd=root, volley_home=volley_home, skip_git_repo_check=True, ephemeral=False),
                model_client=ScriptedResponsesModel([response]),
            ).run("persist token count")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{first.thread_id}.jsonl"))
            resumed = VolleySession.resume_from_rollout(
                rollout_path,
                VolleyConfig(cwd=root, volley_home=volley_home, skip_git_repo_check=True, ephemeral=False),
                model_client=ScriptedResponsesModel([]),
            )

            self.assertEqual(resumed.state.last_token_usage["total_tokens"], 100)
            self.assertEqual(resumed.state.session_usage_tokens(), 100)
            self.assertEqual(resumed.state.session_reasoning_usage_tokens(), 7)
            self.assertEqual(resumed.state.active_context_token_status(), (100, False))
            self.assertEqual(resumed.state.session_context_token_status(), (100, False))

    def test_resume_from_compacted_rollout_restores_context_carryover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(cwd=root, volley_home=volley_home, skip_git_repo_check=True, ephemeral=False)
            session = VolleySession(config, model_client=ScriptedResponsesModel([]))
            user_item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]}
            assistant_item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "work"}]}

            session.state.emit("thread.started")
            session.state.append_history(user_item)
            session.state.emit("item.completed", item=user_item)
            session.state.append_history(assistant_item)
            session.state.emit("item.completed", item=assistant_item)
            session.state.record_token_usage({"input_tokens": 80, "output_tokens": 20, "total_tokens": 100})
            session.state.emit("token_count", info=session.state.token_usage_info())
            pre_compact_context, pre_compact_estimated = session.state.session_context_token_status()
            session.state.compact_with_summary("handoff summary")
            session.state.recompute_token_usage_from_history()
            session.state.start_new_context_epoch(pre_compact_context, estimated=pre_compact_estimated)
            session.state.emit(
                "context_compaction.completed",
                summary="handoff summary",
                replacement_history=list(session.state.history),
                active_context_tokens=session.state.active_context_tokens(),
            )

            rollout_path = session.state.rollout_path()
            resumed = VolleySession.resume_from_rollout(
                rollout_path,
                config,
                model_client=ScriptedResponsesModel([]),
            )

            self.assertEqual(resumed.state.context_carryover_tokens, 100)
            self.assertFalse(resumed.state.context_carryover_estimated)
            self.assertTrue(resumed.state.last_token_usage["estimated"])
            active_context, active_estimated = resumed.state.active_context_token_status()
            session_context, session_estimated = resumed.state.session_context_token_status()
            self.assertGreater(active_context, 0)
            self.assertTrue(active_estimated)
            self.assertEqual(session_context, 100 + active_context)
            self.assertTrue(session_estimated)

    def test_resume_and_fork_from_rollout_seed_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            first_model = ScriptedResponsesModel([message("first answer")])
            first = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=first_model,
            ).run("first")
            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{first.thread_id}.jsonl"))

            resumed = VolleySession.resume_from_rollout(
                rollout_path,
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=ScriptedResponsesModel([message("second answer")]),
            )
            resumed_result = resumed.run("second")
            self.assertEqual(resumed_result.thread_id, first.thread_id)
            self.assertEqual(resumed_result.final_message, "second answer")
            resumed_records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(1 for record in resumed_records if record["type"] == "session_meta"), 1)
            self.assertEqual(
                [item["content"][0]["text"] for item in resumed_result.history if item.get("role") == "assistant"],
                ["first answer", "second answer"],
            )

            forked = VolleySession.fork_from_rollout(
                rollout_path,
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=ScriptedResponsesModel([message("fork answer")]),
            )
            forked_result = forked.run("fork prompt")
            self.assertNotEqual(forked_result.thread_id, first.thread_id)
            fork_rollout = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{forked_result.thread_id}.jsonl"))
            fork_records = [json.loads(line) for line in fork_rollout.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(fork_records[0]["payload"]["meta"]["forked_from_id"], first.thread_id)
            self.assertEqual(forked_result.final_message, "fork answer")

    def test_persistent_rollout_records_exec_command_event_msgs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "call-1",
                                "arguments": json.dumps({"cmd": "printf hi"}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("run command")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            begin = next(payload for payload in event_msgs if payload["type"] == "exec_command_begin")
            end = next(payload for payload in event_msgs if payload["type"] == "exec_command_end")

            self.assertEqual(begin["call_id"], "call-1")
            self.assertEqual(begin["turn_id"], result.turn_id)
            self.assertEqual(begin["command"], ["printf hi"])
            self.assertEqual(begin["cwd"], str(root.resolve()))
            self.assertEqual(begin["parsed_cmd"], [{"type": "unknown", "cmd": "printf hi"}])
            self.assertEqual(begin["source"], "agent")
            self.assertEqual(end["status"], "completed")
            self.assertEqual(end["exit_code"], 0)
            self.assertIn("hi", end["stdout"])
            self.assertIn("hi", end["aggregated_output"])
            self.assertEqual(set(end["duration"]), {"secs", "nanos"})

    def test_persistent_rollout_records_write_stdin_terminal_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            command = (
                f"{sys.executable!r} -c "
                "\"import sys; print('ready', flush=True); print(sys.stdin.readline().strip(), flush=True)\""
            )
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "exec-1",
                                "arguments": json.dumps({"cmd": command, "tty": True, "yield_time_ms": 250}),
                            }
                        ]
                    },
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "write_stdin",
                                "call_id": "stdin-1",
                                "arguments": json.dumps({"session_id": 1, "chars": "hello\n", "yield_time_ms": 1500}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("run interactive command")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            interaction = next(payload for payload in event_msgs if payload["type"] == "terminal_interaction")
            end = [payload for payload in event_msgs if payload["type"] == "exec_command_end"][-1]

            self.assertEqual(interaction["call_id"], "exec-1")
            self.assertEqual(interaction["process_id"], "1")
            self.assertEqual(interaction["stdin"], "hello\n")
            self.assertEqual(end["call_id"], "exec-1")
            self.assertEqual(end["interaction_input"], "hello\n")
            self.assertIn("ready", end["aggregated_output"])
            self.assertIn("hello", end["aggregated_output"])

    def test_persistent_rollout_omits_empty_terminal_interaction_when_background_poll_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from volley import state as codex_state_module

            root = Path(tmp)
            state = codex_state_module.VolleyState(
                VolleyConfig(cwd=root, volley_home=root / "volley-home", skip_git_repo_check=True)
            )
            state._tool_arguments_by_call["poll-1"] = {"session_id": 1, "chars": ""}
            records = codex_state_module._write_stdin_event_payload(
                state,
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "ok": True,
                        "output": "done\n",
                        "metadata": {
                            "event_call_id": "exec-1",
                            "exit_code": 0,
                            "command": "sleep 0.1 && echo done",
                            "workdir": str(root),
                            "aggregated_output": "done\n",
                        },
                    },
                ),
            )
            assert records is not None
            self.assertFalse(
                [payload for payload in records if payload["type"] == "terminal_interaction"],
                "empty background polls that observe process completion should not persist TerminalInteraction",
            )
            end = [payload for payload in records if payload["type"] == "exec_command_end"][-1]
            self.assertEqual(end["call_id"], "exec-1")
            self.assertEqual(end["exit_code"], 0)
            self.assertIn("done", end["aggregated_output"])

    def test_persistent_rollout_records_update_plan_event_msg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            plan = [
                {"step": "Inspect", "status": "completed"},
                {"step": "Patch", "status": "in_progress"},
            ]
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "update_plan",
                                "call_id": "plan-1",
                                "arguments": json.dumps({"explanation": "Working", "plan": plan}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("make a plan")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            plan_update = next(payload for payload in event_msgs if payload["type"] == "plan_update")

            self.assertEqual(plan_update["explanation"], "Working")
            self.assertEqual(plan_update["plan"], plan)

    def test_persistent_rollout_records_view_image_event_msg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "image.png"
            image_path.write_bytes(b"not really a png, but enough for the file handler")
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "view_image",
                                "call_id": "view-1",
                                "arguments": json.dumps({"path": "image.png"}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("view image")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            image_event = next(payload for payload in event_msgs if payload["type"] == "view_image_tool_call")

            self.assertEqual(image_event["call_id"], "view-1")
            self.assertEqual(image_event["path"], str(image_path.resolve()))

    def test_persistent_rollout_records_request_user_input_event_msg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            questions = [
                {
                    "id": "choice",
                    "header": "Choice",
                    "question": "Pick one.",
                    "options": [{"label": "A (Recommended)", "description": "Choose A."}],
                }
            ]
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "request_user_input",
                                "call_id": "input-1",
                                "arguments": json.dumps({"questions": questions}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                    collaboration_mode="Plan",
                    request_user_input_answers={"choice": {"answers": ["A"]}},
                ),
                model_client=model,
            ).run("ask")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            input_event = next(payload for payload in event_msgs if payload["type"] == "request_user_input")

            self.assertEqual(input_event["call_id"], "input-1")
            self.assertEqual(input_event["turn_id"], result.turn_id)
            self.assertEqual(input_event["questions"][0]["id"], "choice")
            self.assertTrue(input_event["questions"][0]["isOther"])

    def test_persistent_rollout_records_apply_patch_event_msgs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("before\n", encoding="utf-8")
            volley_home = root / "volley-home"
            patch = """diff --git a/file.txt b/file.txt
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-before
+after
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "custom_tool_call",
                                "name": "apply_patch",
                                "call_id": "patch-1",
                                "input": patch,
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("patch")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            begin = next(payload for payload in event_msgs if payload["type"] == "patch_apply_begin")
            end = next(payload for payload in event_msgs if payload["type"] == "patch_apply_end")
            changes = {
                "file.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-before\n+after\n",
                    "move_path": None,
                }
            }

            self.assertEqual((root / "file.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual(begin["call_id"], "patch-1")
            self.assertEqual(begin["turn_id"], result.turn_id)
            self.assertTrue(begin["auto_approved"])
            self.assertEqual(begin["changes"], changes)
            self.assertEqual(end["status"], "completed")
            self.assertTrue(end["success"])
            self.assertEqual(end["changes"], changes)

    def test_apply_patch_emits_turn_diff_for_committed_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("before\n", encoding="utf-8")
            patch = """*** Begin Patch
*** Update File: file.txt
@@
-before
+after
*** End Patch
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "apply_patch",
                                "call_id": "patch-1",
                                "arguments": patch,
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).run("patch")

            turn_diffs = [event.payload["unified_diff"] for event in result.events if event.type == "turn_diff"]
            self.assertEqual(len(turn_diffs), 1)
            self.assertIn("diff --git a/file.txt b/file.txt", turn_diffs[0])
            self.assertIn("-before", turn_diffs[0])
            self.assertIn("+after", turn_diffs[0])

    def test_apply_patch_verification_failure_does_not_emit_committed_turn_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            patch = """*** Begin Patch
*** Add File: created.txt
+hello
*** Update File: missing.txt
@@
-old
+new
*** End Patch
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "apply_patch",
                                "call_id": "patch-1",
                                "arguments": patch,
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("patch")

            self.assertFalse((root / "created.txt").exists())
            completed = next(event for event in result.events if event.type == "tool.completed")
            self.assertFalse(completed.payload["ok"])
            self.assertIn("Failed to read file to update", completed.payload["output"])
            self.assertNotIn("partial_failure", completed.payload["metadata"])
            self.assertFalse(any(event.type == "turn_diff" for event in result.events))

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            self.assertFalse(any(payload["type"] == "patch_apply_begin" for payload in event_msgs))
            self.assertFalse(any(payload["type"] == "patch_apply_end" for payload in event_msgs))
            self.assertFalse(any(payload["type"] == "turn_diff" for payload in event_msgs))

    def test_exec_command_apply_patch_heredoc_is_normalized_to_apply_patch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = """apply_patch <<'PATCH'
*** Begin Patch
*** Add File: created.txt
+hello
*** End Patch
PATCH
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "patch-1",
                                "arguments": json.dumps({"cmd": command}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).run("patch through shell")

            self.assertEqual((root / "created.txt").read_text(encoding="utf-8"), "hello\n")
            started = [event for event in result.events if event.type == "tool.started"]
            self.assertEqual([event.payload["name"] for event in started], ["apply_patch"])
            output_items = [item for item in result.history if item.get("type") == "function_call_output"]
            self.assertEqual(output_items[0]["call_id"], "patch-1")
            self.assertIn("Success. Updated the following files:", output_items[0]["output"])
            self.assertTrue(any(event.type == "turn_diff" for event in result.events))

    def test_exec_command_apply_patch_heredoc_with_cd_uses_effective_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            command = """cd sub && apply_patch <<'PATCH'
*** Begin Patch
*** Add File: nested.txt
+inside
*** End Patch
PATCH
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "patch-1",
                                "arguments": json.dumps({"cmd": command}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).run("patch in subdir")

            self.assertEqual((root / "sub" / "nested.txt").read_text(encoding="utf-8"), "inside\n")
            begin = next(event for event in result.events if event.type == "tool.started")
            self.assertEqual(begin.payload["name"], "apply_patch")
            self.assertEqual(begin.payload["arguments"]["workdir"], str((root / "sub").resolve()))
            turn_diff = next(event.payload["unified_diff"] for event in result.events if event.type == "turn_diff")
            self.assertIn("diff --git a/nested.txt b/nested.txt", turn_diff)

    def test_exec_command_apply_patch_heredoc_requires_supported_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = """apply_patch <<'PATCH'
*** Begin Patch
*** Add File: created.txt
+hello
*** End Patch
PATCH
"""
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": "patch-1",
                                "arguments": json.dumps({"cmd": command, "shell": sys.executable}),
                            }
                        ]
                    },
                    message("done"),
                ]
            )
            result = VolleySession(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).run("patch through unsupported shell")

            self.assertFalse((root / "created.txt").exists())
            started = [event for event in result.events if event.type == "tool.started"]
            self.assertEqual([event.payload["name"] for event in started], ["exec_command"])
            self.assertFalse(any(event.type == "turn_diff" for event in result.events))

    def test_search_files_is_not_a_default_model_visible_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.txt").write_text("alpha\nneedle here\n", encoding="utf-8")
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "search_files",
                                "call_id": "search-1",
                                "arguments": json.dumps({"query": "needle", "path": "."}),
                            }
                        ]
                    },
                    message("found needle"),
                ]
            )
            result = VolleySession(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True),
                model_client=model,
            ).run("search")
            outputs = [item for item in result.history if item.get("type") == "function_call_output"]
            self.assertEqual(result.final_message, "found needle")
            self.assertIn("unknown tool: search_files", outputs[0]["output"])

    def test_apply_patch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("before\n", encoding="utf-8")
            patch = """diff --git a/file.txt b/file.txt
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-before
+after
"""
            runtime = ToolRuntime(VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True))
            result = runtime.apply_patch(patch)
            self.assertTrue(result.ok, result.output)
            self.assertEqual((root / "file.txt").read_text(encoding="utf-8"), "after\n")

    def test_unified_exec_output_truncation_matches_upstream_head_tail_shape(self) -> None:
        text = "this is an example of a long output that should be truncated"
        output, original_tokens = codex_tools._truncate_output(text, 5)

        self.assertEqual(original_tokens, 15)
        self.assertEqual(
            output,
            "Total output lines: 1\n\nthis is an…10 tokens truncated… truncated",
        )

        multiline = "this is an example of a long output that should be truncated\nalso some other line"
        output, original_tokens = codex_tools._truncate_output(multiline, 10)

        self.assertEqual(original_tokens, 21)
        self.assertEqual(
            output,
            "Total output lines: 2\n\nthis is an example o…11 tokens truncated…also some other line",
        )

    def test_unified_exec_head_tail_buffer_matches_upstream_cap_shape(self) -> None:
        buffer = codex_tools._HeadTailTextBuffer(max_bytes=10)
        buffer.append("0123456789")
        buffer.append("abcdefghij")

        self.assertEqual(buffer.to_text(), "01234fghij")

        buffer.clear()
        self.assertEqual(buffer.to_text(), "")

    def test_write_stdin_empty_poll_uses_upstream_background_timeout_cap(self) -> None:
        self.assertEqual(codex_tools._write_stdin_yield_time(250, ""), 5000)
        self.assertEqual(codex_tools._write_stdin_yield_time(600000, ""), 300000)
        self.assertEqual(codex_tools._write_stdin_yield_time(600000, "input\n"), 30000)

    def test_apply_patch_tool_supports_codex_freeform_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("before\n", encoding="utf-8")
            patch = """*** Begin Patch
*** Update File: file.txt
@@
-before
+after
*** Add File: added.txt
+new file
*** End Patch
"""
            runtime = ToolRuntime(VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True))
            result = runtime.apply_patch(patch)

            self.assertTrue(result.ok, result.output)
            self.assertIn("Success. Updated the following files:", result.output)
            self.assertEqual((root / "file.txt").read_text(encoding="utf-8"), "after\n")
            self.assertEqual((root / "added.txt").read_text(encoding="utf-8"), "new file\n")

    def test_apply_patch_freeform_matches_upstream_overwrite_move_and_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "duplicate.txt").write_text("old content\n", encoding="utf-8")
            runtime = ToolRuntime(VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True))
            overwrite = runtime.apply_patch(
                "*** Begin Patch\n*** Add File: duplicate.txt\n+new content\n*** End Patch"
            )
            self.assertTrue(overwrite.ok, overwrite.output)
            self.assertEqual(overwrite.output, "Success. Updated the following files:\nA duplicate.txt\n")
            self.assertEqual((root / "duplicate.txt").read_text(encoding="utf-8"), "new content\n")

            (root / "old").mkdir()
            (root / "renamed").mkdir()
            (root / "old" / "name.txt").write_text("from\n", encoding="utf-8")
            (root / "renamed" / "name.txt").write_text("existing\n", encoding="utf-8")
            moved = runtime.apply_patch(
                "*** Begin Patch\n*** Update File: old/name.txt\n*** Move to: renamed/name.txt\n@@\n-from\n+new\n*** End Patch"
            )
            self.assertTrue(moved.ok, moved.output)
            self.assertEqual(moved.output, "Success. Updated the following files:\nM renamed/name.txt\n")
            self.assertFalse((root / "old" / "name.txt").exists())
            self.assertEqual((root / "renamed" / "name.txt").read_text(encoding="utf-8"), "new\n")

            verification_failure = runtime.apply_patch(
                "*** Begin Patch\n*** Add File: created.txt\n+hello\n*** Update File: missing.txt\n@@\n-old\n+new\n*** End Patch"
            )
            self.assertFalse(verification_failure.ok)
            self.assertFalse((root / "created.txt").exists())
            self.assertIn("Failed to read file to update", verification_failure.output)
            self.assertNotIn("partial_failure", verification_failure.metadata)

            empty = runtime.apply_patch("*** Begin Patch\n*** Update File: duplicate.txt\n*** End Patch")
            self.assertFalse(empty.ok)
            self.assertIn("Update file hunk for path 'duplicate.txt' is empty", empty.output)

    def test_apply_patch_freeform_matches_upstream_fixture_scenarios(self) -> None:
        upstream_env = os.environ.get("CODEX_UPSTREAM_DIR")
        if upstream_env:
            upstream_root = Path(upstream_env).expanduser()
        else:
            upstream_root = (
                Path(__file__).resolve().parents[1]
                / "agents"
                / "volley"
                / "upstream"
                / "openai-volley"
            )
        scenarios = upstream_root / "volley-rs" / "apply-patch" / "tests" / "fixtures" / "scenarios"
        if not scenarios.exists():
            self.skipTest(f"upstream apply-patch fixtures not found: {scenarios}")
        expected_failures = {
            "005_rejects_empty_patch",
            "006_rejects_missing_context",
            "007_rejects_missing_file_delete",
            "008_rejects_empty_update_hunk",
            "009_requires_existing_file_for_update",
            "012_delete_directory_fails",
            "013_rejects_invalid_hunk_header",
        }

        def snapshot(root: Path) -> dict[str, bytes]:
            if not root.exists():
                return {}
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        for scenario in sorted(path for path in scenarios.iterdir() if path.is_dir()):
            if scenario.name == "015_failure_after_partial_success_leaves_changes":
                continue
            with self.subTest(scenario=scenario.name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                input_dir = scenario / "input"
                if input_dir.exists():
                    shutil.copytree(input_dir, root, dirs_exist_ok=True)
                runtime = ToolRuntime(VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True))
                result = runtime.apply_patch((scenario / "patch.txt").read_text(encoding="utf-8"))

                if scenario.name in expected_failures:
                    self.assertFalse(result.ok, result.output)
                else:
                    self.assertTrue(result.ok, result.output)
                self.assertEqual(snapshot(root), snapshot(scenario / "expected"), result.output)

    def test_apply_patch_freeform_accepts_upstream_lenient_heredoc_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("before\n", encoding="utf-8")
            runtime = ToolRuntime(VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True))
            result = runtime.apply_patch(
                "<<'EOF'\n"
                "*** Begin Patch\n"
                "*** Update File: file.txt\n"
                "@@\n"
                "-before\n"
                "+after\n"
                "*** End Patch\n"
                "EOF\n"
            )

            self.assertTrue(result.ok, result.output)
            self.assertEqual((root / "file.txt").read_text(encoding="utf-8"), "after\n")

    def test_apply_patch_denies_workspace_write_escape_for_git_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            patch = """diff --git a/../outside.txt b/../outside.txt
new file mode 100644
--- /dev/null
+++ b/../outside.txt
@@ -0,0 +1 @@
+outside
"""
            runtime = ToolRuntime(
                VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True, sandbox="workspace-write")
            )
            result = runtime.apply_patch(patch)

            self.assertFalse(result.ok)
            self.assertIn("path escapes writable workspace", result.output)
            self.assertFalse(outside.exists())
            self.assertTrue(result.metadata["denied"])

    def test_apply_patch_allows_configured_writable_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            allowed = Path(tmp) / "allowed"
            root.mkdir()
            allowed.mkdir()
            runtime = ToolRuntime(
                VolleyConfig(
                    cwd=root,
                    writable_roots=(allowed,),
                    skip_git_repo_check=True,
                    ephemeral=True,
                    sandbox="workspace-write",
                )
            )
            result = runtime.apply_patch(
                "*** Begin Patch\n*** Add File: ../allowed/created.txt\n+ok\n*** End Patch"
            )

            self.assertTrue(result.ok, result.output)
            self.assertEqual((allowed / "created.txt").read_text(encoding="utf-8"), "ok\n")

    def test_apply_patch_requests_approval_for_workspace_write_escape_and_caches_session_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            approvals: list[dict] = []

            def approve(request: dict) -> dict:
                approvals.append(request)
                return {"approved": True, "approved_for_session": True}

            runtime = ToolRuntime(
                VolleyConfig(
                    cwd=root,
                    skip_git_repo_check=True,
                    ephemeral=True,
                    sandbox="workspace-write",
                    approval_policy="on-request",
                    approval_provider=approve,
                )
            )
            added = runtime.apply_patch(
                "*** Begin Patch\n*** Add File: ../outside/created.txt\n+hello\n*** End Patch"
            )
            updated = runtime.apply_patch(
                "*** Begin Patch\n*** Update File: ../outside/created.txt\n@@\n-hello\n+bonjour\n*** End Patch"
            )

            self.assertTrue(added.ok, added.output)
            self.assertTrue(updated.ok, updated.output)
            self.assertEqual((outside / "created.txt").read_text(encoding="utf-8"), "bonjour\n")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["tool"], "apply_patch")
            self.assertEqual(approvals[0]["files"], ["../outside/created.txt"])
            self.assertEqual(added.metadata["approval"], "approved_without_sandbox")

    def test_view_image_uses_upstream_model_catalog_for_detail_capability(self) -> None:
        gpt52 = next(
            spec for spec in ToolRuntime(VolleyConfig(model="gpt-5.2")).specs() if spec.get("name") == "view_image"
        )
        gpt54 = next(
            spec for spec in ToolRuntime(VolleyConfig(model="gpt-5.4")).specs() if spec.get("name") == "view_image"
        )

        self.assertNotIn("detail", gpt52["parameters"]["properties"])
        self.assertIn("detail", gpt54["parameters"]["properties"])

        runtime = ToolRuntime(VolleyConfig(model_supports_image_input=False, skip_git_repo_check=True, ephemeral=True))
        result = runtime.view_image({"path": "missing.png"})
        self.assertFalse(result.ok)
        self.assertIn("you do not support image inputs", result.output)

    def test_view_image_resizes_and_respects_original_detail_capability(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is required for image processing parity tests")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "large.png"
            Image.new("RGBA", (3000, 1000), (10, 20, 30, 255)).save(image_path)

            runtime = ToolRuntime(VolleyConfig(model="gpt-5.2", cwd=root, skip_git_repo_check=True, ephemeral=True))
            resized = runtime.view_image({"path": "large.png"})
            self.assertTrue(resized.ok, resized.output)
            self.assertEqual(json.loads(resized.output)["detail"], "high")
            self.assertLessEqual(resized.metadata["width"], 2048)
            self.assertTrue(resized.metadata["resized"])

            downgraded = runtime.view_image({"path": "large.png", "detail": "original"})
            self.assertTrue(downgraded.ok, downgraded.output)
            self.assertEqual(json.loads(downgraded.output)["detail"], "high")
            self.assertTrue(downgraded.metadata["resized"])

            original_runtime = ToolRuntime(
                VolleyConfig(
                    cwd=root,
                    skip_git_repo_check=True,
                    ephemeral=True,
                    model_supports_image_detail_original=True,
                )
            )
            original = original_runtime.view_image({"path": "large.png", "detail": "original"})
            self.assertTrue(original.ok, original.output)
            self.assertEqual(json.loads(original.output)["detail"], "original")
            data_url = original.metadata["image_url"]
            image_bytes = base64.b64decode(data_url.split(",", 1)[1])
            with Image.open(io.BytesIO(image_bytes)) as decoded:
                self.assertEqual(decoded.size, (3000, 1000))

    def test_long_running_exec_and_write_stdin(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command = (
            f"{sys.executable!r} -c "
            "\"import sys; print('ready', flush=True); print(sys.stdin.readline().strip(), flush=True)\""
        )
        first = runtime.exec_command({"cmd": command, "tty": True, "yield_time_ms": 100})
        payload = first.metadata
        self.assertIn("session_id", payload)
        self.assertIn("Process running with session ID", first.output)
        second = runtime.write_stdin({"session_id": payload["session_id"], "chars": "hello\n", "yield_time_ms": 1500})
        self.assertIn("hello", second.output)
        self.assertIn("ready", second.metadata["aggregated_output"])

    @unittest.skipIf(sys.platform == "win32", "PTY semantics are not used by the Windows shell_command path")
    def test_tool_runtime_interactive_background_sessions_keep_stdin_and_output_isolated(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command_a = "printf 'A-ready\\n'; read line; printf 'A-got:%s\\n' \"$line\""
        command_b = "printf 'B-ready\\n'; read line; printf 'B-got:%s\\n' \"$line\""

        first = runtime.dispatch(
            "exec_command",
            {"cmd": command_a, "tty": True, "yield_time_ms": 250, "shell": "/bin/sh", "login": False},
            call_id="exec-a",
        )
        second = runtime.dispatch(
            "exec_command",
            {"cmd": command_b, "tty": True, "yield_time_ms": 250, "shell": "/bin/sh", "login": False},
            call_id="exec-b",
        )
        self.assertIn("session_id", first.metadata)
        self.assertIn("session_id", second.metadata)
        self.assertNotEqual(first.metadata["session_id"], second.metadata["session_id"])
        runtime.drain_runtime_events()

        b_result = runtime.dispatch(
            "write_stdin",
            {"session_id": second.metadata["session_id"], "chars": "B-input\n", "yield_time_ms": 1500},
            call_id="stdin-b",
        )
        a_result = runtime.dispatch(
            "write_stdin",
            {"session_id": first.metadata["session_id"], "chars": "A-input\n", "yield_time_ms": 1500},
            call_id="stdin-a",
        )

        self.assertTrue(a_result.ok, a_result.output)
        self.assertTrue(b_result.ok, b_result.output)
        self.assertEqual(a_result.metadata["event_call_id"], "exec-a")
        self.assertEqual(b_result.metadata["event_call_id"], "exec-b")
        self.assertIn("A-got:A-input", a_result.metadata["aggregated_output"])
        self.assertNotIn("B-got:B-input", a_result.metadata["aggregated_output"])
        self.assertIn("B-got:B-input", b_result.metadata["aggregated_output"])
        self.assertNotIn("A-got:A-input", b_result.metadata["aggregated_output"])

        deltas = runtime.drain_runtime_events()
        for event in deltas:
            payload = event.get("payload", {})
            text = str(payload.get("delta") or "")
            if "A-got" in text:
                self.assertEqual(payload.get("call_id"), "exec-a")
            if "B-got" in text:
                self.assertEqual(payload.get("call_id"), "exec-b")

    def test_shell_command_interrupts_running_process(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True, include_shell_command_tool=True))
        result_holder: dict[str, ToolResult] = {}
        command = f"{sys.executable!r} -c \"import time; time.sleep(10)\""
        thread = threading.Thread(
            target=lambda: result_holder.setdefault(
                "result",
                runtime.shell_command({"command": command, "timeout_ms": 30000}),
            ),
            daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with runtime._session_lock:
                if runtime._active_commands:
                    break
            time.sleep(0.01)
        runtime.interrupt_all()
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        result = result_holder["result"]
        self.assertFalse(result.ok)
        self.assertTrue(result.metadata["metadata"]["interrupted"])

    def test_request_interrupt_preserves_running_unified_exec_session(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        result_holder: dict[str, ToolResult] = {}
        command = f"{sys.executable!r} -c \"import time; print('ready', flush=True); time.sleep(10)\""
        thread = threading.Thread(
            target=lambda: result_holder.setdefault(
                "result",
                runtime.exec_command({"cmd": command, "yield_time_ms": 30000}),
            ),
            daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with runtime._session_lock:
                if runtime._active_commands:
                    break
            time.sleep(0.01)
        runtime.request_interrupt()
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        result = result_holder["result"]
        self.assertTrue(result.ok)
        self.assertIn("session_id", result.metadata)
        session_id = result.metadata["session_id"]
        with runtime._session_lock:
            self.assertIn(session_id, runtime._sessions)
        runtime.interrupt_all()

    def test_exec_command_captures_partial_output_without_newline(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command = (
            f"{sys.executable!r} -c "
            "\"import sys,time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(0.4)\""
        )
        first = runtime.exec_command({"cmd": command, "yield_time_ms": 250})
        self.assertIn("partial", first.output)
        self.assertIn("session_id", first.metadata)

        final = runtime.write_stdin({"session_id": first.metadata["session_id"], "yield_time_ms": 250})
        self.assertEqual(final.metadata["exit_code"], 0)

    @unittest.skipIf(sys.platform == "win32", "PTY semantics are not used by the Windows shell_command path")
    def test_exec_command_tty_allocates_terminal(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command = (
            f"{sys.executable!r} -c "
            "\"import os,sys; print(str(os.isatty(0))+' '+str(os.isatty(1)), flush=True); "
            "print('got:'+sys.stdin.readline().strip(), flush=True)\""
        )
        first = runtime.exec_command({"cmd": command, "tty": True, "yield_time_ms": 1000})
        self.assertIn("True True", first.output)

        second = runtime.write_stdin({"session_id": first.metadata["session_id"], "chars": "hello\n", "yield_time_ms": 1500})
        self.assertIn("got:hello", second.output)

    @unittest.skipIf(sys.platform == "win32", "PTY semantics are not used by the Windows shell_command path")
    def test_exec_command_tty_handles_sleep_then_input(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command = (
            f"{sys.executable!r} -c "
            "\"import time; print('program started; sleeping 0.2s...', flush=True); "
            "time.sleep(0.2); text = input('please input a sentence: '); "
            "print('completed; got:', text, flush=True)\""
        )
        first = runtime.exec_command({"cmd": command, "tty": True, "yield_time_ms": 100})
        self.assertIn("session_id", first.metadata)

        second = runtime.write_stdin(
            {
                "session_id": first.metadata["session_id"],
                "chars": "这是一句从 Volley 写入的测试输入。\n",
                "yield_time_ms": 1500,
            }
        )

        self.assertIn("please input a sentence:", second.output)
        self.assertIn("completed; got: 这是一句从 Volley 写入的测试输入。", second.output)

    def test_write_stdin_requires_tty_for_non_empty_input(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        command = f"{sys.executable!r} -c \"import time; time.sleep(1)\""
        first = runtime.exec_command({"cmd": command, "yield_time_ms": 250})
        result = runtime.write_stdin({"session_id": first.metadata["session_id"], "chars": "hello\n"})

        self.assertFalse(result.ok)
        self.assertIn("stdin is closed", result.output)

    def test_exec_command_rejects_escalation_without_on_request_approval(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True, approval_policy="never"))
        result = runtime.exec_command({"cmd": "echo no", "sandbox_permissions": "require_escalated"})

        self.assertFalse(result.ok)
        self.assertIn("cannot ask for escalated permissions", result.output)

    def test_exec_command_records_platform_sandbox_metadata(self) -> None:
        runtime = ToolRuntime(VolleyConfig(skip_git_repo_check=True, ephemeral=True, sandbox="workspace-write"))
        result = runtime.exec_command({"cmd": f"{sys.executable!r} -c \"print('sandbox-meta')\""})

        self.assertTrue(result.ok, result.output)
        self.assertIn("sandbox-meta", result.output)
        self.assertEqual(result.metadata["sandbox_policy"], "workspace-write")
        self.assertIn("sandbox_enforced", result.metadata)
        if sys.platform == "darwin" and not codex_tools._platform_sandbox_available():
            self.assertTrue(result.metadata["sandbox_unavailable"])

    @unittest.skipIf(sys.platform != "darwin", "macOS seatbelt argv is Darwin-specific")
    def test_macos_sandbox_argv_wraps_command_with_seatbelt_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writable = root / "writable-extra"
            writable.mkdir()
            tmpdir = root / "tmpdir"
            tmpdir.mkdir()
            previous_tmpdir = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = str(tmpdir)
            previous = codex_tools._PLATFORM_SANDBOX_AVAILABLE_CACHE
            codex_tools._PLATFORM_SANDBOX_AVAILABLE_CACHE = True
            try:
                sandboxed = codex_tools._sandboxed_process_argv(
                    ["/bin/echo", "ok"],
                    config=VolleyConfig(
                        cwd=root,
                        skip_git_repo_check=True,
                        ephemeral=True,
                        sandbox="workspace-write",
                        writable_roots=(writable,),
                    ),
                    cwd=root,
                    workdir=root,
                    bypass_sandbox=False,
                )
            finally:
                codex_tools._PLATFORM_SANDBOX_AVAILABLE_CACHE = previous
                if previous_tmpdir is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous_tmpdir

            self.assertEqual(sandboxed.argv[:2], [codex_tools.MACOS_SANDBOX_EXEC, "-p"])
            self.assertEqual(sandboxed.argv[-2:], ["/bin/echo", "ok"])
            policy = sandboxed.argv[2]
            self.assertIn("(deny default)", policy)
            self.assertIn("(allow file-read*)", policy)
            self.assertIn(str(root), policy)
            self.assertIn(str(writable), policy)
            self.assertIn(str(Path("/tmp").resolve()), policy)
            self.assertIn(str(tmpdir.resolve()), policy)
            self.assertTrue(sandboxed.metadata["sandbox_enforced"])

    def test_workspace_write_roots_match_upstream_tmp_defaults_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tmpdir = root / "tmpdir"
            tmpdir.mkdir()
            previous_tmpdir = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = str(tmpdir)
            try:
                roots = codex_tools._workspace_write_roots(root, (), include_tmp_roots=True)
                excluded = codex_tools._workspace_write_roots(
                    root,
                    (),
                    include_tmp_roots=True,
                    exclude_tmpdir_env_var=True,
                    exclude_slash_tmp=True,
                )
                apply_patch_roots = codex_tools._workspace_write_roots(root, ())
            finally:
                if previous_tmpdir is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous_tmpdir

            self.assertIn(root.resolve(), roots)
            self.assertIn(tmpdir.resolve(), roots)
            self.assertIn(Path("/tmp").resolve(), roots)
            self.assertNotIn(tmpdir.resolve(), excluded)
            self.assertNotIn(Path("/tmp").resolve(), excluded)
            self.assertNotIn(tmpdir.resolve(), apply_patch_roots)
            self.assertNotIn(Path("/tmp").resolve(), apply_patch_roots)

    def test_escalated_exec_bypasses_platform_sandbox_after_approval(self) -> None:
        approvals: list[dict] = []

        def approve(request: dict) -> dict:
            approvals.append(request)
            return {"approved": True}

        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                sandbox="workspace-write",
                approval_policy="on-request",
                approval_provider=approve,
            )
        )
        result = runtime.exec_command(
            {
                "cmd": f"{sys.executable!r} -c \"print('unsandboxed-approved')\"",
                "sandbox_permissions": "require_escalated",
                "justification": "Need unsandboxed execution",
            }
        )

        self.assertTrue(result.ok, result.output)
        self.assertIn("unsandboxed-approved", result.output)
        self.assertTrue(result.metadata["sandbox_bypassed"])
        self.assertFalse(result.metadata["sandbox_enforced"])
        self.assertEqual(approvals[0]["sandbox_permissions"], "require_escalated")

    def test_exec_command_uses_approval_provider_for_escalated_request(self) -> None:
        approvals: list[dict] = []

        def approve(request: dict) -> dict:
            approvals.append(request)
            return {"approved": True}

        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                approval_policy="on-request",
                approval_provider=approve,
            )
        )
        result = runtime.exec_command(
            {
                "cmd": f"{sys.executable!r} -c \"print('approved')\"",
                "sandbox_permissions": "require_escalated",
                "justification": "Need to run a test command",
                "prefix_rule": [sys.executable],
            }
        )

        self.assertTrue(result.ok)
        self.assertIn("approved", result.output)
        self.assertEqual(approvals[0]["tool"], "exec_command")
        self.assertEqual(approvals[0]["sandbox_permissions"], "require_escalated")
        self.assertEqual(approvals[0]["justification"], "Need to run a test command")

    def test_exec_command_uses_permission_request_hook_for_escalated_request(self) -> None:
        hooks: list[dict] = []

        def hook_provider(request: dict) -> dict:
            hooks.append(request)
            if request["event"] == "permission_request":
                return {"decision": "approved_for_session"}
            return {}

        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                approval_policy="on-request",
                hook_provider=hook_provider,
            )
        )
        result = runtime.exec_command(
            {
                "cmd": f"{sys.executable!r} -c \"print('hook-approved')\"",
                "sandbox_permissions": "require_escalated",
                "justification": "Need hook approval",
            }
        )

        self.assertTrue(result.ok)
        self.assertIn("hook-approved", result.output)
        self.assertEqual(hooks[0]["event"], "permission_request")
        self.assertEqual(hooks[0]["tool_name"], "Bash")
        self.assertEqual(hooks[0]["tool_input"]["tool"], "exec_command")

    def test_exec_command_stops_when_approval_provider_denies_escalation(self) -> None:
        runtime = ToolRuntime(
            VolleyConfig(
                skip_git_repo_check=True,
                ephemeral=True,
                approval_policy="on-request",
                approval_provider=lambda _request: False,
            )
        )
        result = runtime.exec_command({"cmd": "echo denied", "sandbox_permissions": "require_escalated"})

        self.assertFalse(result.ok)
        self.assertIn("approval denied", result.output)

    def test_exec_command_on_failure_retries_likely_sandbox_denial_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approvals: list[dict] = []

            def approve(request: dict) -> dict:
                approvals.append(request)
                return {"approved": True}

            script = (
                "from pathlib import Path\n"
                "marker = Path('retry-marker')\n"
                "if not marker.exists():\n"
                "    marker.write_text('seen', encoding='utf-8')\n"
                "    print('sandbox denied: Operation not permitted')\n"
                "    raise SystemExit(1)\n"
                "print('retry-ok')\n"
            )
            runtime = ToolRuntime(
                VolleyConfig(
                    cwd=root,
                    skip_git_repo_check=True,
                    ephemeral=True,
                    approval_policy="on-failure",
                    approval_provider=approve,
                )
            )
            result = runtime.exec_command({"cmd": f"{sys.executable!r} -c {script!r}"})

            self.assertTrue(result.ok, result.output)
            self.assertIn("retry-ok", result.output)
            self.assertTrue(result.metadata["retry_without_sandbox"])
            self.assertEqual(approvals[0]["tool"], "exec_command")
            self.assertTrue(approvals[0]["retry_without_sandbox"])

    def test_exec_command_on_request_requires_default_sandbox_approval_provider(self) -> None:
        runtime = ToolRuntime(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True, approval_policy="on-request")
        )
        result = runtime.exec_command({"cmd": "echo needs approval"})

        self.assertFalse(result.ok)
        self.assertIn("approval required for sandboxed execution", result.output)

    def test_tool_error_is_returned_to_model(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "bad",
                            "arguments": json.dumps({"cmd": "exit 7"}),
                        }
                    ]
                },
                message("handled failure"),
            ]
        )
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("fail")
        outputs = [item for item in result.history if item.get("type") == "function_call_output"]
        self.assertTrue(outputs)
        self.assertIn("Process exited with code 7", outputs[0]["output"])
        self.assertEqual(result.final_message, "handled failure")

    def test_web_search_call_is_recorded_without_local_dispatch(self) -> None:
        model = ScriptedResponsesModel(
            [
                {
                    "output": [
                        {
                            "type": "web_search_call",
                            "id": "ws-1",
                            "status": "completed",
                            "action": {"type": "search", "query": "Volley"},
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "searched"}],
                        },
                    ]
                }
            ]
        )
        result = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=model,
        ).run("search the web")
        self.assertEqual(result.final_message, "searched")
        self.assertTrue(any(item.get("type") == "web_search_call" for item in result.history))
        self.assertFalse(any(event.type == "tool.started" for event in result.events))

    def test_external_context_marks_memory_mode_polluted_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "web_search_call",
                                "id": "ws-1",
                                "status": "completed",
                                "action": {"type": "search", "query": "Volley"},
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "searched"}],
                            },
                        ]
                    }
                ]
            )
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    memory_tool_enabled=True,
                    memory_disable_on_external_context=True,
                    memory_startup_background=False,
                    memory_run_phase2_on_startup=False,
                ),
                model_client=model,
            )

            result = session.run("search the web")

            self.assertEqual(result.final_message, "searched")
            store = session.config.memory_state_store
            self.assertIsNotNone(store)
            assert store is not None
            row = store.conn.execute("SELECT memory_mode FROM threads WHERE id = ?", (result.thread_id,)).fetchone()
            self.assertEqual(row["memory_mode"], "polluted")
            store.close()

    def test_persistent_rollout_records_web_search_event_msg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            model = ScriptedResponsesModel(
                [
                    {
                        "output": [
                            {
                                "type": "web_search_call",
                                "id": "ws-1",
                                "status": "completed",
                                "action": {"type": "search", "query": "Volley"},
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "searched"}],
                            },
                        ]
                    }
                ]
            )
            result = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=model,
            ).run("search the web")

            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{result.thread_id}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            event_msgs = [record["payload"] for record in records if record["type"] == "event_msg"]
            web_search = next(payload for payload in event_msgs if payload["type"] == "web_search_end")

            self.assertEqual(web_search["call_id"], "ws-1")
            self.assertEqual(web_search["query"], "Volley")
            self.assertEqual(web_search["action"], {"type": "search", "query": "Volley"})

    def test_cli_json_and_output_last_message_with_fake_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / "last.txt"
            env = {
                **os.environ,
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("cli done")]),
                "PYTHONPATH": os.getcwd(),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--output-last-message",
                    str(last),
                    "hello",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(last.read_text(encoding="utf-8"), "cli done")
            events = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertTrue(any(event["type"] == "turn.completed" for event in events))

    def test_cli_human_output_renders_tool_progress_with_fake_model(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf hello", "yield_time_ms": 250}),
                    }
                ]
            },
            message("human done"),
        ]
        env = {
            **os.environ,
            "COLUMNS": "64",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "human done")
        self.assertIn("• Ran printf hello", completed.stderr)
        self.assertIn("  └ hello", completed.stderr)
        self.assertIn("hello", completed.stderr)

    def test_cli_human_output_supports_color_always(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf hello", "yield_time_ms": 250}),
                    }
                ]
            },
            message("color done"),
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--color",
                "always",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("\033[1;32m•\033[0m \033[1mRan\033[0m", completed.stderr)
        self.assertIn("• Ran printf hello", _plain_terminal_output(completed.stderr))
        self.assertEqual(completed.stdout.strip(), "color done")

    def test_cli_human_output_wraps_long_command_with_official_gutter(self) -> None:
        long_command = "printf " + ("abcdef" * 12)
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": long_command, "yield_time_ms": 250}),
                    }
                ]
            },
            message("wrapped command done"),
        ]
        env = {
            **os.environ,
            "COLUMNS": "44",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("• Ran printf", completed.stderr)
        self.assertIn("  │ ", completed.stderr)
        self.assertIn("  └ ", completed.stderr)

    def test_cli_human_output_separates_tool_cells(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf one", "yield_time_ms": 250}),
                    }
                ]
            },
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-2",
                        "arguments": json.dumps({"cmd": "printf two", "yield_time_ms": 250}),
                    }
                ]
            },
            message("done"),
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("• Ran printf one\n  └ one\n\n• Ran printf two", completed.stderr)

    def test_cli_human_output_renders_markdown_and_work_separator(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "exec-1",
                        "arguments": json.dumps({"cmd": "printf hello", "yield_time_ms": 250}),
                    }
                ]
            },
            message("## 结果\n这是 **重点** 和 `code`。"),
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--color",
                "always",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("─" * 20, completed.stderr)
        self.assertNotIn("**重点**", completed.stderr)
        self.assertNotIn("`code`", completed.stderr)
        self.assertIn("\033[1m重点\033[0m", completed.stderr)
        self.assertIn("\033[36mcode\033[0m", completed.stderr)
        self.assertIn("\033[1m结果\033[0m", completed.stderr)

    def test_cli_markdown_code_fences_use_syntax_highlighting_and_info_token(self) -> None:
        import volley.cli as cli
        from volley.cli import _ANSI_RE
        from volley.cli import _AnsiStyle
        from volley.cli import _render_markdown_for_terminal

        old_theme = cli._CLI_SYNTAX_THEME
        try:
            self.assertTrue(cli._set_cli_syntax_theme("monokai-extended"))
            self.assertEqual(cli._pygments_style_name(), "monokai")
            rendered = _render_markdown_for_terminal(
                "```python title=\"demo\"\ndef hello():\n    return 'ok'\n```",
                _AnsiStyle(True),
                terminal_width=80,
            )
            joined = "\n".join(rendered)
            plain = _ANSI_RE.sub("", joined)
        finally:
            cli._CLI_SYNTAX_THEME = old_theme

        self.assertNotIn("```", plain)
        self.assertIn("def hello", plain)
        self.assertIn("return 'ok'", plain)
        self.assertRegex(joined, r"\x1b\[[0-9;]*m")

    def test_cli_markdown_code_fences_keep_normal_tokens_readable_on_light_terminal(self) -> None:
        import volley.cli as cli
        from volley.cli import _ANSI_RE
        from volley.cli import _AnsiStyle
        from volley.cli import _render_markdown_for_terminal

        old_theme = cli._CLI_SYNTAX_THEME
        old_colorfgbg = os.environ.get("COLORFGBG")
        try:
            os.environ["COLORFGBG"] = "0;15"
            self.assertTrue(cli._set_cli_syntax_theme("catppuccin-mocha"))
            rendered = _render_markdown_for_terminal(
                "```python\nx = 1\nprint(x)\n```",
                _AnsiStyle(True),
                terminal_width=80,
            )
            joined = "\n".join(rendered)
            plain = _ANSI_RE.sub("", joined)
        finally:
            cli._CLI_SYNTAX_THEME = old_theme
            if old_colorfgbg is None:
                os.environ.pop("COLORFGBG", None)
            else:
                os.environ["COLORFGBG"] = old_colorfgbg

        self.assertIn("x = 1", plain)
        self.assertIn("print(x)", plain)
        self.assertNotIn("\033[38;5;15m", joined)
        self.assertIn("\033[38;5;238m", joined)

    def test_cli_markdown_plain_code_fences_are_not_dimmed(self) -> None:
        from volley.cli import _ANSI_RE
        from volley.cli import _AnsiStyle
        from volley.cli import _render_markdown_for_terminal

        rendered = _render_markdown_for_terminal(
            "```\nplain_code()\n```",
            _AnsiStyle(True),
            terminal_width=80,
        )
        joined = "\n".join(rendered)

        self.assertEqual(_ANSI_RE.sub("", joined), "plain_code()")
        self.assertNotIn("\033[2m", joined)

    def test_cli_ansi_wrapping_preserves_color_boundaries(self) -> None:
        from volley.cli import _ANSI_RE, _visible_len, _wrap_ansi_line

        wrapped = _wrap_ansi_line("\033[31m" + ("x" * 24) + "\033[0m", 8)

        self.assertEqual([_visible_len(line) for line in wrapped], [8, 8, 8])
        self.assertTrue(all(line.startswith("\033[31m") for line in wrapped))
        self.assertTrue(all(line.endswith("\033[0m") for line in wrapped))
        self.assertEqual(_ANSI_RE.sub("", "".join(wrapped)), "x" * 24)

    def test_cli_exec_command_display_uses_visible_shell_colors(self) -> None:
        from volley.cli import _ANSI_RE, _AnsiStyle, _command_display_lines

        lines = _command_display_lines("python3 -m py_compile draw_person.py", _AnsiStyle(True))
        rendered = "\n".join(lines)

        self.assertEqual(_ANSI_RE.sub("", rendered), "python3 -m py_compile draw_person.py")
        self.assertIn("\033[36mpython3\033[0m", rendered)
        self.assertIn("\033[33m-m\033[0m", rendered)
        self.assertRegex(rendered, r"\x1b\[[0-9;]*mdraw_person\.py\x1b\[0m")

    def test_cli_exec_output_dims_tool_results_without_preserving_ansi_colors(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="always", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd",
                    "ok": True,
                    "arguments": {"cmd": "printf color"},
                    "metadata": {"exit_code": 0, "output": "\033[31mred\033[0m\nplain\n"},
                },
            )
        )
        rendered = "\n".join(lines)

        self.assertNotIn("\033[31mred\033[0m", rendered)
        self.assertIn("\033[2mred\033[0m", rendered)
        self.assertIn("\033[2mplain\033[0m", rendered)

    def test_turn_input_reader_rerenders_draft_after_tool_output(self) -> None:
        from volley.cli import _TurnInputReader, _drain_output_queue

        output: "queue.Queue[str]" = queue.Queue()
        output.put("• Ran sleep 0.1")
        reader = _TurnInputReader(enabled=True, color_mode="never")
        reader._buffer = "draft while tool runs"
        reader._cursor = len(reader._buffer)

        rendered_io = io.StringIO()
        with redirect_stderr(rendered_io):
            _drain_output_queue(output, input_reader=reader)

        self.assertEqual(reader._buffer, "draft while tool runs")
        self.assertIn("• Ran sleep 0.1", rendered_io.getvalue())
        self.assertIn("draft while tool runs", rendered_io.getvalue())

    def test_cli_live_background_terminal_idles_between_explicit_waits(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered: list[Any] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=rendered.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "exec-1",
                    "arguments": {"cmd": "python long.py", "yield_time_ms": 1000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "exec_command.output_delta",
                {"call_id": "exec-1", "delta": "first\n", "stream": "stdout"},
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "exec-1",
                    "ok": True,
                    "output": "first\n",
                    "metadata": {
                        "command": "python long.py",
                        "session_id": 1,
                        "aggregated_output": "first\n",
                    },
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "exec_command.output_delta",
                {"call_id": "exec-1", "delta": "idle tick\n", "stream": "stdout"},
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "item.completed",
                {
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "我继续观察这个后台进程。"}],
                    }
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "arguments": {"session_id": 1, "chars": "", "yield_time_ms": 1000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "exec_command.output_delta",
                {"call_id": "exec-1", "delta": "second\n", "stream": "stdout"},
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "ok": True,
                    "output": "second\n",
                    "metadata": {
                        "event_call_id": "exec-1",
                        "session_id": 1,
                        "command": "python long.py",
                        "aggregated_output": "first\nsecond\n",
                    },
                },
            )
        )

        transcript = "\n".join(str(line) for line in rendered)
        self.assertNotIn("idle tick", transcript)
        self.assertIn("• Waited for background terminal · python long.py", transcript)
        self.assertIn("  └ second", transcript)
        self.assertEqual(set(renderer._live_exec_regions), set())
        self.assertNotIn("poll-1", renderer._live_exec_regions)
        self.assertEqual(renderer._live_exec_output_text.get("exec-1"), "second\n")

    def test_cli_merges_consecutive_background_wait_output(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered: list[Any] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=rendered.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "exec-1",
                    "arguments": {"cmd": "python -m unittest discover tests", "yield_time_ms": 1000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "exec-1",
                    "ok": True,
                    "metadata": {
                        "command": "python -m unittest discover tests",
                        "session_id": 1,
                        "aggregated_output": "",
                    },
                },
            )
        )
        for index in range(3):
            call_id = f"poll-{index}"
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": call_id,
                        "arguments": {"session_id": 1, "chars": "", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "exec-1", "delta": "....", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": call_id,
                        "ok": True,
                        "metadata": {
                            "event_call_id": "exec-1",
                            "session_id": 1,
                            "command": "python -m unittest discover tests",
                            "aggregated_output": "." * ((index + 1) * 4),
                            "output": "." * ((index + 1) * 4),
                        },
                    },
                )
            )

        transcript = "\n".join(str(line) for line in rendered)
        self.assertEqual(transcript.count("• Waited for background terminal · python -m unittest discover tests"), 1)
        self.assertIn("............", transcript)

    def test_turn_input_reader_initializes_from_preserved_draft(self) -> None:
        from volley.cli import _TurnInputReader

        reader = _TurnInputReader(enabled=True, color_mode="never", initial_text="kept draft", initial_cursor=4)

        self.assertEqual(reader.draft_text(), "kept draft")
        self.assertEqual(reader.draft_cursor(), 4)

    def test_interactive_turn_preserves_unsubmitted_draft_after_completion(self) -> None:
        from volley import cli

        class FakeTurnInputReader:
            def __init__(
                self,
                *,
                enabled: bool,
                color_mode: str = "auto",
                initial_text: str = "",
                initial_cursor: int | None = None,
            ) -> None:
                self.enabled = enabled
                self.color_mode = color_mode
                self.initial_text = initial_text
                self.initial_cursor = initial_cursor
                self.output_partial_line_open = False
                self._buffer = "typed while agent finishes"
                self._cursor = 5

            def __enter__(self) -> "FakeTurnInputReader":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                pass

            def set_status(self, snapshot: Any) -> None:
                pass

            def poll(self) -> list[Any]:
                return []

            def render(self) -> None:
                pass

            def clear(self) -> None:
                pass

            def draft_text(self) -> str:
                return self._buffer

            def draft_cursor(self) -> int:
                return self._cursor

        def fake_run_session_human(*args: Any, **kwargs: Any) -> int:
            time.sleep(0.05)
            return 0

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=ScriptedResponsesModel([]),
        )
        draft = cli._ComposerDraft()
        original_reader = cli._TurnInputReader
        original_run_session_human = cli._run_session_human
        original_print_finished = cli._print_finished_turn_status
        try:
            cli._TurnInputReader = FakeTurnInputReader
            cli._run_session_human = fake_run_session_human
            cli._print_finished_turn_status = lambda *args, **kwargs: None
            with redirect_stderr(io.StringIO()):
                status = cli._run_session_human_interactive(session, "start", composer_draft=draft)
        finally:
            cli._TurnInputReader = original_reader
            cli._run_session_human = original_run_session_human
            cli._print_finished_turn_status = original_print_finished

        self.assertEqual(status, 0)
        self.assertEqual(draft.text, "typed while agent finishes")
        self.assertEqual(draft.cursor, 5)

    def test_cli_human_output_preserves_intraword_underscores(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("Use snake_case and temp_print_test.py in markdown.")]),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("snake_case", completed.stderr)
        self.assertIn("temp_print_test.py", completed.stderr)
        self.assertNotIn("snakecase", completed.stderr)
        self.assertNotIn("tempprinttest.py", completed.stderr)

    def test_cli_human_output_indents_multiline_agent_message(self) -> None:
        responses = [
            message(
                "第一行\n\n"
                "第二段第一行很长很长很长很长很长很长很长很长很长很长很长很长很长。\n"
                "- 列表项"
            )
        ]
        env = {
            **os.environ,
            "COLUMNS": "48",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        rendered_lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertTrue(rendered_lines[0].startswith("• "), completed.stderr)
        self.assertTrue(all(line.startswith(("• ", "  ")) for line in rendered_lines), completed.stderr)

    def test_cli_human_output_renders_markdown_tables(self) -> None:
        responses = [
            message(
                "| 模块 | 状态 |\n"
                "| --- | --- |\n"
                "| core | done |\n"
                "| tools | partial |"
            )
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("• ┌", completed.stderr)
        self.assertIn("│ 模块", completed.stderr)
        self.assertIn("│ core", completed.stderr)
        self.assertIn("│ tools", completed.stderr)
        self.assertIn("└", completed.stderr)
        self.assertNotIn("| --- | --- |", completed.stderr)

    def test_cli_human_output_wraps_long_markdown_table_cells(self) -> None:
        responses = [
            message(
                "| 模块 | 说明 |\n"
                "| --- | --- |\n"
                "| core | 这一列是一段很长很长的中文说明，用来验证表格单元格会在盒线内换行。 |\n"
                "| tools | short |"
            )
        ]
        env = {
            **os.environ,
            "COLUMNS": "46",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        table_lines = [line for line in completed.stderr.splitlines() if "│" in line or "┌" in line or "└" in line]
        self.assertGreaterEqual(len(table_lines), 6, completed.stderr)
        self.assertTrue(all(line.startswith(("• ", "  ")) for line in table_lines), completed.stderr)
        self.assertIn("验证表格", completed.stderr)

    def test_cli_human_output_renders_reasoning_as_indented_cell(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [
                            {
                                "text": (
                                    "**Inspecting memory and commands**\n\n"
                                    "I think I need to inspect the state.\n"
                                    "Maybe I should run a command."
                                )
                            }
                        ],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                ]
            }
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("• Inspecting memory and commands: I think I need to inspect the state.", completed.stderr)
        self.assertIn("  Maybe I should run a command.", completed.stderr)
        self.assertNotIn("**Inspecting memory and commands**", completed.stderr)

    def test_cli_human_output_wraps_text_with_continuation_indent(self) -> None:
        long_text = (
            "**Inspecting memory and commands**\n\n"
            "I need to inspect the state of prompts in memory and then compare command output "
            "carefully so the rendered transcript keeps every continuation line aligned."
        )
        responses = [{"output": [{"type": "reasoning", "summary": [{"text": long_text}]}]}]
        env = {
            **os.environ,
            "COLUMNS": "72",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        reasoning_lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertGreaterEqual(len(reasoning_lines), 3)
        self.assertTrue(reasoning_lines[0].startswith("• "), completed.stderr)
        self.assertTrue(all(line.startswith("  ") for line in reasoning_lines[1:]), completed.stderr)

    def test_cli_human_output_renders_escaped_model_errors_without_traceback(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": "[]",
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("ERROR: scripted model exhausted", completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)

    def test_cli_human_output_runs_outside_git_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as non_git_dir:
            self.assertFalse((Path(non_git_dir) / ".git").exists())
            env = {
                **os.environ,
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("ok outside git")]),
                "PYTHONPATH": os.getcwd(),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--ephemeral",
                    "--cd",
                    non_git_dir,
                    "hello",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok outside git", completed.stdout)
        self.assertNotIn("not inside a Git repository", completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)

    def test_cli_top_level_errors_do_not_show_runpy_traceback(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": "not-json",
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("ERROR: JSONDecodeError:", completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)
        self.assertNotIn("<frozen runpy>", completed.stderr)
        self.assertNotIn("agents/volley/__main__.py", completed.stderr)

    def test_cli_module_entrypoint_errors_do_not_show_traceback(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": "not-json",
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley.cli",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("ERROR: JSONDecodeError:", completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)
        self.assertNotIn("<frozen runpy>", completed.stderr)
        self.assertNotIn("agents/volley/cli.py", completed.stderr)

    def test_cli_chat_route_errors_do_not_show_traceback(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": "[]",
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("ERROR: scripted model exhausted", completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)
        self.assertNotIn("return _main_chat(raw_argv)", completed.stderr)

    def test_main_chat_direct_call_handles_run_chat_errors(self) -> None:
        from volley import cli

        class RaisingSession:
            def __init__(self, config):
                pass

        original_session = cli.VolleySession
        try:
            cli.VolleySession = RaisingSession
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
                original_stderr = sys.stderr
                sys.stderr = stderr
                try:
                    status = cli._main_chat(["--skip-git-repo-check", "--ephemeral", "hello"])
                finally:
                    sys.stderr = original_stderr
                stderr.seek(0)
                output = stderr.read()
        finally:
            cli.VolleySession = original_session

        self.assertEqual(status, 1)
        self.assertIn("ERROR:", output)
        self.assertNotIn("Traceback (most recent call last)", output)

    def test_cli_json_output_emits_failed_turn_for_escaped_model_errors(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": "[]",
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        failed_events = [event for event in events if event["type"] == "turn.failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["error"], "scripted model exhausted")

    def test_cli_human_output_does_not_duplicate_final_on_stderr(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("single final")]),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "single final")
        self.assertEqual(completed.stderr.count("single final"), 1)

    def test_cli_human_output_renders_official_style_explored_group(self) -> None:
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "list-1",
                        "arguments": json.dumps({"cmd": "rg --files | head -n 2", "yield_time_ms": 250}),
                    }
                ]
            },
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "read-1",
                        "arguments": json.dumps({"cmd": "cat main.py", "yield_time_ms": 250}),
                    }
                ]
            },
            message("explored done"),
        ]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("• Explored", completed.stderr)
        self.assertIn("  └ List", completed.stderr)
        self.assertIn("    Read main.py", completed.stderr)

    def test_cli_human_output_renders_apply_patch_as_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "temp_print_test.py").write_text('print("hello")\n', encoding="utf-8")
            patch = """*** Begin Patch
*** Update File: temp_print_test.py
@@
-print("hello")
+print("bonjour")
*** End Patch
"""
            responses = [
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "apply_patch",
                            "call_id": "patch-1",
                            "arguments": patch,
                        }
                    ]
                },
                message("patch done"),
            ]
            env = {
                **os.environ,
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
                "PYTHONPATH": os.getcwd(),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--cd",
                    str(root),
                    "change the file",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((root / "temp_print_test.py").read_text(encoding="utf-8"), 'print("bonjour")\n')
            self.assertIn("• Edited temp_print_test.py (+1 -1)", completed.stderr)
            self.assertIn('    1 -print("hello")', completed.stderr)
            self.assertIn('    1 +print("bonjour")', completed.stderr)
            self.assertNotIn("apply patch", completed.stderr)
            self.assertNotIn("patch: completed", completed.stderr)
            self.assertNotIn("Success. Updated the following files", completed.stderr)

    def test_cli_chat_reuses_session_for_multiple_prompts(self) -> None:
        responses = [message("first answer"), message("second answer")]
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "chat",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            input="first prompt\nsecond prompt\n/exit\n",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertNotIn("Ask Volley to do anything", completed.stderr)
        self.assertIn("› first prompt", completed.stderr)
        self.assertIn("› second prompt", completed.stderr)
        self.assertIn("• first answer", completed.stderr)
        self.assertIn("• second answer", completed.stderr)

    def test_cli_no_subcommand_routes_to_chat_with_initial_prompt(self) -> None:
        env = {
            **os.environ,
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("top level answer")]),
            "PYTHONPATH": os.getcwd(),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "hello",
            ],
            input="/exit\n",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("› hello", completed.stderr)
        self.assertIn("• top level answer", completed.stderr)
        self.assertNotIn("Ask Volley to do anything", completed.stderr)

    def test_cli_tty_chat_bracketed_multiline_paste_submits_one_message(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("paste response")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("\x1b[200~第一行\n第二行\x1b[201~\r", "paste response"),
                ("/exit\r", None),
            ],
        )
        plain = _plain_terminal_output(output)

        self.assertIn("› 第一行", plain)
        self.assertIn("  第二行", plain)
        self.assertIn("• paste response", plain)
        self.assertNotIn("scripted model exhausted", plain)

    def test_cli_tty_chat_direct_chinese_input_decodes_utf8(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("中文 response")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("帮我看一下结构\r", "中文 response"),
                ("/exit\r", None),
            ],
        )
        plain = _plain_terminal_output(output)

        self.assertIn("› 帮我看一下结构", plain)
        self.assertIn("• 中文 response", plain)
        self.assertNotIn("�", plain)

    def test_cli_tty_chat_prompt_supports_left_arrow_editing(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("done")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("helo\x1b[Dl\r", "• done"),
                ("/exit\r", None),
            ],
            timeout=8.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("› hello", plain)
        self.assertIn("• done", plain)

    def test_prompt_escape_sequences_edit_cursor_like_basic_textarea(self) -> None:
        from volley.cli import _apply_prompt_escape_sequence

        self.assertEqual(_apply_prompt_escape_sequence("abc", 2, b"\x1b[D"), ("abc", 1))
        self.assertEqual(_apply_prompt_escape_sequence("abc", 2, b"\x1b[C"), ("abc", 3))
        self.assertEqual(_apply_prompt_escape_sequence("ab\ncd", 4, b"\x1b[H"), ("ab\ncd", 3))
        self.assertEqual(_apply_prompt_escape_sequence("ab\ncd", 3, b"\x1b[F"), ("ab\ncd", 5))
        self.assertEqual(_apply_prompt_escape_sequence("abc", 1, b"\x1b[3~"), ("ac", 1))
        self.assertEqual(_apply_prompt_escape_sequence("abc\ndefgh", 6, b"\x1b[A"), ("abc\ndefgh", 2))
        self.assertEqual(_apply_prompt_escape_sequence("abc\ndefgh", 2, b"\x1b[B"), ("abc\ndefgh", 6))

    def test_prompt_control_keys_support_readline_style_kill_and_yank(self) -> None:
        from volley.cli import _apply_prompt_control_key

        self.assertEqual(_apply_prompt_control_key("abc", 2, b"\x01", ""), ("abc", 0, ""))
        self.assertEqual(_apply_prompt_control_key("one\ntwo", 5, b"\x01", ""), ("one\ntwo", 4, ""))
        self.assertEqual(_apply_prompt_control_key("one\ntwo", 4, b"\x01", ""), ("one\ntwo", 0, ""))
        self.assertEqual(_apply_prompt_control_key("abc", 2, b"\x05", ""), ("abc", 3, ""))
        self.assertEqual(_apply_prompt_control_key("one\ntwo", 1, b"\x05", ""), ("one\ntwo", 3, ""))
        self.assertEqual(_apply_prompt_control_key("one\ntwo", 3, b"\x05", ""), ("one\ntwo", 7, ""))
        self.assertEqual(_apply_prompt_control_key("abc", 1, b"\x04", ""), ("ac", 1, ""))
        self.assertEqual(_apply_prompt_control_key("wrong paste", 11, b"\x15", ""), ("", 0, "wrong paste"))
        self.assertEqual(_apply_prompt_control_key("abc\ndef", 5, b"\x15", ""), ("abc\nef", 4, "d"))
        self.assertEqual(_apply_prompt_control_key("abc\ndef", 4, b"\x15", ""), ("abcdef", 3, "\n"))

        killed = _apply_prompt_control_key("hello world", 6, b"\x0b", "")
        self.assertEqual(killed, ("hello ", 6, "world"))
        assert killed is not None
        self.assertEqual(_apply_prompt_control_key("hello ", 6, b"\x19", killed[2]), ("hello world", 11, "world"))
        self.assertEqual(_apply_prompt_control_key("abc\ndef", 1, b"\x0b", ""), ("a\ndef", 1, "bc"))
        self.assertEqual(_apply_prompt_control_key("abc\ndef", 3, b"\x0b", ""), ("abcdef", 3, "\n"))

    def test_slash_palette_filters_and_completes_like_upstream_composer(self) -> None:
        from volley.cli import _AnsiStyle
        from volley.cli import _slash_palette_display_lines
        from volley.cli import _slash_palette_rows_for_buffer
        from volley.cli import _slash_selected_completion_text

        rows = _slash_palette_rows_for_buffer("/co", 3)
        self.assertEqual(rows[0].display_name, "compact")
        self.assertIn("summarize conversation", rows[0].description)
        self.assertEqual(
            _slash_selected_completion_text("/ro", 3, 0, trailing_space=False),
            "/rollout",
        )
        self.assertEqual(
            _slash_selected_completion_text("/ro", 3, 0, trailing_space=True),
            "/rollout ",
        )

        rendered = "\n".join(
            _slash_palette_display_lines(
                "/mo",
                3,
                selected_index=0,
                dismissed_for=None,
                style=_AnsiStyle(False),
                width=100,
            )
        )
        self.assertIn("/model", rendered)
        self.assertIn("choose what model and reasoning effort to use", rendered)
        fast_rows = _slash_palette_rows_for_buffer("/fa", 3)
        self.assertEqual(fast_rows[0].display_name, "fast")
        self.assertIn("Fastest inference", fast_rows[0].description)

        self.assertEqual(_slash_palette_rows_for_buffer("/ test", 2), [])
        self.assertEqual(_slash_palette_rows_for_buffer("/zzz", 4), [])
        self.assertEqual(_slash_palette_rows_for_buffer("/bt", 3), [])
        self.assertEqual(_slash_palette_rows_for_buffer("/pet", 4), [])
        self.assertEqual(_slash_palette_rows_for_buffer("/ren", 4), [])
        self.assertEqual(_slash_palette_rows_for_buffer("/app", 4), [])

    def test_tty_prompt_empty_state_matches_upstream_composer_placeholder(self) -> None:
        from volley.cli import _AnsiStyle
        from volley.cli import _composer_status_block
        from volley.cli import _prompt_cursor_position
        from volley.cli import _prompt_display_lines
        from volley.cli import _prompt_visible_text_window
        from volley.cli import _visible_len

        self.assertEqual(
            _prompt_display_lines("", _AnsiStyle(False)),
            ["› Ask Volley to do anything"],
        )
        self.assertEqual(
            _prompt_display_lines("short", _AnsiStyle(False)),
            ["› short"],
        )
        self.assertEqual(
            _prompt_display_lines("one\ntwo", _AnsiStyle(False)),
            ["› one", "  two"],
        )

        colored = _prompt_display_lines("", _AnsiStyle(True))[0]
        self.assertIn("\x1b[1m›\x1b[0m", colored)
        self.assertIn("\x1b[2mAsk Volley to do anything\x1b[0m", colored)

        boxed_lines = _prompt_display_lines("", _AnsiStyle(True), width=40, boxed=True)
        self.assertEqual(len(boxed_lines), 3)
        self.assertTrue(all(_visible_len(line) == 39 for line in boxed_lines))
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244m", boxed_lines[0])
        self.assertIn("\x1b[1;38;2;28;28;28;48;2;244;244;244m›\x1b[0m", boxed_lines[1])
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244m \x1b[0m", boxed_lines[1])
        self.assertNotIn("\x1b[0m \x1b[", boxed_lines[1])
        self.assertIn(
            "\x1b[2;38;2;28;28;28;48;2;244;244;244mAsk Volley to do anything\x1b[0m",
            boxed_lines[1],
        )
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244m", boxed_lines[2])

        boxed_input = _prompt_display_lines("abc", _AnsiStyle(True), width=40, boxed=True)[1]
        self.assertNotIn("\x1b[0m \x1b[", boxed_input)
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244m \x1b[0m", boxed_input)

        wrapped_chinese = _prompt_display_lines("你好世界你好", _AnsiStyle(True), width=12, boxed=True)
        self.assertEqual([_visible_len(line) for line in wrapped_chinese], [11, 11, 11, 11])
        self.assertNotIn("\x1b[0m \x1b[", "\n".join(wrapped_chinese))
        self.assertEqual(_prompt_cursor_position("你好世界你好", 4, 12), (0, 10))
        self.assertEqual(_prompt_cursor_position("你好世界你好", 6, 12), (1, 6))

        self.assertEqual(
            _composer_status_block(["• Working (0s • esc to interrupt)"]),
            ["", "• Working (0s • esc to interrupt)", ""],
        )

        long_text = "\n".join(f"line {index}" for index in range(1, 21))
        display_text, display_cursor = _prompt_visible_text_window(long_text, len(long_text), max_lines=6)
        display_lines = display_text.split("\n")
        self.assertEqual(len(display_lines), 6)
        self.assertEqual(display_lines[0], "... 15 lines above ...")
        self.assertEqual(display_lines[-1], "line 20")
        self.assertEqual(display_cursor, len(display_text))

    def test_turn_input_reader_bounds_long_multiline_composer_render(self) -> None:
        from volley.cli import _TurnInputReader
        from unittest.mock import patch

        long_text = "\n".join(f"line {index:02d}" for index in range(1, 61))
        reader = _TurnInputReader(enabled=True, color_mode="never")
        reader._buffer = long_text
        reader._cursor = len(long_text)

        captured = io.StringIO()
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            with redirect_stderr(captured):
                reader.render()

        plain = _plain_terminal_output(captured.getvalue())
        self.assertIn("... 49 lines above ...", plain)
        self.assertIn("line 60", plain)
        self.assertNotIn("line 01", plain)
        self.assertLessEqual(reader._rendered_lines, 14)

    def test_user_message_history_uses_same_light_background_as_composer(self) -> None:
        from volley.cli import _HumanEventRenderer
        from unittest.mock import patch

        captured = io.StringIO()
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((40, 24))):
            with redirect_stderr(captured):
                _HumanEventRenderer(color_mode="always").render_user_message("hello")

        rendered = captured.getvalue()
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244m", rendered)
        self.assertIn("\x1b[1;2;38;2;28;28;28;48;2;244;244;244m› \x1b[0m", rendered)
        self.assertIn("\x1b[38;2;28;28;28;48;2;244;244;244mhello\x1b[0m", rendered)

    def test_slash_palette_redraw_clears_menu_rows_below_prompt_cursor(self) -> None:
        from volley.cli import _TurnInputReader

        reader = _TurnInputReader(enabled=True, color_mode="never")
        reader._buffer = "/"
        reader._cursor = 1

        captured = io.StringIO()
        with redirect_stderr(captured):
            reader.render()
        rows_below = reader._rendered_rows_below_cursor
        self.assertGreater(rows_below, 0)

        captured = io.StringIO()
        reader._slash_selection = 1
        with redirect_stderr(captured):
            reader.render()

        self.assertIn(f"\x1b[{rows_below}B", captured.getvalue())

    def test_cli_tty_slash_palette_previews_and_enter_selects_command(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("model should not be called")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/", "/model"),
                ("\x1b[B\x1b[B\r", "Started new chat"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("/model", plain)
        self.assertIn("choose what model and reasoning effort to use", plain)
        self.assertIn("/new", plain)
        self.assertNotIn("/ide", plain)
        self.assertNotIn("/approve", plain)
        self.assertNotIn("/rename", plain)
        self.assertNotIn("model should not be called", plain)

    def test_live_status_format_matches_upstream_elapsed_and_adds_context_metrics(self) -> None:
        from volley.cli import _AnsiStyle
        from volley.cli import _LiveTurnStatusSnapshot
        from volley.cli import _format_elapsed_compact
        from volley.cli import _format_tokens_compact
        from volley.cli import _live_status_display_lines

        self.assertEqual(_format_elapsed_compact(0), "0s")
        self.assertEqual(_format_elapsed_compact(61), "1m 01s")
        self.assertEqual(_format_elapsed_compact(3661), "1h 01m 01s")
        self.assertEqual(_format_tokens_compact(999), "999")
        self.assertEqual(_format_tokens_compact(12_700), "12.7K")

        lines = _live_status_display_lines(
            _LiveTurnStatusSnapshot(
                header="Working",
                elapsed_seconds=2,
                auth_label="ChatGPT",
                goal_status=None,
                active_context_tokens=12_700,
                active_context_estimated=True,
                session_context_tokens=18_200,
                session_context_estimated=True,
                session_reasoning_tokens=1_500,
                context_window=400_000,
            ),
            _AnsiStyle(False),
        )

        rendered = "\n".join(lines)
        self.assertIn("Working (2s • esc to interrupt)", rendered)
        self.assertNotIn("• Working", rendered)
        self.assertIn("auth ChatGPT", rendered)
        self.assertIn("ctx 12.7K/400K", rendered)
        self.assertIn("session 18.2K", rendered)
        self.assertIn("reasoning", rendered)
        self.assertIn("1.5K", rendered)
        self.assertNotIn("~", rendered)

        finished = "\n".join(
            _live_status_display_lines(
                _LiveTurnStatusSnapshot(
                    header="Working",
                    elapsed_seconds=125,
                    auth_label="ChatGPT",
                    goal_status=None,
                    active_context_tokens=12_700,
                    active_context_estimated=True,
                    session_context_tokens=18_200,
                    session_context_estimated=True,
                    session_reasoning_tokens=1_500,
                    context_window=400_000,
                    finished=True,
                    outcome="completed",
                ),
                _AnsiStyle(False),
            )
        )
        self.assertIn("  Worked for 2m 05s", finished)
        self.assertIn("ctx 12.7K/400K", finished)
        self.assertIn("auth", finished)
        self.assertIn("ChatGPT", finished)
        self.assertNotIn("esc to interrupt", finished)

        colored_finished = "".join(
            _live_status_display_lines(
                _LiveTurnStatusSnapshot(
                    header="Working",
                    elapsed_seconds=1,
                    auth_label=None,
                    goal_status=None,
                    active_context_tokens=None,
                    active_context_estimated=False,
                    session_context_tokens=None,
                    session_context_estimated=False,
                    session_reasoning_tokens=None,
                    context_window=None,
                    finished=True,
                    outcome="completed",
                ),
                _AnsiStyle(True),
            )
        )
        self.assertRegex(colored_finished, r"\x1b\[[0-9;]*2m  Worked for 1s")

    def test_live_status_blinks_activity_indicator_and_keeps_header_static(self) -> None:
        from volley.cli import _ANSI_RE
        from volley.cli import _AnsiStyle
        from volley.cli import _LiveTurnStatusSnapshot
        from volley.cli import _live_status_display_lines

        first = "\n".join(
            _live_status_display_lines(
                _LiveTurnStatusSnapshot(
                    header="Working",
                    elapsed_seconds=2,
                    auth_label=None,
                    goal_status=None,
                    active_context_tokens=None,
                    active_context_estimated=False,
                    session_context_tokens=None,
                    session_context_estimated=False,
                    session_reasoning_tokens=None,
                    context_window=None,
                    animation_millis=0,
                ),
                _AnsiStyle(True),
            )
        )
        second = "\n".join(
            _live_status_display_lines(
                _LiveTurnStatusSnapshot(
                    header="Working",
                    elapsed_seconds=2,
                    auth_label=None,
                    goal_status=None,
                    active_context_tokens=None,
                    active_context_estimated=False,
                    session_context_tokens=None,
                    session_context_estimated=False,
                    session_reasoning_tokens=None,
                    context_window=None,
                    animation_millis=600,
                ),
                _AnsiStyle(True),
            )
        )

        self.assertIn("Working", _ANSI_RE.sub("", first))
        self.assertIn("• Working", _ANSI_RE.sub("", first))
        self.assertIn("• Working", _ANSI_RE.sub("", second))
        self.assertRegex(first, r"\x1b\[1;90m•")
        self.assertRegex(second, r"\x1b\[2;90m•")
        self.assertNotRegex(first, r"\x1b\[1;38;(2|5);")
        self.assertNotRegex(second, r"\x1b\[1;38;(2|5);")
        self.assertNotEqual(first, second)

    def test_live_status_tracks_background_terminal_waits_like_upstream(self) -> None:
        from volley.cli import _AnsiStyle
        from volley.cli import _LiveTurnStatus
        from volley.cli import _live_status_display_lines

        status = _LiveTurnStatus()
        status.update(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "metadata": {
                        "session_id": 1,
                        "command": "cargo test -p volley-core -- --exact very_long_background_terminal_test_name",
                    },
                },
            )
        )
        status.update(
            codex_types.VolleyEvent(
                "tool.started",
                {"name": "write_stdin", "arguments": {"session_id": 1, "chars": ""}},
            )
        )

        session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True))
        snapshot = status.snapshot(session)
        self.assertEqual(snapshot.header, "Waiting for background terminal")
        self.assertIn("cargo test -p volley-core", snapshot.details or "")

        rendered = "\n".join(_live_status_display_lines(snapshot, _AnsiStyle(False)))
        self.assertIn("Waiting for background terminal", rendered)
        self.assertNotIn("• Waiting for background terminal", rendered)
        self.assertIn("  └ cargo test -p volley-core", rendered)

    def test_command_renderer_hides_unified_exec_metadata_when_no_output(self) -> None:
        from volley.cli import _strip_unified_exec_response_metadata
        from volley.cli import _visible_command_output

        response_text = (
            "Chunk ID: abc123\n"
            "Wall time: 0.0100 seconds\n"
            "Process running with session ID 1\n"
            "Original token count: 0\n"
            "Output:\n"
        )
        meta = {
            "chunk_id": "abc123",
            "wall_time_seconds": 0.01,
            "session_id": 1,
            "output": "",
        }
        self.assertEqual(_visible_command_output(meta, {"output": response_text}), "")
        self.assertEqual(_strip_unified_exec_response_metadata(response_text + "hello\n"), "hello\n")

    def test_cli_human_renderer_indents_multiline_shell_commands_like_upstream(self) -> None:
        from volley.cli import _HumanEventRenderer

        command = "python3 <<'PY'\nprint('hello from heredoc')\nPY"
        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": command},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {
                        "command": command,
                        "exit_code": 0,
                        "output": "hello from heredoc\n",
                    },
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertIn("• Ran python3 <<'PY'", rendered)
        self.assertIn("  │ print('hello from heredoc')", rendered)
        self.assertIn("  │ PY", rendered)
        self.assertIn("  └ hello from heredoc", rendered)

    def test_cli_human_renderer_matches_upstream_background_terminal_interactions(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": "python3 -i", "tty": True},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "output": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "arguments": {"session_id": 7, "chars": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "python3 -i", "output": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "input-1",
                    "arguments": {"session_id": 7, "chars": "ls\npwd\n"},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "input-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "python3 -i", "output": ""},
                },
            )
        )

        rendered = "\n".join(str(getattr(item, "text", item)) for item in lines)
        self.assertIn("↳ Interacted with background terminal · python3 -i", rendered)
        self.assertIn("  └ ls", rendered)
        self.assertIn("    pwd", rendered)
        self.assertNotIn("background terminal · 7", rendered)

    def test_cli_human_renderer_streams_exec_output_delta_without_duplicate_completion_output(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "printf x"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "streamed-output\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"command": "printf x", "exit_code": 0, "output": "streamed-output\n"},
                    },
                )
            )

        rendered_lines = [line for line in _ansi_terminal_lines(rendered_io.getvalue()) if line]
        self.assertEqual(rendered_lines, ["• Ran printf x", "  └ streamed-output"])

    def test_cli_human_renderer_streams_partial_output_on_same_line(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "python -m unittest"},
                    },
                )
            )
            for dot in [".", ".", "."]:
                renderer.render(
                    codex_types.VolleyEvent(
                        "exec_command.output_delta",
                        {"call_id": "cmd-1", "delta": dot, "stream": "stdout"},
                    )
                )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"command": "python -m unittest", "exit_code": 0, "output": "..."},
                    },
                )
            )

        rendered_lines = [line for line in _ansi_terminal_lines(rendered_io.getvalue()) if line]
        self.assertEqual(rendered_lines, ["• Ran python -m unittest", "  └ ..."])
        self.assertNotIn("  └ .\n    .", rendered_io.getvalue())

    def test_cli_human_renderer_marks_long_live_output_but_keeps_tail_streaming(self) -> None:
        from unittest.mock import patch

        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io), patch("shutil.get_terminal_size", return_value=os.terminal_size((24, 24))):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "python noisy.py"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "\n".join(f"line-{i}" for i in range(1, 8)) + "\n", "stream": "stdout"},
                )
            )

        rendered = rendered_io.getvalue()
        self.assertIn("… +3 lines (ctrl + t to view transcript)", rendered)
        self.assertIn("line-1", rendered)
        self.assertIn("line-7", rendered)

    def test_cli_output_drain_does_not_redraw_prompt_over_partial_line(self) -> None:
        import queue

        from volley.cli import _ConsoleWrite
        from volley.cli import _drain_output_queue

        class FakeReader:
            def __init__(self) -> None:
                self.output_partial_line_open = False
                self.clears = 0
                self.renders = 0

            def clear(self) -> None:
                self.clears += 1

            def render(self) -> None:
                self.renders += 1

        output: "queue.Queue[Any]" = queue.Queue()
        reader = FakeReader()
        output.put(_ConsoleWrite("  └ ***", partial_line_open=True))
        with redirect_stderr(io.StringIO()):
            _drain_output_queue(output, input_reader=reader)

        self.assertTrue(reader.output_partial_line_open)
        self.assertEqual(reader.renders, 0)

        output.put(_ConsoleWrite("\n", partial_line_open=False))
        with redirect_stderr(io.StringIO()):
            _drain_output_queue(output, input_reader=reader)
        self.assertFalse(reader.output_partial_line_open)
        self.assertEqual(reader.renders, 1)

    def test_cli_output_drain_batches_prompt_clear_for_live_writes(self) -> None:
        import queue

        from volley.cli import _ConsoleWrite
        from volley.cli import _drain_output_queue

        class FakeReader:
            def __init__(self) -> None:
                self.output_partial_line_open = False
                self.clears = 0
                self.renders = 0

            def clear(self) -> None:
                self.clears += 1

            def render(self) -> None:
                self.renders += 1

        output: "queue.Queue[Any]" = queue.Queue()
        reader = FakeReader()
        output.put(_ConsoleWrite("first\n", partial_line_open=False))
        output.put(_ConsoleWrite("second\n", partial_line_open=False))
        output.put("• Ran command")

        with redirect_stderr(io.StringIO()):
            _drain_output_queue(output, input_reader=reader)

        self.assertEqual(reader.clears, 1)
        self.assertEqual(reader.renders, 1)

    def test_cli_output_drain_coalesces_intermediate_live_frames(self) -> None:
        import queue

        from volley.cli import _ConsoleWrite
        from volley.cli import _drain_output_queue

        output: "queue.Queue[Any]" = queue.Queue()
        output.put(_ConsoleWrite("CLEAR-OLD", live_op="live_clear"))
        output.put(_ConsoleWrite("PANEL-1", live_op="live_panel"))
        output.put(_ConsoleWrite("CLEAR-1", live_op="live_clear"))
        output.put(_ConsoleWrite("PANEL-2", live_op="live_panel"))

        rendered = io.StringIO()
        with redirect_stderr(rendered):
            _drain_output_queue(output)

        self.assertEqual(rendered.getvalue(), "CLEAR-OLDPANEL-2")

    def test_cli_output_drain_synchronizes_tty_redraw_with_input_reader(self) -> None:
        import queue

        from volley.cli import _drain_output_queue

        class FakeTty(io.StringIO):
            def isatty(self) -> bool:
                return True

        class FakeReader:
            enabled = True

            def __init__(self) -> None:
                self.output_partial_line_open = False
                self.cleared = False
                self.rendered = False

            def clear(self) -> None:
                self.cleared = True
                print("CLEAR", file=sys.stderr)

            def render(self) -> None:
                self.rendered = True
                print("PROMPT", file=sys.stderr)

        output: "queue.Queue[Any]" = queue.Queue()
        output.put("• streamed")
        reader = FakeReader()
        rendered = FakeTty()

        with redirect_stderr(rendered):
            _drain_output_queue(output, input_reader=reader)

        value = rendered.getvalue()
        self.assertTrue(value.startswith("\033[?2026h"))
        self.assertTrue(value.endswith("\033[?2026l"))
        self.assertIn("CLEAR\n• streamed\nPROMPT\n", value)
        self.assertTrue(reader.cleared)
        self.assertTrue(reader.rendered)

    def test_cli_human_renderer_serializes_cross_thread_render_calls(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[Any] = []
        first_sink_call = True
        concurrent_started = threading.Event()
        concurrent_thread: threading.Thread | None = None
        renderer: Any = None

        def sink(item: Any) -> None:
            nonlocal first_sink_call, concurrent_thread
            if first_sink_call:
                first_sink_call = False

                def render_concurrent_preview() -> None:
                    concurrent_started.set()
                    renderer.render_pending_input_preview("queued while streaming", active=True)

                concurrent_thread = threading.Thread(target=render_concurrent_preview)
                concurrent_thread.start()
                self.assertTrue(concurrent_started.wait(timeout=1.0))
                time.sleep(0.05)
            lines.append(item)

        renderer = _HumanEventRenderer(color_mode="never", line_sink=sink)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd",
                    "ok": True,
                    "metadata": {
                        "command": "python slow.py",
                        "exit_code": 0,
                        "output": "line one\nline two\n",
                    },
                },
            )
        )
        self.assertIsNotNone(concurrent_thread)
        concurrent_thread.join(timeout=1.0)
        self.assertFalse(concurrent_thread.is_alive())

        rendered = "\n".join(str(line) for line in lines)
        ran_index = rendered.index("• Ran python slow.py")
        output_index = rendered.index("line two")
        queued_index = rendered.index("Messages to be submitted after next tool call")
        self.assertLess(ran_index, output_index)
        self.assertLess(output_index, queued_index)

    def test_cli_dynamic_rows_avoid_terminal_autowrap_column(self) -> None:
        from volley import cli

        style = cli._AnsiStyle(False)
        width = 40
        output_width = width - cli._visible_len("  └ ")
        rows = cli._live_output_display_rows("x" * output_width, style, width)

        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(cli._visible_len(row) < width for row in rows))

        snapshot = cli._LiveTurnStatusSnapshot(
            header="Working",
            elapsed_seconds=1,
            auth_label="ChatGPT",
            goal_status=None,
            active_context_tokens=12345,
            active_context_estimated=False,
            session_context_tokens=12345,
            session_context_estimated=False,
            session_reasoning_tokens=100,
            context_window=272000,
            details="x" * width,
        )
        with unittest.mock.patch.object(cli, "_terminal_columns", return_value=width):
            status_rows = cli._live_status_display_lines(snapshot, style)
        self.assertTrue(all(cli._visible_len(row) < width for row in status_rows))

    def test_cli_human_renderer_restarts_terminal_header_after_agent_message(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "python slow.py", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "booting\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"session_id": 7, "command": "python slow.py", "output": "booting\n"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "item.completed",
                    {
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Still running; I will check again."}],
                        }
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "done\n", "stream": "stdout"},
                )
            )

        rendered = rendered_io.getvalue()
        self.assertIn("• Running python slow.py", rendered)
        self.assertIn("  └ booting\n", rendered)
        self.assertIn("• Still running; I will check again.", rendered)
        self.assertIn("• Waited for background terminal", rendered)
        self.assertIn("  └ done\n", rendered)
        self.assertEqual(rendered.count("• Running python slow.py"), 1)

    def test_cli_human_renderer_freezes_live_output_after_agent_message_until_completion(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "python slow.py", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "booting\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "item.completed",
                    {
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Still running; I will check again."}],
                        }
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "done\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"command": "python slow.py", "exit_code": 0, "output": "booting\ndone\n"},
                    },
                )
            )

        lines = _ansi_terminal_lines(rendered_io.getvalue())
        message_index = lines.index("• Still running; I will check again.")
        self.assertEqual(lines[message_index + 1], "")
        self.assertEqual(lines[message_index + 2], "• Ran python slow.py")
        self.assertEqual(lines[message_index + 3], "  └ booting")
        self.assertEqual(lines[message_index + 4], "    done")
        self.assertEqual(rendered_io.getvalue().count("• Running python slow.py"), 1)
        self.assertEqual(rendered_io.getvalue().count("• Ran python slow.py"), 1)

    def test_cli_human_renderer_background_wait_after_agent_message_shows_only_new_delta(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "arguments": {"cmd": "python long.py", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "boot\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "ok": True,
                        "metadata": {"session_id": 7, "command": "python long.py", "output": "boot\n"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "tick1\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "item.completed",
                    {
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "I am checking progress."}],
                        }
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "tick2\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "ok": True,
                        "metadata": {
                            "session_id": 7,
                            "event_call_id": "bg",
                            "command": "python long.py",
                            "output": "boot\ntick1\ntick2\n",
                        },
                    },
                )
            )

        lines = _ansi_terminal_lines(rendered_io.getvalue())
        message_index = lines.index("• I am checking progress.")
        self.assertEqual(lines[message_index + 1], "")
        self.assertEqual(lines[message_index + 2], "• Waited for background terminal · python long.py")
        self.assertEqual(lines[message_index + 3], "  └ tick2")
        self.assertEqual(lines.count("  └ tick1"), 1)
        self.assertEqual(lines.count("  └ tick2"), 1)
        self.assertNotIn("tick1\n    tick2", rendered_io.getvalue())

    def test_cli_human_renderer_background_delta_after_new_action_does_not_cover_action(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "arguments": {"cmd": "python server.py", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "boot\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "ok": True,
                        "metadata": {"session_id": 7, "command": "python server.py", "output": "boot\n"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": "poll",
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "tick1\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "item.completed",
                    {
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I will run a foreground check while the server continues.",
                                }
                            ],
                        }
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "fg",
                        "arguments": {"cmd": "pytest -q", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "fg", "delta": "test-start\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "tick2\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "fg",
                        "ok": True,
                        "metadata": {"command": "pytest -q", "exit_code": 0, "output": "test-start\n"},
                    },
                )
            )

        lines = _ansi_terminal_lines(rendered_io.getvalue())
        foreground_index = lines.index("• Ran pytest -q")
        self.assertEqual(lines[foreground_index + 1], "  └ test-start")
        self.assertEqual(lines[foreground_index + 2], "• Waited for background terminal · python server.py")
        self.assertEqual(lines[foreground_index + 3], "  └ tick2")
        self.assertEqual(rendered_io.getvalue().count("• Running pytest -q"), 1)
        self.assertEqual(rendered_io.getvalue().count("• Ran pytest -q"), 1)

    def test_cli_human_renderer_hides_running_cells_for_silent_background_command(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": "sleep 10", "yield_time_ms": 30000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "sleep 10", "output": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "arguments": {"session_id": 7, "chars": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "ok": True,
                    "metadata": {
                        "session_id": 7,
                        "event_call_id": "cmd-1",
                        "command": "sleep 10",
                        "exit_code": 0,
                        "output": "",
                    },
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertIn("• Ran sleep 10", rendered)
        self.assertIn("  └ (no output)", rendered)
        self.assertNotIn("• Running sleep 10", rendered)
        self.assertNotIn("Waited for background terminal", rendered)

    def test_cli_human_renderer_streams_background_terminal_wait_output(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": {"cmd": "python3 -i", "tty": True},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"session_id": 7, "output": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "prompt ready\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": "poll-1",
                        "ok": True,
                        "metadata": {
                            "session_id": 7,
                            "event_call_id": "cmd-1",
                            "command": "python3 -i",
                            "output": "prompt ready\n",
                        },
                    },
                )
            )

        rendered = rendered_io.getvalue()
        self.assertIn("• Waited for background terminal · python3 -i", rendered)
        self.assertIn("  └ prompt ready", rendered)
        self.assertEqual(rendered.count("prompt ready"), 1)

    def test_cli_human_renderer_flushes_empty_background_wait_once_before_agent_message(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": "sleep 10", "yield_time_ms": 30000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "sleep 10", "output": ""},
                },
            )
        )
        for poll_id in ("poll-1", "poll-2", "poll-3"):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": poll_id,
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": poll_id,
                        "ok": True,
                        "metadata": {
                            "session_id": 7,
                            "event_call_id": "cmd-1",
                            "command": "sleep 10",
                            "output": "",
                        },
                    },
                )
            )
        renderer.render(
            codex_types.VolleyEvent(
                "item.completed",
                {
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Still running; I will check again."}],
                    }
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertEqual(rendered.count("• Waited for background terminal · sleep 10"), 1)
        self.assertIn("• Still running; I will check again.", rendered)
        self.assertLess(
            rendered.index("• Waited for background terminal · sleep 10"),
            rendered.index("• Still running; I will check again."),
        )
        self.assertNotIn("• Running sleep 10", rendered)

    def test_cli_human_renderer_deduplicates_empty_background_waits_across_agent_messages(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": "sleep 10", "yield_time_ms": 30000},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "sleep 10", "output": ""},
                },
            )
        )
        for poll_id, text in (
            ("poll-1", "Still running; I will wait."),
            ("poll-2", "Still no output; I will continue."),
        ):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": poll_id,
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "write_stdin",
                        "call_id": poll_id,
                        "ok": True,
                        "metadata": {
                            "session_id": 7,
                            "event_call_id": "cmd-1",
                            "command": "sleep 10",
                            "output": "",
                        },
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "item.completed",
                    {
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    },
                )
            )

        rendered = "\n".join(lines)
        self.assertEqual(rendered.count("• Waited for background terminal · sleep 10"), 1)
        self.assertIn("• Still running; I will wait.", rendered)
        self.assertIn("• Still no output; I will continue.", rendered)

    def test_cli_human_renderer_empty_wait_then_interaction_renders_each_block_once(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "arguments": {"cmd": "python3 -i", "tty": True},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": "cmd-1",
                    "ok": True,
                    "metadata": {"session_id": 7, "command": "python3 -i", "output": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "arguments": {"session_id": 7, "chars": ""},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "poll-1",
                    "ok": True,
                    "metadata": {
                        "session_id": 7,
                        "event_call_id": "cmd-1",
                        "command": "python3 -i",
                        "output": "",
                    },
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.started",
                {
                    "name": "write_stdin",
                    "call_id": "stdin-1",
                    "arguments": {"session_id": 7, "chars": "print('hi')\n"},
                },
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "exec_command.output_delta",
                {"call_id": "cmd-1", "delta": "hi\n>>> ", "stream": "stdout"},
            )
        )
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "write_stdin",
                    "call_id": "stdin-1",
                    "ok": True,
                    "metadata": {
                        "session_id": 7,
                        "event_call_id": "cmd-1",
                        "command": "python3 -i",
                        "output": "hi\n>>> ",
                    },
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertEqual(rendered.count("• Waited for background terminal · python3 -i"), 1)
        self.assertEqual(rendered.count("↳ Interacted with background terminal · python3 -i"), 1)
        self.assertIn("  └ print('hi')", rendered)
        self.assertIn("  └ hi", rendered)
        self.assertLess(
            rendered.index("• Waited for background terminal · python3 -i"),
            rendered.index("↳ Interacted with background terminal · python3 -i"),
        )

    def test_cli_human_renderer_suppresses_idle_background_terminal_delta_during_other_command(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "arguments": {"cmd": "python server.py", "yield_time_ms": 1000},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "server boot\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "ok": True,
                        "metadata": {"session_id": 7, "command": "python server.py", "output": "server boot\n"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "fg", "arguments": {"cmd": "pytest -q"}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "fg", "delta": "test tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "idle server tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "fg",
                        "ok": True,
                        "metadata": {"command": "pytest -q", "exit_code": 0, "output": "test tick\n"},
                    },
                )
            )

        rendered = rendered_io.getvalue()
        self.assertIn("server boot", rendered)
        self.assertIn("• Ran pytest -q", rendered)
        self.assertIn("test tick", rendered)
        self.assertNotIn("idle server tick", rendered)
        self.assertEqual(rendered.count("python server.py"), 1)
        self.assertEqual(rendered.count("pytest -q"), 2)

    def test_cli_human_renderer_only_explicitly_waited_background_terminal_streams_with_multiple_backgrounds(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            for call_id, session_id, command, boot in [
                ("bg-a", 11, "python server_a.py", "a boot\n"),
                ("bg-b", 12, "python server_b.py", "b boot\n"),
            ]:
                renderer.render(
                    codex_types.VolleyEvent(
                        "tool.started",
                        {
                            "name": "exec_command",
                            "call_id": call_id,
                            "arguments": {"cmd": command, "yield_time_ms": 1000},
                        },
                    )
                )
                renderer.render(
                    codex_types.VolleyEvent(
                        "exec_command.output_delta",
                        {"call_id": call_id, "delta": boot, "stream": "stdout"},
                    )
                )
                renderer.render(
                    codex_types.VolleyEvent(
                        "tool.completed",
                        {
                            "name": "exec_command",
                            "call_id": call_id,
                            "ok": True,
                            "metadata": {"session_id": session_id, "command": command, "output": boot},
                        },
                    )
                )

            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg-a", "delta": "a idle tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg-b", "delta": "b idle tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "fg", "arguments": {"cmd": "pytest -q"}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "fg", "delta": "fg tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "write_stdin", "call_id": "wait-a", "arguments": {"session_id": 11, "chars": ""}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg-a", "delta": "a waited tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg-b", "delta": "b still idle tick\n", "stream": "stdout"},
                )
            )

        rendered = rendered_io.getvalue()
        self.assertIn("• Running python server_a.py", rendered)
        self.assertIn("• Running python server_b.py", rendered)
        self.assertIn("• Running pytest -q", rendered)
        self.assertIn("• Waited for background terminal · python server_a.py", rendered)
        self.assertIn("a waited tick", rendered)
        self.assertNotIn("a idle tick", rendered)
        self.assertNotIn("b idle tick", rendered)
        self.assertNotIn("b still idle tick", rendered)

    def test_cli_human_renderer_freezes_non_tail_parallel_exec_delta_until_completion(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "cmd-1", "arguments": {"cmd": "one"}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "cmd-2", "arguments": {"cmd": "two"}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "one-1\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-2", "delta": "two-1\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-1", "delta": "one-2\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "ok": True,
                        "metadata": {"command": "one", "exit_code": 0, "output": "one-1\none-2\n"},
                    },
                )
            )

        self.assertEqual(
            _ansi_terminal_lines(rendered_io.getvalue()),
            [
                "• Running one",
                "  └ one-1",
                "• Running two",
                "  └ two-1",
                "",
                "• Ran one",
                "  └ one-1",
                "    one-2",
            ],
        )
        self.assertEqual(rendered_io.getvalue().count("• Running one"), 1)
        self.assertEqual(rendered_io.getvalue().count("• Running two"), 1)
        self.assertEqual(rendered_io.getvalue().count("• Ran one"), 1)

    def test_cli_human_renderer_rewrites_parallel_exec_completion_without_corrupting_siblings(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "one", "arguments": {"cmd": "cmd-one", "yield_time_ms": 1000}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {"name": "exec_command", "call_id": "two", "arguments": {"cmd": "cmd-two", "yield_time_ms": 1000}},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "one", "delta": "one-out\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "two", "delta": "two-out\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "one",
                        "ok": True,
                        "metadata": {"command": "cmd-one", "exit_code": 0, "output": "one-out\n"},
                    },
                )
            )

        self.assertEqual(
            _ansi_terminal_lines(rendered_io.getvalue()),
            [
                "• Ran cmd-one",
                "  └ one-out",
                "• Running cmd-two",
                "  └ two-out",
            ],
        )
        rendered = rendered_io.getvalue()
        self.assertEqual(rendered.count("• Running cmd-one"), 1)
        self.assertEqual(rendered.count("• Running cmd-two"), 1)
        self.assertEqual(rendered.count("• Ran cmd-one"), 1)

    def test_cli_human_renderer_rewrites_both_parallel_exec_completions_once(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            for call_id, command in [("one", "cmd-one"), ("two", "cmd-two")]:
                renderer.render(
                    codex_types.VolleyEvent(
                        "tool.started",
                        {
                            "name": "exec_command",
                            "call_id": call_id,
                            "arguments": {"cmd": command, "yield_time_ms": 1000},
                        },
                    )
                )
            for call_id, output in [("one", "one-out\n"), ("two", "two-out\n")]:
                renderer.render(
                    codex_types.VolleyEvent(
                        "exec_command.output_delta",
                        {"call_id": call_id, "delta": output, "stream": "stdout"},
                    )
                )
            for call_id, command, output in [
                ("one", "cmd-one", "one-out\n"),
                ("two", "cmd-two", "two-out\n"),
            ]:
                renderer.render(
                    codex_types.VolleyEvent(
                        "tool.completed",
                        {
                            "name": "exec_command",
                            "call_id": call_id,
                            "ok": True,
                            "metadata": {"command": command, "exit_code": 0, "output": output},
                        },
                    )
                )

        self.assertEqual(
            _ansi_terminal_lines(rendered_io.getvalue()),
            [
                "• Ran cmd-one",
                "  └ one-out",
                "• Ran cmd-two",
                "  └ two-out",
            ],
        )
        rendered = rendered_io.getvalue()
        for command in ["cmd-one", "cmd-two"]:
            self.assertEqual(rendered.count(f"• Running {command}"), 1)
            self.assertEqual(rendered.count(f"• Ran {command}"), 1)

    def test_cli_human_renderer_suppresses_non_tail_delta_in_many_parallel_exec_outputs(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            for index in range(5):
                renderer.render(
                    codex_types.VolleyEvent(
                        "tool.started",
                        {
                            "name": "exec_command",
                            "call_id": f"cmd-{index}",
                            "arguments": {"cmd": f"job-{index}"},
                        },
                    )
                )
            for index in range(5):
                renderer.render(
                    codex_types.VolleyEvent(
                        "exec_command.output_delta",
                        {
                            "call_id": f"cmd-{index}",
                            "delta": f"line-{index}\n",
                            "stream": "stdout",
                        },
                    )
                )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd-2", "delta": "line-2b\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "cmd-2",
                        "ok": True,
                        "metadata": {"command": "job-2", "exit_code": 0, "output": "line-2\nline-2b\n"},
                    },
                )
            )

        self.assertEqual(
            _ansi_terminal_lines(rendered_io.getvalue()),
            [
                "• Running job-0",
                "  └ line-0",
                "• Running job-1",
                "  └ line-1",
                "• Running job-2",
                "  └ line-2",
                "• Running job-3",
                "  └ line-3",
                "• Running job-4",
                "  └ line-4",
                "",
                "• Ran job-2",
                "  └ line-2",
                "    line-2b",
            ],
        )
        rendered = rendered_io.getvalue()
        for index in range(5):
            self.assertEqual(rendered.count(f"• Running job-{index}"), 1)

    def test_cli_human_renderer_suppresses_background_wait_delta_after_parallel_exec_takes_foreground(self) -> None:
        from volley.cli import _HumanEventRenderer

        rendered_io = io.StringIO()
        renderer = _HumanEventRenderer(color_mode="never")
        with redirect_stderr(rendered_io):
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "arguments": {"cmd": "python3 -i", "tty": True},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.completed",
                    {
                        "name": "exec_command",
                        "call_id": "bg",
                        "ok": True,
                        "metadata": {"session_id": 7, "command": "python3 -i", "output": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "exec_command",
                        "call_id": "cmd",
                        "arguments": {"cmd": "npm test"},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "tool.started",
                    {
                        "name": "write_stdin",
                        "call_id": "poll",
                        "arguments": {"session_id": 7, "chars": ""},
                    },
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "prompt\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "cmd", "delta": "tick\n", "stream": "stdout"},
                )
            )
            renderer.render(
                codex_types.VolleyEvent(
                    "exec_command.output_delta",
                    {"call_id": "bg", "delta": "ready\n", "stream": "stdout"},
                )
            )

        self.assertEqual(
            _ansi_terminal_lines(rendered_io.getvalue()),
            [
                "• Waited for background terminal · python3 -i",
                "  └ prompt",
                "• Running npm test",
                "  └ tick",
            ],
        )
        rendered = rendered_io.getvalue()
        self.assertEqual(rendered.count("• Waited for background terminal · python3 -i"), 1)
        self.assertEqual(rendered.count("• Running npm test"), 1)
        self.assertNotIn("ready", rendered)

    def test_cli_human_renderer_terminal_interaction_uses_command_snapshot(self) -> None:
        from unittest.mock import patch

        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        command = "python3 <<'PY'\nprint('this is a very long heredoc command body')\nPY"
        with patch("shutil.get_terminal_size", return_value=os.terminal_size((64, 24))):
            renderer.render_terminal_interaction("7", "", command=command)

        rendered = "\n".join(lines)
        self.assertIn("• Waited for background terminal ·", rendered)
        self.assertIn("…", rendered)
        self.assertNotIn("\nprint(", rendered)
        self.assertNotIn("very long heredoc command body", rendered)

    def test_resume_transcript_replays_apply_patch_changes_as_file_change(self) -> None:
        from volley.cli import _HumanEventRenderer
        from volley.cli import _render_transcript_event_msg

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        rendered_any = _render_transcript_event_msg(
            {
                "type": "patch_apply_end",
                "call_id": "patch-1",
                "success": True,
                "status": "completed",
                "stdout": "patch: completed\nSuccess. Updated the following files:\nM temp_print_test.py\n",
                "changes": {
                    "temp_print_test.py": {
                        "type": "update",
                        "unified_diff": '@@\n-print("hello")\n+print("bonjour")\n',
                    }
                },
            },
            renderer,
        )

        rendered = "\n".join(lines)
        self.assertTrue(rendered_any)
        self.assertIn("• Edited temp_print_test.py (+1 -1)", rendered)
        self.assertIn('    1 -print("hello")', rendered)
        self.assertIn('    1 +print("bonjour")', rendered)
        self.assertNotIn("patch: completed", rendered)
        self.assertNotIn("Success. Updated the following files", rendered)

    def test_cli_human_renderer_apply_patch_failure_matches_upstream(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer._render_tool_completed(
            {
                "name": "apply_patch",
                "call_id": "patch-1",
                "ok": False,
                "output": "apply_patch: failed to find context\nline 2 of context did not match",
            }
        )

        rendered = "\n".join(lines)
        # Mirrors upstream history_cell/patches.rs::new_patch_apply_failure: a
        # title line followed by the stderr rendered as a dim "  └ " output block.
        self.assertIn("✘ Failed to apply patch", rendered)
        self.assertIn("  └ apply_patch: failed to find context", rendered)
        self.assertIn("    line 2 of context did not match", rendered)
        # The old unrendered "patch: failed" title is gone.
        self.assertFalse(rendered.startswith("patch:"))

    def test_cli_human_renderer_write_stdin_failure_renders_as_interaction(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer._tool_arguments["stdin-1"] = {"session_id": 7, "chars": "print('hi')\n"}
        renderer._render_tool_completed(
            {
                "name": "write_stdin",
                "call_id": "stdin-1",
                "ok": False,
                "output": (
                    "write_stdin failed: stdin is closed for this session; "
                    "rerun exec_command with tty=true to keep stdin open"
                ),
                "metadata": {"session_id": "7", "command": "python3 -i", "event_call_id": "cmd-1"},
            }
        )

        rendered = "\n".join(lines)
        # Mirrors the success path / upstream interaction cell: an "Interacted with
        # background terminal" header plus the error in a dim "  └ " output block,
        # not a bare unrendered "write_stdin: failed".
        self.assertIn("Interacted with background terminal", rendered)
        self.assertIn("python3 -i", rendered)
        self.assertIn("  └ write_stdin failed: stdin is closed", rendered)
        self.assertNotIn("write_stdin: failed", rendered)

    def test_cli_human_renderer_write_stdin_argument_failure_renders_as_tool_failure(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer._tool_arguments["stdin-1"] = {"chars": ""}
        renderer._render_tool_completed(
            {
                "name": "write_stdin",
                "call_id": "stdin-1",
                "ok": False,
                "output": "ValueError: missing field `session_id`",
                "metadata": {"tool": "write_stdin"},
            }
        )

        rendered = "\n".join(lines)
        self.assertIn("• Write stdin failed", rendered)
        self.assertIn("  └ ValueError: missing field `session_id`", rendered)
        self.assertNotIn("write_stdin: failed", rendered)
        self.assertNotIn("Waited for background terminal", rendered)

    def test_cli_human_renderer_unknown_tool_failure_uses_action_cell(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer._render_tool_completed(
            {
                "name": "write_sdtin",
                "call_id": "bad-1",
                "ok": False,
                "output": "unknown tool: write_sdtin",
                "metadata": {"tool": "write_sdtin"},
            }
        )

        rendered = "\n".join(lines)
        self.assertIn("• Write sdtin failed", rendered)
        self.assertIn("  └ unknown tool: write_sdtin", rendered)
        self.assertFalse(rendered.startswith("write_sdtin:"))

    def test_resume_transcript_replays_terminal_interactions_with_command_display(self) -> None:
        from volley.cli import _HumanEventRenderer
        from volley.cli import _render_rollout_transcript_records

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        rendered_any = _render_rollout_transcript_records(
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "terminal_interaction",
                        "call_id": "cmd-1",
                        "process_id": "2",
                        "stdin": "",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "exec_command_end",
                        "call_id": "cmd-1",
                        "process_id": "2",
                        "command": ["python3 -i"],
                        "exit_code": 0,
                        "stdout": "done\n",
                        "aggregated_output": "done\n",
                    },
                },
            ],
            renderer,
        )

        rendered = "\n".join(lines)
        self.assertTrue(rendered_any)
        self.assertIn("• Waited for background terminal · python3 -i", rendered)
        self.assertNotIn("background terminal · 2", rendered)

    def test_resume_transcript_replay_keeps_recent_rows_like_upstream_initial_buffer(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout_path = root / "rollout.jsonl"
            records = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"assistant {index}"}],
                    },
                }
                for index in range(5)
            ]
            rollout_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            session = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=root / "volley-home",
                    skip_git_repo_check=True,
                    terminal_resize_reflow_max_rows=3,
                )
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rendered = cli._render_resumed_transcript(session, source_path=rollout_path, color_mode="never")

            output = stderr.getvalue()
            self.assertTrue(rendered)
            self.assertNotIn("assistant 0", output)
            self.assertNotIn("assistant 2", output)
            self.assertIn("assistant 3", output)
            self.assertIn("assistant 4", output)

    def test_resume_transcript_replay_row_cap_matches_upstream_defaults_and_overrides(self) -> None:
        from volley import cli

        self.assertEqual(
            cli._initial_history_replay_max_rows(VolleyConfig()),
            1000,
        )
        self.assertEqual(
            cli._initial_history_replay_max_rows(VolleyConfig(terminal_resize_reflow_max_rows=42)),
            42,
        )
        self.assertIsNone(
            cli._initial_history_replay_max_rows(VolleyConfig(terminal_resize_reflow_max_rows=0))
        )
        self.assertIsNone(
            cli._initial_history_replay_max_rows(VolleyConfig(terminal_resize_reflow_enabled=False))
        )

    def test_chat_startup_panel_summarizes_model_auth_and_fallback_without_secret_leaks(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            id_token = _fake_jwt(
                {
                    "email": "user@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus",
                        "chatgpt_account_id": "account-from-jwt",
                    },
                }
            )
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": id_token,
                            "access_token": _fake_jwt({"exp": int(time.time()) + 3600}),
                            "refresh_token": "secret-refresh-token",
                            "account_id": "account-from-file",
                        },
                    }
                ),
                encoding="utf-8",
            )
            session = VolleySession(
                VolleyConfig(
                    auth_home=home,
                    model="gpt-5.5",
                    model_reasoning_effort="xhigh",
                    skip_git_repo_check=True,
                    ephemeral=True,
                ),
                model_client=FallbackModelClient(
                    primary=ScriptedResponsesModel([]),
                    fallback=ScriptedResponsesModel([]),
                ),
            )

            rows = dict(cli._chat_startup_panel_rows(session))

        self.assertIn("gpt-5.5 / xhigh", rows["Model"])
        self.assertIn("ChatGPT", rows["Auth"])
        self.assertIn("u***r@example.test", rows["Auth"])
        self.assertIn("plus", rows["Auth"])
        self.assertIn("fallback API key", rows["Auth"])
        self.assertNotIn("user@example.test", rows["Auth"])
        self.assertNotIn("secret-refresh-token", json.dumps(rows))

    def test_chat_startup_panel_uses_complete_border_and_large_logo(self) -> None:
        from volley import cli
        from volley.cli import _AnsiStyle
        from volley.cli import _visible_len

        lines = cli._chat_startup_panel_lines(
            [
                ("Model", "gpt-5.5 / xhigh"),
                ("Auth", "ChatGPT plus"),
                ("Provider", "openai"),
                ("Sandbox", "workspace-write · approvals never"),
                ("Thread", "c37d889a-1f69-4e14-88c3-0de84db9c95a"),
            ],
            _AnsiStyle(False),
        )

        self.assertGreaterEqual(len(lines), 12)
        self.assertTrue(lines[0].startswith("╭"))
        self.assertTrue(lines[0].endswith("╮"))
        self.assertNotIn("Volley", lines[0])
        self.assertIn("██╗   ██╗ ██████╗", "\n".join(lines))
        self.assertIn("███████╗███████╗███████╗", "\n".join(lines))
        self.assertIn("Model", "\n".join(lines))
        self.assertIn("Thread", "\n".join(lines))
        self.assertEqual({_visible_len(line) for line in lines}, {_visible_len(lines[0])})

    def test_chat_status_panel_matches_upstream_boxed_shape_and_limits(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            id_token = _fake_jwt(
                {
                    "email": "user@example.test",
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "pro",
                        "chatgpt_account_id": "account-from-jwt",
                    },
                }
            )
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": id_token,
                            "access_token": _fake_jwt({"exp": int(time.time()) + 3600}),
                            "refresh_token": "secret-refresh-token",
                            "account_id": "account-from-file",
                        },
                    }
                ),
                encoding="utf-8",
            )
            session = VolleySession(
                VolleyConfig(
                    auth_home=home,
                    model="gpt-5.5",
                    model_reasoning_effort="xhigh",
                    skip_git_repo_check=True,
                    ephemeral=True,
                ),
                model_client=FallbackModelClient(
                    primary=ScriptedResponsesModel([]),
                    fallback=ScriptedResponsesModel([]),
                ),
            )
            session.state.record_token_usage(
                {
                    "input_tokens": 1200,
                    "cached_input_tokens": 200,
                    "output_tokens": 900,
                    "total_tokens": 2100,
                    "output_tokens_details": {"reasoning_tokens": 150},
                }
            )
            rate_limits = parse_chatgpt_rate_limit_payload(
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 72,
                            "limit_window_seconds": 18000,
                            "reset_at": int(time.time()) + 600,
                        },
                        "secondary_window": {
                            "used_percent": 45,
                            "limit_window_seconds": 604800,
                            "reset_at": int(time.time()) + 1200,
                        },
                    },
                    "credits": {"has_credits": True, "unlimited": False, "balance": "37.6"},
                }
            )

            rendered = "\n".join(
                cli._chat_status_panel_lines(
                    session,
                    style=cli._AnsiStyle(False),
                    rate_limits=rate_limits,
                    terminal_width=100,
                )
            )

        self.assertIn("/status", rendered)
        self.assertIn("╭", rendered)
        self.assertIn("Volley", rendered)
        self.assertIn("Model", rendered)
        self.assertIn("gpt-5.5 (reasoning xhigh, summaries auto)", rendered)
        self.assertIn("Account:", rendered)
        self.assertIn("u***r@example.test", rendered)
        self.assertNotIn("user@example.test", rendered)
        self.assertIn("Context window:", rendered)
        self.assertIn("Session context:", rendered)
        self.assertIn("5h limit:", rendered)
        self.assertIn("28% left", rendered)
        self.assertIn("Weekly limit:", rendered)
        self.assertIn("55% left", rendered)
        self.assertIn("Credits:", rendered)
        self.assertIn("38 credits", rendered)
        self.assertIn("Rollout:", rendered)

    def test_cli_tty_chat_esc_interrupts_long_running_tool(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sleep-1",
                        "arguments": json.dumps({"cmd": "sleep 10", "yield_time_ms": 30000}),
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            }
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "run sleep",
            ],
            env=env,
            interactions=[
                ("", "run sleep"),
                ("", "Working"),
                ("\x1b", "Conversation interrupted"),
                ("/exit\r", None),
            ],
            timeout=12.0,
        )
        plain = _plain_terminal_output(output)

        self.assertRegex(plain, r"Working \([0-9]+s • esc to interrupt\)")
        self.assertIn("ctx ", plain)
        self.assertIn("session ", plain)
        self.assertIn("Ask Volley to do anything", plain)
        self.assertIn("Conversation interrupted", plain)
        self.assertNotIn("Running sleep 10", plain)
        self.assertNotIn("Chunk ID:", plain)
        self.assertNotIn("Process running with session ID", plain)

    def test_cli_tty_completed_command_without_output_renders_no_output_like_upstream(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "true-1",
                        "arguments": json.dumps({"cmd": "true", "yield_time_ms": 30000}),
                    }
                ]
            },
            message("done"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "run true",
            ],
            env=env,
            interactions=[
                ("", "done"),
                ("/exit\r", None),
            ],
            timeout=12.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Ran true", plain)
        self.assertIn("(no output)", plain)
        self.assertNotIn("Chunk ID:", plain)

    def test_cli_tty_chat_pending_input_submits_during_running_turn(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sleep-1",
                        "arguments": json.dumps({"cmd": "sleep 1.5", "yield_time_ms": 3000}),
                    }
                ]
            },
            message("saw queued input"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "start slow turn",
            ],
            env=env,
            interactions=[
                ("", "start slow turn"),
                ("请继续看 tests\n第二行\r", "Messages to be submitted after next tool call"),
                ("", "saw queued input"),
                ("/exit\r", None),
            ],
            timeout=12.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("› start slow turn", plain)
        self.assertIn("Messages to be submitted after next tool call", plain)
        self.assertIn("› 请继续看 tests", plain)
        self.assertIn("  第二行", plain)
        self.assertIn("• saw queued input", plain)
        self.assertNotIn("�", plain)

    def test_cli_tty_esc_with_pending_input_sends_it_immediately(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sleep-1",
                        "arguments": json.dumps({"cmd": "sleep 10", "yield_time_ms": 30000}),
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
            message("saw immediate steer"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "start slow turn",
            ],
            env=env,
            interactions=[
                ("", "Working"),
                ("urgent steer\r", "Messages to be submitted after next tool call"),
                ("\x1b", "Model interrupted to submit steer instructions."),
                ("", "saw immediate steer"),
                ("/exit\r", None),
            ],
            timeout=20.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("› urgent steer", plain)
        self.assertIn("• saw immediate steer", plain)
        self.assertNotIn("Conversation interrupted", plain)

    def test_cli_tty_chat_answers_request_user_input_tool(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        questions = [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick one.",
                "options": [{"label": "A (Recommended)", "description": "Choose A."}],
            }
        ]
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "input-1",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            message("got answer"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-c",
                "collaboration_mode=Plan",
            ],
            env=env,
            interactions=[
                ("start\r", "Pick one."),
                ("1\r", "got answer"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Questions 1/1 answered", plain)
        self.assertIn("answer: A (Recommended)", plain)
        self.assertIn("• got answer", plain)
        self.assertNotIn("request_user_input was cancelled", plain)

    def test_cli_tty_request_user_input_supports_arrow_selection(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        questions = [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick one.",
                "options": [
                    {"label": "A (Recommended)", "description": "Choose A."},
                    {"label": "B", "description": "Choose B."},
                ],
            }
        ]
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "input-1",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            message("arrow answer accepted"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-c",
                "collaboration_mode=Plan",
            ],
            env=env,
            interactions=[
                ("start\r", "Pick one."),
                ("\x1b[B\r", "arrow answer accepted"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Questions 1/1 answered", plain)
        self.assertIn("answer: B", plain)
        self.assertIn("• arrow answer accepted", plain)
        self.assertNotIn("request_user_input was cancelled", plain)

    def test_cli_tty_request_user_input_supports_other_freeform_answer(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        questions = [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick one.",
                "options": [
                    {"label": "A (Recommended)", "description": "Choose A."},
                    {"label": "B", "description": "Choose B."},
                ],
            }
        ]
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "input-1",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            message("other answer accepted"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "-c",
                "collaboration_mode=Plan",
            ],
            env=env,
            interactions=[
                ("start\r", "None of the above"),
                ("\x1b[B\x1b[B\r", "Other:"),
                ("我想自己填\r", "other answer accepted"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Questions 1/1 answered", plain)
        self.assertIn("answer: None of the above", plain)
        self.assertIn("note: 我想自己填", plain)
        self.assertIn("• other answer accepted", plain)
        self.assertNotIn("request_user_input was cancelled", plain)

    def test_cli_tty_chat_plan_command_enables_request_user_input_tool(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        questions = [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick one.",
                "options": [{"label": "A (Recommended)", "description": "Choose A."}],
            }
        ]
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "input-1",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            message("plan answer accepted"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/plan\r", "Switched to Plan mode."),
                ("ask\r", "Pick one."),
                ("1\r", "plan answer accepted"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Switched to Plan mode.", plain)
        self.assertIn("Questions 1/1 answered", plain)
        self.assertIn("• plan answer accepted", plain)
        self.assertNotIn("unavailable in Default mode", plain)

    def test_cli_tty_chat_inline_plan_command_submits_remainder_in_plan_mode(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        questions = [
            {
                "id": "choice",
                "header": "Choice",
                "question": "Pick one.",
                "options": [{"label": "A (Recommended)", "description": "Choose A."}],
            }
        ]
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "input-1",
                        "arguments": json.dumps({"questions": questions}),
                    }
                ]
            },
            message("inline plan answer accepted"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/plan ask with input\r", "Pick one."),
                ("1\r", "inline plan answer accepted"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Switched to Plan mode.", plain)
        self.assertIn("› ask with input", plain)
        self.assertIn("Questions 1/1 answered", plain)
        self.assertIn("• inline plan answer accepted", plain)
        self.assertNotIn("unavailable in Default mode", plain)

    def test_cli_tty_known_unsupported_slash_command_is_not_sent_to_model(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("model should not be called")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/permissions\r", "not available in this Python CLI"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Command '/permissions' is not available in this Python CLI.", plain)
        self.assertNotIn("model should not be called", plain)

    def test_cli_tty_unknown_slash_command_is_not_sent_to_model(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("model should not be called")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/definitely-not-real\r", "Unrecognized command"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Unrecognized command '/definitely-not-real'", plain)
        self.assertNotIn("model should not be called", plain)

    def test_cli_tty_first_prompt_is_not_rendered_as_queued_follow_up(self) -> None:
        # An idle REPL receiving its first prompt must flow straight into the
        # normal turn renderer. Previous solutions were also echoing the input
        # under a "Queued follow-up inputs" banner before the turn started,
        # which both duplicated the prompt and falsely suggested it was being
        # deferred.
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("hi back")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("hello there\r", "hi back"),
                ("/exit\r", None),
            ],
            timeout=15.0,
        )
        plain = _plain_terminal_output(output)

        prelude = plain.split("hi back", 1)[0]
        self.assertNotIn("Queued follow-up inputs", prelude)
        self.assertNotIn("Messages to be submitted after next tool call", prelude)
        self.assertLessEqual(
            prelude.count("hello there"),
            2,
            msg=f"prompt should not be echoed more than twice; got:\n{prelude}",
        )

    def test_cli_tty_chat_accepts_input_after_esc_interrupt(self) -> None:
        # After the user presses ESC to abort a running turn, the REPL must
        # still accept and dispatch the next prompt. A previous regression
        # tore down the keyboard reader thread on ESC, leaving the REPL
        # blocked on an empty input queue forever.
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sleep-1",
                        "arguments": json.dumps({"cmd": "sleep 10", "yield_time_ms": 30000}),
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
            message("post-interrupt-reply"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("run slow\r", "Working"),
                ("\x1b", "Conversation interrupted"),
                ("ping again\r", "post-interrupt-reply"),
                ("/exit\r", None),
            ],
            timeout=20.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Conversation interrupted", plain)
        self.assertIn("post-interrupt-reply", plain)

    def test_cli_tty_render_uses_crlf_line_endings_in_raw_mode(self) -> None:
        # In interactive TUI mode the tty is in raw mode (no OPOST/ONLCR
        # translation), so a bare LF only advances the cursor row, leaving
        # the column wherever the previous line ended. Successive tool
        # cells therefore drift right until they fall off-screen. The
        # renderer must emit CR+LF for every line break, either by
        # leaving OPOST on or by writing "\r\n" explicitly.
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "cmd-1",
                        "arguments": json.dumps({"cmd": "echo first", "yield_time_ms": 2000}),
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "cmd-2",
                        "arguments": json.dumps({"cmd": "echo second", "yield_time_ms": 2000}),
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 2, "total_tokens": 14},
            },
            message("done"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("run two\r", "done"),
                ("/exit\r", None),
            ],
            timeout=20.0,
        )

        crlf_count = output.count("\r\n")
        bare_lf_count = len(re.findall(r"(?<!\r)\n", output))
        self.assertGreater(
            crlf_count,
            5,
            msg=f"expected many CRLF line endings in raw-mode TUI output; "
                f"saw crlf={crlf_count} bare_lf={bare_lf_count}",
        )
        self.assertLess(
            bare_lf_count,
            crlf_count,
            msg=f"raw-mode TUI emitted more bare LFs than CRLFs "
                f"(crlf={crlf_count} bare_lf={bare_lf_count}); "
                f"successive cells will drift right",
        )

    def test_cli_tty_model_slash_command_is_recognized(self) -> None:
        # `/model` is part of the upstream slash-command surface. Solutions
        # have shipped without it, falling through to "Unrecognized command",
        # which both breaks the upstream workflow and confuses the user.
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("model should not be called")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/model\r", None),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertNotIn("Unrecognized command '/model'", plain)
        self.assertNotIn("model should not be called", plain)

    def test_cli_tty_status_slash_command_renders_local_status(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_AUTH_MODE": "api_key",
            "OPENAI_API_KEY": "sk-test",
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/status\r", "Volley"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Model", plain)
        self.assertIn("Auth", plain)
        self.assertIn("Provider", plain)
        self.assertIn("Thread", plain)

    def test_cli_tty_compact_slash_command_renders_context_compacted_not_summary(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("hidden compact summary")]),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
            ],
            env=env,
            interactions=[
                ("/compact\r", "Context compacted"),
                ("/exit\r", None),
            ],
            timeout=10.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Context compacted", plain)
        self.assertNotIn("hidden compact summary", plain)

    def test_cli_tty_compact_slash_command_queues_while_turn_runs(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        responses = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "sleep-1",
                        "arguments": json.dumps({"cmd": "sleep 1.5", "yield_time_ms": 3000}),
                    }
                ]
            },
            message("regular turn done"),
            message("hidden compact summary"),
        ]
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "PY_VOLLEY_FAKE_RESPONSES": json.dumps(responses),
            "PYTHONPATH": os.getcwd(),
        }
        output = self._run_cli_pty(
            [
                sys.executable,
                "-m",
                "volley",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "start slow turn",
            ],
            env=env,
            interactions=[
                ("", "start slow turn"),
                ("/compact\r", "Queued follow-up inputs"),
                ("", "regular turn done"),
                ("", "Context compacted"),
                ("/exit\r", None),
            ],
            timeout=12.0,
        )
        plain = _plain_terminal_output(output)

        self.assertIn("Queued follow-up inputs", plain)
        self.assertIn("↳ /compact", plain)
        self.assertIn("Context compacted", plain)
        self.assertNotIn("hidden compact summary", plain)

    def test_cli_human_renderer_matches_upstream_compaction_and_warning_cells(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(codex_types.VolleyEvent("context_compaction.completed", {}))
        renderer.render(codex_types.VolleyEvent("warning", {"message": LOCAL_COMPACTION_WARNING_MESSAGE}))

        rendered = "\n".join(lines)
        self.assertIn("• Context compacted", rendered)
        self.assertIn("⚠ Heads up: Long threads and multiple compactions", rendered)
        self.assertNotIn("warning:", rendered)

    def test_chat_user_history_lines_use_upstream_gutter_and_wrapping(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render_user_message("这是一个很长很长的用户输入，用来测试换行之后是不是继续缩进")

        self.assertTrue(lines[0].startswith("› "), lines)
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]), lines)

    def test_cli_human_renderer_matches_upstream_plan_update_cell(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "update_plan",
                    "call_id": "plan-1",
                    "ok": True,
                    "metadata": {
                        "explanation": "Adjust the UI renderer.",
                        "plan": [
                            {"step": "Audit upstream cells", "status": "completed"},
                            {"step": "Port renderer", "status": "in_progress"},
                            {"step": "Verify", "status": "pending"},
                        ],
                    },
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertIn("• Updated Plan", rendered)
        self.assertIn("  └ Adjust the UI renderer.", rendered)
        self.assertIn("✔ Audit upstream cells", rendered)
        self.assertIn("□ Port renderer", rendered)
        self.assertIn("□ Verify", rendered)
        self.assertNotIn("> Port renderer", rendered)

    def test_cli_human_renderer_matches_upstream_web_search_cell(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "item.completed",
                {
                    "item": {
                        "type": "web_search_call",
                        "id": "ws-1",
                        "status": "completed",
                        "query": "short query",
                        "action": {"type": "search", "query": "short query"},
                    }
                },
            )
        )

        self.assertEqual(lines, ["• Searched short query"])

    def test_cli_human_renderer_matches_upstream_request_user_input_result_cell(self) -> None:
        from volley.cli import _HumanEventRenderer

        lines: list[str] = []
        renderer = _HumanEventRenderer(color_mode="never", line_sink=lines.append)
        renderer.render(
            codex_types.VolleyEvent(
                "tool.completed",
                {
                    "name": "request_user_input",
                    "call_id": "input-1",
                    "ok": True,
                    "metadata": {
                        "questions": [
                            {
                                "id": "choice",
                                "header": "Choice",
                                "question": "Pick one.",
                                "isOther": True,
                                "options": [{"label": "A (Recommended)", "description": "Choose A."}],
                            }
                        ],
                        "answers": {"choice": {"answers": ["A (Recommended)"]}},
                    },
                },
            )
        )

        rendered = "\n".join(lines)
        self.assertIn("• Questions 1/1 answered", rendered)
        self.assertIn("  • Pick one.", rendered)
        self.assertIn("    answer: A (Recommended)", rendered)

    def _run_cli_pty(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        interactions: list[tuple[str, str | None]],
        timeout: float = 30.0,
    ) -> str:
        import pty
        import select

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            cwd=os.getcwd(),
            close_fds=True,
        )
        os.close(slave_fd)
        output = bytearray()

        def read_available(wait: float) -> None:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if not readable:
                    if proc.poll() is not None:
                        return
                    continue
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                output.extend(chunk)

        def read_until(needle: str) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                text = output.decode("utf-8", errors="replace")
                plain = _plain_terminal_output(text)
                if needle in plain:
                    return
                read_available(0.1)
            text = output.decode("utf-8", errors="replace")
            plain = _plain_terminal_output(text)
            self.fail(f"timed out waiting for {needle!r}; output was:\n{plain}")

        try:
            read_available(1.5)
            for chars, expected in interactions:
                if chars:
                    os.write(master_fd, chars.encode("utf-8"))
                if expected is not None:
                    read_until(expected)
                else:
                    read_available(0.5)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=2)
            read_available(0.2)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            os.close(master_fd)
        return output.decode("utf-8", errors="replace")

    def test_command_actions_classify_common_exploration_commands(self) -> None:
        self.assertEqual(parse_command_actions("rg --files | head -n 50")[0]["type"], "list_files")
        self.assertEqual(parse_command_actions("rg -n foo agents/volley")[0]["type"], "search")
        self.assertEqual(parse_command_actions("cat agents/volley/cli.py")[0]["type"], "read")

    def test_cli_exec_accepts_upstream_short_aliases_and_compat_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            last = root / "last.txt"
            schema = root / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    }
                ),
                encoding="utf-8",
            )
            extra = root / "extra"
            extra.mkdir()
            env = {
                **os.environ,
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("alias done")]),
                "PYTHONPATH": os.getcwd(),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--experimental-json",
                    "-m",
                    "gpt-5.2",
                    "-C",
                    str(root),
                    "-s",
                    "workspace-write",
                    "--add-dir",
                    str(extra),
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--output-schema",
                    str(schema),
                    "-o",
                    str(last),
                    "hello",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(last.read_text(encoding="utf-8"), "alias done")
            self.assertTrue(completed.stdout.strip())

    def test_cli_exec_loads_config_profile_and_dotted_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            volley_home.mkdir()
            (volley_home / "config.toml").write_text(
                """
model = "base-model"
web_search = "disabled"

[profiles.dev]
model = "profile-model"
web_search = "disabled"
model_reasoning_effort = "high"
""",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "VOLLEY_HOME": str(volley_home),
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("config done")]),
                "PYTHONPATH": os.getcwd(),
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--json",
                    "--profile",
                    "dev",
                    "-c",
                    'profiles.dev.model="override-model"',
                    "-c",
                    'profiles.dev.web_search="live"',
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "hello",
                ],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            events = [json.loads(line) for line in completed.stdout.splitlines()]
            thread_started = next(event for event in events if event["type"] == "thread.started")
            model_request = next(event for event in events if event["type"] == "model.request")
            self.assertEqual(thread_started["model"], "override-model")
            self.assertIn("web_search", model_request["tool_names"])

    def test_cli_exec_reads_official_codex_config_before_python_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            official_home = home / ".volley"
            python_home = home / ".volley-python"
            official_home.mkdir()
            (official_home / "config.toml").write_text(
                """
model = "gpt-5.5"
model_reasoning_effort = "high"
""",
                encoding="utf-8",
            )
            python_home.mkdir()
            (python_home / "config.toml").write_text(
                """
model = "gpt-5.5"
model_reasoning_effort = "low"
""",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("official config done")]),
                "PYTHONPATH": os.getcwd(),
            }
            env.pop("VOLLEY_HOME", None)
            env.pop("VOLLEY_PY_HOME", None)
            env.pop("OPENAI_REASONING_EFFORT", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "hello",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            events = [json.loads(line) for line in completed.stdout.splitlines()]
            thread_started = next(event for event in events if event["type"] == "thread.started")
            self.assertEqual(thread_started["model"], "gpt-5.5")

            rollout_path = next(
                (python_home / "sessions").glob(f"????/??/??/rollout-*-{thread_started['thread_id']}.jsonl")
            )
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            turn_context = next(record["payload"] for record in records if record["type"] == "turn_context")
            self.assertEqual(turn_context["effort"], "high")
            self.assertFalse((official_home / "sessions").exists())

    def test_cli_exec_oss_flags_and_provider_config_with_fake_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            volley_home.mkdir()
            env = {
                **os.environ,
                "VOLLEY_HOME": str(volley_home),
                "PYTHONPATH": os.getcwd(),
            }
            local_provider = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--json",
                    "--oss",
                    "--local-provider",
                    "lmstudio",
                    "--full-auto",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "hello",
                ],
                text=True,
                capture_output=True,
                env={**env, "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("oss done")])},
                check=False,
            )
            self.assertEqual(local_provider.returncode, 0, local_provider.stderr)
            self.assertIn("`--full-auto` is deprecated", local_provider.stderr)
            events = [json.loads(line) for line in local_provider.stdout.splitlines()]
            thread_started = next(event for event in events if event["type"] == "thread.started")
            self.assertEqual(thread_started["model"], "openai/gpt-oss-20b")
            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{thread_started['thread_id']}.jsonl"))
            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["payload"]["meta"]["model_provider"], "lmstudio")

            (volley_home / "config.toml").write_text('oss_provider = "ollama"\n', encoding="utf-8")
            configured_provider = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "--json",
                    "--oss",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "hello",
                ],
                text=True,
                capture_output=True,
                env={**env, "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("oss config done")])},
                check=False,
            )
            self.assertEqual(configured_provider.returncode, 0, configured_provider.stderr)
            configured_events = [json.loads(line) for line in configured_provider.stdout.splitlines()]
            configured_thread = next(event for event in configured_events if event["type"] == "thread.started")
            self.assertEqual(configured_thread["model"], "gpt-oss:20b")

    def test_cli_exec_resume_rollout_path_and_last_with_fake_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            first = VolleySession(
                VolleyConfig(
                    cwd=root,
                    volley_home=volley_home,
                    skip_git_repo_check=True,
                    ephemeral=False,
                ),
                model_client=ScriptedResponsesModel([message("first answer")]),
            ).run("first")
            rollout_path = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{first.thread_id}.jsonl"))

            env = {
                **os.environ,
                "VOLLEY_HOME": str(volley_home),
                "PYTHONPATH": os.getcwd(),
            }
            path_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "resume",
                    "--json",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    str(rollout_path),
                    "second",
                ],
                text=True,
                capture_output=True,
                env={**env, "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("second answer")])},
                check=False,
            )
            self.assertEqual(path_completed.returncode, 0, path_completed.stderr)
            path_events = [json.loads(line) for line in path_completed.stdout.splitlines()]
            path_thread = next(event for event in path_events if event["type"] == "thread.started")
            self.assertEqual(path_thread["thread_id"], first.thread_id)

            last_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "resume",
                    "--last",
                    "--json",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "third",
                ],
                text=True,
                capture_output=True,
                env={**env, "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("third answer")])},
                check=False,
            )
            self.assertEqual(last_completed.returncode, 0, last_completed.stderr)
            last_events = [json.loads(line) for line in last_completed.stdout.splitlines()]
            last_thread = next(event for event in last_events if event["type"] == "thread.started")
            last_turn = next(event for event in last_events if event["type"] == "turn.completed")
            self.assertEqual(last_thread["thread_id"], first.thread_id)
            self.assertEqual(last_turn["final_message"], "third answer")

            records = [json.loads(line) for line in rollout_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(1 for record in records if record["type"] == "session_meta"), 1)
            reconstructed = reconstruct_history_from_rollout(rollout_path)
            assistant_texts = [
                item["content"][0]["text"]
                for item in reconstructed.history
                if item.get("role") == "assistant"
            ]
            self.assertEqual(assistant_texts, ["first answer", "second answer", "third answer"])

            fork_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "exec",
                    "fork",
                    "--json",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    str(rollout_path),
                    "forked",
                ],
                text=True,
                capture_output=True,
                env={**env, "PY_VOLLEY_FAKE_RESPONSES": json.dumps([message("fork answer")])},
                check=False,
            )
            self.assertEqual(fork_completed.returncode, 0, fork_completed.stderr)
            fork_events = [json.loads(line) for line in fork_completed.stdout.splitlines()]
            fork_thread = next(event for event in fork_events if event["type"] == "thread.started")
            self.assertNotEqual(fork_thread["thread_id"], first.thread_id)
            fork_rollout = next((volley_home / "sessions").glob(f"????/??/??/rollout-*-{fork_thread['thread_id']}.jsonl"))
            fork_records = [json.loads(line) for line in fork_rollout.read_text(encoding="utf-8").splitlines()]
            fork_meta = next(record["payload"] for record in fork_records if record["type"] == "session_meta")
            self.assertEqual(fork_meta["meta"]["forked_from_id"], first.thread_id)
            fork_reconstructed = reconstruct_history_from_rollout(fork_rollout)
            fork_assistant_texts = [
                item["content"][0]["text"]
                for item in fork_reconstructed.history
                if item.get("role") == "assistant"
            ]
            self.assertEqual(fork_assistant_texts, ["first answer", "second answer", "third answer", "fork answer"])

    def test_interactive_slash_resume_and_fork_replace_chat_session(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            first = VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("saved answer")]),
            ).run("saved")
            current = VolleySession(config, model_client=ScriptedResponsesModel([]))

            with redirect_stderr(io.StringIO()):
                resumed = cli._handle_interactive_slash_command(current, "/resume --last")

            self.assertTrue(resumed.handled)
            self.assertIsNotNone(resumed.session)
            self.assertTrue(resumed.printed_transcript)
            assert resumed.session is not None
            self.assertEqual(resumed.session.state.thread_id, first.thread_id)
            self.assertEqual(
                [
                    item["content"][0]["text"]
                    for item in resumed.session.state.history
                    if item.get("role") == "assistant"
                ],
                ["saved answer"],
            )

            with redirect_stderr(io.StringIO()):
                forked = cli._handle_interactive_slash_command(resumed.session, "/fork")

            self.assertTrue(forked.handled)
            self.assertTrue(forked.printed_transcript)
            self.assertIsNotNone(forked.session)
            assert forked.session is not None
            self.assertNotEqual(forked.session.state.thread_id, first.thread_id)
            self.assertEqual(forked.session.state.forked_from_id, first.thread_id)
            self.assertEqual(forked.session.state.history, resumed.session.state.history)

    def test_interactive_new_and_clear_start_fresh_chat_session(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = VolleyConfig(cwd=root, skip_git_repo_check=True, ephemeral=True)
            current = VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("old answer")]),
            )
            current.run("old prompt")

            with redirect_stderr(io.StringIO()):
                new_result = cli._handle_interactive_slash_command(current, "/new")

            self.assertTrue(new_result.handled)
            self.assertIsNotNone(new_result.session)
            assert new_result.session is not None
            self.assertNotEqual(new_result.session.state.thread_id, current.state.thread_id)
            self.assertEqual(new_result.session.state.history, [])
            self.assertIs(new_result.session.model_client, current.model_client)

            with redirect_stderr(io.StringIO()):
                clear_result = cli._handle_interactive_slash_command(current, "/clear")

            self.assertTrue(clear_result.handled)
            self.assertIsNotNone(clear_result.session)
            assert clear_result.session is not None
            self.assertNotEqual(clear_result.session.state.thread_id, current.state.thread_id)
            self.assertEqual(clear_result.session.state.history, [])

    def test_interactive_fast_slash_toggles_service_tier_and_status(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_volley_home = os.environ.get("VOLLEY_HOME")
            os.environ["VOLLEY_HOME"] = str(root / "volley-home")
            try:
                session = VolleySession(
                    VolleyConfig(model="gpt-5.5", cwd=root, skip_git_repo_check=True, ephemeral=True),
                    model_client=ScriptedResponsesModel([]),
                )

                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    enabled = cli._handle_interactive_slash_command(session, "/fast", color_mode="never")

                self.assertTrue(enabled.handled)
                self.assertEqual(session.config.service_tier, "priority")
                self.assertEqual(session.config.resolved_service_tier(), "priority")
                self.assertIn("Service tier set to priority", stderr.getvalue())
                self.assertIn("Fast on", "\n".join(cli._chat_status_panel_lines(session, terminal_width=100)))

                config_text = (Path(os.environ["VOLLEY_HOME"]) / "config.toml").read_text(encoding="utf-8")
                self.assertIn('service_tier = "fast"', config_text)
                self.assertIn("fast_default_opt_out = false", config_text)

                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    disabled = cli._handle_interactive_slash_command(session, "/fast", color_mode="never")

                self.assertTrue(disabled.handled)
                self.assertIn("Service tier set to default", stderr.getvalue())
                self.assertIsNone(session.config.service_tier)
                self.assertTrue(session.config.fast_default_opt_out)
                config_text = (Path(os.environ["VOLLEY_HOME"]) / "config.toml").read_text(encoding="utf-8")
                self.assertNotIn("service_tier", config_text)
                self.assertIn("fast_default_opt_out = true", config_text)
            finally:
                if old_volley_home is None:
                    os.environ.pop("VOLLEY_HOME", None)
                else:
                    os.environ["VOLLEY_HOME"] = old_volley_home

    def test_interactive_ps_and_stop_manage_background_terminals(self) -> None:
        from volley import cli

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=ScriptedResponsesModel([]),
        )
        command = f"{sys.executable!r} -c \"import time; time.sleep(5)\""
        result = session.tools.exec_command({"cmd": command, "yield_time_ms": 10})
        self.assertTrue(result.ok)
        self.assertIn("session_id", result.metadata)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            ps_result = cli._handle_interactive_slash_command(session, "/ps")

        self.assertTrue(ps_result.handled)
        self.assertIn("Background terminals:", stderr.getvalue())
        self.assertIn(str(result.metadata["session_id"]), stderr.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            stop_result = cli._handle_interactive_slash_command(session, "/stop")

        self.assertTrue(stop_result.handled)
        self.assertIn("Stopped 1 background terminal", stderr.getvalue())
        self.assertEqual(cli._background_terminal_rows(session), [])

    def test_cli_top_level_help_lists_resume_and_fork(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "volley", "--help"],
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": os.getcwd()},
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("resume", completed.stdout)
        self.assertIn("fork", completed.stdout)

    def test_rollout_picker_rows_use_first_user_message_preview_and_upstream_controls(self) -> None:
        from volley import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            first = VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("saved answer")]),
            ).run("first saved prompt\nwith detail")

            rows = cli._rollout_picker_rows(config)
            row = next(item for item in rows if item.thread_id == first.thread_id)
            self.assertEqual(row.preview, "first saved prompt with detail")

            lines = cli._rollout_picker_display_lines(
                [row],
                title="Resume a previous session",
                style=cli._AnsiStyle(False),
                cwd=root,
                show_all=False,
                query="",
                sort_key="updated",
                selected=0,
                offset=0,
                density="comfortable",
                toolbar_focus="filter",
                expanded=True,
            )
            rendered = "\n".join(lines)
            self.assertIn("Resume a previous session", rendered)
            self.assertIn("Type to search", rendered)
            self.assertIn("Filter:[Cwd]", rendered)
            self.assertIn("Sort:[Updated]", rendered)
            self.assertIn("❯ first saved prompt with detail", rendered)
            self.assertIn("id: " + first.thread_id, rendered)
            self.assertIn("enter resume", rendered)
            self.assertIn("ctrl+o dense/comfortable", rendered)

            many_rows = [
                cli._RolloutPickerRow(
                    path=root / f"rollout-{index}.jsonl",
                    preview=f"session preview {index}",
                    thread_id=f"thread-{index}",
                    created_at=time.time() - index,
                    updated_at=time.time() - index,
                    cwd=str(root),
                )
                for index in range(30)
            ]
            many_lines = cli._rollout_picker_display_lines(
                many_rows,
                title="Resume a previous session",
                style=cli._AnsiStyle(False),
                cwd=root,
                show_all=False,
                query="",
                sort_key="updated",
                selected=0,
                offset=0,
                density="comfortable",
                toolbar_focus="filter",
                expanded=False,
            )
            many_rendered = "\n".join(many_lines)
            self.assertIn("session preview 0", many_rendered)
            self.assertIn("↓", many_rendered)
            self.assertNotIn("session preview 29", many_rendered)

    def test_cli_tty_top_level_resume_opens_picker_and_enters_chat(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("saved answer")]),
            ).run("resume picker prompt")
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "VOLLEY_HOME": str(volley_home),
                "PYTHONPATH": os.getcwd(),
            }

            output = self._run_cli_pty(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "resume",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                ],
                env=env,
                interactions=[
                    ("", "resume picker prompt"),
                    ("\r", "› "),
                    ("/exit\r", None),
                ],
                timeout=10.0,
            )
            plain = _plain_terminal_output(output)

            self.assertIn("Resume a previous session", plain)
            self.assertIn("resume picker prompt", plain)
            self.assertIn("saved answer", plain)
            self.assertIn("Filter:[Cwd]", plain)
            self.assertIn("Sort:[Updated]", plain)
            self.assertNotIn("Select a chat number", plain)

    def test_cli_tty_slash_resume_leaves_gap_before_next_prompt(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("saved answer")]),
            ).run("saved prompt")
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "VOLLEY_HOME": str(volley_home),
                "PYTHONPATH": os.getcwd(),
            }

            output = self._run_cli_pty(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                ],
                env=env,
                interactions=[
                    ("/resume --last\r", "saved answer"),
                    ("", "› "),
                    ("/exit\r", None),
                ],
                timeout=10.0,
            )
            plain = _plain_terminal_output(output)

            self.assertIn("saved answer", plain)
            self.assertRegex(plain, r"saved answer(?:\r?\n\s*){2,}› Ask Volley")

    def test_cli_tty_top_level_fork_uses_same_picker_surface(self) -> None:
        if sys.platform == "win32":
            self.skipTest("PTY smoke is Unix-only")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            volley_home = root / "volley-home"
            config = VolleyConfig(
                cwd=root,
                volley_home=volley_home,
                skip_git_repo_check=True,
                ephemeral=False,
            )
            VolleySession(
                config,
                model_client=ScriptedResponsesModel([message("fork source answer")]),
            ).run("fork picker prompt")
            env = {
                **os.environ,
                "TERM": "xterm-256color",
                "VOLLEY_HOME": str(volley_home),
                "PYTHONPATH": os.getcwd(),
            }

            output = self._run_cli_pty(
                [
                    sys.executable,
                    "-m",
                    "volley",
                    "fork",
                    "-C",
                    str(root),
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                ],
                env=env,
                interactions=[
                    ("", "fork picker prompt"),
                    ("\r", "› "),
                    ("/exit\r", None),
                ],
                timeout=10.0,
            )
            plain = _plain_terminal_output(output)

            self.assertIn("Fork a previous session", plain)
            self.assertIn("fork picker prompt", plain)
            self.assertIn("fork source answer", plain)
            self.assertIn("Filter:[Cwd]", plain)
            self.assertIn("Sort:[Updated]", plain)
            self.assertNotIn("Select a chat number", plain)

    def test_interactive_theme_slash_sets_python_cli_syntax_theme(self) -> None:
        from volley import cli

        old_theme = cli._CLI_SYNTAX_THEME
        try:
            session = VolleySession(
                VolleyConfig(skip_git_repo_check=True, ephemeral=True),
                model_client=ScriptedResponsesModel([]),
            )
            with redirect_stderr(io.StringIO()):
                result = cli._handle_interactive_slash_command(session, "/theme dracula")

            self.assertTrue(result.handled)
            self.assertEqual(cli._CLI_SYNTAX_THEME, "dracula")
            self.assertEqual(cli._pygments_style_name(), "dracula")
        finally:
            cli._CLI_SYNTAX_THEME = old_theme


    # ------------------------------------------------------------------
    # Sanity / smoke tests for the basics agents have shipped broken.
    # ------------------------------------------------------------------

    def test_codex_package_imports_cleanly(self) -> None:
        """`python3 -c "import volley; import volley.X..."` must succeed for every
        submodule the eval reaches into. Catches NameErrors / missing typing
        imports / circular-import explosions that prevent the suite from
        starting at all."""
        env = dict(os.environ)
        env.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        env["PY_VOLLEY_AUTH_MODE"] = "api_key"
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import volley; "
                "import volley.types; import volley.state; import volley.prompts; "
                "import volley.tools; import volley.model; import volley.memory; "
                "import volley.cli",
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_python_m_codex_help_lists_known_subcommands(self) -> None:
        """`python3 -m volley --help` must succeed and mention `exec`. Catches
        agents that ship without a top-level CLI (no `__main__`, no parser)."""
        env = dict(os.environ)
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-m", "volley", "--help"],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("exec", completed.stdout)
        completed = subprocess.run(
            [sys.executable, "-m", "volley", "exec", "--help"],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_default_model_client_is_openai_responses_when_no_fake_env(self) -> None:
        """Without `PY_VOLLEY_FAKE_RESPONSES`, `default_model_client()` must
        return an `OpenAIResponsesModel` instance — the live API client.
        Catches agents that ship hardcoded stubs as the default."""
        env = dict(os.environ)
        env.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os\n"
                "os.environ.pop('PY_VOLLEY_FAKE_RESPONSES', None)\n"
                "os.environ['PY_VOLLEY_AUTH_MODE'] = 'api_key'\n"
                "from volley.model import default_model_client, OpenAIResponsesModel\n"
                "client = default_model_client()\n"
                "assert isinstance(client, OpenAIResponsesModel), "
                "'default_model_client() must return OpenAIResponsesModel when "
                "no PY_VOLLEY_FAKE_RESPONSES is set; got %s' % type(client).__name__\n",
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_codex_session_default_model_client_is_openai_responses(self) -> None:
        """A bare `VolleySession()` (no explicit model_client) must wire to
        `OpenAIResponsesModel`. Catches agents whose VolleySession constructor
        silently picks a stub."""
        env = dict(os.environ)
        env.pop("PY_VOLLEY_FAKE_RESPONSES", None)
        env["PY_VOLLEY_AUTH_MODE"] = "api_key"
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os\n"
                "os.environ.pop('PY_VOLLEY_FAKE_RESPONSES', None)\n"
                "from volley import VolleyConfig, VolleySession\n"
                "from volley.model import OpenAIResponsesModel\n"
                "session = VolleySession(VolleyConfig(skip_git_repo_check=True, ephemeral=True, auth_mode='api_key'))\n"
                "assert isinstance(session.model_client, OpenAIResponsesModel), "
                "'VolleySession.model_client must default to OpenAIResponsesModel; "
                "got %s' % type(session.model_client).__name__\n",
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_py_codex_fake_responses_env_is_honored_by_default_client(self) -> None:
        """Inverse guard: when `PY_VOLLEY_FAKE_RESPONSES` IS set,
        `default_model_client()` must return a `ScriptedResponsesModel` —
        catching agents that ignore the env var and unconditionally call
        OpenAI even in CI / test runs."""
        env = dict(os.environ)
        env["PY_VOLLEY_FAKE_RESPONSES"] = json.dumps([
            {"output": [{"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}]}
        ])
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from volley.model import default_model_client, ScriptedResponsesModel\n"
                "client = default_model_client()\n"
                "assert isinstance(client, ScriptedResponsesModel), "
                "'PY_VOLLEY_FAKE_RESPONSES set must yield ScriptedResponsesModel; "
                "got %s' % type(client).__name__\n",
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_codex_session_stream_routes_through_model_client(self) -> None:
        """`session.run(prompt)` must invoke the configured `model_client`
        (via `.stream(...)` or `.create(...)`) at least once. Catches agents
        whose `session.stream()` yields hardcoded events and bypasses the
        client entirely (teamwork-style fake REPL output)."""
        calls: list[Any] = []

        class _TrackingModel:
            def stream(self, request: PromptRequest):
                calls.append(("stream", request))
                yield ModelStreamEvent(
                    type="response.completed",
                    payload={"id": "track-1", "output": [
                        {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}
                    ]},
                )

            def create(self, request: PromptRequest):
                calls.append(("create", request))
                return type("R", (), {"id": "track-1", "output": [
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ok"}]}
                ], "raw": {}})()

        session = VolleySession(
            VolleyConfig(skip_git_repo_check=True, ephemeral=True),
            model_client=_TrackingModel(),
        )
        session.run("hello")
        self.assertGreaterEqual(
            len(calls), 1,
            "session.run() did not invoke model_client.stream/create — "
            "implementation is yielding hardcoded events instead of using "
            "the configured model client.",
        )

    def test_cli_exec_exit_codes_reflect_outcome(self) -> None:
        """`python3 -m volley exec` must exit 0 on success and non-zero on
        failure. Catches CLIs that always exit 0 (downstream CI cannot tell
        success from failure)."""
        env_ok = dict(os.environ)
        env_ok["PY_VOLLEY_FAKE_RESPONSES"] = json.dumps([
            {"output": [{"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}]}
        ])
        env_ok["PYTHONPATH"] = os.getcwd() + os.pathsep + env_ok.get("PYTHONPATH", "")
        ok = subprocess.run(
            [sys.executable, "-m", "volley", "exec",
             "--skip-git-repo-check", "--ephemeral", "say hi"],
            env=env_ok,
            text=True,
            capture_output=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)

        env_bad = dict(os.environ)
        env_bad["PY_VOLLEY_FAKE_RESPONSES"] = "not-json"
        env_bad["PYTHONPATH"] = os.getcwd() + os.pathsep + env_bad.get("PYTHONPATH", "")
        bad = subprocess.run(
            [sys.executable, "-m", "volley", "exec",
             "--skip-git-repo-check", "--ephemeral", "say hi"],
            env=env_bad,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(bad.returncode, 0,
                            "exec must exit non-zero when the model layer fails")

    def test_apply_patch_rejects_stale_context_without_modifying_files(self) -> None:
        """`apply_patch` must reject a hunk whose context line doesn't match
        the file, AND must not leave any partial modifications behind.
        Atomicity matters — half-applied patches corrupt the workspace."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            target.write_text("real line A\nreal line B\nreal line C\n",
                              encoding="utf-8")
            original = target.read_text(encoding="utf-8")
            runtime = ToolRuntime(VolleyConfig(
                cwd=tmp,
                writable_roots=(tmp,),
                skip_git_repo_check=True,
                ephemeral=True,
            ))
            stale_patch = (
                "*** Begin Patch\n"
                "*** Update File: file.txt\n"
                "@@\n"
                " WRONG CONTEXT LINE\n"
                "-real line B\n"
                "+real line B!!!\n"
                "*** End Patch\n"
            )
            result = runtime.apply_patch({"patch": stale_patch})
            self.assertFalse(result.ok,
                             f"apply_patch should fail on stale context; got ok=True, output={result.output!r}")
            self.assertEqual(target.read_text(encoding="utf-8"), original,
                             "apply_patch must leave the file untouched when "
                             "the patch is rejected")


if __name__ == "__main__":
    unittest.main()
