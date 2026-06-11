from __future__ import annotations

import json
import itertools
import re
import threading
import time

from collections.abc import Iterator
from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .goal import GoalAccountingResult, GoalRuntime
from .memory import MemoryBackgroundTask, MemoryStateStore, MemoryStartupResult, MemoryThreadRecord
from .memory import run_memory_startup_pipeline_once, start_memory_startup_task
from .model import ModelClient, ModelStreamEvent, default_model_client, model_response_to_stream_events
from .prompts import build_base_instructions, build_initial_context_items
from .state import VolleyState, build_compaction_summary_text, extract_proposed_plan_text, parse_memory_citation, prepare_prompt_history, reconstruct_history_from_rollout, strip_memory_citations, strip_proposed_plan_blocks, summarization_prompt, trim_remote_compaction_history_to_fit_context_window
from .tools import ToolRuntime, _load_image_for_prompt
from .tools import ToolResult
from .types import VolleyConfig, VolleyEvent, VolleyResult, PromptRequest
from .types import _catalog_model_context_window


@dataclass(frozen=True)
class _ModelOutput:
    response_id: str
    items: list[dict[str, Any]]


class _ModelStreamFailure(RuntimeError):
    def __init__(self, message: str, *, response_id: str = "", retryable: bool = True, error: Any = None):
        super().__init__(message)
        self.response_id = response_id
        self.retryable = retryable
        self.error = error


@dataclass(frozen=True)
class _HookOutcome:
    should_stop: bool = False
    should_block: bool = False
    block_reason: str | None = None
    stop_reason: str | None = None
    feedback_message: str | None = None
    updated_input: Any = None
    additional_contexts: tuple[str, ...] = ()


class TurnInterrupted(RuntimeError):
    """Raised when the local interactive UI asks the current turn to stop."""


class SteerInputError(RuntimeError):
    """Raised when pending input cannot be attached to the active turn."""


INTERRUPTED_TURN_GUIDANCE = (
    "The user interrupted the previous turn on purpose. Any running unified exec "
    "processes may still be running in the background. If any tools/commands were "
    "aborted, they may have partially executed."
)
LOCAL_COMPACTION_WARNING_MESSAGE = (
    "Heads up: Long threads and multiple compactions can cause the model to be less accurate. "
    "Start a new thread when possible to keep threads small and targeted."
)
_MIN_AGENT_WAIT_TIMEOUT_MS = 10_000
_DEFAULT_AGENT_WAIT_TIMEOUT_MS = 30_000
_MAX_AGENT_WAIT_TIMEOUT_MS = 3_600_000


class VolleySession:
    """Python-native implementation of the public `volley exec` core loop."""

    def __init__(self, config: VolleyConfig | None = None, model_client: ModelClient | None = None):
        self.config = _config_with_memory_state_store(config or VolleyConfig())
        self.state = VolleyState(self.config)
        self.model_client = model_client or default_model_client(self.config)
        self.goals = GoalRuntime(self.config, self.state)
        self.tools = ToolRuntime(
            self.config,
            agent_runtime=_LocalAgentRuntime(self),
            goal_runtime=self.goals,
        )
        self._initial_context_recorded = False
        self._memory_startup_ran = False
        self._session_start_hook_ran = False
        self._interrupt_event = threading.Event()
        self._turn_state_lock = threading.RLock()
        self._active_turn_id: str | None = None
        self._active_turn_kind: str | None = None
        self._pending_input: deque[dict[str, Any]] = deque()
        self._idle_pending_input: deque[dict[str, Any]] = deque()
        self.memory_startup_result: MemoryStartupResult | None = None
        self.memory_startup_task: MemoryBackgroundTask | None = None

    def interrupt(self) -> None:
        self._interrupt_event.set()
        cancel_model = getattr(self.model_client, "cancel", None)
        interrupt_tools = getattr(self.tools, "request_interrupt", None)
        if not callable(interrupt_tools):
            interrupt_tools = getattr(self.tools, "interrupt_all", None)
        if not callable(cancel_model) and not callable(interrupt_tools):
            return

        thread_id = self.state.thread_id

        if callable(interrupt_tools):
            try:
                interrupt_tools()
            except Exception:
                pass

        def cancel_runtime() -> None:
            if callable(cancel_model):
                # Scope cancellation to this session's own in-flight responses so
                # interrupting/closing one agent does not abort sibling agents or
                # the parent that share the same model client. Fall back to the
                # argument-less form for clients that do not support scoping.
                try:
                    cancel_model(thread_id)
                except TypeError:
                    try:
                        cancel_model()
                    except Exception:
                        pass
                except Exception:
                    pass

        threading.Thread(target=cancel_runtime, daemon=True).start()

    def steer_input(self, prompt: str, *, expected_turn_id: str | None = None) -> str:
        """Attach user input to the currently running regular turn.

        Queued input is drained into history before the next model request,
        and the caller gets the active turn id back. If no regular turn is
        running, callers should queue a separate next turn instead.
        """

        item = _user_message(prompt, image_paths=(), cwd=self.config.resolved_cwd())
        return self.inject_response_items([item], expected_turn_id=expected_turn_id)

    def inject_response_items(
        self,
        items: list[dict[str, Any]],
        *,
        expected_turn_id: str | None = None,
    ) -> str:
        if not items:
            raise SteerInputError("input must not be empty")
        with self._turn_state_lock:
            if self._active_turn_id is None:
                raise SteerInputError("no active turn")
            if expected_turn_id is not None and expected_turn_id != self._active_turn_id:
                raise SteerInputError(
                    f"expected active turn {expected_turn_id}, got {self._active_turn_id}"
                )
            if self._active_turn_kind != "regular":
                raise SteerInputError(f"active turn is not steerable: {self._active_turn_kind}")
            for item in items:
                self._pending_input.append(item)
            return self._active_turn_id

    def prepend_pending_input(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        with self._turn_state_lock:
            self._pending_input = deque([*items, *self._pending_input])

    def queue_input_for_next_turn(self, prompt: str) -> None:
        item = _user_message(prompt, image_paths=(), cwd=self.config.resolved_cwd())
        with self._turn_state_lock:
            self._idle_pending_input.append(item)

    def has_pending_input(self) -> bool:
        with self._turn_state_lock:
            return bool(self._pending_input)

    def pop_pending_input_prompts_for_interrupt(self) -> list[str]:
        """Drain user steers that have not yet been committed to history.

        The interactive UI uses this for the reference ESC behavior: pending
        steers can interrupt the current model/tool wait and be resubmitted as
        the next user turn. If the turn loop already drained them into history,
        this returns an empty list so they are not duplicated.
        """

        with self._turn_state_lock:
            if not self._pending_input:
                return []
            items = list(self._pending_input)
            self._pending_input.clear()
        prompts: list[str] = []
        for item in items:
            text = _message_text(item).strip()
            if text:
                prompts.append(text)
        return prompts

    def _check_interrupted(self) -> None:
        if self._interrupt_event.is_set():
            raise TurnInterrupted("interrupted")

    @classmethod
    def resume_from_rollout(
        cls,
        rollout_path: str | Path,
        config: VolleyConfig | None = None,
        model_client: ModelClient | None = None,
    ) -> "VolleySession":
        session = cls(config, model_client)
        reconstruction = reconstruct_history_from_rollout(rollout_path, session.config)
        session.state.history = deepcopy(reconstruction.history)
        if reconstruction.session_meta and reconstruction.session_meta.get("id"):
            session.state.thread_id = str(reconstruction.session_meta["id"])
        session.state.previous_turn_settings = reconstruction.previous_turn_settings
        session.state.reference_context_item = reconstruction.reference_context_item
        session.state.last_token_usage = deepcopy(reconstruction.last_token_usage)
        session.state.total_token_usage = reconstruction.total_token_usage
        session.state.session_reasoning_tokens = reconstruction.session_reasoning_tokens
        session.state.context_carryover_tokens = reconstruction.context_carryover_tokens
        session.state.context_carryover_estimated = reconstruction.context_carryover_estimated
        if session.state.last_token_usage is None and session.state.history:
            session.state.recompute_token_usage_from_history()
        session.state._rollout_path = Path(rollout_path)
        session.state._rollout_initialized = True
        session._initial_context_recorded = _history_has_initial_context(session.state.history)
        return session

    @classmethod
    def fork_from_rollout(
        cls,
        rollout_path: str | Path,
        config: VolleyConfig | None = None,
        model_client: ModelClient | None = None,
    ) -> "VolleySession":
        session = cls(config, model_client)
        reconstruction = reconstruct_history_from_rollout(rollout_path, session.config)
        session.state.history = deepcopy(reconstruction.history)
        session.state._rollout_seed_history = deepcopy(reconstruction.history)
        if reconstruction.session_meta and reconstruction.session_meta.get("id"):
            session.state.forked_from_id = str(reconstruction.session_meta["id"])
        session.state.previous_turn_settings = reconstruction.previous_turn_settings
        session.state.reference_context_item = reconstruction.reference_context_item
        session.state.last_token_usage = deepcopy(reconstruction.last_token_usage)
        session.state.total_token_usage = reconstruction.total_token_usage
        session.state.session_reasoning_tokens = reconstruction.session_reasoning_tokens
        session.state.context_carryover_tokens = reconstruction.context_carryover_tokens
        session.state.context_carryover_estimated = reconstruction.context_carryover_estimated
        if session.state.last_token_usage is None and session.state.history:
            session.state.recompute_token_usage_from_history()
        session._initial_context_recorded = _history_has_initial_context(session.state.history)
        return session

    def run(self, prompt: str) -> VolleyResult:
        final_message = ""
        for event in self.stream(prompt):
            if event.type == "turn.completed":
                final_message = str(event.payload.get("final_message", ""))
        return VolleyResult(
            final_message=final_message,
            events=list(self.state.events),
            thread_id=self.state.thread_id,
            turn_id=self.state.turn_id,
            history=list(self.state.history),
            memory_citations=list(self.state.memory_citations),
        )

    def compact(self, prompt: str | None = None) -> VolleyResult:
        final_message = ""
        for event in self.stream_compact(prompt):
            if event.type == "context_compaction.completed":
                final_message = str(event.payload.get("summary", ""))
        return VolleyResult(
            final_message=final_message,
            events=list(self.state.events),
            thread_id=self.state.thread_id,
            turn_id=self.state.turn_id,
            history=list(self.state.history),
            memory_citations=list(self.state.memory_citations),
        )

    def stream_compact(self, prompt: str | None = None) -> Iterator[VolleyEvent]:
        clear_tool_interrupt = getattr(self.tools, "clear_interrupt", None)
        if callable(clear_tool_interrupt):
            clear_tool_interrupt()
        yield from self._stream_compact(prompt, trigger="manual", reason="manual", phase="standalone_turn")

    def stream_goal_continuation(self) -> Iterator[VolleyEvent]:
        item = self.goals.continuation_item_if_active()
        if item is None:
            return
        yield from self.stream(_message_text(item))

    def _stream_compact(
        self,
        prompt: str | None = None,
        *,
        trigger: str,
        reason: str = "context_limit",
        phase: str = "pre_sampling",
        model: str | None = None,
        inject_initial_context: bool | None = None,
    ) -> Iterator[VolleyEvent]:
        if trigger == "manual":
            self.state.start_turn()
            self._begin_active_turn("compact")
        cwd = self.config.resolved_cwd()
        compact_model = model or self.config.model
        compact_config = replace(self.config, model=compact_model)
        compact_prompt = prompt or self.config.compact_prompt or summarization_prompt()
        pre_compact_session_tokens, pre_compact_session_estimated = self.state.session_context_token_status()

        if trigger == "manual":
            yield self.state.emit("turn.started", compact=True)
        yield self.state.emit(
            "context_compaction.started",
            trigger=trigger,
            reason=reason,
            phase=phase,
            model=compact_model,
            active_context_tokens=self.state.active_context_tokens(),
            auto_compact_limit=compact_config.resolved_auto_compact_token_limit(),
        )
        pre_hook = yield from self._run_hook(
            "pre_compact",
            trigger=trigger,
            reason=reason,
            phase=phase,
            model=compact_model,
            compact=True,
            transcript_path=self._hook_transcript_path(),
        )
        yield from self._record_hook_additional_contexts(pre_hook)
        if pre_hook.should_stop or pre_hook.should_block:
            yield self.state.emit(
                "turn.aborted",
                reason=(
                    pre_hook.stop_reason
                    or pre_hook.block_reason
                    or "PreCompact hook stopped execution"
                ),
                compact=True,
                trigger=trigger,
            )
            if trigger == "manual":
                self._end_active_turn()
            return True

        should_inject_initial_context = phase == "mid_turn" if inject_initial_context is None else inject_initial_context
        initial_context = build_initial_context_items(self.config, cwd=cwd) if should_inject_initial_context else []
        summary_suffix = ""
        implementation = "responses"
        compacted_message: str | None = None
        if self._should_use_remote_compaction():
            remote_request = self._remote_compact_request(compact_config, compact_model, cwd)
            yield self.state.emit(
                "model.request",
                iteration=1,
                compact=True,
                trigger=trigger,
                remote_compaction=True,
                endpoint="responses/compact",
                tool_names=[_tool_spec_name(tool) for tool in remote_request.tools],
            )
            try:
                remote_history = self._compact_remote_history(remote_request)
            except Exception as exc:
                if self.config.remote_compaction == "required":
                    yield self.state.emit(
                        "turn.failed",
                        error=f"remote compaction failed: {exc}",
                        compact=True,
                        trigger=trigger,
                    )
                    if trigger == "manual":
                        self._end_active_turn()
                    return True
                yield self.state.emit(
                    "stream_error",
                    message="Remote compact failed; falling back to local compact prompt.",
                    error=str(exc),
                    compact=True,
                    trigger=trigger,
                    remote_compaction=True,
                )
            else:
                implementation = "responses_compact"
                compacted_message = ""
                self.state.compact_with_remote_history(remote_history, initial_context=initial_context)
                yield self.state.emit(
                    "model.response",
                    response_id="",
                    response={"output": remote_history},
                    compact=True,
                    trigger=trigger,
                    remote_compaction=True,
                    endpoint="responses/compact",
                )

        if implementation == "responses":
            compact_input = _user_message(compact_prompt)
            compact_history = prepare_prompt_history([*self.state.history, compact_input], compact_config)
            request = self._local_compact_request(compact_config, compact_model, cwd, compact_history)
            yield self.state.emit("model.request", iteration=1, compact=True, trigger=trigger, tool_names=[])
            model_output = yield from self._stream_model_output(request, compact=True, trigger=trigger, record_history=False)
            for item in model_output.items:
                text = _assistant_text(item)
                if text:
                    summary_suffix = text
            self.state.compact_with_summary(summary_suffix, initial_context=initial_context)
            # Pin the persisted checkpoint message to the summary explicitly: the
            # recent-activity offload block (when enabled) appends items after the
            # summary, so deriving it from the last history message would be wrong.
            compacted_message = build_compaction_summary_text(summary_suffix)

        self.state.recompute_token_usage_from_history()
        self.state.start_new_context_epoch(pre_compact_session_tokens, estimated=pre_compact_session_estimated)
        self._initial_context_recorded = bool(initial_context)
        completed_payload: dict[str, Any] = {
            "summary": summary_suffix,
            "trigger": trigger,
            "reason": reason,
            "phase": phase,
            "implementation": implementation,
            "remote_compaction": implementation == "responses_compact",
            "initial_context_injected": bool(initial_context),
            "replacement_history": list(self.state.history),
            "active_context_tokens": self.state.active_context_tokens(),
        }
        if compacted_message is not None:
            completed_payload["compacted_message"] = compacted_message
        yield self.state.emit("context_compaction.completed", **completed_payload)
        if implementation == "responses":
            yield self.state.emit("warning", message=LOCAL_COMPACTION_WARNING_MESSAGE)
        post_hook = yield from self._run_hook(
            "post_compact",
            trigger=trigger,
            reason=reason,
            phase=phase,
            model=compact_model,
            compact=True,
            transcript_path=self._hook_transcript_path(),
        )
        yield from self._record_hook_additional_contexts(post_hook)
        if post_hook.should_stop or post_hook.should_block:
            yield self.state.emit(
                "turn.aborted",
                reason=(
                    post_hook.stop_reason
                    or post_hook.block_reason
                    or "PostCompact hook stopped execution"
                ),
                compact=True,
                trigger=trigger,
            )
            if trigger == "manual":
                self._end_active_turn()
            return True
        if trigger == "manual":
            self._end_active_turn()
        return False

    def _local_compact_request(
        self,
        compact_config: VolleyConfig,
        compact_model: str,
        cwd: Path,
        compact_history: list[dict[str, Any]],
    ) -> PromptRequest:
        reasoning = compact_config.resolved_reasoning()
        return PromptRequest(
            model=compact_model,
            instructions=build_base_instructions(
                prompt_asset=compact_config.prompt_asset,
                model=compact_model,
                cwd=cwd,
                sandbox=compact_config.sandbox,
                approval_policy=compact_config.approval_policy,
                volley_home=compact_config.resolved_volley_home(),
                memory_tool_enabled=compact_config.memory_tool_enabled,
                use_memories=compact_config.use_memories,
            ),
            input=compact_history,
            tools=[],
            parallel_tool_calls=False,
            prompt_cache_key=self.state.thread_id,
            reasoning=reasoning,
            include=["reasoning.encrypted_content"] if reasoning is not None else [],
            store=compact_config.provider_is_azure_responses_endpoint,
            service_tier=compact_config.resolved_service_tier(),
            client_metadata=self._client_metadata(),
            session_id=self.state.thread_id,
            thread_id=self.state.thread_id,
            verbosity=compact_config.resolved_verbosity(),
        )

    def _remote_compact_request(
        self,
        compact_config: VolleyConfig,
        compact_model: str,
        cwd: Path,
    ) -> PromptRequest:
        reasoning = compact_config.resolved_reasoning()
        compact_history, _deleted_items = trim_remote_compaction_history_to_fit_context_window(
            self.state.history,
            compact_config,
        )
        return PromptRequest(
            model=compact_model,
            instructions=build_base_instructions(
                prompt_asset=compact_config.prompt_asset,
                model=compact_model,
                cwd=cwd,
                sandbox=compact_config.sandbox,
                approval_policy=compact_config.approval_policy,
                volley_home=compact_config.resolved_volley_home(),
                memory_tool_enabled=compact_config.memory_tool_enabled,
                use_memories=compact_config.use_memories,
            ),
            input=prepare_prompt_history(compact_history, compact_config),
            tools=self.tools.specs(),
            parallel_tool_calls=compact_config.resolved_parallel_tool_calls(),
            prompt_cache_key=self.state.thread_id,
            reasoning=reasoning,
            include=["reasoning.encrypted_content"] if reasoning is not None else [],
            store=compact_config.provider_is_azure_responses_endpoint,
            service_tier=compact_config.resolved_service_tier(),
            client_metadata=self._client_metadata(),
            session_id=self.state.thread_id,
            thread_id=self.state.thread_id,
            verbosity=compact_config.resolved_verbosity(),
        )

    def _compact_remote_history(self, request: PromptRequest) -> list[dict[str, Any]]:
        compact = getattr(self.model_client, "compact", None)
        if not callable(compact):
            raise RuntimeError("model client does not implement remote compaction")
        return compact(
            request,
            session_id=self.state.thread_id,
            thread_id=self.state.thread_id,
            installation_id=self.state.installation_id,
        )

    def _should_use_remote_compaction(self) -> bool:
        mode = self.config.remote_compaction
        if mode == "off":
            return False
        if mode == "required":
            return True
        if not _provider_supports_remote_compaction(self.config):
            return False
        return callable(getattr(self.model_client, "compact", None))

    def stream(self, prompt: str) -> Iterator[VolleyEvent]:
        self._interrupt_event.clear()
        clear_tool_interrupt = getattr(self.tools, "clear_interrupt", None)
        if callable(clear_tool_interrupt):
            clear_tool_interrupt()
        self.state.start_turn()
        self._begin_active_turn("regular")
        cwd = self.config.resolved_cwd()
        try:
            self._maybe_run_memory_startup()

            yield self.state.emit("thread.started", cwd=str(cwd), model=self.config.model)
            yield self.state.emit("turn.started")
            self.goals.on_turn_start()
            self._check_interrupted()

            if not self._session_start_hook_ran:
                self._session_start_hook_ran = True
                session_hook = yield from self._run_hook("session_start", source="local")
                yield from self._record_hook_additional_contexts(session_hook)
                if session_hook.should_stop:
                    yield self.state.emit("turn.failed", error=session_hook.stop_reason or "session_start hook stopped the turn")
                    return

            final_message = ""
            auto_compaction_markers: set[tuple[int, int, int]] = set()
            previous_model = self._previous_model_for_downshift_compaction()
            if previous_model is not None:
                compact_aborted = yield from self._stream_compact(
                    None,
                    trigger="auto",
                    reason="model_downshift",
                    phase="pre_sampling",
                    model=previous_model,
                    inject_initial_context=False,
                )
                if compact_aborted:
                    return
                auto_compaction_markers.add(self._auto_compaction_marker())

            if self._should_auto_compact():
                marker = self._auto_compaction_marker()
                if marker not in auto_compaction_markers:
                    auto_compaction_markers.add(marker)
                    compact_aborted = yield from self._stream_compact(
                        None,
                        trigger="auto",
                        reason="context_limit",
                        phase="pre_sampling",
                        inject_initial_context=False,
                    )
                    if compact_aborted:
                        return
                    auto_compaction_markers.add(self._auto_compaction_marker())

            if not self._initial_context_recorded:
                for context_item in build_initial_context_items(self.config, cwd=cwd):
                    self._check_interrupted()
                    self.state.append_history(context_item)
                    yield self.state.emit("item.completed", item=context_item)
                self._initial_context_recorded = True

            user_item = _user_message(prompt, image_paths=self.config.input_images, cwd=cwd)
            user_hook = yield from self._run_hook("user_prompt_submit", prompt=prompt, pending_input=False)
            if user_hook.should_stop:
                yield from self._record_hook_additional_contexts(user_hook)
                yield self.state.emit("turn.failed", error=user_hook.stop_reason or "user_prompt_submit hook stopped the turn")
                return
            self.state.append_history(user_item)
            yield self.state.emit("item.completed", item=user_item)
            yield from self._record_hook_additional_contexts(user_hook)

            can_drain_pending_input = False
            for iteration in itertools.count(1):
                if self.config.max_iterations is not None and iteration > self.config.max_iterations:
                    break
                self._check_interrupted()
                if iteration > 1 and self._should_auto_compact():
                    marker = self._auto_compaction_marker()
                    if marker not in auto_compaction_markers:
                        auto_compaction_markers.add(marker)
                        compact_aborted = yield from self._stream_compact(
                            None,
                            trigger="auto",
                            reason="context_limit",
                            phase="mid_turn",
                            inject_initial_context=True,
                        )
                        if compact_aborted:
                            return
                        auto_compaction_markers.add(self._auto_compaction_marker())
                        can_drain_pending_input = False

                if can_drain_pending_input:
                    pending_items = self._take_pending_input()
                    blocked_pending_input = False
                    accepted_pending: list[tuple[dict[str, Any], _HookOutcome]] = []
                    for index, pending_item in enumerate(pending_items):
                        self._check_interrupted()
                        pending_hook = yield from self._inspect_pending_input(pending_item)
                        if pending_hook.should_stop or pending_hook.should_block:
                            remaining = pending_items[index + 1 :]
                            if remaining:
                                self.prepend_pending_input(remaining)
                            yield from self._record_hook_additional_contexts(pending_hook)
                            blocked_pending_input = True
                            break
                        accepted_pending.append((pending_item, pending_hook))
                    for pending_item, pending_hook in accepted_pending:
                        self._check_interrupted()
                        self.state.append_history(pending_item)
                        yield self.state.emit("item.completed", item=pending_item, pending_input=True)
                        yield from self._record_hook_additional_contexts(pending_hook)
                    if blocked_pending_input and not accepted_pending:
                        if self.has_pending_input():
                            continue
                        yield self.state.emit("turn.failed", error="pending input was blocked by user_prompt_submit hook")
                        return

                yield from self._drain_agent_notifications()

                reasoning = self.config.resolved_reasoning()
                request = PromptRequest(
                    model=self.config.model,
                    instructions=build_base_instructions(
                        prompt_asset=self.config.prompt_asset,
                        model=self.config.model,
                        cwd=cwd,
                        sandbox=self.config.sandbox,
                        approval_policy=self.config.approval_policy,
                        volley_home=self.config.resolved_volley_home(),
                        memory_tool_enabled=self.config.memory_tool_enabled,
                        use_memories=self.config.use_memories,
                    ),
                    input=self.state.prompt_history(),
                    tools=self.tools.specs(),
                    parallel_tool_calls=self.config.resolved_parallel_tool_calls(),
                    prompt_cache_key=self.state.thread_id,
                    reasoning=reasoning,
                    include=["reasoning.encrypted_content"] if reasoning is not None else [],
                    store=self.config.provider_is_azure_responses_endpoint,
                    service_tier=self.config.resolved_service_tier(),
                    client_metadata=self._client_metadata(),
                    session_id=self.state.thread_id,
                    thread_id=self.state.thread_id,
                    verbosity=self.config.resolved_verbosity(),
                    output_schema=self.config.output_schema,
                    output_schema_strict=self.config.output_schema_strict,
                )
                if iteration == 1:
                    self._retry_primary_auth_on_next_request_if_available()
                yield self.state.emit(
                    "model.request",
                    iteration=iteration,
                    tool_names=[_tool_spec_name(tool) for tool in request.tools],
                )
                self._check_interrupted()
                model_output = yield from self._stream_model_output(request)
                self._check_interrupted()

                tool_calls = []
                for item in model_output.items:
                    text = _assistant_text(item)
                    if text:
                        final_message = text

                    tool_call = _tool_call_from_item(item)
                    if tool_call is not None:
                        tool_calls.append(tool_call)

                can_drain_pending_input = True
                if not tool_calls and not self.has_pending_input():
                    stop_hook = yield from self._run_hook("stop", final_message=final_message)
                    yield from self._record_hook_additional_contexts(stop_hook)
                    if stop_hook.should_block:
                        continuation = (
                            stop_hook.feedback_message
                            or stop_hook.block_reason
                            or "Stop hook requested the agent to continue."
                        )
                        context_item = _hook_context_message(continuation)
                        self.state.append_history(context_item)
                        yield self.state.emit("item.completed", item=context_item, hook_context=True)
                        continue
                    if stop_hook.should_stop:
                        yield self.state.emit("turn.failed", error=stop_hook.stop_reason or "stop hook stopped the turn")
                        return
                    after_agent_hook = yield from self._run_hook("after_agent", final_message=final_message)
                    yield from self._record_hook_additional_contexts(after_agent_hook)
                    if after_agent_hook.should_stop:
                        yield self.state.emit(
                            "turn.failed",
                            error=after_agent_hook.stop_reason or "after_agent hook stopped the turn",
                        )
                        return
                    yield from self._emit_goal_accounting_result(self.goals.on_turn_finished(completed=True))
                    self.state.write_last_message(final_message)
                    yield self.state.emit("turn.completed", final_message=final_message)
                    return

                if tool_calls:
                    yield from self._dispatch_tool_calls(tool_calls)
                    self._check_interrupted()

            yield self.state.emit("turn.failed", error="max_iterations exceeded")
        except TurnInterrupted:
            yield from self._emit_goal_accounting_result(self.goals.on_turn_aborted())
            self._record_interrupted_turn_marker()
            yield self.state.emit("turn.aborted", reason="interrupted")
            return
        finally:
            self._end_active_turn()

    def _retry_primary_auth_on_next_request_if_available(self) -> None:
        retry_primary = getattr(self.model_client, "retry_primary_on_next_request", None)
        if not callable(retry_primary):
            return
        try:
            retry_primary()
        except Exception:
            pass

    def _stream_model_output(
        self,
        request: PromptRequest,
        *,
        compact: bool = False,
        trigger: str | None = None,
        record_history: bool = True,
    ) -> Iterator[VolleyEvent]:
        max_retries = self.config.resolved_model_stream_max_retries()
        retry = 0
        while True:
            history_snapshot = list(self.state.history)
            try:
                return (yield from self._stream_model_output_once(
                    request,
                    compact=compact,
                    trigger=trigger,
                    record_history=record_history,
                ))
            except TurnInterrupted:
                raise
            except _ModelStreamFailure as exc:
                self._check_interrupted()
                retryable = exc.retryable
                message = str(exc)
                error = exc.error
                response_id = exc.response_id
            except Exception as exc:
                self._check_interrupted()
                retryable = _is_retryable_model_stream_error(exc)
                message = str(exc) or type(exc).__name__
                error = exc
                response_id = ""

            self.state.history = history_snapshot
            if not retryable or retry >= max_retries:
                yield self.state.emit("model.response", response_id=response_id, failed=True, **_model_event_scope(compact=compact, trigger=trigger))
                yield self.state.emit("turn.failed", error=message)
                raise RuntimeError(message)

            retry += 1
            delay = _model_stream_retry_delay_seconds(self.config, retry, error)
            yield self.state.emit(
                "stream_error",
                message=f"Reconnecting... {retry}/{max_retries}",
                additional_details=message,
                retry=retry,
                max_retries=max_retries,
                delay_seconds=delay,
                compact=compact,
                trigger=trigger,
            )
            self._check_interrupted()
            if delay > 0:
                time.sleep(delay)
            self._check_interrupted()

    def _stream_model_output_once(
        self,
        request: PromptRequest,
        *,
        compact: bool = False,
        trigger: str | None = None,
        record_history: bool = True,
    ) -> Iterator[VolleyEvent]:
        response_id = ""
        items: list[dict[str, Any]] = []
        scope = _model_event_scope(compact=compact, trigger=trigger)
        active_aggregates: dict[str, dict[str, str]] = {}
        for stream_event in self._stream_model_events(request):
            self._check_interrupted()
            if stream_event.type == "item.started":
                item_id = str(stream_event.payload.get("item_id") or "")
                if item_id:
                    active_aggregates[item_id] = {}
                yield self.state.emit("item.started", **scope, **_item_stream_payload(stream_event))
                continue
            if stream_event.type == "item.delta":
                item_id = str(stream_event.payload.get("item_id") or "")
                aggregate = _update_stream_aggregate(active_aggregates, stream_event)
                yield self.state.emit(
                    "item.delta",
                    **scope,
                    **_delta_stream_payload(stream_event),
                    aggregate=aggregate,
                )
                continue
            if stream_event.type == "item.completed":
                item = stream_event.payload.get("item")
                if not isinstance(item, dict):
                    continue
                item_id = str(stream_event.payload.get("item_id") or "")
                aggregate = active_aggregates.pop(item_id, {}) if item_id else {}
                normalized = _normalize_response_item(item)
                normalized = self._strip_and_record_memory_citation(normalized)
                self._maybe_mark_memory_polluted_for_external_context(normalized)
                items.append(normalized)
                if record_history:
                    self.state.append_history(normalized)
                yield self.state.emit(
                    "item.completed",
                    item=normalized,
                    **scope,
                    **_completed_stream_payload(stream_event),
                    aggregate=aggregate,
                )
                continue
            if stream_event.type == "token_count":
                usage = stream_event.payload.get("usage")
                self.state.record_token_usage(usage if isinstance(usage, dict) else None)
                self.goals.record_token_usage(usage if isinstance(usage, dict) else None)
                payload = dict(stream_event.payload)
                payload.setdefault("info", self.state.token_usage_info())
                yield self.state.emit(
                    "token_count",
                    **scope,
                    **payload,
                )
                continue
            if stream_event.type == "model.response":
                response_id = str(stream_event.payload.get("response_id") or response_id)
                yield self.state.emit("model.response", response_id=response_id, **scope, **_model_stream_payload(stream_event))
                continue
            if stream_event.type == "model.failed":
                response_id = str(stream_event.payload.get("response_id") or response_id)
                error = stream_event.payload.get("error") or "model stream failed"
                raise _ModelStreamFailure(
                    str(error),
                    response_id=response_id,
                    retryable=_is_retryable_model_stream_error(error),
                    error=error,
                )
            if stream_event.type == "warning":
                message = str(stream_event.payload.get("message") or "")
                if message:
                    yield self.state.emit("warning", message=message)
                continue
            self._check_interrupted()
        return _ModelOutput(response_id=response_id, items=items)

    def _stream_model_events(self, request: PromptRequest) -> Iterator[ModelStreamEvent]:
        stream = getattr(self.model_client, "stream", None)
        if callable(stream):
            yield from stream(request)
            return
        yield from model_response_to_stream_events(self.model_client.create(request))

    def _dispatch_tool_calls(self, tool_calls: list[dict[str, Any]]) -> Iterator[VolleyEvent]:
        parallel_enabled = self.config.resolved_parallel_tool_calls()
        pending_parallel: list[dict[str, Any]] = []

        def flush_parallel() -> Iterator[VolleyEvent]:
            nonlocal pending_parallel
            if not pending_parallel:
                return
            calls = pending_parallel
            pending_parallel = []
            for call in calls:
                self._check_interrupted()
                yield self.state.emit(
                    "tool.started",
                    name=call["name"],
                    call_id=call["call_id"],
                    arguments=call["arguments"],
                )
            with ThreadPoolExecutor(max_workers=len(calls)) as executor:
                futures = [executor.submit(self._dispatch_tool_preserving_interrupt, call) for call in calls]
                for call, future in zip(calls, futures):
                    self._check_interrupted()
                    result = yield from self._await_tool_future(future)
                    yield from self._drain_tool_runtime_events(include_output_delta=True)
                    yield from self._record_tool_result(call, result)

        for call in tool_calls:
            self._check_interrupted()
            normalize_call = getattr(self.tools, "normalize_tool_call", None)
            if callable(normalize_call):
                call = normalize_call(call)
            pre_hook = yield from self._run_tool_pre_hook(call)
            if pre_hook.should_block:
                yield from flush_parallel()
                hook_tool = _tool_hook_contract(call)
                result = ToolResult(
                    False,
                    _pre_tool_block_message(call, pre_hook.block_reason),
                    {"hook_blocked": True, "tool": call["name"], "hook_tool_name": hook_tool.name},
                )
                yield self.state.emit(
                    "tool.started",
                    name=call["name"],
                    call_id=call["call_id"],
                    arguments=call["arguments"],
                )
                yield from self._record_tool_result(call, result)
                continue
            if pre_hook.updated_input is not None:
                try:
                    call = _tool_call_with_updated_hook_input(call, pre_hook.updated_input)
                except ValueError as exc:
                    yield from flush_parallel()
                    result = ToolResult(
                        False,
                        str(exc),
                        {"hook_updated_input_error": True, "tool": call["name"]},
                    )
                    yield self.state.emit(
                        "tool.started",
                        name=call["name"],
                        call_id=call["call_id"],
                        arguments=call["arguments"],
                    )
                    yield from self._record_tool_result(call, result)
                    continue
            if parallel_enabled and self.tools.supports_parallel(call["name"]):
                pending_parallel.append(call)
                continue
            yield from flush_parallel()
            self._check_interrupted()
            yield self.state.emit(
                "tool.started",
                name=call["name"],
                call_id=call["call_id"],
                arguments=call["arguments"],
            )
            result = yield from self._dispatch_tool_call_with_runtime_events(call)
            yield from self._drain_tool_runtime_events(include_output_delta=True)
            self._check_interrupted()
            yield from self._record_tool_result(call, result)
        yield from flush_parallel()

    def _dispatch_tool_call_with_runtime_events(self, call: dict[str, Any]) -> Iterator[VolleyEvent | ToolResult]:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._dispatch_tool_preserving_interrupt, call)
            result = yield from self._await_tool_future(future)
        return result

    def _dispatch_tool_preserving_interrupt(self, call: dict[str, Any]) -> ToolResult:
        try:
            return self.tools.dispatch(
                call["name"],
                call["arguments"],
                call_id=call["call_id"],
                clear_interrupt=False,
            )
        except TypeError as exc:
            if "clear_interrupt" not in str(exc):
                raise
            return self.tools.dispatch(call["name"], call["arguments"], call_id=call["call_id"])

    def _await_tool_future(self, future: Any) -> Iterator[VolleyEvent | ToolResult]:
        while True:
            try:
                return future.result(timeout=0.03)
            except FutureTimeoutError:
                yield from self._drain_tool_runtime_events(include_output_delta=True)
                self._check_interrupted()

    def _drain_tool_runtime_events(self, *, include_output_delta: bool = True) -> Iterator[VolleyEvent]:
        drain = getattr(self.tools, "drain_runtime_events", None)
        if not callable(drain):
            return
        for event in drain():
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            payload = event.get("payload", {})
            if not isinstance(event_type, str) or not isinstance(payload, dict):
                continue
            if not include_output_delta and event_type == "exec_command.output_delta":
                continue
            yield self.state.emit(event_type, **payload)

    def _record_tool_result(self, call: dict[str, Any], result: Any) -> Iterator[VolleyEvent]:
        if getattr(result, "ok", False):
            hook_contract = _post_tool_hook_contract(call, result)
            if hook_contract is not None:
                post_hook = yield from self._run_hook(
                    "post_tool_use",
                    tool_name=hook_contract.name,
                    matcher_aliases=list(hook_contract.matcher_aliases),
                    tool_use_id=hook_contract.tool_use_id or call["call_id"],
                    tool_input=hook_contract.tool_input,
                    tool_response=hook_contract.tool_response,
                )
                replacement_text = (
                    post_hook.feedback_message
                    or (post_hook.stop_reason if post_hook.should_stop else None)
                )
                if replacement_text:
                    result = ToolResult(
                        result.ok,
                        replacement_text,
                        {**result.metadata, "post_tool_use_replaced_output": True},
                        result.response_output,
                    )
                yield from self._record_hook_additional_contexts(post_hook)
                if post_hook.should_stop:
                    result = ToolResult(
                        False,
                        result.output,
                        {**result.metadata, "post_tool_use_stopped": True},
                        result.response_output,
                    )
        output_item = _tool_output_item(call, result)
        self.state.append_history(output_item)
        yield self.state.emit(
            "tool.completed",
            name=call["name"],
            call_id=call["call_id"],
            ok=result.ok,
            output=result.output,
            metadata=result.metadata,
        )
        if call["name"] == "apply_patch":
            unified_diff = self.state.record_apply_patch_turn_diff(result.metadata)
            if unified_diff is not None:
                yield self.state.emit("turn_diff", unified_diff=unified_diff)
        yield self.state.emit("item.completed", item=output_item)
        handler_executed = not bool(result.metadata.get("hook_blocked")) and call.get("name") != "unknown"
        yield from self._emit_goal_accounting_result(
            self.goals.on_tool_finished(call["name"], handler_executed=handler_executed)
        )
        yield from self._drain_agent_notifications()

    def _emit_goal_accounting_result(self, result: GoalAccountingResult) -> Iterator[VolleyEvent]:
        yield from self._emit_goal_events(result.events)
        if result.steering_items:
            try:
                self.inject_response_items(list(result.steering_items), expected_turn_id=self.state.turn_id)
            except SteerInputError:
                pass

    def _emit_goal_events(self, events: tuple[Any, ...]) -> Iterator[VolleyEvent]:
        for event in events:
            event_type = getattr(event, "type", None)
            payload = getattr(event, "payload", None)
            if isinstance(event_type, str) and isinstance(payload, dict):
                yield self.state.emit(event_type, **payload)

    def _drain_agent_notifications(self) -> Iterator[VolleyEvent]:
        runtime = getattr(self.tools, "agent_runtime", None)
        drain = getattr(runtime, "drain_notifications", None)
        if not callable(drain):
            return
        for item in drain():
            if not isinstance(item, dict):
                continue
            self.state.append_history(item)
            yield self.state.emit("item.completed", item=item, subagent_notification=True)

    def _run_tool_pre_hook(self, call: dict[str, Any]) -> Iterator[VolleyEvent]:
        hook_contract = _pre_tool_hook_contract(call)
        if hook_contract is None:
            return _HookOutcome()
        outcome = yield from self._run_hook(
            "pre_tool_use",
            tool_name=hook_contract.name,
            matcher_aliases=list(hook_contract.matcher_aliases),
            tool_use_id=call["call_id"],
            tool_input=hook_contract.tool_input,
        )
        yield from self._record_hook_additional_contexts(outcome)
        return outcome

    def _inspect_pending_input(self, item: dict[str, Any]) -> Iterator[VolleyEvent]:
        if item.get("type") == "message" and item.get("role") == "user":
            outcome = yield from self._run_hook(
                "user_prompt_submit",
                prompt=_message_text(item),
                pending_input=True,
            )
            return outcome
        return _HookOutcome()

    def _record_hook_additional_contexts(self, outcome: _HookOutcome) -> Iterator[VolleyEvent]:
        for context in outcome.additional_contexts:
            if not context.strip():
                continue
            item = _hook_context_message(context)
            self.state.append_history(item)
            yield self.state.emit("item.completed", item=item, hook_context=True)

    def _run_hook(self, event_name: str, **payload: Any) -> Iterator[VolleyEvent]:
        provider = self.config.hook_provider
        if provider is None:
            return _HookOutcome()
        request = {
            "event": event_name,
            "session_id": self.state.thread_id,
            "turn_id": self.state.turn_id,
            "cwd": str(self.config.resolved_cwd()),
            "model": self.config.model,
            "approval_policy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            **payload,
        }
        yield self.state.emit("hook.started", name=event_name, request=_json_safe(request))
        try:
            decision = provider(request)
        except Exception as exc:
            yield self.state.emit(
                "hook.completed",
                name=event_name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                outcome={},
            )
            return _HookOutcome()
        outcome = _normalize_hook_outcome(decision)
        yield self.state.emit("hook.completed", name=event_name, ok=True, outcome=_json_safe(decision))
        return outcome

    def _hook_transcript_path(self) -> str | None:
        if self.config.ephemeral:
            return None
        return str(self.state.rollout_path())

    def _should_auto_compact(self) -> bool:
        limit = self.config.resolved_auto_compact_token_limit()
        return limit is not None and self.state.active_context_tokens() >= limit

    def _auto_compaction_marker(self) -> tuple[int, int, int]:
        return (len(self.state.history), self.state.active_context_tokens(), self.state.approx_history_tokens())

    def _previous_model_for_downshift_compaction(self) -> str | None:
        previous = self.state.previous_turn_settings or {}
        previous_model = previous.get("model")
        if not isinstance(previous_model, str) or not previous_model:
            return None
        if previous_model.lower() == self.config.model.lower():
            return None
        previous_window = _catalog_model_context_window(previous_model)
        current_window = self.config.resolved_model_context_window()
        if previous_window is None or current_window is None or previous_window <= current_window:
            return None
        limit = self.config.resolved_auto_compact_token_limit()
        if limit is None or self.state.active_context_tokens() <= limit:
            return None
        return previous_model

    def _client_metadata(self) -> dict[str, str]:
        metadata = {"x-codex-installation-id": self.state.installation_id}
        if self.config.client_metadata:
            metadata.update(self.config.client_metadata)
        return metadata

    def _strip_and_record_memory_citation(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("type") != "message" or item.get("role") != "assistant":
            return item
        citations: list[str] = []
        proposed_plans: list[str] = []
        changed = False
        content = []
        for part in item.get("content", []):
            if not isinstance(part, dict):
                content.append(part)
                continue
            text = part.get("text")
            if isinstance(text, str):
                visible, part_citations = strip_memory_citations(text)
                if part_citations:
                    changed = True
                    citations.extend(part_citations)
                if self.config.collaboration_mode == "Plan":
                    proposed_plan = extract_proposed_plan_text(visible)
                    if proposed_plan is not None:
                        proposed_plans.append(proposed_plan)
                    visible_without_plan = strip_proposed_plan_blocks(visible)
                    if visible_without_plan != visible:
                        changed = True
                        visible = visible_without_plan
                if visible != text:
                    part = {**part, "text": visible}
            content.append(part)
        citation = parse_memory_citation(citations)
        if citation is not None:
            self.state.record_memory_citation(citation)
            self._record_memory_usage(citation)
            changed = True
        if proposed_plans:
            changed = True
        if not changed:
            return item
        updated = dict(item)
        updated["content"] = content
        if citation is not None:
            updated["memory_citation"] = citation
        if proposed_plans:
            updated["proposed_plan"] = proposed_plans[-1]
        return updated

    def _record_memory_usage(self, citation: dict[str, Any]) -> None:
        thread_ids = [str(thread_id) for thread_id in citation.get("rollout_ids", []) if str(thread_id)]
        if not thread_ids or self.config.memory_state_store is None:
            return
        try:
            self.config.memory_state_store.record_stage1_output_usage(thread_ids)
        except Exception:
            pass

    def _maybe_mark_memory_polluted_for_external_context(self, item: dict[str, Any]) -> None:
        if not self.config.memory_disable_on_external_context:
            return
        if item.get("type") not in {"tool_search_call", "tool_search_output", "web_search_call"}:
            return
        state_store = self.config.memory_state_store
        if state_store is None:
            return
        try:
            state_store.mark_thread_memory_mode_polluted(self.state.thread_id)
        except Exception:
            pass

    def _begin_active_turn(self, kind: str) -> None:
        with self._turn_state_lock:
            self._active_turn_id = self.state.turn_id
            self._active_turn_kind = kind
            self._pending_input.clear()
            while self._idle_pending_input:
                self._pending_input.append(self._idle_pending_input.popleft())

    def _end_active_turn(self) -> None:
        with self._turn_state_lock:
            self._active_turn_id = None
            self._active_turn_kind = None
            self._pending_input.clear()

    def _take_pending_input(self) -> list[dict[str, Any]]:
        with self._turn_state_lock:
            if not self._pending_input:
                return []
            items = list(self._pending_input)
            self._pending_input.clear()
            return items

    def _record_interrupted_turn_marker(self) -> None:
        self.state.append_history(_turn_aborted_marker())

    def _maybe_run_memory_startup(self) -> None:
        if self._memory_startup_ran or not _should_run_memory_startup(self.config):
            return
        self._memory_startup_ran = True
        state_store = self.config.memory_state_store
        if state_store is None:
            return
        self._upsert_current_memory_thread(state_store)
        rate_limit_snapshot = _memory_rate_limit_snapshot(self.config)
        if self.config.memory_startup_background:
            state_store_path = getattr(state_store, "path", None)
            self.memory_startup_task = start_memory_startup_task(
                volley_home=self.config.resolved_volley_home(),
                model_client=self.model_client,
                state_store_path=state_store_path,
                base_config=self.config,
                max_rollouts=self.config.memory_max_rollouts_per_startup,
                max_raw_memories_for_consolidation=self.config.memory_max_raw_memories_for_consolidation,
                max_unused_days=self.config.memory_max_unused_days,
                max_rollout_age_days=self.config.memory_max_rollout_age_days,
                min_rollout_idle_hours=self.config.memory_min_rollout_idle_hours,
                current_thread_id=self.state.thread_id,
                model_context_window=self.config.model_context_window,
                run_phase2=self.config.memory_run_phase2_on_startup,
                rate_limit_snapshot=rate_limit_snapshot,
                min_rate_limit_remaining_percent=self.config.memory_min_rate_limit_remaining_percent,
            )
            return
        try:
            self.memory_startup_result = run_memory_startup_pipeline_once(
                volley_home=self.config.resolved_volley_home(),
                model_client=self.model_client,
                state_store=state_store,
                base_config=self.config,
                max_rollouts=self.config.memory_max_rollouts_per_startup,
                max_raw_memories_for_consolidation=self.config.memory_max_raw_memories_for_consolidation,
                max_unused_days=self.config.memory_max_unused_days,
                max_rollout_age_days=self.config.memory_max_rollout_age_days,
                min_rollout_idle_hours=self.config.memory_min_rollout_idle_hours,
                current_thread_id=self.state.thread_id,
                model_context_window=self.config.model_context_window,
                run_phase2=self.config.memory_run_phase2_on_startup,
                rate_limit_snapshot=rate_limit_snapshot,
                min_rate_limit_remaining_percent=self.config.memory_min_rate_limit_remaining_percent,
            )
        except Exception:
            self.memory_startup_result = None

    def _upsert_current_memory_thread(self, state_store: MemoryStateStore) -> None:
        memory_mode = "enabled" if self.config.memory_generate_memories else "disabled"
        try:
            state_store.upsert_thread(
                MemoryThreadRecord(
                    thread_id=self.state.thread_id,
                    rollout_path=self.state.rollout_path(),
                    cwd=self.config.resolved_cwd(),
                    updated_at=datetime.now(timezone.utc),
                    source=self.config.session_source,
                    memory_mode=memory_mode,
                )
            )
        except Exception:
            pass


@dataclass(frozen=True)
class _ToolHookContract:
    name: str
    matcher_aliases: tuple[str, ...]
    tool_input: Any
    tool_response: Any | None = None
    tool_use_id: str | None = None


def _tool_hook_contract(call: dict[str, Any], result: Any | None = None) -> _ToolHookContract:
    name = str(call.get("name") or "")
    args = _tool_call_arguments_object(call.get("arguments"))
    if name == "exec_command":
        command = str(args.get("cmd") or "")
        return _ToolHookContract(
            name="Bash",
            matcher_aliases=(),
            tool_input={"command": command},
            tool_response=_post_tool_response_for_hook(name, result) if result is not None else None,
            tool_use_id=_tool_result_event_call_id(result) or str(call.get("call_id") or ""),
        )
    if name == "write_stdin":
        metadata = getattr(result, "metadata", None) if result is not None else None
        command = ""
        if isinstance(metadata, dict):
            command = str(metadata.get("command") or "")
        return _ToolHookContract(
            name="Bash",
            matcher_aliases=(),
            tool_input={"command": command},
            tool_response=_post_tool_response_for_hook(name, result) if result is not None else None,
            tool_use_id=_tool_result_event_call_id(result) or str(call.get("call_id") or ""),
        )
    if name == "shell_command":
        command = str(args.get("command") or "")
        return _ToolHookContract(
            name="Bash",
            matcher_aliases=(),
            tool_input={"command": command},
            tool_response=_post_tool_response_for_hook(name, result) if result is not None else None,
        )
    if name == "apply_patch":
        patch = _apply_patch_hook_command(call.get("arguments"))
        return _ToolHookContract(
            name="apply_patch",
            matcher_aliases=("Write", "Edit"),
            tool_input={"command": patch},
            tool_response=_post_tool_response_for_hook(name, result) if result is not None else None,
        )
    return _ToolHookContract(
        name=name,
        matcher_aliases=(),
        tool_input=call.get("arguments"),
        tool_response=_post_tool_response_for_hook(name, result) if result is not None else None,
    )


def _pre_tool_hook_contract(call: dict[str, Any]) -> _ToolHookContract | None:
    name = str(call.get("name") or "")
    if name in {"exec_command", "shell_command", "apply_patch"}:
        return _tool_hook_contract(call)
    return None


def _post_tool_hook_contract(call: dict[str, Any], result: Any) -> _ToolHookContract | None:
    name = str(call.get("name") or "")
    if name not in {"exec_command", "write_stdin", "shell_command", "apply_patch"}:
        return None
    contract = _tool_hook_contract(call, result)
    if contract.tool_response is None:
        return None
    if name == "write_stdin" and isinstance(contract.tool_input, dict) and not contract.tool_input.get("command"):
        return None
    return contract


def _tool_call_with_updated_hook_input(call: dict[str, Any], updated_input: Any) -> dict[str, Any]:
    command = _updated_hook_command(updated_input)
    name = str(call.get("name") or "")
    if name == "exec_command":
        args = _tool_call_arguments_object(call.get("arguments"))
        return {**call, "arguments": {**args, "cmd": command}}
    if name == "shell_command":
        args = _tool_call_arguments_object(call.get("arguments"))
        return {**call, "arguments": {**args, "command": command}}
    if name == "apply_patch":
        original = call.get("arguments")
        if isinstance(original, dict):
            return {**call, "arguments": {**original, "patch": command}}
        return {**call, "arguments": command}
    return {**call, "arguments": updated_input}


def _updated_hook_command(updated_input: Any) -> str:
    if isinstance(updated_input, dict):
        value = updated_input.get("command")
    else:
        value = updated_input
    if not isinstance(value, str) or not value:
        raise ValueError("hook updated_input must contain a non-empty `command` string")
    return value


def _tool_call_arguments_object(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _apply_patch_hook_command(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        patch = arguments.get("patch")
        return patch if isinstance(patch, str) else ""
    return ""


def _post_tool_response_for_hook(tool_name: str, result: Any | None) -> Any | None:
    if result is None:
        return None
    metadata = getattr(result, "metadata", None)
    if tool_name in {"exec_command", "write_stdin"}:
        if not isinstance(metadata, dict) or "session_id" in metadata:
            return None
        output = metadata.get("output")
        return output if isinstance(output, str) else ""
    if tool_name == "shell_command":
        return getattr(result, "output", "")
    if tool_name == "apply_patch":
        return getattr(result, "output", "")
    if isinstance(metadata, dict):
        response_text = metadata.get("response_text")
        if isinstance(response_text, str) and response_text:
            return response_text
    return None


def _tool_result_event_call_id(result: Any | None) -> str | None:
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("event_call_id")
    return value if isinstance(value, str) and value else None


def _pre_tool_block_message(call: dict[str, Any], block_reason: str | None) -> str:
    reason = block_reason or "blocked"
    contract = _tool_hook_contract(call)
    command = None
    if isinstance(contract.tool_input, dict):
        raw_command = contract.tool_input.get("command")
        if isinstance(raw_command, str) and raw_command:
            command = raw_command
    if contract.name in {"Bash", "apply_patch"} and command:
        return f"Command blocked by PreToolUse hook: {reason}. Command: {command}"
    return f"Tool call blocked by PreToolUse hook: {reason}. Tool: {contract.name}"


class _LocalAgentRecord:
    def __init__(self, session: VolleySession, nickname: str | None, reference: str):
        self.session = session
        self.nickname = nickname
        self.reference = reference
        self.condition = threading.Condition()
        self.pending: deque[str] = deque()
        self.thread: threading.Thread | None = None
        self.running = False
        self.shutdown = False
        self.interrupted = False
        self.final_message: str | None = None
        self.error: str | None = None


class _LocalAgentRuntime:
    def __init__(self, parent: VolleySession):
        self.parent = parent
        self._agents: dict[str, _LocalAgentRecord] = {}
        self._pending_notifications: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()

    def spawn_agent(self, arguments: dict[str, Any]) -> Any:
        prompt = _agent_prompt(arguments)
        if not prompt:
            return _agent_tool_error("spawn_agent requires message or text items")
        child_depth = self.parent.config.agent_depth + 1
        if child_depth > self.parent.config.max_agent_depth:
            return _agent_tool_error("Agent depth limit reached. Solve the task yourself.")
        child = self._new_child_session(arguments)
        nickname = _agent_nickname(arguments)
        agent_id = child.state.thread_id
        record = _LocalAgentRecord(child, nickname, _agent_reference(arguments, agent_id))
        with self._lock:
            self._agents[agent_id] = record
        self._start_agent(record, prompt)
        payload = {"agent_id": agent_id, "nickname": nickname}
        return _agent_tool_ok(payload)

    def send_input(self, arguments: dict[str, Any]) -> Any:
        target = str(arguments.get("target") or "")
        prompt = _agent_prompt(arguments)
        if not prompt:
            return _agent_tool_error("send_input requires message or text items")
        record = self._agents.get(target)
        if record is None:
            return _agent_tool_error(f"agent not found: {target}", {"status": "not_found"})
        submission_id = f"sub-{int(time.time() * 1000)}"
        start_prompt: str | None = None
        interrupt_running = False
        with record.condition:
            if record.shutdown:
                record.shutdown = False
            if record.running:
                if bool(arguments.get("interrupt")):
                    record.pending.appendleft(prompt)
                    interrupt_running = True
                else:
                    record.pending.append(prompt)
            else:
                start_prompt = prompt
            record.condition.notify_all()
        if interrupt_running:
            record.session.interrupt()
        if start_prompt is not None:
            self._start_agent(record, start_prompt)
        return _agent_tool_ok({"submission_id": submission_id})

    def resume_agent(self, arguments: dict[str, Any]) -> Any:
        agent_id = str(arguments.get("id") or "")
        record = self._agents.get(agent_id)
        if record is None:
            return _agent_tool_ok({"status": "not_found"})
        with record.condition:
            record.shutdown = False
            status = _agent_status(record)
        return _agent_tool_ok({"status": status})

    def wait_agent(self, arguments: dict[str, Any]) -> Any:
        targets = arguments.get("targets")
        if not isinstance(targets, list) or not targets:
            return _agent_tool_error("wait_agent requires non-empty targets")
        try:
            requested_timeout_ms = int(arguments.get("timeout_ms", _DEFAULT_AGENT_WAIT_TIMEOUT_MS))
        except (TypeError, ValueError):
            requested_timeout_ms = _DEFAULT_AGENT_WAIT_TIMEOUT_MS
        if requested_timeout_ms <= 0:
            return _agent_tool_error("timeout_ms must be greater than zero")
        timeout_ms = min(max(requested_timeout_ms, _MIN_AGENT_WAIT_TIMEOUT_MS), _MAX_AGENT_WAIT_TIMEOUT_MS)
        target_ids = [str(item) for item in targets]
        initial = self._final_agent_statuses(target_ids)
        if initial:
            return _agent_tool_ok({"status": initial, "timed_out": False})
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            statuses = self._final_agent_statuses(target_ids)
            if statuses:
                return _agent_tool_ok({"status": statuses, "timed_out": False})
        return _agent_tool_ok({"status": {}, "timed_out": True})

    def close_agent(self, arguments: dict[str, Any]) -> Any:
        target = str(arguments.get("target") or "")
        record = self._agents.get(target)
        if record is None:
            return _agent_tool_ok({"previous_status": "not_found"})
        interrupt_running = False
        with record.condition:
            previous = _agent_status(record)
            record.pending.clear()
            record.shutdown = True
            interrupt_running = record.running
            record.condition.notify_all()
        if interrupt_running:
            record.session.interrupt()
        return _agent_tool_ok({"previous_status": previous})

    def request_interrupt(self) -> None:
        with self._lock:
            records = list(self._agents.values())
        for record in records:
            with record.condition:
                if record.running:
                    record.interrupted = True
                record.condition.notify_all()
            record.session.interrupt()

    def interrupt_all(self) -> None:
        with self._lock:
            records = list(self._agents.values())
        for record in records:
            with record.condition:
                record.pending.clear()
                record.shutdown = True
                if record.running:
                    record.interrupted = True
                record.condition.notify_all()
            record.session.interrupt()

    def drain_notifications(self) -> list[dict[str, Any]]:
        with self._lock:
            notifications = list(self._pending_notifications)
            self._pending_notifications.clear()
        return notifications

    def _final_agent_statuses(self, targets: list[str]) -> dict[str, Any]:
        statuses: dict[str, Any] = {}
        for target in targets:
            record = self._agents.get(target)
            if record is None:
                statuses[target] = "not_found"
                continue
            with record.condition:
                status = _agent_status(record)
            if _agent_status_is_final(status):
                statuses[target] = status
        return statuses

    def _new_child_session(self, arguments: dict[str, Any]) -> VolleySession:
        model = arguments.get("model") if isinstance(arguments.get("model"), str) else self.parent.config.model
        reasoning_effort = (
            arguments.get("reasoning_effort")
            if isinstance(arguments.get("reasoning_effort"), str)
            else self.parent.config.model_reasoning_effort
        )
        child_depth = self.parent.config.agent_depth + 1
        child_config = replace(
            self.parent.config,
            model=model,
            model_reasoning_effort=reasoning_effort,
            agent_depth=child_depth,
            include_multi_agent_tools=child_depth < self.parent.config.max_agent_depth,
        )
        child = VolleySession(child_config, model_client=self.parent.model_client)
        if bool(arguments.get("fork_context")):
            child.state.history = _forked_agent_history(self.parent.state.history)
            child.state.forked_from_id = self.parent.state.thread_id
            child._initial_context_recorded = _history_has_initial_context(child.state.history)
        return child

    def _start_agent(self, record: _LocalAgentRecord, prompt: str) -> None:
        with record.condition:
            record.running = True
            record.interrupted = False
            record.error = None
        thread = threading.Thread(target=self._run_agent_loop, args=(record, prompt), daemon=True)
        record.thread = thread
        thread.start()

    def _run_agent_loop(self, record: _LocalAgentRecord, prompt: str) -> None:
        next_prompt: str | None = prompt
        while next_prompt is not None:
            try:
                result = record.session.run(next_prompt)
                final_message = result.final_message
                error = None
                interrupted = False
            except TurnInterrupted:
                final_message = None
                error = None
                interrupted = True
            except Exception as exc:  # pragma: no cover - exercised through model/tool failures.
                final_message = None
                error = f"{type(exc).__name__}: {exc}"
                interrupted = False
            with record.condition:
                record.final_message = final_message
                record.error = error
                record.interrupted = interrupted
                if record.pending and not record.shutdown:
                    next_prompt = record.pending.popleft()
                    record.running = True
                    record.interrupted = False
                    record.error = None
                    continue
                record.running = False
                record.condition.notify_all()
                should_notify = not record.shutdown and _agent_status_is_final(_agent_status(record))
            if should_notify:
                self._notify_parent_agent_completed(record)
            return

    def _notify_parent_agent_completed(self, record: _LocalAgentRecord) -> None:
        with record.condition:
            status = _agent_status(record)
        item = _subagent_notification_message(record.reference, status)
        with self._lock:
            self._pending_notifications.append(item)


def _agent_tool_ok(payload: dict[str, Any]) -> Any:
    from .tools import ToolResult

    return ToolResult(True, json.dumps(payload), payload)


def _agent_tool_error(message: str, metadata: dict[str, Any] | None = None) -> Any:
    from .tools import ToolResult

    return ToolResult(False, message, metadata or {})


def _agent_prompt(arguments: dict[str, Any]) -> str:
    message = arguments.get("message")
    if isinstance(message, str) and message.strip():
        return message
    items = arguments.get("items")
    if not isinstance(items, list):
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            chunks.append(item["text"])
        elif isinstance(item.get("path"), str):
            name = item.get("name")
            prefix = f"{name}: " if isinstance(name, str) and name else ""
            chunks.append(f"{prefix}{item['path']}")
        elif isinstance(item.get("name"), str):
            chunks.append(item["name"])
    return "\n".join(chunk for chunk in chunks if chunk.strip())


def _agent_nickname(arguments: dict[str, Any]) -> str | None:
    agent_type = arguments.get("agent_type")
    return agent_type if isinstance(agent_type, str) and agent_type else None


def _agent_reference(arguments: dict[str, Any], agent_id: str) -> str:
    task_name = arguments.get("task_name")
    if isinstance(task_name, str) and task_name.strip():
        return task_name.strip()
    return agent_id


def _agent_status(record: _LocalAgentRecord) -> Any:
    if record.shutdown:
        return "shutdown"
    if record.running:
        return "running"
    if record.interrupted:
        return "interrupted"
    if record.error is not None:
        return {"errored": record.error}
    return {"completed": record.final_message}


def _agent_status_is_final(status: Any) -> bool:
    if isinstance(status, dict):
        return "completed" in status or "errored" in status
    return status in {"interrupted", "shutdown", "not_found"}


def _forked_agent_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    forked: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        if role in {"system", "developer", "user", "assistant"}:
            forked.append(deepcopy(item))
    return forked


def _inside_git_repo(cwd: Any) -> bool:
    path = cwd
    while True:
        if (path / ".git").exists():
            return True
        if path.parent == path:
            return False
        path = path.parent


def _config_with_memory_state_store(config: VolleyConfig) -> VolleyConfig:
    if not _should_run_memory_startup(config) or config.memory_state_store is not None:
        return config
    try:
        state_store = MemoryStateStore.open_volley_home(config.resolved_volley_home())
    except Exception:
        return config
    return replace(config, memory_state_store=state_store)


def _should_run_memory_startup(config: VolleyConfig) -> bool:
    return (
        config.memory_tool_enabled
        and not config.ephemeral
        and config.agent_depth == 0
    )


def _memory_rate_limit_snapshot(config: VolleyConfig) -> Any | None:
    provider = config.memory_rate_limit_provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        return None


def _history_has_initial_context(history: list[dict[str, Any]]) -> bool:
    saw_developer = False
    saw_contextual_user = False
    for item in history:
        if item.get("type") != "message":
            continue
        if item.get("role") == "developer":
            if _is_recent_activity_marker_message(item):
                continue
            saw_developer = True
            continue
        if item.get("role") == "user":
            texts = [
                part.get("text")
                for part in item.get("content", [])
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if any(text.startswith("# AGENTS.md instructions for ") for text in texts) or any(
                text.strip().startswith("<environment_context>") for text in texts
            ):
                saw_contextual_user = True
    return saw_developer or saw_contextual_user


def _is_recent_activity_marker_message(item: dict[str, Any]) -> bool:
    text = _message_text(item).strip()
    return (text.startswith("<recent_activity>") and text.endswith("</recent_activity>")) or text == "<recent_activity_end />"


def _user_message(
    text: str,
    *,
    image_paths: tuple[Path | str, ...] = (),
    cwd: Path | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for index, raw_path in enumerate(image_paths, start=1):
        path = _resolve_input_image_path(raw_path, cwd)
        try:
            processed = _load_image_for_prompt(path, original=False)
            content.extend(
                [
                    {"type": "input_text", "text": f"<image name=[Image #{index}]>"},
                    {
                        "type": "input_image",
                        "image_url": processed["image_url"],
                        "detail": "high",
                    },
                    {"type": "input_text", "text": "</image>"},
                ]
            )
        except Exception as exc:
            content.append(
                {
                    "type": "input_text",
                    "text": f"Image located at `{path}` could not be loaded: {exc}",
                }
            )
    content.append({"type": "input_text", "text": text})
    return {
        "type": "message",
        "role": "user",
        "content": content,
    }


def _turn_aborted_marker() -> dict[str, Any]:
    return _user_message(f"<turn_aborted>\n{INTERRUPTED_TURN_GUIDANCE}\n</turn_aborted>")


def _hook_context_message(text: str) -> dict[str, Any]:
    return _user_message(f"<hook_context>\n{text}\n</hook_context>")


def _subagent_notification_message(agent_reference: str, status: Any) -> dict[str, Any]:
    payload = json.dumps(
        {"agent_path": agent_reference, "status": status},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _user_message(f"<subagent_notification>\n{payload}\n</subagent_notification>")


def _message_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in item.get("content", []):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _normalize_hook_outcome(decision: Any) -> _HookOutcome:
    if decision is None:
        return _HookOutcome()
    if not isinstance(decision, dict):
        if decision is True:
            return _HookOutcome()
        if decision is False:
            return _HookOutcome(should_stop=True, stop_reason="hook returned false")
        return _HookOutcome()
    contexts = decision.get("additional_contexts", ())
    if isinstance(contexts, str):
        additional_contexts = (contexts,)
    elif isinstance(contexts, list):
        additional_contexts = tuple(str(item) for item in contexts)
    else:
        additional_contexts = ()
    return _HookOutcome(
        should_stop=bool(decision.get("should_stop", False)),
        should_block=bool(decision.get("should_block", False)),
        block_reason=_optional_string(decision.get("block_reason")),
        stop_reason=_optional_string(decision.get("stop_reason")),
        feedback_message=_optional_string(decision.get("feedback_message")),
        updated_input=decision.get("updated_input"),
        additional_contexts=additional_contexts,
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return repr(value)


def _resolve_input_image_path(path: Path | str, cwd: Path | None) -> Path:
    image_path = Path(path).expanduser()
    if image_path.is_absolute():
        return image_path
    return (cwd or Path.cwd()).joinpath(image_path).resolve()


def _model_event_scope(*, compact: bool, trigger: str | None) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    if compact:
        scope["compact"] = True
    if trigger is not None:
        scope["trigger"] = trigger
    return scope


def _is_retryable_model_stream_error(error: Any) -> bool:
    status = _model_error_status(error)
    if status is not None:
        if status in {408, 409, 429, 500, 502, 503, 504}:
            return not _model_error_text_matches(error, _NON_RETRYABLE_MODEL_ERROR_PARTS)
        if 400 <= status < 500:
            return False

    if isinstance(error, dict):
        if error.get("retryable") is True:
            return True
        if error.get("retryable") is False:
            return False
    text = _model_error_text(error)
    if not text:
        return False
    lowered = text.lower()
    if any(part in lowered for part in _NON_RETRYABLE_MODEL_ERROR_PARTS):
        return False
    return any(part in lowered for part in _RETRYABLE_MODEL_ERROR_PARTS)


def _model_stream_retry_delay_seconds(config: VolleyConfig, retry: int, error: Any) -> float:
    retry_after = _model_error_retry_after_seconds(error)
    if retry_after is not None:
        return max(0.0, retry_after)
    base = config.resolved_model_stream_retry_base_delay_ms() / 1000.0
    return base * (2 ** max(0, retry - 1))


_RETRYABLE_MODEL_ERROR_PARTS = (
    "stream closed",
    "stream disconnected",
    "stream dropped",
    "connection",
    "connect",
    "timeout",
    "timed out",
    "temporarily",
    "transient",
    "eof",
    "server error",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "rate limit",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
)


_NON_RETRYABLE_MODEL_ERROR_PARTS = (
    "invalid_request_error",
    "invalid request",
    "context length",
    "context_window",
    "context window",
    "quota",
    "billing",
    "insufficient_quota",
    "usage limit",
    "unsupported",
    "invalid image",
    "authentication",
    "unauthorized",
    "permission denied",
)


def _model_error_status(error: Any) -> int | None:
    if isinstance(error, dict):
        for key in ("status", "status_code", "http_status", "http_status_code"):
            value = error.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    for attr in ("status_code", "status", "http_status", "http_status_code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _model_error_retry_after_seconds(error: Any) -> float | None:
    if isinstance(error, dict):
        for key in ("retry_after", "retry_after_seconds"):
            parsed = _float_or_none(error.get(key))
            if parsed is not None:
                return parsed
    for attr in ("retry_after", "retry_after_seconds"):
        parsed = _float_or_none(getattr(error, attr, None))
        if parsed is not None:
            return parsed
    text = _model_error_text(error)
    match = re.search(r"(?:try again|retry)[^0-9]{0,40}([0-9]+(?:\.[0-9]+)?)\s*(ms|s|seconds?|milliseconds?)\b", text, re.I)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit == "ms" or unit.startswith("millisecond"):
            return value / 1000.0
        return value
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _model_error_text_matches(error: Any, parts: tuple[str, ...]) -> bool:
    lowered = _model_error_text(error).lower()
    return any(part in lowered for part in parts)


def _model_error_text(error: Any) -> str:
    if isinstance(error, dict):
        pieces: list[str] = []
        for key in ("message", "type", "code", "error"):
            value = error.get(key)
            if isinstance(value, str):
                pieces.append(value)
            elif isinstance(value, dict):
                pieces.append(_model_error_text(value))
        return " ".join(piece for piece in pieces if piece)
    return str(error)


def _item_stream_payload(event: ModelStreamEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    item = event.payload.get("item")
    if isinstance(item, dict):
        payload["item"] = item
    for key in ("item_id", "output_index", "raw_type"):
        if key in event.payload:
            payload[key] = event.payload[key]
    return payload


def _delta_stream_payload(event: ModelStreamEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("item_id", "output_index", "content_index", "summary_index", "delta", "raw_type"):
        if key in event.payload:
            payload[key] = event.payload[key]
    return payload


def _completed_stream_payload(event: ModelStreamEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("item_id", "output_index", "raw_type"):
        if key in event.payload:
            payload[key] = event.payload[key]
    return payload


def _model_stream_payload(event: ModelStreamEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("usage", "raw_type"):
        if key in event.payload:
            payload[key] = event.payload[key]
    return payload


def _update_stream_aggregate(
    active_aggregates: dict[str, dict[str, str]],
    event: ModelStreamEvent,
) -> dict[str, str]:
    item_id = str(event.payload.get("item_id") or "")
    if not item_id:
        return {}
    aggregate = active_aggregates.setdefault(item_id, {})
    delta = event.payload.get("delta")
    if not isinstance(delta, str):
        return dict(aggregate)
    raw_type = str(event.payload.get("raw_type") or "")
    field = _aggregate_field_for_delta(raw_type)
    aggregate[field] = aggregate.get(field, "") + delta
    return dict(aggregate)


def _aggregate_field_for_delta(raw_type: str) -> str:
    if "function_call_arguments" in raw_type or "tool_call_arguments" in raw_type:
        return "arguments"
    if "custom_tool_call_input" in raw_type:
        return "input"
    if "reasoning" in raw_type:
        return "reasoning"
    return "text"


def _normalize_response_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    if "type" not in normalized and "role" in normalized:
        normalized["type"] = "message"
    return normalized


def _assistant_text(item: dict[str, Any]) -> str:
    if item.get("type") != "message" or item.get("role") != "assistant":
        return ""
    chunks = []
    for part in item.get("content", []):
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _tool_call_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type == "function_call":
        return {
            "kind": "function",
            "name": str(item.get("name")),
            "call_id": str(item.get("call_id") or item.get("id")),
            "arguments": _parse_arguments(item.get("arguments", {})),
        }
    if item_type == "custom_tool_call":
        return {
            "kind": "custom",
            "name": str(item.get("name")),
            "call_id": str(item.get("call_id") or item.get("id")),
            "arguments": item.get("input", ""),
        }
    if item_type == "local_shell_call":
        action = item.get("action", {})
        if isinstance(action, dict) and action.get("type") == "exec":
            return {
                "kind": "function",
                "name": "exec_command",
                "call_id": str(item.get("call_id") or item.get("id")),
                "arguments": {
                    "cmd": action.get("command", ""),
                    "workdir": action.get("working_directory"),
                    "timeout_ms": action.get("timeout_ms"),
                },
            }
    return None


def _tool_spec_name(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    if isinstance(name, str) and name:
        return name
    return str(tool.get("type"))


def _provider_supports_remote_compaction(config: VolleyConfig) -> bool:
    provider = config.model_provider_id.lower()
    return provider == "openai" or config.provider_is_azure_responses_endpoint


def _parse_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _tool_output_item(call: dict[str, Any], result: Any) -> dict[str, Any]:
    output = result.response_output if getattr(result, "response_output", None) is not None else result.output
    if call["kind"] == "custom":
        return {
            "type": "custom_tool_call_output",
            "call_id": call["call_id"],
            "output": output,
        }
    return {
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": output,
    }
