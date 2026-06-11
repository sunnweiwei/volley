from __future__ import annotations

import json
import difflib
import hashlib
import shlex
import subprocess
import time
import uuid

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .prompts import build_base_instructions, default_current_date, default_timezone, read_asset
from .types import VolleyConfig, VolleyEvent, new_thread_id, new_turn_id


COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000
IMAGE_CONTENT_OMITTED_PLACEHOLDER = "image content omitted because you do not support image input"
VOLLEY_ROLLOUT_ITEM_TYPES = frozenset(
    {"session_meta", "turn_context", "event_msg", "response_item", "compacted"}
)


@dataclass(frozen=True)
class RolloutReconstruction:
    history: list[dict[str, Any]]
    previous_turn_settings: dict[str, Any] | None
    reference_context_item: dict[str, Any] | None
    session_meta: dict[str, Any] | None = None
    legacy_compaction_without_replacement_history: bool = False
    last_token_usage: dict[str, Any] | None = None
    total_token_usage: int = 0
    session_reasoning_tokens: int = 0
    context_carryover_tokens: int = 0
    context_carryover_estimated: bool = False


@dataclass
class VolleyState:
    config: VolleyConfig
    thread_id: str = field(default_factory=new_thread_id)
    turn_id: str = field(default_factory=new_turn_id)
    installation_id: str = field(default_factory=new_thread_id)
    forked_from_id: str | None = None
    history: list[dict] = field(default_factory=list)
    events: list[VolleyEvent] = field(default_factory=list)
    memory_citations: list[dict] = field(default_factory=list)
    previous_turn_settings: dict[str, Any] | None = None
    reference_context_item: dict[str, Any] | None = None
    last_token_usage: dict[str, Any] | None = None
    total_token_usage: int = 0
    session_reasoning_tokens: int = 0
    context_carryover_tokens: int = 0
    context_carryover_estimated: bool = False
    _rollout_initialized: bool = False
    _rollout_seed_history: list[dict[str, Any]] = field(default_factory=list)
    _rollout_path: Path | None = None
    _started_at: datetime | None = None
    _turn_started_at: float | None = None
    _tool_started_at: dict[str, float] = field(default_factory=dict)
    _tool_arguments_by_call: dict[str, Any] = field(default_factory=dict)
    _tool_changes_by_call: dict[str, dict[str, Any]] = field(default_factory=dict)
    _turn_diff_valid: bool = True
    _turn_diff_baseline_by_path: dict[str, str] = field(default_factory=dict)
    _turn_diff_current_by_path: dict[str, str] = field(default_factory=dict)
    _turn_diff_origin_by_current_path: dict[str, str] = field(default_factory=dict)

    def start_turn(self) -> None:
        self.turn_id = new_turn_id()
        self._turn_started_at = None
        self._turn_diff_valid = True
        self._turn_diff_baseline_by_path.clear()
        self._turn_diff_current_by_path.clear()
        self._turn_diff_origin_by_current_path.clear()

    def append_history(self, item: dict) -> None:
        self.history.append(item)

    def emit(self, event_type: str, **payload: object) -> VolleyEvent:
        event = VolleyEvent(
            event_type,
            {
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                **payload,
            },
        )
        self.events.append(event)
        self._persist_event(event)
        return event

    def record_apply_patch_turn_diff(self, metadata: Any) -> str | None:
        if not isinstance(metadata, dict):
            return None
        changes = metadata.get("changes")
        if not isinstance(changes, list) or not changes:
            return None
        previous_diff = _turn_diff_unified_diff(self)
        tracker_changed = False
        for raw_change in changes:
            if not isinstance(raw_change, dict):
                self._turn_diff_valid = False
                tracker_changed = True
                continue
            if _track_apply_patch_change(self, raw_change):
                tracker_changed = True
            else:
                self._turn_diff_valid = False
                tracker_changed = True
        unified_diff = _turn_diff_unified_diff(self)
        if tracker_changed and (previous_diff is not None or unified_diff is not None):
            return unified_diff or ""
        return None

    def write_last_message(self, message: str) -> None:
        output = self.config.resolved_output_last_message()
        if output is None:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(message, encoding="utf-8")

    def compact_with_summary(self, summary_suffix: str, initial_context: list[dict] | None = None) -> list[dict]:
        pre_compaction_history = list(self.history)
        summary_text = build_compaction_summary_text(summary_suffix)
        recent_block = self._build_recent_context_offload(pre_compaction_history)
        # When the recent block is active it becomes the single carrier of recent
        # verbatim turns, REPLACING the reference recent-user-message prefix (one
        # place, no duplication). Everything older is still in the summary.
        user_messages = [] if recent_block else collect_user_message_texts(self.history)
        compacted = build_compacted_history([], user_messages, summary_text)
        if initial_context:
            compacted = insert_initial_context_before_last_real_user_or_summary(compacted, initial_context)
        self.history = compacted + recent_block
        return list(self.history)

    def compact_with_remote_history(
        self,
        compacted_history: list[dict[str, Any]],
        initial_context: list[dict] | None = None,
    ) -> list[dict]:
        pre_compaction_history = list(self.history)
        # Remote output is the SERVER's compacted history (encrypted checkpoint +
        # whatever verbatim messages it chose to keep). We never discard the
        # server's choice; we only append our recent-activity block after it. In
        # practice the endpoint returns just the encrypted item, so there is no
        # duplication; if it does retain messages, keeping them is the safe call.
        compacted = process_remote_compacted_history(compacted_history, initial_context or [])
        self.history = compacted + self._build_recent_context_offload(pre_compaction_history)
        return list(self.history)

    def _build_recent_context_offload(self, pre_compaction_history: list[dict[str, Any]]) -> list[dict]:
        """Reference-port divergence (opt-in). Build an extra, aggressively
        truncated "recent activity" block from the pre-compaction history so the
        next turn can see what the agent just did. Returns [] when disabled."""
        budget = self.config.resolved_recent_context_offload_tokens()
        if budget <= 0:
            return []
        offload_dir: Path | None = None
        configured = self.config.recent_context_offload_dir
        if configured is not None:
            offload_dir = Path(configured).expanduser()
        elif not self.config.ephemeral:
            offload_dir = self.config.resolved_volley_home() / "compaction_offload" / self.thread_id
        return build_recent_context_offload(
            pre_compaction_history,
            budget,
            self.config,
            offload_dir=offload_dir,
        )

    def approx_history_tokens(self) -> int:
        return sum(_estimate_prompt_visible_response_item_token_count(item, self.config) for item in self.history)

    def active_context_tokens(self) -> int:
        tokens, _estimated = _active_context_token_status_for_history(self.history, self.last_token_usage, self.config)
        return max(0, tokens or 0)

    def active_context_token_status(self) -> tuple[int | None, bool]:
        return _active_context_token_status_for_history(self.history, self.last_token_usage, self.config)

    def session_usage_tokens(self) -> int | None:
        if self.total_token_usage <= 0:
            return None
        return self.total_token_usage

    def session_reasoning_usage_tokens(self) -> int | None:
        if self.session_reasoning_tokens <= 0:
            return None
        return self.session_reasoning_tokens

    def session_context_token_status(self) -> tuple[int | None, bool]:
        active_context, active_estimated = self.active_context_token_status()
        if active_context is None:
            if self.context_carryover_tokens > 0:
                return self.context_carryover_tokens, self.context_carryover_estimated
            return None, True
        return self.context_carryover_tokens + active_context, self.context_carryover_estimated or active_estimated

    def start_new_context_epoch(self, carryover_tokens: int | None = None, *, estimated: bool = False) -> None:
        if carryover_tokens is None:
            carryover_tokens, estimated = self.session_context_token_status()
        self.context_carryover_tokens = max(0, carryover_tokens or 0)
        self.context_carryover_estimated = bool(estimated)

    def record_token_usage(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        self.last_token_usage = dict(usage)
        total = _usage_total_tokens(usage)
        if total is not None:
            self.total_token_usage += max(0, total)
        reasoning_tokens = _usage_reasoning_tokens(usage)
        if reasoning_tokens is not None:
            self.session_reasoning_tokens += max(0, reasoning_tokens)

    def recompute_token_usage_from_history(self) -> None:
        total = self.estimate_token_count_with_base_instructions()
        self.last_token_usage = {
            "input_tokens": total,
            "output_tokens": 0,
            "total_tokens": total,
            "estimated": True,
        }

    def estimate_token_count_with_base_instructions(self) -> int:
        return _estimate_token_count_with_base_instructions(self.history, self.config)

    def token_usage_info(self) -> dict[str, Any] | None:
        if self.last_token_usage is None and self.total_token_usage <= 0:
            return None
        return {
            "last_token_usage": self.last_token_usage,
            "total_token_usage": self.total_token_usage,
            "session_reasoning_tokens": self.session_reasoning_tokens,
        }

    def prompt_history(self) -> list[dict[str, Any]]:
        return prepare_prompt_history(self.history, self.config)

    def record_memory_citation(self, citation: dict) -> None:
        self.memory_citations.append(citation)

    def rollout_path(self) -> Path:
        if self._rollout_path is None:
            started_at = self._session_started_at().astimezone()
            sessions = self.config.resolved_volley_home() / "sessions"
            directory = sessions / f"{started_at.year:04d}" / f"{started_at.month:02d}" / f"{started_at.day:02d}"
            filename = f"rollout-{started_at.strftime('%Y-%m-%dT%H-%M-%S')}-{self.thread_id}.jsonl"
            self._rollout_path = directory / filename
        return self._rollout_path

    def read_rollout_records(self) -> list[dict[str, Any]]:
        return load_rollout_records(self.rollout_path())

    def _persist_event(self, event: VolleyEvent) -> None:
        if self.config.ephemeral:
            return
        root = self.rollout_path().parent
        root.mkdir(parents=True, exist_ok=True)
        records = self._rollout_records_for_event(event)
        if not records:
            return
        with self.rollout_path().open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _rollout_records_for_event(self, event: VolleyEvent) -> list[dict[str, Any]]:
        records = []
        if not self._rollout_initialized:
            records.append(_rollout_line("session_meta", self._session_meta_payload()))
            for item in self._rollout_seed_history:
                records.append(_rollout_line("response_item", item))
            self._rollout_seed_history.clear()
            self._rollout_initialized = True

        if event.type == "thread.started":
            return records
        if event.type == "turn.started":
            self._turn_started_at = time.time()
            turn_context = self._turn_context_payload()
            self.previous_turn_settings = _previous_turn_settings(turn_context)
            self.reference_context_item = dict(turn_context)
            records.append(_rollout_line("turn_context", turn_context))
            records.append(_rollout_line("event_msg", _turn_started_payload(self)))
            return records
        if event.type == "item.completed":
            item = event.payload.get("item")
            if isinstance(item, dict):
                records.append(_rollout_line("response_item", item))
                event_msg = _response_item_event_payload(item)
                if event_msg is not None:
                    records.append(_rollout_line("event_msg", event_msg))
            return records
        if event.type == "turn.completed":
            records.append(_rollout_line("event_msg", _turn_completed_payload(self, event)))
            self._turn_started_at = None
            return records
        if event.type == "turn.aborted":
            marker = _last_turn_aborted_marker(self.history)
            if marker is not None:
                records.append(_rollout_line("response_item", marker))
            records.append(_rollout_line("event_msg", _turn_aborted_payload(self, event)))
            self._turn_started_at = None
            return records
        if event.type == "turn.failed":
            records.append(_rollout_line("event_msg", _error_payload(str(event.payload.get("error") or "turn failed"))))
            self._turn_started_at = None
            return records
        if event.type == "warning":
            records.append(_rollout_line("event_msg", {"type": "warning", "message": str(event.payload.get("message", ""))}))
            return records
        if event.type == "stream_error":
            records.append(_rollout_line("event_msg", _stream_error_payload(event)))
            return records
        if event.type == "token_count":
            records.append(_rollout_line("event_msg", _token_count_payload(event)))
            return records
        if event.type == "thread.goal.updated":
            goal = event.payload.get("goal")
            if isinstance(goal, dict):
                records.append(
                    _rollout_line(
                        "event_msg",
                        {
                            "type": "thread_goal_updated",
                            "thread_id": self.thread_id,
                            "turn_id": event.payload.get("turn_id"),
                            "goal": goal,
                        },
                    )
                )
            return records
        if event.type == "thread.goal.cleared":
            records.append(
                _rollout_line(
                    "event_msg",
                    {
                        "type": "thread_goal_cleared",
                        "thread_id": self.thread_id,
                    },
                )
            )
            return records
        if event.type == "turn_diff":
            records.append(
                _rollout_line("event_msg", {"type": "turn_diff", "unified_diff": str(event.payload.get("unified_diff") or "")})
            )
            return records
        if event.type == "hook.started":
            records.append(_rollout_line("event_msg", _hook_started_payload(event)))
            return records
        if event.type == "hook.completed":
            records.append(_rollout_line("event_msg", _hook_completed_payload(event)))
            return records
        if event.type == "tool.started":
            call_id = str(event.payload.get("call_id") or "")
            self._tool_started_at[call_id] = time.time()
            arguments = event.payload.get("arguments")
            if isinstance(arguments, dict) or str(event.payload.get("name") or "") == "apply_patch":
                self._tool_arguments_by_call[call_id] = arguments
            tool_event = _tool_started_event_payload(self, event)
            if tool_event is not None:
                records.append(_rollout_line("event_msg", tool_event))
            return records
        if event.type == "tool.completed":
            tool_event = _tool_completed_event_payload(self, event)
            if isinstance(tool_event, list):
                for item in tool_event:
                    records.append(_rollout_line("event_msg", item))
            elif tool_event is not None:
                records.append(_rollout_line("event_msg", tool_event))
            return records
        if event.type == "context_compaction.completed":
            if "compacted_message" in event.payload:
                compacted_message = str(event.payload.get("compacted_message") or "")
            else:
                compacted_message = _last_history_message_text(self.history) or str(event.payload.get("summary", ""))
            records.append(
                _rollout_line(
                    "compacted",
                    {
                        "message": compacted_message,
                        "replacement_history": list(self.history),
                    },
                )
            )
            if event.payload.get("initial_context_injected"):
                turn_context = self._turn_context_payload()
                self.previous_turn_settings = _previous_turn_settings(turn_context)
                self.reference_context_item = dict(turn_context)
                records.append(_rollout_line("turn_context", turn_context))
            else:
                self.previous_turn_settings = None
                self.reference_context_item = None
            records.append(_rollout_line("event_msg", {"type": "context_compacted"}))
            return records
        return records

    def _session_meta_payload(self) -> dict[str, Any]:
        meta = {
            "id": self.thread_id,
            "forked_from_id": self.forked_from_id,
            "timestamp": self._session_timestamp(),
            "cwd": str(self.config.resolved_cwd()),
            "originator": "python-volley",
            "cli_version": "python-port",
            "source": self.config.session_source,
            "thread_source": None,
            "agent_path": None,
            "agent_nickname": None,
            "agent_role": None,
            "model_provider": self.config.model_provider_id,
            "base_instructions": {
                "text": build_base_instructions(
                    prompt_asset=self.config.prompt_asset,
                    model=self.config.model,
                    cwd=self.config.resolved_cwd(),
                    sandbox=self.config.sandbox,
                    approval_policy=self.config.approval_policy,
                    volley_home=self.config.resolved_volley_home(),
                    memory_tool_enabled=self.config.memory_tool_enabled,
                    use_memories=self.config.use_memories,
                )
            },
            "dynamic_tools": None,
            "memory_mode": "enabled"
            if self.config.memory_tool_enabled and self.config.memory_generate_memories
            else "disabled",
        }
        return _drop_none({"meta": _drop_none(meta), "git": _git_info(self.config.resolved_cwd())})

    def _session_started_at(self) -> datetime:
        if self._started_at is None:
            self._started_at = datetime.now().astimezone()
        return self._started_at

    def _session_timestamp(self) -> str:
        return self._session_started_at().astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _turn_context_payload(self) -> dict[str, Any]:
        reasoning = self.config.resolved_reasoning() or {}
        return _drop_none(
            {
                "turn_id": self.turn_id,
                "cwd": str(self.config.resolved_cwd()),
                "current_date": self.config.current_date or default_current_date(),
                "timezone": self.config.timezone or default_timezone(),
                "approval_policy": _approval_policy_protocol_value(self.config.approval_policy),
                "sandbox_policy": _sandbox_policy(self.config),
                "permission_profile": _permission_profile(self.config),
                "file_system_sandbox_policy": _file_system_sandbox_policy(self.config),
                "model": self.config.model,
                "model_context_window": self.config.resolved_model_context_window(),
                "collaboration_mode": None,
                "realtime_active": False,
                "effort": reasoning.get("effort"),
                "summary": reasoning.get("summary", "none"),
                "truncation_policy": _truncation_policy(self.config),
            }
        )


def summarization_prompt() -> str:
    return read_asset("prompts/compact/prompt.md")


def load_rollout_records(path: Path | str) -> list[dict[str, Any]]:
    rollout_path = Path(path)
    if not rollout_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw_line in rollout_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def reconstruct_history_from_rollout(
    source: Path | str | list[dict[str, Any]],
    config: VolleyConfig | None = None,
) -> RolloutReconstruction:
    """Materialize the public rollout history semantics used by resume/fork.

    Upstream scans newest-to-oldest to find the surviving compaction checkpoint
    and resume metadata, then replays the suffix forward. This Python helper
    keeps the same externally visible rules for the JSONL shapes this port
    writes today: response items append to history, compaction replacement
    history replaces history, legacy compaction rebuilds compacted history, and
    rollback markers drop the newest user turn boundaries.
    """

    records = load_rollout_records(source) if isinstance(source, (str, Path)) else list(source)
    history: list[dict[str, Any]] = []
    session_meta: dict[str, Any] | None = None
    latest_turn_context: dict[str, Any] | None = None
    previous_turn_settings: dict[str, Any] | None = None
    reference_context_item: dict[str, Any] | None = None
    user_turn_contexts: list[dict[str, Any] | None] = []
    legacy_compaction_without_replacement_history = False
    last_boundary_from_response_item = False
    last_record_was_compacted = False
    last_token_usage: dict[str, Any] | None = None
    total_token_usage = 0
    session_reasoning_tokens = 0
    context_carryover_tokens = 0
    context_carryover_estimated = False

    def set_resume_context(turn_context: dict[str, Any] | None) -> None:
        nonlocal previous_turn_settings, reference_context_item
        if turn_context is None:
            previous_turn_settings = None
            reference_context_item = None
            return
        previous_turn_settings = _previous_turn_settings(turn_context)
        reference_context_item = dict(turn_context)

    def record_user_turn_context() -> None:
        turn_context = dict(latest_turn_context) if latest_turn_context is not None else None
        user_turn_contexts.append(turn_context)
        set_resume_context(turn_context)

    def reset_resume_context_from_surviving_turns() -> None:
        for turn_context in reversed(user_turn_contexts):
            if turn_context is not None:
                set_resume_context(turn_context)
                return
        set_resume_context(None)

    def record_token_info(info: dict[str, Any] | None) -> None:
        nonlocal last_token_usage, total_token_usage, session_reasoning_tokens
        if not isinstance(info, dict):
            return
        usage = info.get("last_token_usage")
        if isinstance(usage, dict):
            last_token_usage = dict(usage)
        total = info.get("total_token_usage")
        if isinstance(total, (int, float)):
            total_token_usage = max(0, int(total))
        reasoning = info.get("session_reasoning_tokens")
        if isinstance(reasoning, (int, float)):
            session_reasoning_tokens = max(0, int(reasoning))

    def current_session_context_status() -> tuple[int | None, bool]:
        active_context, active_estimated = _active_context_token_status_for_history(
            history,
            last_token_usage,
            config,
        )
        if active_context is None:
            if context_carryover_tokens > 0:
                return context_carryover_tokens, context_carryover_estimated
            return None, True
        return context_carryover_tokens + active_context, context_carryover_estimated or active_estimated

    for record in records:
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta" and isinstance(payload, dict):
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
            session_meta = dict(meta)
            last_boundary_from_response_item = False
            last_record_was_compacted = False
            continue
        if record_type == "turn_context" and isinstance(payload, dict):
            latest_turn_context = payload
            if last_record_was_compacted:
                set_resume_context(dict(payload))
            last_boundary_from_response_item = False
            last_record_was_compacted = False
            continue
        if record_type == "response_item" and isinstance(payload, dict):
            if _is_user_turn_boundary(payload):
                record_user_turn_context()
                last_boundary_from_response_item = True
            else:
                last_boundary_from_response_item = False
            history.append(payload)
            last_record_was_compacted = False
            continue
        if record_type == "compacted" and isinstance(payload, dict):
            pre_compact_context, pre_compact_estimated = current_session_context_status()
            replacement_history = payload.get("replacement_history")
            if isinstance(replacement_history, list):
                history = [item for item in replacement_history if isinstance(item, dict)]
            else:
                legacy_compaction_without_replacement_history = True
                message = str(payload.get("message") or "")
                history = build_compacted_history([], collect_user_message_texts(history), message)
            if pre_compact_context is not None:
                context_carryover_tokens = max(0, pre_compact_context)
                context_carryover_estimated = bool(pre_compact_estimated)
            if config is not None:
                estimated_total = _estimate_token_count_with_base_instructions(history, config)
                last_token_usage = {
                    "input_tokens": estimated_total,
                    "output_tokens": 0,
                    "total_tokens": estimated_total,
                    "estimated": True,
                }
            user_turn_contexts = [None for item in history if _is_user_turn_boundary(item)]
            previous_turn_settings = None
            reference_context_item = None
            last_boundary_from_response_item = False
            last_record_was_compacted = True
            continue
        if record_type == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type == "user_message":
                if last_boundary_from_response_item:
                    set_resume_context(user_turn_contexts[-1] if user_turn_contexts else None)
                else:
                    record_user_turn_context()
            elif event_type in {"thread_rolled_back", "ThreadRolledBack"}:
                count = _rollback_turn_count(payload)
                history = _drop_last_n_user_turns(history, count)
                if count > 0:
                    del user_turn_contexts[max(0, len(user_turn_contexts) - count) :]
                    reset_resume_context_from_surviving_turns()
            elif event_type == "token_count":
                record_token_info(payload.get("info") if isinstance(payload.get("info"), dict) else None)
            last_boundary_from_response_item = False
            last_record_was_compacted = False
            continue
        if record_type == "item.completed":
            item = record.get("item")
            if isinstance(item, dict):
                if _is_user_turn_boundary(item):
                    record_user_turn_context()
                    last_boundary_from_response_item = True
                else:
                    last_boundary_from_response_item = False
                history.append(item)
            last_record_was_compacted = False
            continue
        last_boundary_from_response_item = False
        last_record_was_compacted = False

    return RolloutReconstruction(
        history=history,
        previous_turn_settings=previous_turn_settings,
        reference_context_item=None if legacy_compaction_without_replacement_history else reference_context_item,
        session_meta=session_meta,
        legacy_compaction_without_replacement_history=legacy_compaction_without_replacement_history,
        last_token_usage=last_token_usage,
        total_token_usage=total_token_usage,
        session_reasoning_tokens=session_reasoning_tokens,
        context_carryover_tokens=context_carryover_tokens,
        context_carryover_estimated=context_carryover_estimated,
    )


def summary_prefix() -> str:
    return read_asset("prompts/compact/summary_prefix.md")


def build_compaction_summary_text(summary_suffix: str) -> str:
    return f"{summary_prefix()}\n{summary_suffix}"


def collect_user_message_texts(history: list[dict]) -> list[str]:
    messages: list[str] = []
    for item in history:
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        if _is_contextual_user_message(item):
            continue
        chunks = []
        for part in item.get("content", []):
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        message = "".join(chunks)
        if message and not is_summary_message(message):
            messages.append(message)
    return messages


def insert_initial_context_before_last_real_user_or_summary(
    compacted_history: list[dict],
    initial_context: list[dict],
) -> list[dict]:
    history = list(compacted_history)
    insertion_index: int | None = None
    last_user_or_summary_index: int | None = None

    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if not _is_user_turn_boundary(item):
            if insertion_index is None and item.get("type") in {"compaction", "context_compaction"}:
                insertion_index = index
            continue
        message = _message_text(item)
        last_user_or_summary_index = index if last_user_or_summary_index is None else last_user_or_summary_index
        if not is_summary_message(message):
            insertion_index = index
            break

    if insertion_index is None:
        insertion_index = last_user_or_summary_index
    if insertion_index is None:
        history.extend(initial_context)
    else:
        history[insertion_index:insertion_index] = list(initial_context)
    return history


def collect_message_texts(history: list[dict]) -> list[str]:
    messages: list[str] = []
    for item in history:
        if item.get("type") != "message":
            continue
        chunks = []
        for part in item.get("content", []):
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        if chunks:
            messages.append("".join(chunks))
    return messages


def _items_after_last_model_generated_item(history: list[dict]) -> list[dict]:
    for index in range(len(history) - 1, -1, -1):
        if _is_model_generated_item(history[index]):
            return history[index + 1 :]
    return []


def prepare_prompt_history(history: list[dict[str, Any]], config: VolleyConfig) -> list[dict[str, Any]]:
    items = [
        _truncate_tool_output_for_prompt(deepcopy(item), config.resolved_tool_output_truncation_tokens())
        for item in history
        if _is_api_message(item)
    ]
    _ensure_call_outputs_present(items)
    _remove_orphan_outputs(items)
    if not config.resolved_supports_image_input():
        _strip_images_when_unsupported(items)
    return items


def trim_remote_compaction_history_to_fit_context_window(
    history: list[dict[str, Any]],
    config: VolleyConfig,
) -> tuple[list[dict[str, Any]], int]:
    context_window = config.resolved_model_context_window()
    trimmed = [deepcopy(item) for item in history]
    deleted_items = 0
    if context_window is None:
        return trimmed, deleted_items

    while (
        trimmed
        and _estimate_token_count_with_base_instructions(trimmed, config) > context_window
    ):
        if not _is_remote_compaction_trim_item(trimmed[-1]):
            break
        trimmed.pop()
        deleted_items += 1
    return trimmed, deleted_items


def _is_api_message(item: dict[str, Any]) -> bool:
    if item.get("type") == "message":
        return item.get("role") != "system"
    return item.get("type") in {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
        "reasoning",
        "web_search_call",
        "tool_search_call",
        "tool_search_output",
        "image_generation_call",
        "compaction",
        "context_compaction",
    }


def _ensure_call_outputs_present(items: list[dict[str, Any]]) -> None:
    missing: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(items):
        item_type = item.get("type")
        call_id = _call_id(item)
        if not call_id:
            continue
        if item_type == "function_call" and not _has_function_output(items, call_id):
            missing.append((index, {"type": "function_call_output", "call_id": call_id, "output": "aborted"}))
        elif item_type == "custom_tool_call" and not _has_custom_output(items, call_id):
            missing.append((index, {"type": "custom_tool_call_output", "call_id": call_id, "output": "aborted"}))
        elif item_type == "local_shell_call" and not _has_function_output(items, call_id):
            missing.append((index, {"type": "function_call_output", "call_id": call_id, "output": "aborted"}))
        elif item_type == "tool_search_call" and not _has_tool_search_output(items, call_id):
            missing.append(
                (
                    index,
                    {
                        "type": "tool_search_output",
                        "call_id": call_id,
                        "status": "completed",
                        "execution": "client",
                        "tools": [],
                    },
                )
            )
    for index, output in reversed(missing):
        items.insert(index + 1, output)


def _remove_orphan_outputs(items: list[dict[str, Any]]) -> None:
    function_ids = {_call_id(item) for item in items if item.get("type") == "function_call"}
    local_shell_ids = {_call_id(item) for item in items if item.get("type") == "local_shell_call"}
    custom_ids = {_call_id(item) for item in items if item.get("type") == "custom_tool_call"}
    tool_search_ids = {_call_id(item) for item in items if item.get("type") == "tool_search_call"}
    function_ids.discard("")
    local_shell_ids.discard("")
    custom_ids.discard("")
    tool_search_ids.discard("")

    retained: list[dict[str, Any]] = []
    for item in items:
        item_type = item.get("type")
        call_id = _call_id(item)
        if item_type == "function_call_output" and call_id not in function_ids | local_shell_ids:
            continue
        if item_type == "custom_tool_call_output" and call_id not in custom_ids:
            continue
        if item_type == "tool_search_output" and item.get("execution") != "server" and call_id and call_id not in tool_search_ids:
            continue
        retained.append(item)
    items[:] = retained


def _has_function_output(items: list[dict[str, Any]], call_id: str) -> bool:
    return any(item.get("type") == "function_call_output" and _call_id(item) == call_id for item in items)


def _has_custom_output(items: list[dict[str, Any]], call_id: str) -> bool:
    return any(item.get("type") == "custom_tool_call_output" and _call_id(item) == call_id for item in items)


def _has_tool_search_output(items: list[dict[str, Any]], call_id: str) -> bool:
    return any(item.get("type") == "tool_search_output" and _call_id(item) == call_id for item in items)


def _call_id(item: dict[str, Any]) -> str:
    value = item.get("call_id") or item.get("id")
    return str(value) if value is not None else ""


def _strip_images_when_unsupported(items: list[dict[str, Any]]) -> None:
    for item in items:
        item_type = item.get("type")
        if item_type == "message" and isinstance(item.get("content"), list):
            item["content"] = _strip_image_content_items(item["content"])
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            item["output"] = _strip_images_from_tool_output(item.get("output"))
        elif item_type == "image_generation_call":
            result = item.get("result")
            if isinstance(result, list):
                result.clear()
            elif isinstance(result, str):
                item["result"] = ""


def _strip_image_content_items(content: list[Any]) -> list[Any]:
    normalized: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "input_image":
            normalized.append({"type": "input_text", "text": IMAGE_CONTENT_OMITTED_PLACEHOLDER})
        else:
            normalized.append(part)
    return normalized


def _strip_images_from_tool_output(output: Any) -> Any:
    if isinstance(output, dict):
        for key in ("content", "body"):
            value = output.get(key)
            if isinstance(value, list):
                output[key] = _strip_image_content_items(value)
        return output
    if isinstance(output, list):
        return _strip_image_content_items(output)
    return output


def _truncate_tool_output_for_prompt(item: dict[str, Any], token_limit: int) -> dict[str, Any]:
    if item.get("type") not in {"function_call_output", "custom_tool_call_output"}:
        return item
    item["output"] = _truncate_tool_output_value(item.get("output"), token_limit)
    return item


def _truncate_tool_output_value(value: Any, token_limit: int) -> Any:
    if isinstance(value, str):
        return _truncate_text_for_prompt(value, token_limit)
    if isinstance(value, dict):
        for key in ("output", "text"):
            if isinstance(value.get(key), str):
                value[key] = _truncate_text_for_prompt(value[key], token_limit)
        for key in ("content", "body"):
            if isinstance(value.get(key), list):
                value[key] = [
                    _truncate_tool_output_value(part, token_limit) if isinstance(part, (dict, str, list)) else part
                    for part in value[key]
                ]
        return value
    if isinstance(value, list):
        return [_truncate_tool_output_value(part, token_limit) for part in value]
    return value


def _truncate_text_for_prompt(text: str, token_limit: int) -> str:
    budget = max(1, int(token_limit * 4 * 1.2))
    if len(text) <= budget:
        return text
    marker_budget = 64
    keep = max(1, budget - marker_budget)
    head = max(1, (keep * 2) // 3)
    tail = max(0, keep - head)
    removed_tokens = max(1, (len(text) - head - tail + 3) // 4)
    marker = f"\n…{removed_tokens} tokens truncated…\n"
    return text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else "")


def is_summary_message(message: str) -> bool:
    return message.startswith(f"{summary_prefix()}\n")


def build_compacted_history(
    initial_context: list[dict],
    user_messages: list[str],
    summary_text: str,
    max_tokens: int = COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> list[dict]:
    history = list(initial_context)
    selected_messages: list[str] = []
    remaining = max(max_tokens, 0)
    for message in reversed(user_messages):
        if remaining == 0:
            break
        tokens = _approx_token_count(message)
        if tokens <= remaining:
            selected_messages.append(message)
            remaining -= tokens
        else:
            selected_messages.append(_truncate_to_tokens(message, remaining))
            break
    selected_messages.reverse()

    for message in selected_messages:
        history.append(_user_message_item(message))
    history.append(_user_message_item(summary_text or "(no summary available)"))
    return history


# --- Reference-port divergence: post-compaction recent-activity offload block ---
# This whole section has no official counterpart. It is gated behind
# VolleyConfig.recent_context_offload_tokens (default 0 = disabled), so the
# default compaction behavior stays byte-for-byte identical to official Volley.
# See PARITY_AUDIT.md ("Summary And Compaction").
RECENT_OFFLOAD_HEADER = (
    "<recent_activity>\n"
    "Below is a compressed log of the most recent work done just before this "
    "context checkpoint, kept so you can continue without redoing finished work. "
    "Long content was truncated; where it was, the full original was saved to a "
    "file and the path is noted inline — read that file if you need the complete "
    "content.\n"
    "</recent_activity>"
)
RECENT_OFFLOAD_FOOTER = "<recent_activity_end />"
_RECENT_ASSISTANT_TEXT_MAX_TOKENS = 2_000
_RECENT_USER_TEXT_MAX_TOKENS = 2_000
_RECENT_TOOL_ARG_MAX_TOKENS = 400
_RECENT_TOOL_OUTPUT_MAX_TOKENS = 1_500
# Tools whose output is reconstructable on demand (re-read the file), so we drop
# the captured output and leave only a short pointer note.
_FILE_TOOL_NAMES = frozenset({"apply_patch", "read_file", "view_image"})
_FILE_READ_COMMAND_PREFIXES = ("cat ", "head ", "tail ", "less ", "more ", "bat ", "sed -n", "nl ", "type ")


def build_recent_context_offload(
    history: list[dict[str, Any]],
    budget_tokens: int,
    config: VolleyConfig,
    *,
    offload_dir: Path | None,
) -> list[dict]:
    """Build the divergent recent-activity block (see module section header)."""
    if budget_tokens <= 0:
        return []

    history = _remove_previous_recent_activity_blocks(history)
    name_by_call: dict[str, str] = {}
    cmd_by_call: dict[str, str] = {}
    for item in history:
        if item.get("type") in {"function_call", "custom_tool_call", "local_shell_call"}:
            call_id = _call_id(item)
            if not call_id:
                continue
            name_by_call[call_id] = str(
                item.get("name") or ("shell" if item.get("type") == "local_shell_call" else "")
            )
            cmd_by_call[call_id] = _recent_call_command_text(item)

    selected: list[dict[str, Any]] = []
    used = 0
    for item in reversed(history):
        compressed = _compress_recent_item(item, name_by_call, cmd_by_call, offload_dir)
        if compressed is None:
            continue
        cost = _estimate_prompt_visible_response_item_token_count(compressed, config)
        if selected and used + cost > budget_tokens:
            break
        selected.append(compressed)
        used += cost
    selected.reverse()
    _remove_orphan_outputs(selected)
    _ensure_call_outputs_present(selected)
    if not selected:
        return []

    header = {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": RECENT_OFFLOAD_HEADER}],
    }
    footer = {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": RECENT_OFFLOAD_FOOTER}],
    }
    return [header, *selected, footer]


def _remove_previous_recent_activity_blocks(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    buffered_block: list[dict[str, Any]] | None = None
    for item in history:
        if _is_recent_activity_header_message(item):
            if buffered_block is not None:
                retained.extend(buffered_block)
            buffered_block = [item]
            continue
        if buffered_block is not None:
            buffered_block.append(item)
            if _is_recent_activity_footer_message(item):
                buffered_block = None
            continue
        retained.append(item)
    if buffered_block is not None:
        retained.extend(buffered_block)
    return retained


def _is_recent_activity_header_message(item: dict[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "developer" and _message_text(item).strip() == RECENT_OFFLOAD_HEADER


def _is_recent_activity_footer_message(item: dict[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "developer" and _message_text(item).strip() == RECENT_OFFLOAD_FOOTER


def _compress_recent_item(
    item: dict[str, Any],
    name_by_call: dict[str, str],
    cmd_by_call: dict[str, str],
    offload_dir: Path | None,
) -> dict[str, Any] | None:
    item_type = item.get("type")

    if item_type == "message":
        role = item.get("role")
        if role == "assistant":
            text = _message_text(item)
            if not text:
                return None
            kept = _truncate_and_offload(
                text, max_tokens=_RECENT_ASSISTANT_TEXT_MAX_TOKENS, offload_dir=offload_dir, label="assistant"
            )
            return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": kept}]}
        if role == "user" and not _is_contextual_user_message(item):
            # Keep real user turns so the block reads as a coherent recent
            # transcript (the prompts that triggered the agent's actions), not a
            # pile of orphan assistant/tool items. Contextual wrappers
            # (environment_context, AGENTS.md, etc.) are dropped. Long user text
            # is truncated/offloaded like everything else. This may overlap the
            # most-recent user turn(s) the prefix already retains; the overlap is
            # bounded and harmless.
            text = _message_text(item)
            if not text:
                return None
            if is_summary_message(text):
                return None
            kept = _truncate_and_offload(
                text, max_tokens=_RECENT_USER_TEXT_MAX_TOKENS, offload_dir=offload_dir, label="user"
            )
            return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": kept}]}
        # developer / contextual messages are not part of the recent transcript.
        return None

    if item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
        return _compress_recent_call(item)

    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return _compress_recent_output(item, name_by_call, cmd_by_call, offload_dir)

    # reasoning, web_search_call, tool_search_*, image_generation_call, compaction,
    # context_compaction, etc. are not useful as verbatim recent context.
    return None


def _compress_recent_call(item: dict[str, Any]) -> dict[str, Any]:
    item_type = item["type"]
    call_id = _call_id(item)
    if item_type == "local_shell_call":
        sanitized: dict[str, Any] = {"type": "local_shell_call", "action": item.get("action", {})}
        if item.get("status") is not None:
            sanitized["status"] = item["status"]
        if call_id:
            sanitized["call_id"] = call_id
        return sanitized
    if item_type == "custom_tool_call":
        return {
            "type": "custom_tool_call",
            "call_id": call_id,
            "name": item.get("name", ""),
            "input": _truncate_text_no_offload(str(item.get("input", "")), _RECENT_TOOL_ARG_MAX_TOKENS),
        }
    arguments = item.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": item.get("name", ""),
        "arguments": _truncate_text_no_offload(arguments, _RECENT_TOOL_ARG_MAX_TOKENS),
    }


def _compress_recent_output(
    item: dict[str, Any],
    name_by_call: dict[str, str],
    cmd_by_call: dict[str, str],
    offload_dir: Path | None,
) -> dict[str, Any]:
    call_id = _call_id(item)
    out_type = item["type"]
    name = name_by_call.get(call_id, "")
    command = cmd_by_call.get(call_id, "")

    if _recent_output_is_droppable(name, command):
        note = "(output omitted to save context — re-read the file or re-run the command if you need it)"
        return {"type": out_type, "call_id": call_id, "output": note}

    text = _recent_output_text(item.get("output"))
    kept = _truncate_and_offload(
        text, max_tokens=_RECENT_TOOL_OUTPUT_MAX_TOKENS, offload_dir=offload_dir, label="output"
    )
    return {"type": out_type, "call_id": call_id, "output": kept}


def _recent_output_is_droppable(name: str, command: str) -> bool:
    if name in _FILE_TOOL_NAMES:
        return True
    stripped = command.strip().lower()
    return any(stripped.startswith(prefix) for prefix in _FILE_READ_COMMAND_PREFIXES)


def _recent_call_command_text(item: dict[str, Any]) -> str:
    args = item.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return args
    if isinstance(args, dict):
        command = args.get("command")
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        if isinstance(command, str):
            return command
    action = item.get("action")
    if isinstance(action, dict):
        command = action.get("command")
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        if isinstance(command, str):
            return command
    return ""


def _recent_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("output", "text"):
            value = output.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(output, ensure_ascii=False)
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False)


def _truncate_text_no_offload(text: str, max_tokens: int) -> str:
    if _approx_token_count(text) <= max_tokens:
        return text
    head = _truncate_to_tokens(text, max_tokens)
    return f"{head}\n[... truncated {_approx_token_count(text) - max_tokens} tokens ...]"


def _truncate_and_offload(text: str, *, max_tokens: int, offload_dir: Path | None, label: str) -> str:
    if _approx_token_count(text) <= max_tokens:
        return text
    omitted = _approx_token_count(text) - max_tokens
    head = _truncate_to_tokens(text, max_tokens)
    path = _write_offload_file(offload_dir, label, text)
    if path is not None:
        note = f"\n[... truncated {omitted} tokens. Full original saved to {path} — read that file for the complete content. ...]"
    else:
        note = f"\n[... truncated {omitted} tokens ...]"
    return head + note


def _write_offload_file(offload_dir: Path | None, label: str, text: str) -> Path | None:
    if offload_dir is None:
        return None
    try:
        offload_dir.mkdir(parents=True, exist_ok=True)
        path = offload_dir / f"{label}-{uuid.uuid4().hex}.txt"
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def process_remote_compacted_history(
    compacted_history: list[dict[str, Any]],
    initial_context: list[dict] | None = None,
) -> list[dict]:
    retained = [
        deepcopy(item)
        for item in compacted_history
        if _should_keep_remote_compacted_history_item(item)
    ]
    return insert_initial_context_before_last_real_user_or_summary(retained, list(initial_context or []))


def _should_keep_remote_compacted_history_item(item: dict[str, Any]) -> bool:
    item_type = item.get("type")
    if item_type == "message":
        role = item.get("role")
        if role == "developer":
            return False
        if role == "assistant":
            return True
        if role == "user":
            return not _is_contextual_user_message(item) or _is_hook_context_message(item)
        return False
    if item_type in {"compaction", "context_compaction"}:
        return True
    return False


def _is_hook_context_message(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    content = item.get("content", [])
    if not content:
        return False
    for part in content:
        if not isinstance(part, dict):
            return False
        text = part.get("text")
        if not isinstance(text, str) or not _matches_context_fragment(text, "<hook_context>", "</hook_context>"):
            return False
    return True


def _user_message_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _is_model_generated_item(item: dict[str, Any]) -> bool:
    item_type = item.get("type")
    if item_type == "message":
        return item.get("role") == "assistant"
    return item_type in {
        "reasoning",
        "function_call",
        "web_search_call",
        "image_generation_call",
        "custom_tool_call",
        "local_shell_call",
        "compaction",
        "context_compaction",
    }


def _is_remote_compaction_trim_item(item: dict[str, Any]) -> bool:
    if item.get("type") == "message":
        return item.get("role") == "developer"
    return item.get("type") in {
        "function_call_output",
        "tool_search_output",
        "custom_tool_call_output",
    }


def _estimate_response_item_token_count(item: dict[str, Any]) -> int:
    return _approx_tokens_from_byte_count(_estimate_response_item_model_visible_bytes(item))


def _estimate_prompt_visible_response_item_token_count(item: dict[str, Any], config: VolleyConfig) -> int:
    model_visible = deepcopy(item)
    model_visible = _truncate_tool_output_for_prompt(model_visible, config.resolved_tool_output_truncation_tokens())
    if not config.resolved_supports_image_input():
        _strip_images_when_unsupported([model_visible])
    return _estimate_response_item_token_count(model_visible)


def _active_context_token_status_for_history(
    history: list[dict[str, Any]],
    last_token_usage: dict[str, Any] | None,
    config: VolleyConfig | None,
) -> tuple[int | None, bool]:
    last_tokens = _usage_total_tokens(last_token_usage or {})
    local_tokens = 0
    if config is not None:
        local_tokens = sum(
            _estimate_prompt_visible_response_item_token_count(item, config)
            for item in _items_after_last_model_generated_item(history)
        )
    if last_tokens is None and local_tokens <= 0:
        return None, True
    estimated = bool((last_token_usage or {}).get("estimated")) or local_tokens > 0
    return max(0, last_tokens or 0) + local_tokens, estimated


def _estimate_token_count_with_base_instructions(history: list[dict[str, Any]], config: VolleyConfig) -> int:
    base_instructions = build_base_instructions(
        prompt_asset=config.prompt_asset,
        model=config.model,
        cwd=config.resolved_cwd(),
        sandbox=config.sandbox,
        approval_policy=config.approval_policy,
        volley_home=config.resolved_volley_home(),
        memory_tool_enabled=config.memory_tool_enabled,
        use_memories=config.use_memories,
    )
    return _approx_token_count(base_instructions) + sum(
        _estimate_prompt_visible_response_item_token_count(item, config) for item in history
    )


def _estimate_response_item_model_visible_bytes(item: dict[str, Any]) -> int:
    item_type = item.get("type")
    if item_type in {"reasoning", "compaction", "context_compaction"}:
        encrypted = item.get("encrypted_content")
        if isinstance(encrypted, str):
            return max(0, (len(encrypted) * 3) // 4 - 650)
    try:
        return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except TypeError:
        return len(str(item).encode("utf-8"))


def _approx_token_count(text: str) -> int:
    return _approx_tokens_from_byte_count(len(text.encode("utf-8")))


def _approx_tokens_from_byte_count(byte_count: int) -> int:
    if byte_count <= 0:
        return 0
    return (byte_count + 3) // 4


def _usage_total_tokens(usage: dict[str, Any]) -> int | None:
    for key in ("total_tokens", "total_token_count", "total"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, (int, float)) and isinstance(output_tokens, (int, float)):
        return int(input_tokens + output_tokens)
    return None


def _usage_reasoning_tokens(usage: dict[str, Any]) -> int | None:
    for key in ("reasoning_output_tokens", "reasoning_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    for details_key in ("output_tokens_details", "completion_tokens_details"):
        details = usage.get(details_key)
        if not isinstance(details, dict):
            continue
        for key in ("reasoning_tokens", "reasoning_output_tokens"):
            value = details.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
    return None


def _truncate_to_tokens(text: str, tokens: int) -> str:
    return text[: max(tokens, 0) * 4]


def _previous_turn_settings(turn_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": turn_context.get("model"),
        "realtime_active": turn_context.get("realtime_active"),
    }


def _is_user_turn_boundary(item: dict[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "user" and not _is_contextual_user_message(item)


def _is_contextual_user_message(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    content = item.get("content", [])
    if not content:
        return False
    saw_context = False
    for part in content:
        if not isinstance(part, dict):
            return False
        text = part.get("text")
        if not isinstance(text, str):
            return False
        if _matches_context_fragment(text, "# AGENTS.md instructions for ", "</INSTRUCTIONS>") or _matches_context_fragment(
            text,
            "<environment_context>",
            "</environment_context>",
        ) or _matches_context_fragment(
            text,
            "<turn_aborted>",
            "</turn_aborted>",
        ) or _matches_context_fragment(
            text,
            "<hook_context>",
            "</hook_context>",
        ) or _matches_context_fragment(
            text,
            "<subagent_notification>",
            "</subagent_notification>",
        ) or _matches_context_fragment(
            text,
            "<goal_context>",
            "</goal_context>",
        ):
            saw_context = True
            continue
        return False
    return saw_context


def _matches_context_fragment(text: str, start: str, end: str) -> bool:
    trimmed = text.strip()
    return trimmed.startswith(start) and trimmed.endswith(end)


def _is_turn_aborted_marker(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    for part in item.get("content", []):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            if _matches_context_fragment(part["text"], "<turn_aborted>", "</turn_aborted>"):
                return True
    return False


def _message_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in item.get("content", []):
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _drop_last_n_user_turns(history: list[dict[str, Any]], num_turns: int) -> list[dict[str, Any]]:
    if num_turns <= 0:
        return history
    remaining = num_turns
    for index in range(len(history) - 1, -1, -1):
        if _is_user_turn_boundary(history[index]):
            remaining -= 1
            if remaining == 0:
                return history[:index]
    return []


def _rollback_turn_count(payload: dict[str, Any]) -> int:
    value = payload.get("num_turns", payload.get("num_user_turns", 0))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _rollout_line(item_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _timestamp(),
        "type": item_type,
        "payload": payload,
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _git_info(cwd: Path) -> dict[str, Any] | None:
    commit = _git_value(cwd, ["git", "rev-parse", "HEAD"])
    branch = _git_value(cwd, ["git", "branch", "--show-current"])
    repository_url = _git_value(cwd, ["git", "config", "--get", "remote.origin.url"])
    if not any([commit, branch, repository_url]):
        return None
    return _drop_none({"commit_hash": commit, "branch": branch, "repository_url": repository_url})


def _git_value(cwd: Path, command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _sandbox_policy(config: VolleyConfig) -> dict[str, Any]:
    network_access = config.network_access == "enabled"
    if config.sandbox == "danger-full-access":
        return {"type": "danger-full-access"}
    if config.sandbox == "read-only":
        return {"type": "read-only", "network_access": network_access}
    return {
        "type": "workspace-write",
        "writable_roots": [str(Path(root).expanduser().resolve()) for root in config.writable_roots],
        "network_access": network_access,
        "exclude_tmpdir_env_var": config.exclude_tmpdir_env_var,
        "exclude_slash_tmp": config.exclude_slash_tmp,
    }


def _file_system_sandbox_policy(config: VolleyConfig) -> dict[str, Any]:
    if config.sandbox == "danger-full-access":
        return {"kind": "unrestricted", "entries": []}
    if config.sandbox == "read-only":
        return {"kind": "restricted", "entries": [_fs_entry(_special_path("root"), "read")]}
    return {"kind": "restricted", "entries": _workspace_write_file_system_entries(config)}


def _workspace_write_file_system_entries(config: VolleyConfig) -> list[dict[str, Any]]:
    entries = [
        _fs_entry(_special_path("root"), "read"),
        _fs_entry(_special_path("project_roots"), "write"),
        _fs_entry(_special_path("project_roots", ".git"), "read"),
        _fs_entry(_special_path("project_roots", ".agents"), "read"),
        _fs_entry(_special_path("project_roots", ".volley"), "read"),
    ]
    if not config.exclude_slash_tmp:
        entries.append(_fs_entry(_special_path("slash_tmp"), "write"))
    if not config.exclude_tmpdir_env_var:
        entries.append(_fs_entry(_special_path("tmpdir"), "write"))
    entries.extend(_fs_entry({"path": str(Path(root).expanduser().resolve())}, "write") for root in config.writable_roots)
    return entries


def _permission_profile(config: VolleyConfig) -> dict[str, Any]:
    if config.sandbox == "danger-full-access":
        return {"type": "disabled"}
    file_system_policy = _file_system_sandbox_policy(config)
    if file_system_policy["kind"] == "unrestricted":
        file_system = {"type": "unrestricted"}
    else:
        file_system = {"type": "restricted", "entries": file_system_policy["entries"]}
    return {"type": "managed", "file_system": file_system, "network": config.network_access}


def _fs_entry(path: dict[str, Any], access: str) -> dict[str, Any]:
    return {"path": path, "access": access}


def _special_path(kind: str, subpath: str | None = None) -> dict[str, Any]:
    value = {"kind": kind}
    if subpath is not None:
        value["subpath"] = subpath
    return {"type": "special", "value": value}


def _truncation_policy(config: VolleyConfig) -> dict[str, Any] | None:
    limit = config.resolved_auto_compact_token_limit()
    if limit is None:
        return None
    return {"mode": "tokens", "limit": limit}


def _approval_policy_protocol_value(value: str) -> str:
    return {
        "untrusted": "untrusted",
        "on-failure": "on-failure",
        "on-request": "on-request",
        "never": "never",
    }[value]


def _collaboration_mode_protocol_value(value: str) -> str:
    return {
        "Default": "default",
        "Plan": "plan",
        "Execute": "default",
        "Pair Programming": "default",
    }[value]


def _turn_started_payload(state: VolleyState) -> dict[str, Any]:
    return {
        "type": "task_started",
        "turn_id": state.turn_id,
        "started_at": int(state._turn_started_at or time.time()),
        "model_context_window": state.config.resolved_model_context_window(),
        "collaboration_mode_kind": _collaboration_mode_protocol_value(state.config.collaboration_mode),
    }


def _turn_completed_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any]:
    completed_at = time.time()
    duration_ms = None
    if state._turn_started_at is not None:
        duration_ms = int((completed_at - state._turn_started_at) * 1000)
    return _drop_none(
        {
            "type": "task_complete",
            "turn_id": state.turn_id,
            "last_agent_message": event.payload.get("final_message") or None,
            "completed_at": int(completed_at),
            "duration_ms": duration_ms,
            "time_to_first_token_ms": None,
        }
    )


def _turn_aborted_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any]:
    completed_at = time.time()
    duration_ms = None
    if state._turn_started_at is not None:
        duration_ms = int((completed_at - state._turn_started_at) * 1000)
    return _drop_none(
        {
            "type": "turn_aborted",
            "turn_id": state.turn_id,
            "reason": event.payload.get("reason") or "interrupted",
            "completed_at": int(completed_at),
            "duration_ms": duration_ms,
        }
    )


def _token_count_payload(event: VolleyEvent) -> dict[str, Any]:
    return {
        "type": "token_count",
        "info": event.payload.get("info"),
        "rate_limits": event.payload.get("rate_limits"),
    }


def _stream_error_payload(event: VolleyEvent) -> dict[str, Any]:
    return _drop_none(
        {
            "type": "stream_error",
            "message": str(event.payload.get("message") or ""),
            "additional_details": event.payload.get("additional_details"),
            "codex_error_info": {"type": "response_stream_disconnected"},
        }
    )


def _hook_started_payload(event: VolleyEvent) -> dict[str, Any]:
    return {
        "type": "hook_started",
        "hook_event_name": str(event.payload.get("name") or ""),
        "request": event.payload.get("request"),
    }


def _hook_completed_payload(event: VolleyEvent) -> dict[str, Any]:
    return {
        "type": "hook_completed",
        "hook_event_name": str(event.payload.get("name") or ""),
        "success": bool(event.payload.get("ok")),
        "outcome": event.payload.get("outcome"),
        "error": event.payload.get("error"),
    }


_COLLAB_AGENT_TOOL_EVENT_TYPES = {
    "spawn_agent",
    "send_input",
    "resume_agent",
    "wait_agent",
    "close_agent",
}


def _tool_started_event_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any] | None:
    name = str(event.payload.get("name") or "")
    call_id = str(event.payload.get("call_id") or "")
    if name == "exec_command":
        arguments = event.payload.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        command = str(args.get("cmd") or "")
        return {
            "type": "exec_command_begin",
            "call_id": call_id,
            "turn_id": state.turn_id,
            "started_at_ms": int(time.time() * 1000),
            "command": [command],
            "cwd": str(_tool_workdir(state, args)),
            "parsed_cmd": parse_command_actions(command),
            "source": "agent",
        }
    if name in _COLLAB_AGENT_TOOL_EVENT_TYPES:
        return _collab_tool_started_event_payload(state, event)
    return None


def _collab_tool_started_event_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any] | None:
    name = str(event.payload.get("name") or "")
    call_id = str(event.payload.get("call_id") or "")
    arguments = event.payload.get("arguments")
    args = arguments if isinstance(arguments, dict) else {}
    base = {
        "call_id": call_id,
        "started_at_ms": int(time.time() * 1000),
        "sender_thread_id": state.thread_id,
    }
    if name == "spawn_agent":
        return {
            **base,
            "type": "collab_agent_spawn_begin",
            "prompt": _collab_prompt(args),
            "model": _collab_spawn_model(state, args),
            "reasoning_effort": _collab_spawn_reasoning_effort(state, args),
        }
    if name == "send_input":
        return {
            **base,
            "type": "collab_agent_interaction_begin",
            "receiver_thread_id": str(args.get("target") or ""),
            "prompt": _collab_prompt(args),
        }
    if name == "wait_agent":
        targets = args.get("targets")
        return {
            **base,
            "type": "collab_waiting_begin",
            "receiver_thread_ids": [str(item) for item in targets] if isinstance(targets, list) else [],
        }
    if name == "close_agent":
        return {
            **base,
            "type": "collab_close_begin",
            "receiver_thread_id": str(args.get("target") or ""),
        }
    if name == "resume_agent":
        return {
            **base,
            "type": "collab_resume_begin",
            "receiver_thread_id": str(args.get("id") or ""),
        }
    return None


def _collab_tool_completed_event_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any] | None:
    name = str(event.payload.get("name") or "")
    call_id = str(event.payload.get("call_id") or "")
    raw_args = state._tool_arguments_by_call.pop(call_id, {})
    args = raw_args if isinstance(raw_args, dict) else {}
    state._tool_started_at.pop(call_id, None)
    result = _collab_tool_result_payload(event)
    base = {
        "call_id": call_id,
        "completed_at_ms": int(time.time() * 1000),
        "sender_thread_id": state.thread_id,
    }
    if name == "spawn_agent":
        agent_id = _optional_str(result.get("agent_id"))
        return {
            **base,
            "type": "collab_agent_spawn_end",
            "new_thread_id": agent_id,
            "prompt": _collab_prompt(args),
            "model": _collab_spawn_model(state, args),
            "reasoning_effort": _collab_spawn_reasoning_effort(state, args),
            "status": "running" if bool(event.payload.get("ok")) and agent_id else "not_found",
        }
    if name == "send_input":
        return {
            **base,
            "type": "collab_agent_interaction_end",
            "receiver_thread_id": str(args.get("target") or ""),
            "prompt": _collab_prompt(args),
            "status": result.get("status") or ("running" if bool(event.payload.get("ok")) else {"errored": str(event.payload.get("output") or "")}),
        }
    if name == "wait_agent":
        statuses = result.get("status")
        return {
            **base,
            "type": "collab_waiting_end",
            "statuses": statuses if isinstance(statuses, dict) else {},
        }
    if name == "close_agent":
        return {
            **base,
            "type": "collab_close_end",
            "receiver_thread_id": str(args.get("target") or ""),
            "status": result.get("previous_status") or ("not_found" if not bool(event.payload.get("ok")) else "shutdown"),
        }
    if name == "resume_agent":
        return {
            **base,
            "type": "collab_resume_end",
            "receiver_thread_id": str(args.get("id") or ""),
            "status": result.get("status") or ("not_found" if not bool(event.payload.get("ok")) else "running"),
        }
    return None


def _collab_tool_result_payload(event: VolleyEvent) -> dict[str, Any]:
    metadata = event.payload.get("metadata")
    result = dict(metadata) if isinstance(metadata, dict) else {}
    output = event.payload.get("output")
    if isinstance(output, str) and output:
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            result = {**parsed, **result}
    return result


def _collab_prompt(args: dict[str, Any]) -> str:
    message = args.get("message")
    if isinstance(message, str):
        return message
    items = args.get("items")
    if not isinstance(items, list):
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
        elif isinstance(item.get("path"), str):
            chunks.append(item["path"])
        elif isinstance(item.get("name"), str):
            chunks.append(item["name"])
    return "\n".join(chunks)


def _collab_spawn_model(state: VolleyState, args: dict[str, Any]) -> str:
    value = args.get("model")
    return value if isinstance(value, str) and value else state.config.model


def _collab_spawn_reasoning_effort(state: VolleyState, args: dict[str, Any]) -> str | None:
    value = args.get("reasoning_effort")
    effort = value if isinstance(value, str) and value else state.config.model_reasoning_effort
    return effort if effort in {"none", "minimal", "low", "medium", "high", "xhigh"} else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _tool_completed_event_payload(state: VolleyState, event: VolleyEvent) -> dict[str, Any] | list[dict[str, Any]] | None:
    name = str(event.payload.get("name") or "")
    call_id = str(event.payload.get("call_id") or "")
    if name == "view_image":
        metadata = event.payload.get("metadata")
        if not isinstance(metadata, dict) or "path" not in metadata:
            return None
        return {"type": "view_image_tool_call", "call_id": call_id, "path": str(metadata["path"])}
    if name == "request_user_input":
        metadata = event.payload.get("metadata")
        if not isinstance(metadata, dict) or "questions" not in metadata:
            return None
        return {
            "type": "request_user_input",
            "call_id": call_id,
            "turn_id": state.turn_id,
            "questions": metadata.get("questions", []),
        }
    if name == "apply_patch":
        metadata = event.payload.get("metadata")
        changes = _apply_patch_metadata_changes(metadata)
        success = bool(event.payload.get("ok"))
        if not changes and success:
            changes = _apply_patch_changes(state._tool_arguments_by_call.get(call_id))
        if not changes:
            return None
        output = str(event.payload.get("output") or "")
        return [
            {
                "type": "patch_apply_begin",
                "call_id": call_id,
                "turn_id": state.turn_id,
                "auto_approved": True,
                "changes": changes,
            },
            {
                "type": "patch_apply_end",
                "call_id": call_id,
                "turn_id": state.turn_id,
                "stdout": output if success else "",
                "stderr": "" if success else output,
                "success": success,
                "changes": changes,
                "status": "completed" if success else "failed",
            },
        ]
    if name == "update_plan":
        metadata = event.payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        return {
            "type": "plan_update",
            "explanation": metadata.get("explanation"),
            "plan": metadata.get("plan", []),
        }
    if name in _COLLAB_AGENT_TOOL_EVENT_TYPES:
        return _collab_tool_completed_event_payload(state, event)
    if name != "exec_command":
        if name == "write_stdin":
            return _write_stdin_event_payload(state, event)
        return None
    metadata = event.payload.get("metadata")
    if not isinstance(metadata, dict) or "exit_code" not in metadata:
        return None
    output = str(metadata.get("output") or event.payload.get("output") or "")
    exit_code = _int_value(metadata.get("exit_code"), 1)
    duration_seconds = float(metadata.get("wall_time_seconds") or 0)
    started = state._tool_started_at.pop(call_id, None)
    raw_args = state._tool_arguments_by_call.pop(call_id, {})
    args = raw_args if isinstance(raw_args, dict) else {}
    if started is not None and duration_seconds <= 0:
        duration_seconds = max(0.0, time.time() - started)
    command = str(args.get("cmd") or "")
    return {
        "type": "exec_command_end",
        "call_id": call_id,
        "process_id": str(metadata.get("session_id")) if metadata.get("session_id") is not None else None,
        "turn_id": state.turn_id,
        "completed_at_ms": int(time.time() * 1000),
        "command": [command],
        "cwd": str(_tool_workdir(state, args)),
        "parsed_cmd": [_parsed_command(command)],
        "source": "agent",
        "stdout": str(metadata.get("stdout") or output),
        "stderr": str(metadata.get("stderr") or ""),
        "aggregated_output": str(metadata.get("aggregated_output") or output),
        "exit_code": exit_code,
        "duration": _duration_payload(duration_seconds),
        "formatted_output": str(event.payload.get("output") or ""),
        "status": "completed" if exit_code == 0 else "failed",
    }


def _write_stdin_event_payload(state: VolleyState, event: VolleyEvent) -> list[dict[str, Any]] | None:
    metadata = event.payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    call_id = str(event.payload.get("call_id") or "")
    raw_args = state._tool_arguments_by_call.pop(call_id, {})
    args = raw_args if isinstance(raw_args, dict) else {}
    state._tool_started_at.pop(call_id, None)
    process_id = args.get("session_id")
    event_call_id = str(metadata.get("event_call_id") or "")
    stdin = str(args.get("chars") or "")
    records = []
    if stdin or metadata.get("session_id") is not None or metadata.get("process_id") is not None:
        records.append(
            {
                "type": "terminal_interaction",
                "call_id": event_call_id,
                "process_id": str(process_id),
                "stdin": stdin,
            }
        )
    if "exit_code" not in metadata:
        return records
    output = str(metadata.get("output") or "")
    command = str(metadata.get("command") or "")
    workdir = Path(str(metadata.get("workdir") or state.config.resolved_cwd())).resolve()
    exit_code = _int_value(metadata.get("exit_code"), 1)
    records.append(
        {
            "type": "exec_command_end",
            "call_id": event_call_id,
            "process_id": str(process_id),
            "turn_id": state.turn_id,
            "completed_at_ms": int(time.time() * 1000),
            "command": [command],
            "cwd": str(workdir),
            "parsed_cmd": parse_command_actions(command),
            "source": "agent",
            "interaction_input": stdin,
            "stdout": str(metadata.get("stdout") or output),
            "stderr": str(metadata.get("stderr") or ""),
            "aggregated_output": str(metadata.get("aggregated_output") or output),
            "exit_code": exit_code,
            "duration": _duration_payload(float(metadata.get("wall_time_seconds") or 0)),
            "formatted_output": str(event.payload.get("output") or ""),
            "status": "completed" if exit_code == 0 else "failed",
        }
    )
    return records


def _tool_workdir(state: VolleyState, args: dict[str, Any]) -> Path:
    workdir = args.get("workdir")
    if isinstance(workdir, str) and workdir:
        path = Path(workdir).expanduser()
        if not path.is_absolute():
            path = state.config.resolved_cwd() / path
        return path.resolve()
    return state.config.resolved_cwd()


def _tool_path(state: VolleyState, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = state.config.resolved_cwd() / path
    return path.resolve()


def parse_command_actions(command: str) -> list[dict[str, Any]]:
    """Best-effort ParsedCommand metadata for shell transcript rendering."""

    stripped = _strip_leading_cd(command.strip())
    actions: list[dict[str, Any]] = []
    for segment in _split_command_sequence(stripped):
        action = _parse_command_segment(segment)
        if action is None:
            continue
        if isinstance(action, list):
            actions.extend(action)
        else:
            actions.append(action)
    deduped: list[dict[str, Any]] = []
    for action in actions:
        if not deduped or deduped[-1] != action:
            deduped.append(action)
    if deduped and not any(action.get("type") == "unknown" for action in deduped):
        return deduped
    return [{"type": "unknown", "cmd": stripped or command}]


def _parsed_command(command: str) -> dict[str, Any]:
    actions = parse_command_actions(command)
    return actions[0] if actions else {"type": "unknown", "cmd": command}


def _split_command_sequence(command: str) -> list[str]:
    parts = []
    current = []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [command]
    for token in tokens:
        if token in {"&&", ";"}:
            if current:
                parts.append(" ".join(current))
                current = []
            continue
        current.append(token)
    if current:
        parts.append(" ".join(current))
    return parts or [command]


def _strip_leading_cd(command: str) -> str:
    if not command.startswith("cd "):
        return command
    lexer = shlex.shlex(command, posix=True, punctuation_chars="&;|")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return command
    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        return " ".join(tokens[3:])
    return command


def _parse_command_segment(segment: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    primary = _primary_pipeline_command(segment)
    try:
        tokens = shlex.split(primary)
    except ValueError:
        return {"type": "unknown", "cmd": segment}
    if not tokens:
        return None
    command = shlex.join(tokens)
    program = Path(tokens[0]).name
    if program == "git" and len(tokens) >= 2 and tokens[1] == "ls-files":
        path = _last_path_arg(tokens[2:])
        return _drop_none({"type": "list_files", "cmd": command, "path": path})
    if program in {"rg", "ripgrep"} and "--files" in tokens[1:]:
        path = _last_path_arg([token for token in tokens[1:] if token != "--files"])
        return _drop_none({"type": "list_files", "cmd": command, "path": path})
    if program in {"fd", "find", "tree", "ls"}:
        path = _list_path_arg(program, tokens[1:])
        return _drop_none({"type": "list_files", "cmd": command, "path": path})
    if program in {"rg", "ripgrep", "grep", "git"}:
        search = _search_action(program, tokens, command)
        if search is not None:
            return search
    if program in {"cat", "bat"}:
        files = _file_args(tokens[1:])
        if files:
            return [_read_action(command, path) for path in files]
    if program in {"sed", "head", "tail", "nl"}:
        path = _last_path_arg(tokens[1:])
        if path:
            return _read_action(command, path)
    return {"type": "unknown", "cmd": segment}


def _primary_pipeline_command(segment: str) -> str:
    parts = segment.split("|")
    if not parts:
        return segment
    first = parts[0].strip()
    if len(parts) == 1:
        return first
    allowed = {"head", "tail", "sed", "sort", "wc", "nl"}
    for later in parts[1:]:
        try:
            tokens = shlex.split(later.strip())
        except ValueError:
            return segment
        if not tokens:
            continue
        if Path(tokens[0]).name not in allowed:
            return segment
    return first


def _read_action(command: str, path: str) -> dict[str, Any]:
    return {"type": "read", "cmd": command, "name": Path(path).name or path, "path": path}


def _search_action(program: str, tokens: list[str], command: str) -> dict[str, Any] | None:
    args = tokens[1:]
    if program == "git":
        if not args or args[0] != "grep":
            return None
        args = args[1:]
    query = None
    paths: list[str] = []
    skip_next = False
    options_with_values = {"-e", "-f", "-g", "--glob", "--type", "-t", "-A", "-B", "-C", "-m", "--max-count"}
    for idx, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in options_with_values:
            if token == "-e" and idx + 1 < len(args):
                query = args[idx + 1]
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if query is None:
            query = token
        else:
            paths.append(token)
    if query is None:
        return None
    return _drop_none({"type": "search", "cmd": command, "query": query, "path": paths[-1] if paths else None})


def _list_path_arg(program: str, args: list[str]) -> str | None:
    if program == "find":
        return _first_path_arg(args)
    return _last_path_arg(args)


def _first_path_arg(args: list[str]) -> str | None:
    for token in args:
        if _looks_like_path_arg(token):
            return token
    return None


def _last_path_arg(args: list[str]) -> str | None:
    paths = [token for token in args if _looks_like_path_arg(token)]
    return paths[-1] if paths else None


def _file_args(args: list[str]) -> list[str]:
    return [token for token in args if _looks_like_path_arg(token)]


def _looks_like_path_arg(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token in {"|", "&&", ";"}:
        return False
    return True


def _duration_payload(seconds: float) -> dict[str, int]:
    whole = int(max(seconds, 0.0))
    nanos = int((max(seconds, 0.0) - whole) * 1_000_000_000)
    return {"secs": whole, "nanos": nanos}


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _apply_patch_changes(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        patch = arguments
    elif isinstance(arguments, dict):
        patch = arguments.get("patch") if isinstance(arguments.get("patch"), str) else ""
    else:
        patch = ""
    if not patch:
        return {}
    if "*** Begin Patch" in patch:
        return _apply_patch_lark_changes(patch)
    return _unified_diff_changes(patch)


def _apply_patch_metadata_changes(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    changes = metadata.get("changes")
    if not isinstance(changes, list):
        return {}
    rendered: dict[str, Any] = {}
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        change_type = change.get("type")
        if not isinstance(path, str) or not isinstance(change_type, str):
            continue
        if change_type == "add":
            rendered[path] = {
                "type": "add",
                "content": str(change.get("content") or ""),
            }
        elif change_type == "delete":
            rendered[path] = {
                "type": "delete",
                "content": str(change.get("content") or ""),
            }
        elif change_type == "update":
            rendered[path] = {
                "type": "update",
                "unified_diff": str(change.get("unified_diff") or ""),
                "move_path": change.get("move_path"),
            }
    return rendered


def _unified_diff_changes(patch: str) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    current_path: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    hunk_lines: list[str] = []
    add_lines: list[str] = []
    delete_lines: list[str] = []

    def flush() -> None:
        nonlocal current_path, old_path, new_path, hunk_lines, add_lines, delete_lines
        if current_path is None:
            return
        if old_path == "/dev/null":
            changes[current_path] = {"type": "add", "content": "\n".join(add_lines) + ("\n" if add_lines else "")}
        elif new_path == "/dev/null":
            changes[current_path] = {"type": "delete", "content": "\n".join(delete_lines) + ("\n" if delete_lines else "")}
        else:
            changes[current_path] = {"type": "update", "unified_diff": "".join(hunk_lines), "move_path": None}
        current_path = None
        old_path = None
        new_path = None
        hunk_lines = []
        add_lines = []
        delete_lines = []

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split()
            if len(parts) >= 4:
                current_path = _strip_diff_prefix(parts[3])
            continue
        if line.startswith("--- "):
            old_path = _strip_diff_prefix(line[4:].strip())
            continue
        if line.startswith("+++ "):
            new_path = _strip_diff_prefix(line[4:].strip())
            if current_path is None:
                current_path = new_path if new_path != "/dev/null" else old_path
            continue
        if current_path is None:
            continue
        if line.startswith("@@"):
            hunk_lines.append(f"{line}\n")
        elif line.startswith("+"):
            hunk_lines.append(f"{line}\n")
            add_lines.append(line[1:])
        elif line.startswith("-"):
            hunk_lines.append(f"{line}\n")
            delete_lines.append(line[1:])
    flush()
    return changes


def _apply_patch_lark_changes(patch: str) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    current_path: str | None = None
    current_type: str | None = None
    move_path: str | None = None
    content_lines: list[str] = []
    hunk_lines: list[str] = []

    def flush() -> None:
        nonlocal current_path, current_type, move_path, content_lines, hunk_lines
        if current_path is None or current_type is None:
            return
        if current_type == "add":
            changes[current_path] = {"type": "add", "content": "\n".join(content_lines) + ("\n" if content_lines else "")}
        elif current_type == "delete":
            changes[current_path] = {"type": "delete", "content": ""}
        elif current_type == "update":
            changes[current_path] = {"type": "update", "unified_diff": "".join(hunk_lines), "move_path": move_path}
        current_path = None
        current_type = None
        move_path = None
        content_lines = []
        hunk_lines = []

    for line in patch.splitlines():
        if line.startswith("*** Add File: "):
            flush()
            current_path = line.removeprefix("*** Add File: ").strip()
            current_type = "add"
            continue
        if line.startswith("*** Delete File: "):
            flush()
            current_path = line.removeprefix("*** Delete File: ").strip()
            current_type = "delete"
            continue
        if line.startswith("*** Update File: "):
            flush()
            current_path = line.removeprefix("*** Update File: ").strip()
            current_type = "update"
            continue
        if line.startswith("*** Move to: "):
            move_path = line.removeprefix("*** Move to: ").strip()
            continue
        if line.startswith("*** End Patch"):
            flush()
            continue
        if current_type == "add" and line.startswith("+"):
            content_lines.append(line[1:])
        elif current_type == "update" and (line.startswith("@@") or line.startswith("+") or line.startswith("-")):
            hunk_lines.append(f"{line}\n")
    flush()
    return changes


def _strip_diff_prefix(path: str) -> str:
    if path in {"/dev/null", "dev/null"}:
        return "/dev/null"
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _track_apply_patch_change(state: VolleyState, change: dict[str, Any]) -> bool:
    change_type = str(change.get("type") or "")
    path = _display_patch_path(change.get("path"))
    if not path:
        return False
    if change_type == "add":
        content = change.get("content")
        if not isinstance(content, str):
            return False
        overwritten = change.get("overwritten_content")
        overwritten_content = overwritten if isinstance(overwritten, str) else None
        state._turn_diff_origin_by_current_path.pop(path, None)
        if (
            path not in state._turn_diff_current_by_path
            and path not in state._turn_diff_baseline_by_path
            and overwritten_content is not None
        ):
            state._turn_diff_baseline_by_path[path] = overwritten_content
        state._turn_diff_current_by_path[path] = content
        return True
    if change_type == "delete":
        content = change.get("content")
        if not isinstance(content, str):
            return False
        if state._turn_diff_current_by_path.pop(path, None) is None and path not in state._turn_diff_baseline_by_path:
            state._turn_diff_baseline_by_path[path] = content
        state._turn_diff_origin_by_current_path.pop(path, None)
        return True
    if change_type != "update":
        return False
    old_content = change.get("old_content")
    new_content = change.get("new_content")
    if not isinstance(old_content, str) or not isinstance(new_content, str):
        return False
    if path not in state._turn_diff_current_by_path and path not in state._turn_diff_baseline_by_path:
        state._turn_diff_baseline_by_path[path] = old_content
    move_path = _display_patch_path(change.get("move_path"))
    if move_path:
        overwritten_move = change.get("overwritten_move_content")
        if (
            move_path not in state._turn_diff_current_by_path
            and move_path not in state._turn_diff_baseline_by_path
            and isinstance(overwritten_move, str)
        ):
            state._turn_diff_baseline_by_path[move_path] = overwritten_move
        origin = state._turn_diff_origin_by_current_path.pop(path, path)
        state._turn_diff_current_by_path.pop(path, None)
        state._turn_diff_current_by_path[move_path] = new_content
        state._turn_diff_origin_by_current_path.pop(move_path, None)
        if move_path != origin:
            state._turn_diff_origin_by_current_path[move_path] = origin
    else:
        state._turn_diff_current_by_path[path] = new_content
    return True


def _turn_diff_unified_diff(state: VolleyState) -> str | None:
    if not state._turn_diff_valid:
        return None
    rename_pairs = _turn_diff_rename_pairs(state)
    paired_destinations = set(rename_pairs.values())
    handled: set[str] = set()
    paths = sorted(
        set(state._turn_diff_baseline_by_path) | set(state._turn_diff_current_by_path),
        key=lambda value: value.replace("\\", "/"),
    )
    aggregated = ""
    for path in paths:
        if path in handled:
            continue
        handled.add(path)
        if path in paired_destinations:
            continue
        if path in rename_pairs:
            destination = rename_pairs[path]
            handled.add(destination)
            diff = _render_turn_diff(
                path,
                state._turn_diff_baseline_by_path.get(path),
                destination,
                state._turn_diff_current_by_path.get(destination),
            )
        else:
            diff = _render_turn_diff(
                path,
                state._turn_diff_baseline_by_path.get(path),
                path,
                state._turn_diff_current_by_path.get(path),
            )
        if diff:
            aggregated += diff
            if not aggregated.endswith("\n"):
                aggregated += "\n"
    return aggregated or None


def _turn_diff_rename_pairs(state: VolleyState) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for dest_path, origin_path in state._turn_diff_origin_by_current_path.items():
        if (
            dest_path == origin_path
            or origin_path in state._turn_diff_current_by_path
            or dest_path not in state._turn_diff_current_by_path
            or origin_path not in state._turn_diff_baseline_by_path
            or dest_path in state._turn_diff_baseline_by_path
        ):
            continue
        pairs[origin_path] = dest_path
    return pairs


def _render_turn_diff(
    left_path: str,
    left_content: str | None,
    right_path: str,
    right_content: str | None,
) -> str | None:
    if left_content == right_content:
        return None
    left_display = left_path.replace("\\", "/")
    right_display = right_path.replace("\\", "/")
    left_oid = _git_blob_oid(left_content) if left_content is not None else "0" * 40
    right_oid = _git_blob_oid(right_content) if right_content is not None else "0" * 40
    lines = [f"diff --git a/{left_display} b/{right_display}\n"]
    if left_content is None:
        lines.append("new file mode 100644\n")
    elif right_content is None:
        lines.append("deleted file mode 100644\n")
    lines.append(f"index {left_oid}..{right_oid}\n")
    old_header = f"a/{left_display}" if left_content is not None else "/dev/null"
    new_header = f"b/{right_display}" if right_content is not None else "/dev/null"
    lines.extend(
        difflib.unified_diff(
            (left_content or "").splitlines(keepends=True),
            (right_content or "").splitlines(keepends=True),
            fromfile=old_header,
            tofile=new_header,
            n=3,
        )
    )
    return "".join(lines)


def _git_blob_oid(content: str) -> str:
    data = content.encode("utf-8")
    header = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()


def _display_patch_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.replace("\\", "/")


def _error_payload(message: str) -> dict[str, Any]:
    return {"type": "error", "message": message, "codex_error_info": None}


def _last_turn_aborted_marker(history: list[dict]) -> dict[str, Any] | None:
    if not history:
        return None
    item = history[-1]
    if not _is_turn_aborted_marker(item):
        return None
    return item


def _response_item_event_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("type") == "web_search_call":
        return _web_search_event_payload(item)
    return _message_event_payload(item)


def _web_search_event_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = item.get("id") or item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    action = item.get("action")
    if not isinstance(action, dict):
        action = {}
    query = item.get("query")
    if not isinstance(query, str):
        query = action.get("query")
    if not isinstance(query, str):
        query = ""
    return {
        "type": "web_search_end",
        "call_id": call_id,
        "query": query,
        "action": action,
    }


def _message_event_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("type") != "message":
        return None
    role = item.get("role")
    text = _message_text(item)
    if role == "user":
        return {
            "type": "user_message",
            "message": text,
            "images": None,
            "local_images": [],
            "text_elements": [],
        }
    if role == "assistant":
        return {
            "type": "agent_message",
            "message": text,
            "phase": None,
            "memory_citation": item.get("memory_citation"),
        }
    return None


def _message_text(item: dict[str, Any]) -> str:
    chunks = []
    for part in item.get("content", []):
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _last_history_message_text(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("type") == "message":
            text = _message_text(item)
            if text:
                return text
    return ""


def strip_memory_citations(text: str) -> tuple[str, list[str]]:
    open_tag = "<oai-mem-citation>"
    close_tag = "</oai-mem-citation>"
    visible_parts: list[str] = []
    citations: list[str] = []
    cursor = 0
    while True:
        start = text.find(open_tag, cursor)
        if start == -1:
            visible_parts.append(text[cursor:])
            break
        visible_parts.append(text[cursor:start])
        body_start = start + len(open_tag)
        end = text.find(close_tag, body_start)
        if end == -1:
            citations.append(text[body_start:])
            break
        citations.append(text[body_start:end])
        cursor = end + len(close_tag)
    return "".join(visible_parts), citations


def strip_proposed_plan_blocks(text: str) -> str:
    return "".join(segment for kind, segment in _proposed_plan_segments(text) if kind == "normal")


def extract_proposed_plan_text(text: str) -> str | None:
    saw_plan = False
    plan_text = ""
    current: list[str] = []
    for kind, segment in _proposed_plan_segments(text):
        if kind == "plan_start":
            saw_plan = True
            current = []
        elif kind == "plan_delta":
            current.append(segment)
        elif kind == "plan_end":
            plan_text = "".join(current)
    return plan_text if saw_plan else None


def _proposed_plan_segments(text: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    in_plan = False
    saw_unclosed_plan = False
    for line in text.splitlines(keepends=True):
        slug = line.rstrip("\n").rstrip("\r").strip()
        if in_plan:
            if slug == "</proposed_plan>":
                segments.append(("plan_end", ""))
                in_plan = False
                saw_unclosed_plan = False
            else:
                segments.append(("plan_delta", line))
            continue
        if slug == "<proposed_plan>":
            segments.append(("plan_start", ""))
            in_plan = True
            saw_unclosed_plan = True
        else:
            segments.append(("normal", line))
    if saw_unclosed_plan:
        segments.append(("plan_end", ""))
    return segments


def parse_memory_citation(citations: list[str]) -> dict | None:
    entries: list[dict] = []
    rollout_ids: list[str] = []
    seen_rollout_ids: set[str] = set()
    for citation in citations:
        entries_block = _extract_block(citation, "<citation_entries>", "</citation_entries>")
        if entries_block is not None:
            for line in entries_block.splitlines():
                entry = _parse_memory_citation_entry(line)
                if entry is not None:
                    entries.append(entry)
        ids_block = _extract_block(citation, "<rollout_ids>", "</rollout_ids>")
        if ids_block is None:
            ids_block = _extract_block(citation, "<thread_ids>", "</thread_ids>")
        if ids_block is not None:
            for raw_id in ids_block.splitlines():
                rollout_id = raw_id.strip()
                if rollout_id and rollout_id not in seen_rollout_ids:
                    seen_rollout_ids.add(rollout_id)
                    rollout_ids.append(rollout_id)
    if not entries and not rollout_ids:
        return None
    return {"entries": entries, "rollout_ids": rollout_ids}


def _parse_memory_citation_entry(line: str) -> dict | None:
    line = line.strip()
    if not line or "|note=[" not in line:
        return None
    location, note = line.rsplit("|note=[", 1)
    if not note.endswith("]"):
        return None
    note = note[:-1].strip()
    if ":" not in location:
        return None
    path, line_range = location.rsplit(":", 1)
    if "-" not in line_range:
        return None
    line_start, line_end = line_range.split("-", 1)
    try:
        start = int(line_start.strip())
        end = int(line_end.strip())
    except ValueError:
        return None
    return {"path": path.strip(), "line_start": start, "line_end": end, "note": note}


def _extract_block(text: str, open_tag: str, close_tag: str) -> str | None:
    if open_tag not in text:
        return None
    _, rest = text.split(open_tag, 1)
    if close_tag not in rest:
        return None
    body, _ = rest.split(close_tag, 1)
    return body
