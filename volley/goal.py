from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .prompts import read_asset
from .types import VolleyConfig


GoalStatus = Literal[
    "active",
    "paused",
    "blocked",
    "usage_limited",
    "budget_limited",
    "complete",
]

GOAL_STATUS_WIRE = {
    "active": "active",
    "paused": "paused",
    "blocked": "blocked",
    "usage_limited": "usageLimited",
    "budget_limited": "budgetLimited",
    "complete": "complete",
}
GOAL_STATUS_FROM_WIRE = {value: key for key, value in GOAL_STATUS_WIRE.items()}
GOAL_STATUS_FROM_WIRE.update({key: key for key in GOAL_STATUS_WIRE})
MAX_THREAD_GOAL_OBJECTIVE_CHARS = 4_000


class _Unset:
    pass


_UNSET = _Unset()


@dataclass(frozen=True)
class GoalRuntimeEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoalToolOperationResult:
    ok: bool
    output: str
    metadata: dict[str, Any]
    response_output: Any = None
    events: tuple[GoalRuntimeEvent, ...] = ()


@dataclass(frozen=True)
class GoalAccountingResult:
    events: tuple[GoalRuntimeEvent, ...] = ()
    steering_items: tuple[dict[str, Any], ...] = ()


@dataclass
class ThreadGoal:
    thread_id: str
    goal_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int

    def to_record(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "status": self.status,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "time_used_seconds": self.time_used_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_protocol(self) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "objective": self.objective,
            "status": GOAL_STATUS_WIRE[self.status],
            **({"tokenBudget": self.token_budget} if self.token_budget is not None else {}),
            "tokensUsed": self.tokens_used,
            "timeUsedSeconds": self.time_used_seconds,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


class GoalStore:
    """Small persisted thread-goal store.

    The Rust implementation stores goals in its local state database. This
    Python core keeps the same thread-keyed protocol state in a volley-home JSON
    file so the runtime stays self-contained.
    """

    def __init__(self, volley_home: Path | str):
        self.path = Path(volley_home).expanduser().resolve() / "thread_goals.json"
        self._lock = threading.RLock()

    def get_thread_goal(self, thread_id: str) -> ThreadGoal | None:
        with self._lock:
            record = self._read().get(thread_id)
        return _goal_from_record(record) if isinstance(record, dict) else None

    def replace_thread_goal(
        self,
        thread_id: str,
        objective: str,
        status: GoalStatus = "active",
        token_budget: int | None = None,
    ) -> ThreadGoal:
        with self._lock:
            goals = self._read()
            now = _now_seconds()
            goal = ThreadGoal(
                thread_id=thread_id,
                goal_id=str(uuid.uuid4()),
                objective=objective,
                status=_status_after_budget_limit(status, 0, token_budget),
                token_budget=token_budget,
                tokens_used=0,
                time_used_seconds=0,
                created_at=now,
                updated_at=now,
            )
            goals[thread_id] = goal.to_record()
            self._write(goals)
            return goal

    def insert_thread_goal(
        self,
        thread_id: str,
        objective: str,
        status: GoalStatus = "active",
        token_budget: int | None = None,
    ) -> ThreadGoal | None:
        with self._lock:
            goals = self._read()
            if thread_id in goals:
                return None
            now = _now_seconds()
            goal = ThreadGoal(
                thread_id=thread_id,
                goal_id=str(uuid.uuid4()),
                objective=objective,
                status=_status_after_budget_limit(status, 0, token_budget),
                token_budget=token_budget,
                tokens_used=0,
                time_used_seconds=0,
                created_at=now,
                updated_at=now,
            )
            goals[thread_id] = goal.to_record()
            self._write(goals)
            return goal

    def update_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: GoalStatus | None = None,
        token_budget: int | None | object = _UNSET,
        expected_goal_id: str | None = None,
    ) -> ThreadGoal | None:
        with self._lock:
            goals = self._read()
            goal = _goal_from_record(goals.get(thread_id))
            if goal is None:
                return None
            if expected_goal_id is not None and goal.goal_id != expected_goal_id:
                return None
            if objective is not None:
                goal.objective = objective
            if token_budget is not _UNSET:
                goal.token_budget = token_budget  # type: ignore[assignment]
            if status is not None:
                goal.status = _updated_status(goal, status)
            elif token_budget is not _UNSET and goal.status == "active":
                goal.status = _status_after_budget_limit(goal.status, goal.tokens_used, goal.token_budget)
            goal.updated_at = _now_seconds()
            goals[thread_id] = goal.to_record()
            self._write(goals)
            return goal

    def pause_active_thread_goal(self, thread_id: str) -> ThreadGoal | None:
        return self._update_active_thread_goal_status(thread_id, "paused")

    def usage_limit_active_thread_goal(self, thread_id: str) -> ThreadGoal | None:
        return self._update_active_thread_goal_status(thread_id, "usage_limited")

    def delete_thread_goal(self, thread_id: str) -> bool:
        with self._lock:
            goals = self._read()
            existed = thread_id in goals
            goals.pop(thread_id, None)
            if existed:
                self._write(goals)
            return existed

    def account_thread_goal_usage(
        self,
        thread_id: str,
        *,
        time_delta_seconds: int,
        token_delta: int,
        mode: str,
        expected_goal_id: str | None = None,
    ) -> tuple[bool, ThreadGoal | None]:
        time_delta_seconds = max(0, int(time_delta_seconds))
        token_delta = max(0, int(token_delta))
        if time_delta_seconds == 0 and token_delta == 0:
            return False, self.get_thread_goal(thread_id)
        with self._lock:
            goals = self._read()
            goal = _goal_from_record(goals.get(thread_id))
            if goal is None:
                return False, None
            if expected_goal_id is not None and goal.goal_id != expected_goal_id:
                return False, goal
            if not _status_allowed_for_accounting(goal.status, mode):
                return False, goal
            goal.time_used_seconds += time_delta_seconds
            goal.tokens_used += token_delta
            if _status_allowed_for_budget_limit(goal.status, mode):
                goal.status = _status_after_budget_limit(goal.status, goal.tokens_used, goal.token_budget)
            goal.updated_at = _now_seconds()
            goals[thread_id] = goal.to_record()
            self._write(goals)
            return True, goal

    def _update_active_thread_goal_status(self, thread_id: str, status: GoalStatus) -> ThreadGoal | None:
        with self._lock:
            goals = self._read()
            goal = _goal_from_record(goals.get(thread_id))
            if goal is None:
                return None
            if goal.status != "active" and not (status == "usage_limited" and goal.status == "budget_limited"):
                return None
            goal.status = status
            goal.updated_at = _now_seconds()
            goals[thread_id] = goal.to_record()
            self._write(goals)
            return goal

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            tmp_path = Path(handle.name)
        tmp_path.replace(self.path)


class GoalRuntime:
    def __init__(self, config: VolleyConfig, state: Any):
        self.config = config
        self.state = state
        self.store = GoalStore(config.resolved_volley_home())
        self._lock = threading.RLock()
        self._current_turn_id: str | None = None
        self._turn_account_tokens = True
        self._active_goal_id: str | None = None
        self._wall_active_goal_id: str | None = None
        self._wall_last_accounted_at = time.monotonic()
        self._last_accounted_goal_tokens = 0
        self._cumulative_goal_tokens = 0
        self._budget_limit_reported_goal_id: str | None = None

    def tools_available(self) -> bool:
        return bool(self.config.goals_enabled and not self.config.ephemeral)

    def get_goal(self) -> ThreadGoal | None:
        if not self.tools_available():
            return None
        return self.store.get_thread_goal(self.state.thread_id)

    def on_turn_start(self) -> None:
        with self._lock:
            self._current_turn_id = self.state.turn_id
            self._turn_account_tokens = self.config.collaboration_mode != "Plan"
            self._active_goal_id = None
            self._last_accounted_goal_tokens = self._cumulative_goal_tokens
            if not self.tools_available() or not self._turn_account_tokens:
                self._clear_wall_clock()
                return
            goal = self.store.get_thread_goal(self.state.thread_id)
            if goal is not None and goal.status in {"active", "budget_limited"}:
                self._mark_active_goal(goal.goal_id, reset_token_baseline=False)
            else:
                self._clear_wall_clock()

    def on_turn_finished(self, *, completed: bool) -> GoalAccountingResult:
        result = GoalAccountingResult()
        if completed:
            result = self.account_progress(mode="active_only", budget_limit_steering=False)
        with self._lock:
            self._current_turn_id = None
            self._active_goal_id = None
        return result

    def on_turn_aborted(self) -> GoalAccountingResult:
        result = self.account_progress(mode="active_only", budget_limit_steering=False)
        with self._lock:
            self._current_turn_id = None
            self._active_goal_id = None
        return result

    def on_tool_finished(self, tool_name: str, *, handler_executed: bool = True) -> GoalAccountingResult:
        if tool_name == "update_goal" or not handler_executed:
            return GoalAccountingResult()
        return self.account_progress(mode="active_only", budget_limit_steering=True)

    def record_token_usage(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        with self._lock:
            self._cumulative_goal_tokens += max(0, goal_token_delta_for_usage(usage))

    def continuation_item_if_active(self) -> dict[str, Any] | None:
        if not self.tools_available() or self.config.collaboration_mode == "Plan":
            return None
        goal = self.store.get_thread_goal(self.state.thread_id)
        if goal is None or goal.status != "active":
            return None
        return goal_context_input_item(continuation_prompt(goal))

    def create_goal(self, arguments: Any) -> GoalToolOperationResult:
        if not self.tools_available():
            return _goal_tool_error("goals feature is disabled or this thread is ephemeral", "create_goal")
        try:
            args = _expect_object(arguments)
            objective = _validate_objective(str(args.get("objective") or "").strip())
            token_budget = _validate_goal_budget(args.get("token_budget"))
        except ValueError as exc:
            return _goal_tool_error(str(exc), "create_goal")
        self.account_wall_clock_usage(mode="active_only")
        goal = self.store.insert_thread_goal(self.state.thread_id, objective, "active", token_budget)
        if goal is None:
            return _goal_tool_error(
                "cannot create a new goal because this thread already has a goal; use update_goal only when the existing goal is complete",
                "create_goal",
            )
        with self._lock:
            self._budget_limit_reported_goal_id = None
            self._mark_active_goal(goal.goal_id, reset_token_baseline=True)
        event = _goal_updated_event(goal, self.state.turn_id)
        return _goal_tool_success(goal, report_completion_budget=False, tool="create_goal", events=(event,))

    def get_goal_tool(self, arguments: Any) -> GoalToolOperationResult:
        if not self.tools_available():
            return _goal_tool_error("goals feature is disabled or this thread is ephemeral", "get_goal")
        if isinstance(arguments, str) and arguments.strip():
            try:
                json.loads(arguments)
            except json.JSONDecodeError as exc:
                return _goal_tool_error(str(exc), "get_goal")
        goal = self.store.get_thread_goal(self.state.thread_id)
        return _goal_tool_success(goal, report_completion_budget=False, tool="get_goal")

    def update_goal(self, arguments: Any) -> GoalToolOperationResult:
        if not self.tools_available():
            return _goal_tool_error("goals feature is disabled or this thread is ephemeral", "update_goal")
        try:
            args = _expect_object(arguments)
            raw_status = str(args.get("status") or "")
            status = _status_from_wire(raw_status)
            if status not in {"complete", "blocked"}:
                raise ValueError(
                    "update_goal can only mark the existing goal complete or blocked; pause, resume, budget-limited, and usage-limited status changes are controlled by the user or system"
                )
        except ValueError as exc:
            return _goal_tool_error(str(exc), "update_goal")
        account = self.account_progress(mode="active_or_complete" if status == "complete" else "active_or_stopped", budget_limit_steering=False)
        goal = self.store.update_thread_goal(self.state.thread_id, status=status)
        if goal is None:
            return _goal_tool_error("cannot update goal because this thread has no goal", "update_goal", events=account.events)
        with self._lock:
            self._active_goal_id = None
            self._clear_wall_clock()
        event = _goal_updated_event(goal, self.state.turn_id)
        return _goal_tool_success(
            goal,
            report_completion_budget=status == "complete",
            tool="update_goal",
            events=(*account.events, event),
        )

    def set_goal_external(
        self,
        *,
        objective: str | None = None,
        status: GoalStatus | None = None,
        token_budget: int | None | object = _UNSET,
        replace_existing: bool = False,
    ) -> tuple[ThreadGoal, tuple[GoalRuntimeEvent, ...]]:
        if not self.tools_available():
            raise RuntimeError("thread goals require a persisted thread; this thread is ephemeral")
        objective = _validate_objective(objective.strip()) if isinstance(objective, str) else None
        if token_budget is not _UNSET:
            token_budget = _validate_goal_budget(token_budget)
        self.account_progress(mode="active_only", budget_limit_steering=False)
        if replace_existing and objective is not None:
            goal = self.store.replace_thread_goal(
                self.state.thread_id,
                objective,
                status or "active",
                token_budget if token_budget is not _UNSET else None,
            )
        else:
            existing = self.store.get_thread_goal(self.state.thread_id)
            if existing is None:
                if objective is None:
                    raise RuntimeError(f"cannot update goal because thread {self.state.thread_id} has no goal")
                goal = self.store.replace_thread_goal(
                    self.state.thread_id,
                    objective,
                    status or "active",
                    token_budget if token_budget is not _UNSET else None,
                )
            else:
                goal = self.store.update_thread_goal(
                    self.state.thread_id,
                    objective=objective,
                    status=status,
                    token_budget=token_budget,
                    expected_goal_id=existing.goal_id,
                )
                if goal is None:
                    raise RuntimeError("cannot update goal because this thread has no goal")
        with self._lock:
            self._budget_limit_reported_goal_id = None
            if goal.status == "active":
                self._mark_active_goal(goal.goal_id, reset_token_baseline=True)
            else:
                self._active_goal_id = None
                self._clear_wall_clock()
        return goal, (_goal_updated_event(goal, self.state.turn_id),)

    def clear_goal_external(self) -> tuple[bool, tuple[GoalRuntimeEvent, ...]]:
        if not self.tools_available():
            raise RuntimeError("thread goals require a persisted thread; this thread is ephemeral")
        self.account_progress(mode="active_only", budget_limit_steering=False)
        cleared = self.store.delete_thread_goal(self.state.thread_id)
        with self._lock:
            self._budget_limit_reported_goal_id = None
            self._active_goal_id = None
            self._clear_wall_clock()
        return cleared, (GoalRuntimeEvent("thread.goal.cleared", {"thread_id": self.state.thread_id}),) if cleared else ()

    def account_wall_clock_usage(self, *, mode: str) -> GoalAccountingResult:
        return self._account_progress(mode=mode, budget_limit_steering=False, tokens_only=False)

    def account_progress(self, *, mode: str, budget_limit_steering: bool) -> GoalAccountingResult:
        return self._account_progress(mode=mode, budget_limit_steering=budget_limit_steering, tokens_only=True)

    def _account_progress(self, *, mode: str, budget_limit_steering: bool, tokens_only: bool) -> GoalAccountingResult:
        if not self.tools_available() or self.config.collaboration_mode == "Plan":
            return GoalAccountingResult()
        with self._lock:
            if not self._turn_account_tokens:
                return GoalAccountingResult()
            expected_goal_id = self._active_goal_id or self._wall_active_goal_id
            if expected_goal_id is None:
                return GoalAccountingResult()
            token_delta = 0 if not tokens_only else self._cumulative_goal_tokens - self._last_accounted_goal_tokens
            time_delta = self._wall_time_delta(expected_goal_id)
            if token_delta <= 0 and time_delta <= 0:
                return GoalAccountingResult()
        updated, goal = self.store.account_thread_goal_usage(
            self.state.thread_id,
            time_delta_seconds=time_delta,
            token_delta=token_delta,
            mode=mode,
            expected_goal_id=expected_goal_id,
        )
        if not updated or goal is None:
            with self._lock:
                self._wall_last_accounted_at = time.monotonic()
                if goal is None or goal.status not in {"active", "budget_limited"}:
                    self._active_goal_id = None
                    self._clear_wall_clock()
            return GoalAccountingResult()

        clear_active = goal.status in {"paused", "blocked", "usage_limited", "complete"} or (
            goal.status == "budget_limited" and not budget_limit_steering
        )
        with self._lock:
            self._last_accounted_goal_tokens = self._cumulative_goal_tokens
            self._wall_last_accounted_at = time.monotonic()
            if clear_active:
                self._active_goal_id = None
                self._clear_wall_clock()
            if goal.status != "budget_limited":
                self._budget_limit_reported_goal_id = None

        event = _goal_updated_event(goal, self.state.turn_id)
        should_steer = (
            budget_limit_steering
            and goal.status == "budget_limited"
            and self._budget_limit_reported_goal_id != goal.goal_id
        )
        if should_steer:
            with self._lock:
                self._budget_limit_reported_goal_id = goal.goal_id
            return GoalAccountingResult(events=(event,), steering_items=(goal_context_input_item(budget_limit_prompt(goal)),))
        return GoalAccountingResult(events=(event,))

    def _mark_active_goal(self, goal_id: str, *, reset_token_baseline: bool) -> None:
        if self._budget_limit_reported_goal_id != goal_id:
            self._budget_limit_reported_goal_id = None
        self._active_goal_id = goal_id
        if self._wall_active_goal_id != goal_id:
            self._wall_active_goal_id = goal_id
            self._wall_last_accounted_at = time.monotonic()
        if reset_token_baseline:
            self._last_accounted_goal_tokens = self._cumulative_goal_tokens

    def _clear_wall_clock(self) -> None:
        self._wall_active_goal_id = None
        self._wall_last_accounted_at = time.monotonic()

    def _wall_time_delta(self, expected_goal_id: str) -> int:
        if self._wall_active_goal_id != expected_goal_id:
            return 0
        return max(0, int(time.monotonic() - self._wall_last_accounted_at))


def get_goal_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "get_goal",
        "description": "Get the current goal for this thread, including status, budgets, token and elapsed-time usage, and remaining token budget.",
        "strict": False,
        "parameters": _object_schema({}, []),
    }


def create_goal_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "create_goal",
        "description": (
            "Create a goal only when explicitly requested by the user or system/developer instructions; do not infer goals from ordinary tasks.\n"
            "Set token_budget only when an explicit token budget is requested. Fails if a goal exists; use update_goal only for status."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "objective": {
                    "type": "string",
                    "description": "Required. The concrete objective to start pursuing. This starts a new active goal only when no goal is currently defined; if a goal already exists, this tool fails.",
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Optional positive token budget for the new active goal.",
                },
            },
            ["objective"],
        ),
    }


def update_goal_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "update_goal",
        "description": (
            "Update the existing goal.\n"
            "Use this tool only to mark the goal achieved or genuinely blocked.\n"
            "Set status to `complete` only when the objective has actually been achieved and no required work remains.\n"
            "Set status to `blocked` only when the same blocking condition has repeated for at least three consecutive goal turns, counting the original/user-triggered turn and any automatic continuations, and the agent cannot make meaningful progress without user input or an external-state change.\n"
            "If the user resumes a goal that was previously marked `blocked`, treat the resumed run as a fresh blocked audit. If the same blocking condition then repeats for at least three consecutive resumed goal turns, set status to `blocked` again.\n"
            "Once the blocked threshold is satisfied, do not keep reporting that you are still blocked while leaving the goal active; set status to `blocked`.\n"
            "Do not use `blocked` merely because the work is hard, slow, uncertain, incomplete, or would benefit from clarification.\n"
            "Do not mark a goal complete merely because its budget is nearly exhausted or because you are stopping work.\n"
            "You cannot use this tool to pause, resume, budget-limit, or usage-limit a goal; those status changes are controlled by the user or system.\n"
            "When marking a budgeted goal achieved with status `complete`, report the final token usage from the tool result to the user."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                    "description": "Required. Set to `complete` only when the objective is achieved and no required work remains. Set to `blocked` only after the same blocking condition has recurred for at least three consecutive goal turns and the agent is at an impasse. After a previously blocked goal is resumed, the resumed run starts a fresh blocked audit.",
                }
            },
            ["status"],
        ),
    }


def continuation_prompt(goal: ThreadGoal) -> str:
    return _render_goal_template(
        "prompts/goals/continuation.md",
        goal,
        include_time=False,
    )


def budget_limit_prompt(goal: ThreadGoal) -> str:
    return _render_goal_template(
        "prompts/goals/budget_limit.md",
        goal,
        include_time=True,
    )


def objective_updated_prompt(goal: ThreadGoal) -> str:
    return _render_goal_template(
        "prompts/goals/objective_updated.md",
        goal,
        include_time=False,
    )


def goal_context_input_item(prompt: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": f"<goal_context>\n{prompt}\n</goal_context>"}],
    }


def goal_token_delta_for_usage(usage: dict[str, Any]) -> int:
    input_tokens = _int_usage(usage, "input_tokens")
    cached = _int_usage(usage, "cached_input_tokens")
    if cached == 0:
        details = usage.get("input_tokens_details")
        if isinstance(details, dict):
            cached = _int_usage(details, "cached_tokens")
    output_tokens = _int_usage(usage, "output_tokens")
    return max(0, input_tokens - cached) + max(0, output_tokens)


def goal_summary(goal: ThreadGoal) -> str:
    parts = [f"Objective: {goal.objective}"]
    if goal.time_used_seconds > 0:
        parts.append(f"Time: {format_goal_elapsed_seconds(goal.time_used_seconds)}.")
    if goal.token_budget is not None:
        parts.append(f"Tokens: {format_tokens_compact(goal.tokens_used)}/{format_tokens_compact(goal.token_budget)}.")
    return " ".join(parts)


def format_goal_elapsed_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours >= 24:
        days = hours // 24
        remaining_hours = hours % 24
        return f"{days}d {remaining_hours}h {remaining_minutes}m"
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def format_tokens_compact(value: int | float) -> str:
    value = max(0, int(value))
    if value == 0:
        return "0"
    if value < 1_000:
        return str(value)
    scaled = float(value)
    suffix = "K"
    if value >= 1_000_000_000_000:
        scaled = value / 1_000_000_000_000.0
        suffix = "T"
    elif value >= 1_000_000_000:
        scaled = value / 1_000_000_000.0
        suffix = "B"
    elif value >= 1_000_000:
        scaled = value / 1_000_000.0
        suffix = "M"
    else:
        scaled = value / 1_000.0
    decimals = 2 if scaled < 10 else 1 if scaled < 100 else 0
    formatted = f"{scaled:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def _render_goal_template(relative_path: str, goal: ThreadGoal, *, include_time: bool) -> str:
    template = read_asset(relative_path)
    token_budget = str(goal.token_budget) if goal.token_budget is not None else "none"
    remaining_tokens = str(max(0, goal.token_budget - goal.tokens_used)) if goal.token_budget is not None else "unbounded"
    values = {
        "objective": _escape_xml_text(goal.objective),
        "tokens_used": str(goal.tokens_used),
        "token_budget": token_budget,
        "remaining_tokens": remaining_tokens,
        "time_used_seconds": str(goal.time_used_seconds),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", value)
    return rendered


def _goal_updated_event(goal: ThreadGoal, turn_id: str | None) -> GoalRuntimeEvent:
    return GoalRuntimeEvent(
        "thread.goal.updated",
        {
            "thread_id": goal.thread_id,
            "turn_id": turn_id,
            "goal": goal.to_protocol(),
        },
    )


def _goal_tool_success(
    goal: ThreadGoal | None,
    *,
    report_completion_budget: bool,
    tool: str,
    events: tuple[GoalRuntimeEvent, ...] = (),
) -> GoalToolOperationResult:
    response = _goal_response(goal, report_completion_budget=report_completion_budget)
    output = json.dumps(response, ensure_ascii=False, indent=2, sort_keys=False)
    return GoalToolOperationResult(True, output, {"tool": tool, "goal": response.get("goal")}, output, events)


def _goal_tool_error(
    message: str,
    tool: str,
    *,
    events: tuple[GoalRuntimeEvent, ...] = (),
) -> GoalToolOperationResult:
    return GoalToolOperationResult(False, message, {"tool": tool}, None, events)


def _goal_response(goal: ThreadGoal | None, *, report_completion_budget: bool) -> dict[str, Any]:
    protocol = goal.to_protocol() if goal is not None else None
    remaining = None
    if goal is not None and goal.token_budget is not None:
        remaining = max(0, goal.token_budget - goal.tokens_used)
    completion_budget_report = None
    if report_completion_budget and goal is not None and goal.status == "complete":
        if goal.token_budget is not None or goal.time_used_seconds > 0:
            completion_budget_report = (
                "Goal achieved. Report final usage from this tool result's structured goal fields. "
                "If `goal.tokenBudget` is present, include token usage from `goal.tokensUsed` and "
                "`goal.tokenBudget`. If `goal.timeUsedSeconds` is greater than 0, summarize elapsed "
                "time in a concise, human-friendly form appropriate to the response language."
            )
    return {
        "goal": protocol,
        "remainingTokens": remaining,
        "completionBudgetReport": completion_budget_report,
    }


def _goal_from_record(record: Any) -> ThreadGoal | None:
    if not isinstance(record, dict):
        return None
    try:
        return ThreadGoal(
            thread_id=str(record["thread_id"]),
            goal_id=str(record["goal_id"]),
            objective=str(record["objective"]),
            status=_status_from_wire(str(record["status"])),
            token_budget=_optional_int(record.get("token_budget")),
            tokens_used=max(0, int(record.get("tokens_used", 0))),
            time_used_seconds=max(0, int(record.get("time_used_seconds", 0))),
            created_at=int(record.get("created_at", 0)),
            updated_at=int(record.get("updated_at", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _status_from_wire(value: str) -> GoalStatus:
    normalized = GOAL_STATUS_FROM_WIRE.get(value)
    if normalized is None:
        raise ValueError(f"unknown thread goal status `{value}`")
    return normalized  # type: ignore[return-value]


def _updated_status(goal: ThreadGoal, status: GoalStatus) -> GoalStatus:
    if goal.status == "budget_limited" and status in {"paused", "blocked"}:
        return "budget_limited"
    if status == "active" and goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
        return "budget_limited"
    return status


def _status_after_budget_limit(status: GoalStatus, tokens_used: int, token_budget: int | None) -> GoalStatus:
    if status == "active" and token_budget is not None and tokens_used >= token_budget:
        return "budget_limited"
    return status


def _status_allowed_for_accounting(status: GoalStatus, mode: str) -> bool:
    if mode == "active_status_only":
        return status == "active"
    if mode == "active_only":
        return status in {"active", "budget_limited"}
    if mode == "active_or_complete":
        return status in {"active", "budget_limited", "complete"}
    if mode == "active_or_stopped":
        return status in {"active", "paused", "blocked", "usage_limited", "budget_limited"}
    return False


def _status_allowed_for_budget_limit(status: GoalStatus, mode: str) -> bool:
    if mode in {"active_status_only", "active_only", "active_or_complete"}:
        return status == "active"
    if mode == "active_or_stopped":
        return status in {"active", "paused", "blocked", "usage_limited", "budget_limited"}
    return False


def _validate_objective(value: str) -> str:
    if not value:
        raise ValueError("goal objective must not be empty")
    if len(value) > MAX_THREAD_GOAL_OBJECTIVE_CHARS:
        raise ValueError(f"goal objective must be at most {MAX_THREAD_GOAL_OBJECTIVE_CHARS} characters")
    return value


def _validate_goal_budget(value: Any) -> int | None:
    if value is None:
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        raise ValueError("goal budgets must be positive when provided") from None
    if budget <= 0:
        raise ValueError("goal budgets must be positive when provided")
    return budget


def _expect_object(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(arguments, dict):
        raise ValueError("goal tool arguments must be an object")
    return arguments


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required is not None:
        schema["required"] = required
    return schema


def _int_usage(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _escape_xml_text(input_text: str) -> str:
    return input_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now_seconds() -> int:
    return int(time.time())
