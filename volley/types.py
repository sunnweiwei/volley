from __future__ import annotations

import json
import os
import sys
import uuid

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ApprovalPolicy = Literal["untrusted", "on-failure", "on-request", "never"]
NetworkAccess = Literal["restricted", "enabled"]
CollaborationMode = Literal["Default", "Plan", "Execute", "Pair Programming"]
RemoteCompactionMode = Literal["auto", "off", "required"]
AuthModeSelection = Literal["auto", "api_key", "chatgpt"]
LifecyclePhase = Literal["thread", "turn", "model", "item", "tool", "compaction", "diagnostic"]


@dataclass(frozen=True)
class LifecycleStep:
    name: str
    event_type: str
    phase: LifecyclePhase
    terminal: bool = False
    mutates_history: bool = False


EXEC_LIFECYCLE: tuple[LifecycleStep, ...] = (
    LifecycleStep("thread_start", "thread.started", "thread"),
    LifecycleStep("turn_start", "turn.started", "turn"),
    LifecycleStep("record_user_input", "item.completed", "item", mutates_history=True),
    LifecycleStep("model_request", "model.request", "model"),
    LifecycleStep("model_item_start", "item.started", "item"),
    LifecycleStep("model_item_delta", "item.delta", "item"),
    LifecycleStep("token_count", "token_count", "model"),
    LifecycleStep("model_response", "model.response", "model"),
    LifecycleStep("record_model_item", "item.completed", "item", mutates_history=True),
    LifecycleStep("tool_start", "tool.started", "tool"),
    LifecycleStep("exec_output_delta", "exec_command.output_delta", "tool"),
    LifecycleStep("tool_complete", "tool.completed", "tool"),
    LifecycleStep("turn_diff", "turn_diff", "turn"),
    LifecycleStep("record_tool_output", "item.completed", "item", mutates_history=True),
    LifecycleStep("turn_complete", "turn.completed", "turn", terminal=True),
    LifecycleStep("turn_aborted", "turn.aborted", "turn", terminal=True, mutates_history=True),
    LifecycleStep("turn_failed", "turn.failed", "turn", terminal=True),
    LifecycleStep("stream_error", "stream_error", "diagnostic"),
    LifecycleStep("hook_start", "hook.started", "diagnostic"),
    LifecycleStep("hook_complete", "hook.completed", "diagnostic"),
)

COMPACTION_LIFECYCLE: tuple[LifecycleStep, ...] = (
    LifecycleStep("compaction_start", "context_compaction.started", "compaction"),
    LifecycleStep("compaction_model_request", "model.request", "model"),
    LifecycleStep("compaction_model_item_start", "item.started", "item"),
    LifecycleStep("compaction_model_item_delta", "item.delta", "item"),
    LifecycleStep("compaction_token_count", "token_count", "model"),
    LifecycleStep("compaction_model_response", "model.response", "model"),
    LifecycleStep("record_compaction_item", "item.completed", "item"),
    LifecycleStep("replace_history", "context_compaction.completed", "compaction", mutates_history=True),
    LifecycleStep("compaction_warning", "warning", "diagnostic"),
)

GOAL_LIFECYCLE: tuple[LifecycleStep, ...] = (
    LifecycleStep("thread_goal_updated", "thread.goal.updated", "thread", mutates_history=False),
    LifecycleStep("thread_goal_cleared", "thread.goal.cleared", "thread", mutates_history=False),
)

KNOWN_EVENT_TYPES = frozenset(
    step.event_type for step in (*EXEC_LIFECYCLE, *COMPACTION_LIFECYCLE, *GOAL_LIFECYCLE)
)
TERMINAL_TURN_EVENT_TYPES = frozenset(
    step.event_type for step in EXEC_LIFECYCLE if step.terminal
)


def lifecycle_event_types() -> tuple[str, ...]:
    return tuple(dict.fromkeys(step.event_type for step in (*EXEC_LIFECYCLE, *COMPACTION_LIFECYCLE, *GOAL_LIFECYCLE)))


@dataclass(frozen=True)
class VolleyConfig:
    """Runtime configuration for the Python Volley core."""

    model: str = field(default_factory=lambda: os.environ.get("OPENAI_MODEL", "gpt-5.5"))
    model_provider_id: str = "openai"
    session_source: str = "cli"
    cwd: Path | str = field(default_factory=Path.cwd)
    sandbox: SandboxMode = "workspace-write"
    approval_policy: ApprovalPolicy = "never"
    network_access: NetworkAccess = "restricted"
    writable_roots: tuple[Path | str, ...] = ()
    exclude_tmpdir_env_var: bool = False
    exclude_slash_tmp: bool = False
    volley_home: Path | str | None = None
    auth_mode: AuthModeSelection = field(default_factory=lambda: _default_auth_mode())
    auth_home: Path | str | None = None
    chatgpt_base_url: str | None = None
    openai_base_url: str | None = None
    gemini_base_url: str | None = None
    json_events: bool = False
    output_last_message: Path | str | None = None
    skip_git_repo_check: bool = False
    ephemeral: bool = False
    max_iterations: int | None = None
    prompt_asset: str = "auto"
    compact_prompt: str | None = None
    model_context_window: int | None = None
    model_auto_compact_token_limit: int | None = None
    include_unified_exec_tool: bool = field(default_factory=lambda: sys.platform != "win32")
    include_shell_command_tool: bool = field(default_factory=lambda: sys.platform == "win32")
    include_update_plan_tool: bool = True
    include_request_user_input_tool: bool = True
    include_view_image_tool: bool = True
    include_multi_agent_tools: bool = True
    goals_enabled: bool = True
    include_goal_tools: bool = True
    agent_depth: int = 0
    max_agent_depth: int = 1
    include_web_search_tool: bool = True
    web_search_external_web_access: bool = False
    web_search_filters: dict[str, Any] | None = None
    web_search_user_location: dict[str, Any] | None = None
    web_search_context_size: Literal["low", "medium", "high"] | None = None
    web_search_content_types: tuple[str, ...] | None = None
    include_environment_context: bool = True
    include_permissions_instructions: bool = True
    collaboration_mode: CollaborationMode = "Default"
    approval_provider: Any | None = None
    hook_provider: Any | None = None
    request_user_input_available_modes: tuple[CollaborationMode, ...] = ("Plan",)
    request_user_input_answers: dict[str, Any] | None = None
    request_user_input_provider: Any | None = None
    model_supports_image_input: bool | None = None
    model_supports_image_detail_original: bool | None = None
    memory_tool_enabled: bool = False
    memory_generate_memories: bool = True
    memory_disable_on_external_context: bool = False
    use_memories: bool = True
    memory_state_store: Any | None = None
    memory_startup_background: bool = True
    memory_run_phase2_on_startup: bool = True
    memory_max_raw_memories_for_consolidation: int = 256
    memory_max_unused_days: int = 30
    memory_max_rollout_age_days: int = 10
    memory_max_rollouts_per_startup: int = 2
    memory_min_rollout_idle_hours: int = 6
    memory_rate_limit_provider: Any | None = None
    memory_min_rate_limit_remaining_percent: int = 25
    model_reasoning_effort: str | None = None
    model_reasoning_summary: str | None = None
    model_verbosity: str | None = None
    service_tier: str | None = None
    fast_mode_enabled: bool = True
    fast_default_opt_out: bool = False
    account_plan_type: str | None = None
    model_stream_max_retries: int | None = None
    model_stream_retry_base_delay_ms: int = 200
    show_raw_agent_reasoning: bool = False
    bypass_hook_trust: bool = False
    client_metadata: dict[str, str] | None = None
    output_schema: dict[str, Any] | None = None
    output_schema_strict: bool = True
    input_images: tuple[Path | str, ...] = ()
    provider_is_azure_responses_endpoint: bool = False
    remote_compaction: RemoteCompactionMode = field(default_factory=lambda: _default_remote_compaction_mode())
    # Reference-port divergence from official Volley. After the normal compaction
    # runs, this REPLACES the recent-user-message prefix with one coherent,
    # aggressively-truncated "recent activity" block (recent user turns +
    # assistant prose + tool calls + program outputs) built from the
    # pre-compaction history, capped at this many tokens; long content is
    # offloaded to files under `recent_context_offload_dir` with an inline note so
    # the model can read the full original back. `None` = auto: on (10k) for
    # persistent sessions, off for ephemeral ones; set 0 for behavior identical to
    # official Volley; negative = ~10% of the context window. See PARITY_AUDIT.md.
    recent_context_offload_tokens: int | None = field(default_factory=lambda: _default_recent_context_offload_tokens())
    recent_context_offload_dir: Path | str | None = None
    terminal_resize_reflow_enabled: bool = True
    terminal_resize_reflow_max_rows: int | None = None
    current_date: str | None = None
    timezone: str | None = None
    use_responses_api: bool = True

    def resolved_cwd(self) -> Path:
        return Path(self.cwd).expanduser().resolve()

    def resolved_volley_home(self) -> Path:
        if self.volley_home is not None:
            return Path(self.volley_home).expanduser().resolve()
        return Path(os.environ.get("VOLLEY_PY_HOME", "~/.volley-python")).expanduser().resolve()

    def resolved_auth_home(self) -> Path | None:
        if self.auth_home is not None:
            return Path(self.auth_home).expanduser().resolve()
        return None

    def resolved_output_last_message(self) -> Path | None:
        if self.output_last_message is None:
            return None
        return Path(self.output_last_message).expanduser().resolve()

    def resolved_reasoning(self) -> dict[str, str | None] | None:
        if not _model_supports_reasoning(self.model):
            if self.model_reasoning_effort is None and self.model_reasoning_summary is None:
                return None
        model_info = _model_catalog_info(self.model)
        effort = (
            self.model_reasoning_effort
            or os.environ.get("OPENAI_REASONING_EFFORT")
            or str(model_info.get("default_reasoning_level") or "medium")
        )
        summary = self.model_reasoning_summary or str(model_info.get("default_reasoning_summary") or "auto")
        reasoning: dict[str, str | None] = {"effort": effort}
        if summary != "none":
            reasoning["summary"] = summary
        return reasoning

    def resolved_verbosity(self) -> str | None:
        if self.model_verbosity is not None:
            return self.model_verbosity
        model_info = _model_catalog_info(self.model)
        if model_info.get("support_verbosity") is False:
            return None
        value = model_info.get("default_verbosity")
        return str(value) if value else None

    def resolved_parallel_tool_calls(self) -> bool:
        model_info = _model_catalog_info(self.model)
        value = model_info.get("supports_parallel_tool_calls")
        return bool(value) if isinstance(value, bool) else True

    def resolved_service_tier(self) -> str | None:
        configured = normalize_service_tier(self.service_tier)
        if configured is not None:
            return configured
        if self.fast_default_opt_out or not self.fast_mode_enabled:
            return None
        if not _catalog_model_supports_fast_mode(self.model):
            return None
        if _is_enterprise_default_service_tier_plan(self.account_plan_type):
            return "priority"
        return None

    def resolved_model_service_tiers(self) -> list[dict[str, str]]:
        return _catalog_model_service_tiers(self.model)

    def resolved_model_supports_fast_mode(self) -> bool:
        if not self.fast_mode_enabled:
            return False
        return _catalog_model_supports_fast_mode(self.model)

    def resolved_model_stream_max_retries(self) -> int:
        if self.model_stream_max_retries is None:
            return 5
        return max(0, min(int(self.model_stream_max_retries), 100))

    def resolved_model_stream_retry_base_delay_ms(self) -> int:
        return max(0, int(self.model_stream_retry_base_delay_ms))

    def resolved_supports_image_input(self) -> bool:
        if self.model_supports_image_input is not None:
            return self.model_supports_image_input
        modalities = _model_catalog_info(self.model).get("input_modalities")
        if isinstance(modalities, list):
            return "image" in modalities
        return True

    def resolved_tool_output_truncation_tokens(self) -> int:
        policy = _model_catalog_info(self.model).get("truncation_policy")
        if isinstance(policy, dict):
            limit = policy.get("limit")
            if isinstance(limit, int) and limit > 0:
                return limit
        return 10_000

    def resolved_model_context_window(self) -> int | None:
        if self.model_context_window is not None:
            return self.model_context_window
        return _catalog_model_context_window(self.model)

    def resolved_auto_compact_token_limit(self) -> int | None:
        context_limit = None
        context_window = self.resolved_model_context_window()
        if context_window is not None:
            context_limit = (context_window * 9) // 10
        config_limit = (
            self.model_auto_compact_token_limit
            if self.model_auto_compact_token_limit is not None
            else _catalog_model_auto_compact_token_limit(self.model)
        )
        if context_limit is None:
            return config_limit
        if config_limit is None:
            return context_limit
        return min(config_limit, context_limit)

    def resolved_recent_context_offload_tokens(self) -> int:
        """Token budget for the divergent post-compaction recent-activity block.

        `None` (the default) means "auto": on (RECENT_CONTEXT_OFFLOAD_DEFAULT_TOKENS)
        for persistent sessions, off for ephemeral/throwaway sessions. A negative
        value derives ~10% of the context window (mirroring the 90% auto-compact
        threshold's buffer). 0 disables the feature (official-reference-identical
        behavior).
        """
        configured = self.recent_context_offload_tokens
        if configured is None:
            return 0 if self.ephemeral else RECENT_CONTEXT_OFFLOAD_DEFAULT_TOKENS
        if configured < 0:
            window = self.resolved_model_context_window()
            if window is None:
                return 0
            return window // 10
        return max(0, configured)


@dataclass(frozen=True)
class VolleyEvent:
    """JSONL-friendly event emitted by the Python Volley core."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.payload}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class VolleyResult:
    final_message: str
    events: list[VolleyEvent]
    thread_id: str
    turn_id: str
    history: list[dict[str, Any]]
    memory_citations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.final_message)


@dataclass(frozen=True)
class PromptRequest:
    model: str
    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    parallel_tool_calls: bool = True
    tool_choice: str = "auto"
    store: bool = False
    stream: bool = True
    prompt_cache_key: str | None = None
    reasoning: dict[str, Any] | None = None
    include: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    output_schema_strict: bool = True
    verbosity: str | None = None
    service_tier: str | None = None
    client_metadata: dict[str, str] | None = None
    session_id: str | None = None
    thread_id: str | None = None

    def to_responses_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": self.instructions,
            "input": [_responses_input_item(item) for item in self.input],
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "parallel_tool_calls": self.parallel_tool_calls,
            "reasoning": self.reasoning,
            "store": self.store,
            "stream": self.stream,
            "include": self.include,
        }
        if self.prompt_cache_key:
            kwargs["prompt_cache_key"] = self.prompt_cache_key
        if self.service_tier:
            kwargs["service_tier"] = self.service_tier
        if self.client_metadata is not None:
            kwargs["client_metadata"] = self.client_metadata
        text = _create_text_param(self.verbosity, self.output_schema, self.output_schema_strict)
        if text is not None:
            kwargs["text"] = text
        return kwargs

    def to_compact_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [_responses_input_item(item) for item in self.input],
            "tools": self.tools,
            "parallel_tool_calls": self.parallel_tool_calls,
        }
        if self.instructions:
            payload["instructions"] = self.instructions
        if self.reasoning is not None:
            payload["reasoning"] = self.reasoning
        if self.service_tier:
            payload["service_tier"] = self.service_tier
        if self.prompt_cache_key:
            payload["prompt_cache_key"] = self.prompt_cache_key
        text = _create_text_param(self.verbosity, self.output_schema, self.output_schema_strict)
        if text is not None:
            payload["text"] = text
        return payload


@dataclass(frozen=True)
class ModelResponse:
    id: str
    output: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


def new_thread_id() -> str:
    return str(uuid.uuid4())


def new_turn_id() -> str:
    return str(uuid.uuid4())


def _model_supports_reasoning(model: str) -> bool:
    lowered = model.lower()
    return lowered.startswith("gpt-5") or lowered.startswith("o")


def _default_auth_mode() -> AuthModeSelection:
    value = os.environ.get("PY_VOLLEY_AUTH_MODE", "auto").replace("-", "_").lower()
    if value in {"auto", "api_key", "chatgpt"}:
        return value  # type: ignore[return-value]
    if value in {"api", "apikey"}:
        return "api_key"
    return "auto"


RECENT_CONTEXT_OFFLOAD_DEFAULT_TOKENS = 10_000


def _default_recent_context_offload_tokens() -> int | None:
    # None = "auto" (resolved per-session: on for persistent, off for ephemeral).
    raw = os.environ.get("PY_VOLLEY_RECENT_CONTEXT_OFFLOAD_TOKENS")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _default_remote_compaction_mode() -> RemoteCompactionMode:
    value = os.environ.get("PY_VOLLEY_REMOTE_COMPACTION", "auto").lower()
    if value in {"auto", "off", "required"}:
        return value  # type: ignore[return-value]
    return "auto"


_MODEL_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None


def _model_catalog_info(model: str) -> dict[str, Any]:
    global _MODEL_CATALOG_CACHE
    if _MODEL_CATALOG_CACHE is None:
        _MODEL_CATALOG_CACHE = {}
        path = Path(__file__).with_name("assets") / "models.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            payload = {}
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list):
            for entry in models:
                if isinstance(entry, dict) and isinstance(entry.get("slug"), str):
                    _MODEL_CATALOG_CACHE[entry["slug"].lower()] = entry
    return _MODEL_CATALOG_CACHE.get(model.lower(), {})


def _catalog_model_context_window(model: str) -> int | None:
    info = _model_catalog_info(model)
    for key in ("context_window", "max_context_window"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _catalog_model_auto_compact_token_limit(model: str) -> int | None:
    value = _model_catalog_info(model).get("auto_compact_token_limit")
    if isinstance(value, int) and value > 0:
        return value
    return None


def normalize_service_tier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() == "fast":
        return "priority"
    if normalized.lower() == "priority":
        return "priority"
    if normalized.lower() == "flex":
        return "flex"
    return normalized


def _catalog_model_service_tiers(model: str) -> list[dict[str, str]]:
    info = _model_catalog_info(model)
    tiers = info.get("service_tiers")
    if not isinstance(tiers, list):
        return []
    out: list[dict[str, str]] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        tier_id = tier.get("id")
        name = tier.get("name")
        description = tier.get("description")
        if isinstance(tier_id, str) and isinstance(name, str) and isinstance(description, str):
            out.append({"id": tier_id, "name": name.lower(), "description": description})
    return out


def _catalog_model_supports_fast_mode(model: str) -> bool:
    info = _model_catalog_info(model)
    tiers = info.get("service_tiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, dict) and tier.get("id") == "priority":
                return True
    speed_tiers = info.get("additional_speed_tiers")
    return isinstance(speed_tiers, list) and any(str(tier).lower() == "fast" for tier in speed_tiers)


def _is_enterprise_default_service_tier_plan(plan_type: str | None) -> bool:
    if not plan_type:
        return False
    normalized = plan_type.strip().lower()
    return normalized in {
        "enterprise",
        "business",
        "enterprise_cbp_usage_based",
        "team",
        "self_serve_business_usage_based",
    }


def _create_text_param(
    verbosity: str | None,
    output_schema: dict[str, Any] | None,
    output_schema_strict: bool,
) -> dict[str, Any] | None:
    if verbosity is None and output_schema is None:
        return None
    text: dict[str, Any] = {}
    if verbosity is not None:
        text["verbosity"] = verbosity
    if output_schema is not None:
        text["format"] = {
            "type": "json_schema",
            "strict": output_schema_strict,
            "schema": output_schema,
            "name": "codex_output_schema",
        }
    return text


def _responses_input_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type")
    if item_type == "message":
        sanitized: dict[str, Any] = {
            "type": "message",
            "role": item.get("role", "user"),
            "content": [_responses_content_item(part) for part in item.get("content", [])],
        }
        if item.get("phase") is not None:
            sanitized["phase"] = item["phase"]
        return sanitized
    if item_type == "function_call":
        sanitized = {
            "type": "function_call",
            "name": item.get("name", ""),
            "arguments": item.get("arguments", "{}"),
            "call_id": item.get("call_id") or item.get("id", ""),
        }
        if item.get("namespace") is not None:
            sanitized["namespace"] = item["namespace"]
        return sanitized
    if item_type == "function_call_output":
        return {
            "type": "function_call_output",
            "call_id": item.get("call_id", ""),
            "output": item.get("output", ""),
        }
    if item_type == "custom_tool_call":
        sanitized = {
            "type": "custom_tool_call",
            "call_id": item.get("call_id") or item.get("id", ""),
            "name": item.get("name", ""),
            "input": item.get("input", ""),
        }
        if item.get("status") is not None:
            sanitized["status"] = item["status"]
        return sanitized
    if item_type == "custom_tool_call_output":
        sanitized = {
            "type": "custom_tool_call_output",
            "call_id": item.get("call_id", ""),
            "output": item.get("output", ""),
        }
        if item.get("name") is not None:
            sanitized["name"] = item["name"]
        return sanitized
    if item_type == "local_shell_call":
        sanitized = {
            "type": "local_shell_call",
            "status": item.get("status"),
            "action": item.get("action", {}),
        }
        if item.get("call_id") is not None:
            sanitized["call_id"] = item["call_id"]
        return sanitized
    if item_type == "web_search_call":
        sanitized = {"type": "web_search_call"}
        if item.get("status") is not None:
            sanitized["status"] = item["status"]
        if item.get("action") is not None:
            sanitized["action"] = item["action"]
        return sanitized
    if item_type == "reasoning":
        sanitized = {
            "type": "reasoning",
            "summary": item.get("summary", []),
            "encrypted_content": item.get("encrypted_content"),
        }
        if item.get("content") is not None:
            sanitized["content"] = item["content"]
        return sanitized
    return {key: value for key, value in item.items() if key not in {"id", "status"}}


def _responses_content_item(part: Any) -> Any:
    if not isinstance(part, dict):
        return part
    part_type = part.get("type")
    if part_type in {"input_text", "output_text"}:
        return {"type": part_type, "text": part.get("text", "")}
    if part_type == "input_image":
        sanitized = {"type": "input_image", "image_url": part.get("image_url", "")}
        if part.get("detail") is not None:
            sanitized["detail"] = part["detail"]
        return sanitized
    return {key: value for key, value in part.items() if key not in {"annotations", "logprobs"}}
