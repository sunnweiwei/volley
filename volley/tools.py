from __future__ import annotations

import json
import base64
import difflib
import errno
import io
import mimetypes
import os
import random
import re
import select
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .goal import GoalToolOperationResult
from .goal import create_goal_spec, get_goal_spec, update_goal_spec
from .prompts import read_asset
from .types import VolleyConfig

try:
    import pty
except ImportError:  # pragma: no cover - Windows uses shell_command by default.
    pty = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is present in the supported test/runtime env.
    Image = None


MIN_YIELD_TIME_MS = 250
MIN_EMPTY_YIELD_TIME_MS = 5_000
MAX_YIELD_TIME_MS = 30_000
DEFAULT_MAX_BACKGROUND_TERMINAL_TIMEOUT_MS = 300_000
DEFAULT_EXEC_YIELD_TIME_MS = 10_000
DEFAULT_WRITE_STDIN_YIELD_TIME_MS = 250
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
APPROX_BYTES_PER_TOKEN = 4
UNIFIED_EXEC_OUTPUT_MAX_BYTES = 1024 * 1024
MAX_UNIFIED_EXEC_PROCESSES = 64
MAX_PROMPT_IMAGE_DIMENSION = 2048
MODEL_CATALOG_JSON = Path(__file__).resolve().parent / "assets" / "models.json"
SEATBELT_BASE_POLICY = Path(__file__).resolve().parent / "assets" / "seatbelt_base_policy.sbpl"
MACOS_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_MODEL_CATALOG_CACHE: dict[str, dict[str, Any]] | None = None
_PLATFORM_SANDBOX_AVAILABLE_CACHE: bool | None = None


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str
    metadata: dict[str, Any]
    response_output: Any = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    spec: dict[str, Any]
    handler: Callable[[Any], ToolResult]
    freeform: bool = False
    supports_parallel: bool = False


@dataclass(frozen=True)
class SandboxedProcessArgv:
    argv: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ApplyPatchShellInvocation:
    patch: str
    workdir: str | None = None


class AgentRuntime(Protocol):
    def spawn_agent(self, arguments: dict[str, Any]) -> ToolResult: ...

    def send_input(self, arguments: dict[str, Any]) -> ToolResult: ...

    def resume_agent(self, arguments: dict[str, Any]) -> ToolResult: ...

    def wait_agent(self, arguments: dict[str, Any]) -> ToolResult: ...

    def close_agent(self, arguments: dict[str, Any]) -> ToolResult: ...

    def request_interrupt(self) -> None: ...

    def interrupt_all(self) -> None: ...


class ToolRuntime:
    def __init__(
        self,
        config: VolleyConfig,
        agent_runtime: AgentRuntime | None = None,
        goal_runtime: Any | None = None,
    ):
        self.config = config
        self.cwd = config.resolved_cwd()
        self.agent_runtime = agent_runtime
        self.goal_runtime = goal_runtime
        self._sessions: dict[int, RunningCommand] = {}
        self._active_commands: list[RunningCommand] = []
        self._next_session_id = 1
        self._session_lock = threading.Lock()
        self._approval_cache: set[str] = set()
        self._runtime_event_lock = threading.Lock()
        self._runtime_events: list[dict[str, Any]] = []
        self._interrupt_event = threading.Event()

    def definitions(self) -> list[ToolDefinition]:
        tools = []
        if self.config.include_unified_exec_tool:
            tools.extend(
                [
                    ToolDefinition("exec_command", exec_command_spec(), self.exec_command, supports_parallel=True),
                    ToolDefinition("write_stdin", write_stdin_spec(), self.write_stdin),
                ]
            )
        if self.config.include_update_plan_tool:
            tools.append(ToolDefinition("update_plan", update_plan_spec(), self.update_plan))
        if self._goal_tools_available():
            tools.extend(
                [
                    ToolDefinition("get_goal", get_goal_spec(), self.get_goal),
                    ToolDefinition("create_goal", create_goal_spec(), self.create_goal),
                    ToolDefinition("update_goal", update_goal_spec(), self.update_goal),
                ]
            )
        if self.config.include_request_user_input_tool:
            tools.append(
                ToolDefinition(
                    "request_user_input",
                    request_user_input_spec(self.config),
                    self.request_user_input,
                )
            )
        tools.append(ToolDefinition("apply_patch", apply_patch_spec(), self.apply_patch, freeform=True))
        if self.config.include_view_image_tool:
            tools.append(ToolDefinition("view_image", view_image_spec(self.config), self.view_image, supports_parallel=True))
        if self.config.include_shell_command_tool:
            tools.append(ToolDefinition("shell_command", shell_command_spec(), self.shell_command, supports_parallel=True))
        if self.config.include_multi_agent_tools:
            tools.extend(
                [
                    ToolDefinition("spawn_agent", spawn_agent_spec(), self.spawn_agent),
                    ToolDefinition("send_input", send_input_spec(), self.send_input),
                    ToolDefinition("resume_agent", resume_agent_spec(), self.resume_agent),
                    ToolDefinition("wait_agent", wait_agent_spec(), self.wait_agent),
                    ToolDefinition("close_agent", close_agent_spec(), self.close_agent),
                ]
            )
        if self.config.include_web_search_tool:
            tools.append(ToolDefinition("web_search", web_search_spec(self.config), self.hosted_web_search))
        return tools

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec for tool in self.definitions()]

    def supports_parallel(self, name: str) -> bool:
        return any(tool.name == name and tool.supports_parallel for tool in self.definitions())

    def drain_runtime_events(self) -> list[dict[str, Any]]:
        with self._runtime_event_lock:
            events = list(self._runtime_events)
            self._runtime_events.clear()
        return events

    def _record_runtime_event(self, event_type: str, **payload: Any) -> None:
        with self._runtime_event_lock:
            self._runtime_events.append({"type": event_type, "payload": payload})

    def _exec_output_delta_callback(self, call_id: str) -> Callable[[str, str], None]:
        def emit(text: str, stream: str) -> None:
            if text:
                self._record_runtime_event(
                    "exec_command.output_delta",
                    call_id=call_id,
                    delta=text,
                    stream=stream,
                )

        return emit

    def request_interrupt(self) -> None:
        self._interrupt_event.set()
        interrupt_agents = getattr(self.agent_runtime, "request_interrupt", None)
        if callable(interrupt_agents):
            interrupt_agents()

    def clear_interrupt(self) -> None:
        self._interrupt_event.clear()

    def dispatch(
        self,
        name: str,
        arguments: Any,
        *,
        call_id: str | None = None,
        clear_interrupt: bool = True,
    ) -> ToolResult:
        if clear_interrupt:
            self._interrupt_event.clear()
        registry = {tool.name: tool for tool in self.definitions()}
        tool = registry.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool: {name}", {"tool": name})
        if call_id is not None and name in {"exec_command", "write_stdin"}:
            arguments = _with_internal_call_id(arguments, call_id)
        try:
            return tool.handler(arguments)
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}", {"tool": name})

    def _goal_tools_available(self) -> bool:
        if not self.config.goals_enabled or not self.config.include_goal_tools:
            return False
        runtime = self.goal_runtime
        available = getattr(runtime, "tools_available", None)
        return bool(callable(available) and available())

    def _goal_tool_result(self, result: GoalToolOperationResult) -> ToolResult:
        for event in result.events:
            self._record_runtime_event(event.type, **event.payload)
        return ToolResult(result.ok, result.output, result.metadata, result.response_output)

    def get_goal(self, arguments: Any) -> ToolResult:
        runtime = self.goal_runtime
        handler = getattr(runtime, "get_goal_tool", None)
        if not callable(handler):
            return ToolResult(False, "goals feature is unavailable", {"tool": "get_goal"})
        return self._goal_tool_result(handler(arguments))

    def create_goal(self, arguments: Any) -> ToolResult:
        runtime = self.goal_runtime
        handler = getattr(runtime, "create_goal", None)
        if not callable(handler):
            return ToolResult(False, "goals feature is unavailable", {"tool": "create_goal"})
        return self._goal_tool_result(handler(arguments))

    def update_goal(self, arguments: Any) -> ToolResult:
        runtime = self.goal_runtime
        handler = getattr(runtime, "update_goal", None)
        if not callable(handler):
            return ToolResult(False, "goals feature is unavailable", {"tool": "update_goal"})
        return self._goal_tool_result(handler(arguments))

    def normalize_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        name = str(call.get("name") or "")
        arguments = call.get("arguments")
        if name == "exec_command":
            args = arguments if isinstance(arguments, dict) else {}
            command = args.get("cmd")
            workdir_value = args.get("workdir")
        elif name == "shell_command":
            args = arguments if isinstance(arguments, dict) else {}
            command = args.get("command")
            workdir_value = args.get("workdir")
        else:
            return call
        if not isinstance(command, str):
            return call
        shell = str(args.get("shell") or _default_shell())
        login = bool(args.get("login", True))
        if not _can_intercept_apply_patch_shell(shell, login):
            return call
        invocation = _maybe_parse_apply_patch_shell(command)
        if invocation is None:
            return call
        base_workdir = self._resolve_workdir(workdir_value)
        patch_workdir = _resolve_apply_patch_shell_workdir(base_workdir, invocation.workdir)
        return {
            **call,
            "name": "apply_patch",
            "arguments": {"patch": invocation.patch, "workdir": str(patch_workdir)},
            "original_name": name,
        }

    def interrupt_all(self) -> None:
        self._interrupt_event.set()
        with self._session_lock:
            commands = [*self._active_commands, *self._sessions.values()]
            self._sessions.clear()
        seen: set[int] = set()
        for running in commands:
            identity = id(running)
            if identity in seen:
                continue
            seen.add(identity)
            running.interrupt()
        interrupt_agents = getattr(self.agent_runtime, "interrupt_all", None)
        if callable(interrupt_agents):
            interrupt_agents()

    def exec_command(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        call_id = str(args.pop("_codex_call_id", ""))
        command = _expect_string(args, "cmd")
        permission_error = self._sandbox_permission_error("exec_command", args)
        if permission_error is not None:
            return permission_error
        preflight_error = self._default_sandbox_approval_error(
            "exec_command",
            args,
            cache_keys=[_approval_cache_key("exec_command", self.cwd, command, bool(args.get("tty", False)))],
        )
        if preflight_error is not None:
            return preflight_error
        workdir = self._resolve_workdir(args.get("workdir"))
        yield_time_ms = _clamp_exec_yield_time(int(args.get("yield_time_ms", DEFAULT_EXEC_YIELD_TIME_MS)))
        max_output_tokens = int(args.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        shell = str(args.get("shell") or _default_shell())
        login = bool(args.get("login", True))
        tty = bool(args.get("tty", False))

        result = self._run_exec_command_once(
            call_id=call_id,
            command=command,
            workdir=workdir,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
            shell=shell,
            login=login,
            tty=tty,
            bypass_sandbox=args.get("sandbox_permissions") == "require_escalated",
        )
        retry_error = self._sandbox_retry_error(
            "exec_command",
            args,
            result,
            cache_keys=[_approval_cache_key("exec_command:retry", workdir, command, tty)],
        )
        if retry_error is not None:
            return retry_error
        if not self._should_retry_without_sandbox(result):
            return result
        retry_result = self._run_exec_command_once(
            call_id=call_id,
            command=command,
            workdir=workdir,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
            shell=shell,
            login=login,
            tty=tty,
            bypass_sandbox=True,
        )
        retry_result.metadata["retry_without_sandbox"] = True
        retry_result.metadata["initial_sandbox_failure"] = _sandbox_retry_summary(result)
        return retry_result

    def _run_exec_command_once(
        self,
        *,
        call_id: str,
        command: str,
        workdir: Path,
        yield_time_ms: int,
        max_output_tokens: int,
        shell: str,
        login: bool,
        tty: bool,
        bypass_sandbox: bool,
    ) -> ToolResult:
        start = time.monotonic()
        sandboxed = _sandboxed_process_argv(
            _shell_argv(shell, command, login),
            config=self.config,
            cwd=self.cwd,
            workdir=workdir,
            bypass_sandbox=bypass_sandbox,
        )
        try:
            process, pty_fd = _spawn_process(
                sandboxed.argv,
                cwd=workdir,
                tty=tty,
            )
        except OSError as exc:
            return ToolResult(False, f"exec_command failed: {exc}", {"tool": "exec_command", **sandboxed.metadata})
        running = RunningCommand(
            process,
            command=command,
            workdir=workdir,
            tty=tty,
            event_call_id=call_id,
            pty_fd=pty_fd,
            sandbox_metadata=sandboxed.metadata,
            output_callback=self._exec_output_delta_callback(call_id),
        )
        running.start_readers()
        self._register_active_command(running)
        try:
            _wait_for_process_or_timeout(process, yield_time_ms, stop_event=self._interrupt_event)

            if process.poll() is None:
                session_id = self._register_session(running)
                payload = running.snapshot(start, max_output_tokens)
                payload["session_id"] = session_id
                payload["response_text"] = _unified_exec_response_text(payload)
                return ToolResult(True, payload["response_text"], payload)

            payload = running.snapshot(start, max_output_tokens)
            payload["exit_code"] = process.returncode
            payload["response_text"] = _unified_exec_response_text(payload)
            return ToolResult(True, payload["response_text"], payload)
        finally:
            self._unregister_active_command(running)

    def write_stdin(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        args.pop("_codex_call_id", None)
        session_id = int(args.get("session_id"))
        chars = str(args.get("chars", ""))
        yield_time_ms = _write_stdin_yield_time(int(args.get("yield_time_ms", DEFAULT_WRITE_STDIN_YIELD_TIME_MS)), chars)
        max_output_tokens = int(args.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        with self._session_lock:
            running = self._sessions.get(session_id)
        if running is None:
            return ToolResult(False, f"unknown exec session: {session_id}", {"session_id": session_id})
        if chars and not running.tty:
            message = "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true to keep stdin open"
            return ToolResult(False, message, {"session_id": session_id, "event_call_id": running.event_call_id})

        start = time.monotonic()
        if chars and running.process.poll() is None:
            running.write_stdin(chars)
        _wait_for_process_or_timeout(running.process, yield_time_ms, stop_event=self._interrupt_event)
        try:
            running.process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        payload = running.snapshot(start, max_output_tokens)
        payload["event_call_id"] = running.event_call_id
        payload["interaction_input"] = chars
        payload["command"] = running.command
        payload["workdir"] = str(running.workdir)
        if running.process.poll() is None:
            payload["session_id"] = session_id
        else:
            payload["exit_code"] = running.process.returncode
            with self._session_lock:
                self._sessions.pop(session_id, None)
        payload["response_text"] = _unified_exec_response_text(payload)
        return ToolResult(True, payload["response_text"], payload)

    def shell_command(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        command = _expect_string(args, "command")
        permission_error = self._sandbox_permission_error("shell_command", args)
        if permission_error is not None:
            return permission_error
        workdir = self._resolve_workdir(args.get("workdir"))
        timeout_ms = max(int(args.get("timeout_ms", 120000)), 1000)
        start = time.monotonic()
        try:
            process, _pty_fd = _spawn_process(
                _shell_argv(_default_shell(), command, bool(args.get("login", True))),
                cwd=workdir,
                tty=False,
            )
        except OSError as exc:
            return ToolResult(False, f"shell_command failed: {exc}", {"tool": "shell_command"})
        running = RunningCommand(
            process,
            command=command,
            workdir=workdir,
            tty=False,
            event_call_id="",
        )
        running.start_readers()
        self._register_active_command(running)
        timed_out = False
        interrupted = False
        try:
            _wait_for_process_or_timeout(process, timeout_ms, stop_event=self._interrupt_event)
            interrupted = self._interrupt_event.is_set()
            if process.poll() is None:
                timed_out = not interrupted
                running.interrupt()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            payload_snapshot = running.snapshot(start, DEFAULT_MAX_OUTPUT_TOKENS)
        finally:
            self._unregister_active_command(running)
        output = payload_snapshot.get("aggregated_output") or payload_snapshot.get("output") or ""
        exit_code = process.returncode
        ok = exit_code == 0 and not timed_out and not interrupted
        payload = {
            "output": output,
            "metadata": {
                "exit_code": exit_code,
                "duration_seconds": round(time.monotonic() - start, 3),
                "timed_out": timed_out,
                "interrupted": interrupted,
            },
        }
        return ToolResult(ok, json.dumps(payload), payload)

    def apply_patch(self, arguments: Any) -> ToolResult:
        if isinstance(arguments, str):
            patch = arguments
            cwd = self.cwd
        else:
            args = _expect_object(arguments)
            patch = _expect_string(args, "patch")
            cwd = self._resolve_workdir(args.get("workdir"))
        if self.config.sandbox == "read-only":
            return ToolResult(False, "apply_patch is denied in read-only sandbox", {"denied": True})
        if _looks_like_volley_freeform_patch(patch):
            return self._apply_volley_patch(patch, cwd=cwd)
        sandbox_error = self._diff_patch_sandbox_error(patch, cwd=cwd)
        if sandbox_error is not None:
            return sandbox_error

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            git_cwd, git_argv = _git_apply_invocation(cwd, patch_path)
            first = subprocess.run(
                git_argv,
                cwd=git_cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            if first.returncode == 0:
                return ToolResult(True, "patch applied", {"returncode": 0, "strategy": "git apply"})
            second = subprocess.run(
                ["patch", "--batch", "--fuzz=5", "-p1", "-i", str(patch_path)],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            output = _join_output(first.stdout + second.stdout, first.stderr + second.stderr)
            return ToolResult(
                second.returncode == 0,
                output or "patch applied",
                {"returncode": second.returncode, "strategy": "patch"},
            )
        finally:
            patch_path.unlink(missing_ok=True)

    def update_plan(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        if self.config.collaboration_mode == "Plan":
            return ToolResult(
                False,
                "update_plan is a TODO/checklist tool and is not allowed in Plan mode",
                {"plan": args.get("plan", []), "explanation": args.get("explanation")},
            )
        return ToolResult(True, "Plan updated", {"plan": args.get("plan", []), "explanation": args.get("explanation")})

    def request_user_input(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        if self.config.agent_depth > 0:
            return ToolResult(
                False,
                "request_user_input can only be used by the root thread",
                {"agent_depth": self.config.agent_depth},
            )
        normalized = _normalize_request_user_input_questions(args.get("questions"))
        if isinstance(normalized, ToolResult):
            return normalized
        questions = normalized

        if self.config.collaboration_mode not in self.config.request_user_input_available_modes:
            message = f"request_user_input is unavailable in {self.config.collaboration_mode} mode"
            return ToolResult(False, message, {"available_modes": list(self.config.request_user_input_available_modes)})

        if self.config.request_user_input_answers is None:
            if self.config.request_user_input_provider is not None:
                response = self.config.request_user_input_provider(questions)
                if response is None:
                    return ToolResult(
                        False,
                        "request_user_input was cancelled before receiving a response",
                        {"questions": questions},
                    )
                payload = response if isinstance(response, dict) and "answers" in response else {"answers": response}
                return ToolResult(True, json.dumps(payload), {"questions": questions, **payload})
            return ToolResult(
                False,
                "request_user_input was cancelled before receiving a response",
                {"questions": questions},
            )
        payload = {"answers": self.config.request_user_input_answers}
        return ToolResult(True, json.dumps(payload), {"questions": questions, **payload})

    def view_image(self, arguments: Any) -> ToolResult:
        args = _expect_object(arguments)
        detail = args.get("detail")
        if detail not in {None, "original"}:
            return ToolResult(
                False,
                f"view_image.detail only supports `original`; omit `detail` for default resized behavior, got `{detail}`",
                {"detail": detail},
            )
        if not _supports_image_input(self.config):
            return ToolResult(
                False,
                "view_image is not allowed because you do not support image inputs",
                {"model": self.config.model},
            )
        path = self._resolve_path(_expect_string(args, "path"))
        if not path.is_file():
            return ToolResult(False, f"image path `{path}` is not a file", {"path": str(path)})
        use_original_detail = detail == "original" and _can_request_original_image_detail(self.config)
        try:
            processed = _load_image_for_prompt(path, original=use_original_detail)
        except ValueError as exc:
            return ToolResult(False, f"unable to process image at `{path}`: {exc}", {"path": str(path)})
        image_detail = "original" if use_original_detail else "high"
        response = {
            "content": [
                {
                    "type": "input_image",
                    "image_url": processed["image_url"],
                    "detail": image_detail,
                }
            ],
            "success": True,
        }
        metadata = {
            "path": str(path),
            "detail": image_detail,
            "requested_detail": detail,
            **processed,
        }
        return ToolResult(
            True,
            json.dumps({"image_url": processed["image_url"], "detail": image_detail}),
            metadata,
            response,
        )

    def hosted_web_search(self, arguments: Any) -> ToolResult:
        return ToolResult(
            False,
            "web_search is a hosted Responses API tool and is not dispatched locally",
            {"hosted": True, "tool": "web_search"},
        )

    def multi_agent_unavailable(self, arguments: Any) -> ToolResult:
        return ToolResult(
            False,
            "multi-agent runtime is not implemented in the Python port yet",
            {"implemented": False},
        )

    def spawn_agent(self, arguments: Any) -> ToolResult:
        if self.agent_runtime is None:
            return self.multi_agent_unavailable(arguments)
        return self.agent_runtime.spawn_agent(_expect_object(arguments))

    def send_input(self, arguments: Any) -> ToolResult:
        if self.agent_runtime is None:
            return self.multi_agent_unavailable(arguments)
        return self.agent_runtime.send_input(_expect_object(arguments))

    def resume_agent(self, arguments: Any) -> ToolResult:
        if self.agent_runtime is None:
            return self.multi_agent_unavailable(arguments)
        return self.agent_runtime.resume_agent(_expect_object(arguments))

    def wait_agent(self, arguments: Any) -> ToolResult:
        if self.agent_runtime is None:
            return self.multi_agent_unavailable(arguments)
        return self.agent_runtime.wait_agent(_expect_object(arguments))

    def close_agent(self, arguments: Any) -> ToolResult:
        if self.agent_runtime is None:
            return self.multi_agent_unavailable(arguments)
        return self.agent_runtime.close_agent(_expect_object(arguments))

    def _register_session(self, running: "RunningCommand") -> int:
        pruned: RunningCommand | None = None
        with self._session_lock:
            if len(self._sessions) >= MAX_UNIFIED_EXEC_PROCESSES:
                prune_id = _session_id_to_prune(self._sessions)
                if prune_id is not None:
                    pruned = self._sessions.pop(prune_id, None)
            session_id = self._next_session_id
            self._next_session_id += 1
            self._sessions[session_id] = running
        if pruned is not None:
            pruned.interrupt()
        return session_id

    def _register_active_command(self, running: "RunningCommand") -> None:
        with self._session_lock:
            self._active_commands.append(running)

    def _unregister_active_command(self, running: "RunningCommand") -> None:
        with self._session_lock:
            self._active_commands = [item for item in self._active_commands if item is not running]

    def _resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        resolved = path.resolve()
        if self.config.sandbox in {"read-only", "workspace-write"}:
            if resolved != self.cwd and self.cwd not in resolved.parents:
                raise ValueError(f"path escapes workspace: {value}")
        return resolved

    def _resolve_workdir(self, value: Any) -> Path:
        if value is None or value == "":
            return self.cwd
        path = self._resolve_path(str(value))
        if not path.exists():
            raise ValueError(f"workdir does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"workdir is not a directory: {path}")
        return path

    def _sandbox_permission_error(self, tool_name: str, args: dict[str, Any]) -> ToolResult | None:
        sandbox_permissions = args.get("sandbox_permissions")
        if sandbox_permissions != "require_escalated":
            return None
        if self.config.approval_policy != "on-request":
            approval_policy = self.config.approval_policy
            message = (
                f"approval policy is {approval_policy}; reject command - you cannot ask for escalated "
                f"permissions if the approval policy is {approval_policy}"
            )
            return ToolResult(False, message, {"sandbox_permissions": sandbox_permissions})
        request = {
            "tool": tool_name,
            "hook_tool_name": _approval_hook_tool_name(tool_name),
            "matcher_aliases": _approval_hook_matcher_aliases(tool_name),
            "sandbox_permissions": sandbox_permissions,
            "justification": args.get("justification"),
            "prefix_rule": args.get("prefix_rule"),
        }
        if "cmd" in args:
            request["cmd"] = args["cmd"]
        if "command" in args:
            request["command"] = args["command"]
        return self._approval_error(
            request,
            denied_message="approval denied for escalated permissions",
            missing_provider_message=(
                "approval required for escalated permissions, but no approval provider is configured in the Python local runtime"
            ),
            cache_keys=[
                _approval_cache_key(
                    f"{tool_name}:require_escalated",
                    self.cwd,
                    str(args.get("cmd") or args.get("command") or ""),
                    bool(args.get("tty", False)),
                )
            ],
        )

    def _apply_volley_patch(self, patch: str, *, cwd: Path | None = None) -> ToolResult:
        cwd = cwd or self.cwd
        try:
            changes = _parse_volley_patch(patch)
            writable_roots = None
            if self.config.sandbox == "workspace-write":
                writable_roots = _workspace_write_roots(cwd, self.config.writable_roots)
            affected = self._apply_volley_patch_changes_with_approval(
                patch,
                changes,
                cwd=cwd,
                writable_roots=writable_roots,
            )
        except _PatchApplicationError as exc:
            affected = exc.affected
            return ToolResult(
                False,
                str(exc),
                {
                    "strategy": "volley apply_patch",
                    "returncode": 1,
                    "files": affected.files(),
                    "changes": affected.changes,
                    "partial_failure": bool(affected.changes),
                    "approval": affected.approval,
                },
            )
        except Exception as exc:
            return ToolResult(False, str(exc), {"strategy": "volley apply_patch", "returncode": 1})
        output = _format_apply_patch_success(affected)
        return ToolResult(
            True,
            output,
            {
                "returncode": 0,
                "strategy": "volley apply_patch",
                "files": affected.files(),
                "changes": affected.changes,
                "approval": affected.approval,
            },
        )

    def _diff_patch_sandbox_error(self, patch: str, *, cwd: Path | None = None) -> ToolResult | None:
        if self.config.sandbox != "workspace-write":
            return None
        cwd = cwd or self.cwd
        touched_paths = _diff_patch_touched_paths(patch)
        if not touched_paths:
            return ToolResult(
                False,
                "apply_patch is denied in workspace-write sandbox because touched paths could not be determined",
                {"denied": True, "sandbox": self.config.sandbox},
            )
        writable_roots = _workspace_write_roots(cwd, self.config.writable_roots)
        for path in touched_paths:
            try:
                _safe_writable_path(cwd, writable_roots, path)
            except ValueError as exc:
                approval_error = self._apply_patch_approval_error(
                    patch=patch,
                    files=touched_paths,
                    reason=str(exc),
                )
                if approval_error is None:
                    return None
                return ToolResult(
                    False,
                    str(exc),
                    {"denied": True, "sandbox": self.config.sandbox, "path": path},
                )
        return None

    def _apply_volley_patch_changes_with_approval(
        self,
        patch: str,
        changes: list[dict[str, Any]],
        *,
        cwd: Path,
        writable_roots: list[Path] | None,
    ) -> "_PatchAffected":
        try:
            return _apply_volley_patch_changes(cwd, changes, writable_roots=writable_roots)
        except _PatchApplicationError:
            raise
        except ValueError as exc:
            if self.config.sandbox != "workspace-write" or not _is_workspace_escape_error(exc):
                raise
            files = _volley_patch_touched_paths(changes)
            approval_error = self._apply_patch_approval_error(
                patch=patch,
                files=files,
                reason=str(exc),
            )
            if approval_error is not None:
                raise
            affected = _apply_volley_patch_changes(cwd, changes, writable_roots=None)
            affected.approval = "approved_without_sandbox"
            return affected

    def _default_sandbox_approval_error(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        cache_keys: list[str],
    ) -> ToolResult | None:
        if self.config.sandbox == "danger-full-access":
            return None
        if self.config.approval_policy not in {"on-request", "untrusted"}:
            return None
        if args.get("sandbox_permissions") == "require_escalated":
            return None
        request = {
            "tool": tool_name,
            "hook_tool_name": _approval_hook_tool_name(tool_name),
            "matcher_aliases": _approval_hook_matcher_aliases(tool_name),
            "sandbox": self.config.sandbox,
            "approval_policy": self.config.approval_policy,
            "reason": None,
        }
        if "cmd" in args:
            request["cmd"] = args["cmd"]
        if "command" in args:
            request["command"] = args["command"]
        return self._approval_error(
            request,
            denied_message="approval denied for sandboxed execution",
            missing_provider_message=(
                "approval required for sandboxed execution, but no approval provider is configured in the Python local runtime"
            ),
            cache_keys=cache_keys,
        )

    def _sandbox_retry_error(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult,
        *,
        cache_keys: list[str],
    ) -> ToolResult | None:
        if not self._should_retry_without_sandbox(result):
            return None
        request = {
            "tool": tool_name,
            "hook_tool_name": _approval_hook_tool_name(tool_name),
            "matcher_aliases": _approval_hook_matcher_aliases(tool_name),
            "sandbox_permissions": "require_escalated",
            "retry_without_sandbox": True,
            "reason": "command failed; retry without sandbox?",
            "approval_policy": self.config.approval_policy,
        }
        if "cmd" in args:
            request["cmd"] = args["cmd"]
        if "command" in args:
            request["command"] = args["command"]
        return self._approval_error(
            request,
            denied_message="approval denied for retry without sandbox",
            missing_provider_message="approval required to retry without sandbox, but no approval provider is configured in the Python local runtime",
            cache_keys=cache_keys,
        )

    def _should_retry_without_sandbox(self, result: ToolResult) -> bool:
        if self.config.approval_policy not in {"on-failure", "untrusted"}:
            return False
        if self.config.sandbox == "danger-full-access":
            return False
        if result.metadata.get("session_id") is not None:
            return False
        exit_code = result.metadata.get("exit_code")
        if exit_code in {None, 0}:
            return False
        return _is_likely_sandbox_denied_output(result.output) or _is_likely_sandbox_denied_output(
            str(result.metadata.get("output") or "")
        )

    def _apply_patch_approval_error(
        self,
        *,
        patch: str,
        files: list[str],
        reason: str,
    ) -> ToolResult | None:
        if self.config.approval_policy not in {"on-request", "on-failure", "untrusted"}:
            return ToolResult(
                False,
                reason,
                {"denied": True, "sandbox": self.config.sandbox, "files": files},
            )
        request = {
            "tool": "apply_patch",
            "hook_tool_name": "apply_patch",
            "matcher_aliases": ["Write", "Edit"],
            "sandbox": self.config.sandbox,
            "sandbox_permissions": "require_escalated",
            "retry_without_sandbox": True,
            "reason": reason,
            "files": files,
            "patch": patch,
        }
        return self._approval_error(
            request,
            denied_message="approval denied for apply_patch",
            missing_provider_message=(
                "approval required for apply_patch outside the writable workspace, but no approval provider is configured in the Python local runtime"
            ),
            cache_keys=[_approval_cache_key("apply_patch", self.cwd, path) for path in files],
        )

    def _approval_error(
        self,
        request: dict[str, Any],
        *,
        denied_message: str,
        missing_provider_message: str,
        cache_keys: list[str],
    ) -> ToolResult | None:
        if cache_keys and all(key in self._approval_cache for key in cache_keys):
            return None
        hook_decision = self._permission_request_hook_decision(request)
        if hook_decision is not None:
            if _approval_decision_granted(hook_decision):
                if _approval_decision_for_session(hook_decision):
                    self._approval_cache.update(cache_keys)
                return None
            return ToolResult(
                False,
                denied_message,
                {
                    "approval_policy": self.config.approval_policy,
                    "sandbox": self.config.sandbox,
                    "approval_request": _redacted_approval_request(request),
                    "permission_request_hook": True,
                },
            )
        if self.config.approval_provider is None:
            metadata = {
                "approval_policy": self.config.approval_policy,
                "sandbox": self.config.sandbox,
                "approval_request": _redacted_approval_request(request),
            }
            if "sandbox_permissions" in request:
                metadata["sandbox_permissions"] = request["sandbox_permissions"]
            return ToolResult(False, missing_provider_message, metadata)
        decision = self.config.approval_provider(request)
        if _approval_decision_granted(decision):
            if _approval_decision_for_session(decision):
                self._approval_cache.update(cache_keys)
            return None
        return ToolResult(
            False,
            denied_message,
            {
                "approval_policy": self.config.approval_policy,
                "sandbox": self.config.sandbox,
                "approval_request": _redacted_approval_request(request),
            },
        )

    def _permission_request_hook_decision(self, request: dict[str, Any]) -> Any | None:
        provider = getattr(self.config, "hook_provider", None)
        if provider is None:
            return None
        hook_request = {
            "event": "permission_request",
            "cwd": str(self.cwd),
            "model": self.config.model,
            "approval_policy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "tool_name": request.get("hook_tool_name") or request.get("tool"),
            "matcher_aliases": list(request.get("matcher_aliases") or []),
            "tool_input": dict(request),
        }
        self._record_runtime_event("hook.started", name="permission_request", request=_json_safe(hook_request))
        try:
            outcome = provider(hook_request)
        except Exception as exc:
            self._record_runtime_event(
                "hook.completed",
                name="permission_request",
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                outcome={},
            )
            return None
        self._record_runtime_event("hook.completed", name="permission_request", ok=True, outcome=_json_safe(outcome))
        if not isinstance(outcome, dict):
            return outcome
        if "decision" in outcome or "review_decision" in outcome:
            return outcome
        nested = outcome.get("permission_decision")
        if nested is not None:
            return nested
        for key in ("approved", "allow", "granted"):
            if key in outcome:
                return outcome
        return None


class RunningCommand:
    def __init__(
        self,
        process: subprocess.Popen[Any],
        *,
        command: str,
        workdir: Path,
        tty: bool,
        event_call_id: str,
        pty_fd: int | None = None,
        sandbox_metadata: dict[str, Any] | None = None,
        output_callback: Callable[[str, str], None] | None = None,
    ):
        self.process = process
        self.command = command
        self.workdir = workdir
        self.tty = tty
        self.event_call_id = event_call_id
        self.pty_fd = pty_fd
        self.sandbox_metadata = dict(sandbox_metadata or {})
        self.output_callback = output_callback
        self.last_used = time.monotonic()
        self._lock = threading.Lock()
        self._stdout = _HeadTailTextBuffer()
        self._stderr = _HeadTailTextBuffer()
        self._all_stdout = _HeadTailTextBuffer()
        self._all_stderr = _HeadTailTextBuffer()
        self._threads: list[threading.Thread] = []

    def start_readers(self) -> None:
        if self.pty_fd is not None:
            thread = threading.Thread(target=self._read_pty, daemon=True)
            thread.start()
            self._threads.append(thread)
            return
        if self.process.stdout is not None:
            thread = threading.Thread(target=self._read_stream, args=(self.process.stdout, self._stdout), daemon=True)
            thread.start()
            self._threads.append(thread)
        if self.process.stderr is not None:
            thread = threading.Thread(target=self._read_stream, args=(self.process.stderr, self._stderr), daemon=True)
            thread.start()
            self._threads.append(thread)

    def write_stdin(self, chars: str) -> None:
        data = chars.encode("utf-8", errors="replace")
        if self.pty_fd is not None:
            os.write(self.pty_fd, data)
            return
        if self.process.stdin is not None:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def interrupt(self) -> None:
        if self.process.poll() is None:
            try:
                if sys.platform != "win32":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:  # pragma: no cover - Windows uses shell_command by default.
                    self.process.terminate()
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform != "win32":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:  # pragma: no cover
                        self.process.kill()
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
        self._close_pipes()

    def snapshot(self, start: float, max_output_tokens: int) -> dict[str, Any]:
        self.last_used = time.monotonic()
        if self.process.poll() is not None:
            for thread in self._threads:
                thread.join(timeout=0.1)
            self._close_pipes()
        with self._lock:
            stdout = self._stdout.to_text()
            stderr = self._stderr.to_text()
            aggregated_stdout = self._all_stdout.to_text()
            aggregated_stderr = self._all_stderr.to_text()
            output = _join_output(stdout, stderr)
            aggregated_output = _join_output(aggregated_stdout, aggregated_stderr)
            self._stdout.clear()
            self._stderr.clear()
        truncated, original_count = _truncate_output(output, max_output_tokens)
        return {
            "chunk_id": _generate_chunk_id(),
            "wall_time_seconds": round(time.monotonic() - start, 3),
            "original_token_count": original_count,
            "stdout": stdout,
            "stderr": stderr,
            "aggregated_output": aggregated_output,
            "output": truncated,
            **self.sandbox_metadata,
        }

    def _read_stream(self, stream: Any, target: _HeadTailTextBuffer) -> None:
        fd = stream.fileno()
        while True:
            try:
                readable, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not readable:
                if self.process.poll() is not None:
                    return
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            if not chunk:
                return
            text = _decode_output_chunk(chunk)
            with self._lock:
                target.append(text)
                if target is self._stdout:
                    self._all_stdout.append(text)
                    stream_name = "stdout"
                else:
                    self._all_stderr.append(text)
                    stream_name = "stderr"
            self._record_output_delta(text, stream_name)

    def _read_pty(self) -> None:
        assert self.pty_fd is not None
        while True:
            try:
                readable, _, _ = select.select([self.pty_fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not readable:
                if self.process.poll() is not None:
                    # Drain one final time after exit before letting the reader finish.
                    try:
                        chunk = os.read(self.pty_fd, 4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    self._record_stdout(_decode_output_chunk(chunk))
                continue
            try:
                chunk = os.read(self.pty_fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            if not chunk:
                return
            self._record_stdout(_decode_output_chunk(chunk))

    def _record_stdout(self, text: str) -> None:
        with self._lock:
            self._stdout.append(text)
            self._all_stdout.append(text)
        self._record_output_delta(text, "stdout")

    def _record_output_delta(self, text: str, stream: str) -> None:
        callback = self.output_callback
        if callback is not None:
            callback(text, stream)

    def _close_pipes(self) -> None:
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if self.pty_fd is not None:
            try:
                os.close(self.pty_fd)
            except OSError:
                pass
            self.pty_fd = None


class _HeadTailTextBuffer:
    def __init__(self, max_bytes: int = UNIFIED_EXEC_OUTPUT_MAX_BYTES):
        self.max_bytes = max(0, int(max_bytes))
        self.head_budget = self.max_bytes // 2
        self.tail_budget = self.max_bytes - self.head_budget
        self._head: list[bytes] = []
        self._tail: list[bytes] = []
        self._head_bytes = 0
        self._tail_bytes = 0

    def append(self, text: str) -> None:
        self._push_chunk(text.encode("utf-8", errors="replace"))

    def clear(self) -> None:
        self._head.clear()
        self._tail.clear()
        self._head_bytes = 0
        self._tail_bytes = 0

    def to_text(self) -> str:
        return b"".join([*self._head, *self._tail]).decode("utf-8", errors="replace")

    def _push_chunk(self, chunk: bytes) -> None:
        if not chunk or self.max_bytes == 0:
            return
        if self._head_bytes < self.head_budget:
            remaining_head = self.head_budget - self._head_bytes
            if len(chunk) <= remaining_head:
                self._head.append(chunk)
                self._head_bytes += len(chunk)
                return
            head_part = chunk[:remaining_head]
            tail_part = chunk[remaining_head:]
            if head_part:
                self._head.append(head_part)
                self._head_bytes += len(head_part)
            self._push_tail(tail_part)
            return
        self._push_tail(chunk)

    def _push_tail(self, chunk: bytes) -> None:
        if self.tail_budget == 0:
            return
        if len(chunk) >= self.tail_budget:
            kept = chunk[-self.tail_budget :]
            self._tail = [kept]
            self._tail_bytes = len(kept)
            return
        self._tail.append(chunk)
        self._tail_bytes += len(chunk)
        self._trim_tail()

    def _trim_tail(self) -> None:
        excess = self._tail_bytes - self.tail_budget
        while excess > 0 and self._tail:
            first = self._tail[0]
            if excess >= len(first):
                excess -= len(first)
                self._tail_bytes -= len(first)
                self._tail.pop(0)
                continue
            self._tail[0] = first[excess:]
            self._tail_bytes -= excess
            break


def exec_command_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "exec_command",
        "description": "Runs a command in a PTY, returning output or a session ID for ongoing interaction.",
        "strict": False,
        "parameters": _object_schema(
            {
                "cmd": {"type": "string", "description": "Shell command to execute."},
                "workdir": {"type": "string", "description": "Optional working directory to run the command in; defaults to the turn cwd."},
                "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                "tty": {
                    "type": "boolean",
                    "description": "Whether to allocate a TTY for the command. Defaults to false (plain pipes); set to true to open a PTY and access TTY process.",
                },
                "yield_time_ms": {"type": "number", "description": "How long to wait (in milliseconds) for output before yielding."},
                "max_output_tokens": {"type": "number", "description": "Maximum number of tokens to return. Excess output will be truncated."},
                "login": {"type": "boolean", "description": "Whether to run the shell with -l/-i semantics. Defaults to true."},
                "sandbox_permissions": {"type": "string", "description": "Sandbox permissions for the command."},
                "justification": {"type": "string", "description": "Question shown when requesting escalated execution."},
                "prefix_rule": {"type": "array", "items": {"type": "string"}, "description": "Suggested reusable prefix rule."},
            },
            ["cmd"],
        ),
        "output_schema": unified_exec_output_schema(),
    }


def write_stdin_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "write_stdin",
        "description": "Writes characters to an existing unified exec session and returns recent output.",
        "strict": False,
        "parameters": _object_schema(
            {
                "session_id": {"type": "number", "description": "Identifier of the running unified exec session."},
                "chars": {"type": "string", "description": "Bytes to write to stdin (may be empty to poll)."},
                "yield_time_ms": {"type": "number", "description": "How long to wait (in milliseconds) for output before yielding."},
                "max_output_tokens": {"type": "number", "description": "Maximum number of tokens to return. Excess output will be truncated."},
            },
            ["session_id"],
        ),
        "output_schema": unified_exec_output_schema(),
    }


def apply_patch_spec() -> dict[str, Any]:
    return {
        "type": "custom",
        "name": "apply_patch",
        "description": "Use the `apply_patch` tool to edit files. This is a FREEFORM tool, so do not wrap the patch in JSON.",
        "format": {
            "type": "grammar",
            "syntax": "lark",
            "definition": read_asset("grammars/apply_patch.lark"),
        },
    }


def shell_command_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "shell_command",
        "description": "Runs a shell command and returns its output. Always set the `workdir` param when using the shell_command function.",
        "strict": False,
        "parameters": _object_schema(
            {
                "command": {"type": "string", "description": "The shell script to execute in the user's default shell."},
                "workdir": {"type": "string", "description": "The working directory to execute the command in."},
                "timeout_ms": {"type": "number", "description": "The timeout for the command in milliseconds."},
                "login": {"type": "boolean", "description": "Whether to run with login shell semantics."},
                "sandbox_permissions": {"type": "string", "description": "Sandbox permissions for the command."},
                "justification": {"type": "string", "description": "Question shown when requesting escalated execution."},
                "prefix_rule": {"type": "array", "items": {"type": "string"}},
            },
            ["command"],
        ),
    }


def update_plan_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "update_plan",
        "description": "Updates the task plan.",
        "strict": False,
        "parameters": _object_schema(
            {
                "explanation": {"type": "string"},
                "plan": {
                    "type": "array",
                    "items": _object_schema(
                        {"step": {"type": "string"}, "status": {"type": "string"}},
                        ["step", "status"],
                    ),
                },
            },
            ["plan"],
        ),
    }


def request_user_input_spec(config: VolleyConfig) -> dict[str, Any]:
    modes = _format_allowed_modes(config.request_user_input_available_modes)
    return {
        "type": "function",
        "name": "request_user_input",
        "description": (
            "Request user input for one to three short questions and wait for the response. "
            f"This tool is only available in {modes}."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "questions": {
                    "type": "array",
                    "description": "Questions to show the user. Prefer 1 and do not exceed 3",
                    "items": _object_schema(
                        {
                            "id": {
                                "type": "string",
                                "description": "Stable identifier for mapping answers (snake_case).",
                            },
                            "header": {
                                "type": "string",
                                "description": "Short header label shown in the UI (12 or fewer chars).",
                            },
                            "question": {
                                "type": "string",
                                "description": "Single-sentence prompt shown to the user.",
                            },
                            "options": {
                                "type": "array",
                                "description": (
                                    "Provide 2-3 mutually exclusive choices. Put the recommended option first "
                                    "and suffix its label with \"(Recommended)\". Do not include an \"Other\" "
                                    "option in this list; the client will add a free-form \"Other\" option automatically."
                                ),
                                "items": _object_schema(
                                    {
                                        "label": {
                                            "type": "string",
                                            "description": "User-facing label (1-5 words).",
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "One short sentence explaining impact/tradeoff if selected.",
                                        },
                                    },
                                    ["label", "description"],
                                ),
                            },
                        },
                        ["id", "header", "question", "options"],
                    ),
                },
            },
            ["questions"],
        ),
    }


def view_image_spec(config: VolleyConfig | None = None) -> dict[str, Any]:
    properties = {
        "path": {"type": "string", "description": "Local filesystem path to an image file"},
    }
    if config is None or _can_request_original_image_detail(config):
        properties["detail"] = {
            "type": "string",
            "description": "Optional detail override. The only supported value is `original`; omit this field for default resized behavior. Use `original` to preserve the file's original resolution instead of resizing to fit. This is important when high-fidelity image perception or precise localization is needed, especially for CUA agents.",
        }
    return {
        "type": "function",
        "name": "view_image",
        "description": "View a local image from the filesystem (only use if given a full filepath by the user, and the image isn't already attached to the thread context within <image ...> tags).",
        "strict": False,
        "parameters": _object_schema(properties, ["path"]),
        "output_schema": _object_schema(
            {
                "image_url": {"type": "string", "description": "Data URL for the loaded image."},
                "detail": {
                    "type": ["string", "null"],
                    "description": "Image detail hint returned by view_image. Returns `original` when original resolution is preserved, otherwise `null`.",
                },
            },
            ["image_url", "detail"],
        ),
    }


def spawn_agent_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "spawn_agent",
        "description": (
            "Spawn a sub-agent for a well-scoped task. Returns the spawned agent id plus the "
            "user-facing nickname when available. Spawned agents inherit your current model by default. "
            "Omit `model` to use that preferred default; set `model` only when an explicit override is needed."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "message": {
                    "type": "string",
                    "description": "Initial plain-text task for the new agent. Use either message or items.",
                },
                "items": _collab_input_items_schema(),
                "agent_type": {"type": "string", "description": "Optional type name for the new agent."},
                "fork_context": {
                    "type": "boolean",
                    "description": (
                        "When true, fork the current thread history into the new agent before sending the "
                        "initial prompt."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override for the new agent. Leave unset to inherit the same model "
                        "as the parent."
                    ),
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional reasoning effort override for the new agent.",
                },
            }
        ),
        "output_schema": _object_schema(
            {
                "agent_id": {"type": "string", "description": "Thread identifier for the spawned agent."},
                "nickname": {
                    "type": ["string", "null"],
                    "description": "User-facing nickname for the spawned agent when available.",
                },
            },
            ["agent_id", "nickname"],
        ),
    }


def send_input_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "send_input",
        "description": (
            "Send a message to an existing agent. Use interrupt=true to redirect work immediately. "
            "You should reuse the agent by send_input if you believe your assigned task is highly dependent "
            "on the context of a previous task."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "target": {"type": "string", "description": "Agent id to message (from spawn_agent)."},
                "message": {
                    "type": "string",
                    "description": "Legacy plain-text message to send to the agent. Use either message or items.",
                },
                "items": _collab_input_items_schema(),
                "interrupt": {
                    "type": "boolean",
                    "description": "When true, stop the agent's current task and handle this immediately.",
                },
            },
            ["target"],
        ),
        "output_schema": _object_schema(
            {"submission_id": {"type": "string", "description": "Identifier for the queued input submission."}},
            ["submission_id"],
        ),
    }


def resume_agent_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "resume_agent",
        "description": "Resume a previously closed agent by id so it can receive send_input and wait_agent calls.",
        "strict": False,
        "parameters": _object_schema({"id": {"type": "string", "description": "Agent id to resume."}}, ["id"]),
        "output_schema": _object_schema({"status": _agent_status_schema()}, ["status"]),
    }


def wait_agent_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "wait_agent",
        "description": (
            "Wait for agents to reach a final status. Completed statuses may include the agent's final message. "
            "Returns empty status when timed out. Once the agent reaches a final status, a notification message "
            "will be received containing the same completed status."
        ),
        "strict": False,
        "parameters": _object_schema(
            {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent ids to wait on. Pass multiple ids to wait for whichever finishes first.",
                },
                "timeout_ms": {
                    "type": "number",
                    "description": "Optional timeout in milliseconds. Defaults to 30000, min 10000, max 3600000. Prefer longer waits (minutes) to avoid busy polling.",
                },
            },
            ["targets"],
        ),
        "output_schema": _object_schema(
            {
                "status": {
                    "type": "object",
                    "description": "Final statuses keyed by agent id.",
                    "additionalProperties": _agent_status_schema(),
                },
                "timed_out": {"type": "boolean"},
            },
            ["status", "timed_out"],
        ),
    }


def close_agent_spec() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "close_agent",
        "description": (
            "Close an agent and any open descendants when they are no longer needed, and return the target "
            "agent's previous status before shutdown was requested. Don't keep agents open for too long if "
            "they are not needed anymore."
        ),
        "strict": False,
        "parameters": _object_schema({"target": {"type": "string", "description": "Agent id to close."}}, ["target"]),
        "output_schema": _object_schema({"previous_status": _agent_status_schema()}, ["previous_status"]),
    }


def web_search_spec(config: VolleyConfig) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "type": "web_search",
        "external_web_access": config.web_search_external_web_access,
    }
    if config.web_search_filters is not None:
        spec["filters"] = config.web_search_filters
    if config.web_search_user_location is not None:
        spec["user_location"] = config.web_search_user_location
    if config.web_search_context_size is not None:
        spec["search_context_size"] = config.web_search_context_size
    if config.web_search_content_types is not None:
        spec["search_content_types"] = list(config.web_search_content_types)
    return spec


def unified_exec_output_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "chunk_id": {"type": "string"},
            "wall_time_seconds": {"type": "number"},
            "exit_code": {"type": "number"},
            "session_id": {"type": "number"},
            "original_token_count": {"type": "number"},
            "output": {"type": "string"},
        },
        ["wall_time_seconds", "output"],
    )


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _collab_input_items_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "description": "Structured input items. Use this to pass explicit mentions.",
        "items": _object_schema(
            {
                "type": {"type": "string", "description": "Input item type."},
                "text": {"type": "string", "description": "Text content when type is text."},
                "image_url": {"type": "string", "description": "Image URL when type is image."},
                "path": {"type": "string", "description": "Path for local image, skill, or mention target."},
                "name": {"type": "string", "description": "Display name when type is skill or mention."},
            }
        ),
    }


def _agent_status_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {"type": "string", "enum": ["pending_init", "running", "interrupted", "shutdown", "not_found"]},
            {
                "type": "object",
                "properties": {"completed": {"type": ["string", "null"]}},
                "required": ["completed"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"errored": {"type": "string"}},
                "required": ["errored"],
                "additionalProperties": False,
            },
        ]
    }


def _format_allowed_modes(modes: tuple[str, ...]) -> str:
    if not modes:
        return "no modes"
    if len(modes) == 1:
        return f"{modes[0]} mode"
    if len(modes) == 2:
        return f"{modes[0]} or {modes[1]} mode"
    return "modes: " + ",".join(modes)


def _normalize_request_user_input_questions(value: Any) -> list[dict[str, Any]] | ToolResult:
    if not isinstance(value, list) or not value:
        return ToolResult(False, "request_user_input requires one to three questions", {"questions": value})
    if len(value) > 3:
        return ToolResult(False, "request_user_input accepts at most three questions", {"questions": value})

    questions: list[dict[str, Any]] = []
    for question in value:
        if not isinstance(question, dict):
            return ToolResult(False, "request_user_input questions must be objects", {"question": question})
        question_id = question.get("id")
        header = question.get("header")
        prompt = question.get("question")
        if not isinstance(question_id, str) or not isinstance(header, str) or not isinstance(prompt, str):
            return ToolResult(
                False,
                "request_user_input questions must include string id, header, and question fields",
                {"question": question_id},
            )
        options = question.get("options")
        if not isinstance(options, list) or not options:
            return ToolResult(
                False,
                "request_user_input requires non-empty options for every question",
                {"question": question_id},
            )
        normalized_options: list[dict[str, str]] = []
        for option in options:
            if not isinstance(option, dict):
                return ToolResult(
                    False,
                    "request_user_input options must be objects",
                    {"question": question_id, "option": option},
                )
            label = option.get("label")
            description = option.get("description")
            if not isinstance(label, str) or not isinstance(description, str):
                return ToolResult(
                    False,
                    "request_user_input options must include string label and description fields",
                    {"question": question_id},
                )
            normalized_options.append({"label": label, "description": description})
        questions.append(
            {
                "id": question_id,
                "header": header,
                "question": prompt,
                "isOther": True,
                "isSecret": bool(question.get("isSecret", False)),
                "options": normalized_options,
            }
        )
    return questions


def _expect_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"expected JSON object arguments: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("expected object arguments")
    return value


def _with_internal_call_id(arguments: Any, call_id: str) -> Any:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict):
            parsed["_codex_call_id"] = call_id
            return parsed
        return arguments
    if isinstance(arguments, dict):
        return {**arguments, "_codex_call_id": call_id}
    return arguments


def _approval_decision_granted(decision: Any) -> bool:
    if isinstance(decision, dict):
        for key in ("approved", "allow", "granted"):
            if key in decision:
                return bool(decision[key])
        value = decision.get("decision") or decision.get("review_decision")
        if isinstance(value, str):
            normalized = value.lower().replace("-", "_")
            return normalized in {"approve", "approved", "allow", "allowed", "granted", "approved_for_session", "allow_for_session"}
        return False
    if isinstance(decision, str):
        normalized = decision.lower().replace("-", "_")
        return normalized in {"approve", "approved", "allow", "allowed", "granted", "approved_for_session", "allow_for_session"}
    return bool(decision)


def _approval_decision_for_session(decision: Any) -> bool:
    if not isinstance(decision, dict):
        return False
    for key in ("approved_for_session", "approvedForSession", "allow_for_session", "allowForSession"):
        if key in decision:
            return bool(decision[key])
    value = decision.get("decision") or decision.get("review_decision")
    if isinstance(value, str):
        normalized = value.lower().replace("-", "_")
        return normalized in {"approved_for_session", "allow_for_session"}
    return False


def _approval_cache_key(tool_name: str, *parts: Any) -> str:
    payload = [tool_name, *[str(part) for part in parts]]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _approval_hook_tool_name(tool_name: str) -> str:
    if tool_name in {"exec_command", "shell_command"}:
        return "Bash"
    return tool_name


def _approval_hook_matcher_aliases(tool_name: str) -> list[str]:
    if tool_name == "apply_patch":
        return ["Write", "Edit"]
    return []


def _redacted_approval_request(request: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(request)
    patch = redacted.get("patch")
    if isinstance(patch, str) and len(patch) > 4000:
        redacted["patch"] = patch[:4000] + "\n...[truncated]..."
    return redacted


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _is_likely_sandbox_denied_output(text: str) -> bool:
    lowered = text.lower()
    needles = (
        "operation not permitted",
        "permission denied",
        "read-only file system",
        "sandbox denied",
        "sandbox violation",
        "not allowed by sandbox",
        "denied by policy",
    )
    return any(needle in lowered for needle in needles)


def _sandbox_retry_summary(result: ToolResult) -> dict[str, Any]:
    return {
        "exit_code": result.metadata.get("exit_code"),
        "output": result.output,
        "wall_time_seconds": result.metadata.get("wall_time_seconds"),
    }


def _is_workspace_escape_error(exc: ValueError) -> bool:
    return "path escapes writable workspace" in str(exc)


def _expect_string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _maybe_parse_apply_patch_shell(command: str) -> ApplyPatchShellInvocation | None:
    match = re.fullmatch(
        r"""\s*(?:(?:cd\s+(?P<cd>'[^']*'|"[^"]*"|[^\s&|;]+)\s*&&\s*)?"""
        r"""(?P<cmd>apply_patch|applypatch)\s*<<-?\s*(?P<delim>\S+)\s*\n(?P<body>.*)\n(?P<end>[A-Za-z0-9_-]+)\s*)""",
        command,
        re.DOTALL,
    )
    if match is None:
        return None
    delimiter = _strip_shell_word_quotes(match.group("delim"))
    if not delimiter or delimiter != match.group("end"):
        return None
    workdir = None
    cd_value = match.group("cd")
    if cd_value is not None:
        cd_parts = shlex.split(cd_value)
        if len(cd_parts) != 1:
            return None
        workdir = cd_parts[0]
    body = match.group("body")
    return ApplyPatchShellInvocation(patch=body, workdir=workdir)


def _strip_shell_word_quotes(value: str) -> str:
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    return value


def _resolve_apply_patch_shell_workdir(base_workdir: Path, workdir: str | None) -> Path:
    if workdir is None or workdir == "":
        return base_workdir
    path = Path(workdir).expanduser()
    if not path.is_absolute():
        path = base_workdir / path
    return path.resolve()


def _default_shell() -> str:
    """Pick the platform-appropriate default shell.

    On Windows there is no `SHELL` env var by convention; fall back to
    `ComSpec` (typically `cmd.exe`) so the POSIX `/bin/bash` default doesn't
    leak into Windows subprocess invocations.
    """
    explicit = os.environ.get("SHELL")
    if explicit:
        return explicit
    if sys.platform == "win32":
        return os.environ.get("ComSpec") or "cmd.exe"
    return "/bin/bash"


def _can_intercept_apply_patch_shell(shell: str, login: bool) -> bool:
    return _is_supported_apply_patch_shell_argv(_shell_argv(shell, "", login))


def _is_supported_apply_patch_shell_argv(argv: list[str]) -> bool:
    if len(argv) < 3:
        return False
    name = Path(argv[0]).stem.lower()
    flag = argv[-2].lower()
    if name in {"bash", "zsh", "sh"}:
        return len(argv) == 3 and flag in {"-lc", "-c"}
    if name in {"pwsh", "powershell"}:
        if len(argv) == 4 and argv[1].lower() == "-noprofile":
            return flag == "-command"
        return len(argv) == 3 and flag == "-command"
    if name == "cmd":
        return len(argv) == 3 and flag == "/c"
    return False


def _shell_argv(shell: str, command: str, login: bool) -> list[str]:
    name = Path(shell).name
    if name in {"bash", "zsh", "sh"}:
        return [shell, "-lc" if login else "-c", command]
    if sys.platform == "win32":
        return [shell, "/C", command]
    return [shell, "-c", command]


def _sandboxed_process_argv(
    argv: list[str],
    *,
    config: VolleyConfig,
    cwd: Path,
    workdir: Path,
    bypass_sandbox: bool,
) -> SandboxedProcessArgv:
    metadata: dict[str, Any] = {
        "sandbox_policy": config.sandbox,
        "sandbox_enforced": False,
        "sandbox_bypassed": False,
    }
    if bypass_sandbox or config.sandbox == "danger-full-access":
        metadata["sandbox_bypassed"] = bool(bypass_sandbox)
        return SandboxedProcessArgv(list(argv), metadata)
    if sys.platform == "darwin":
        metadata["sandbox_type"] = "macos_seatbelt"
        if not _platform_sandbox_available():
            metadata["sandbox_unavailable"] = True
            return SandboxedProcessArgv(list(argv), metadata)
        policy = _macos_seatbelt_policy(config, cwd=cwd, workdir=workdir)
        metadata["sandbox_enforced"] = True
        return SandboxedProcessArgv([MACOS_SANDBOX_EXEC, "-p", policy, "--", *argv], metadata)
    metadata["sandbox_unavailable"] = True
    metadata["sandbox_type"] = "unsupported_platform"
    return SandboxedProcessArgv(list(argv), metadata)


def _platform_sandbox_available() -> bool:
    global _PLATFORM_SANDBOX_AVAILABLE_CACHE
    if _PLATFORM_SANDBOX_AVAILABLE_CACHE is not None:
        return _PLATFORM_SANDBOX_AVAILABLE_CACHE
    if sys.platform != "darwin":
        _PLATFORM_SANDBOX_AVAILABLE_CACHE = False
        return False
    if shutil.which(MACOS_SANDBOX_EXEC) != MACOS_SANDBOX_EXEC:
        _PLATFORM_SANDBOX_AVAILABLE_CACHE = False
        return False
    try:
        completed = subprocess.run(
            [MACOS_SANDBOX_EXEC, "-p", "(version 1)\n(allow default)", "--", "/usr/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception:
        _PLATFORM_SANDBOX_AVAILABLE_CACHE = False
        return False
    _PLATFORM_SANDBOX_AVAILABLE_CACHE = completed.returncode == 0
    return _PLATFORM_SANDBOX_AVAILABLE_CACHE


def _macos_seatbelt_policy(config: VolleyConfig, *, cwd: Path, workdir: Path) -> str:
    try:
        base_policy = SEATBELT_BASE_POLICY.read_text(encoding="utf-8")
    except OSError:
        base_policy = "(version 1)\n(deny default)\n(allow process-exec)\n(allow process-fork)\n"
    sections = [base_policy, "; allow read-only file operations\n(allow file-read*)"]
    if config.sandbox == "workspace-write":
        writable_roots = _workspace_write_roots(
            cwd,
            config.writable_roots,
            include_tmp_roots=True,
            exclude_tmpdir_env_var=config.exclude_tmpdir_env_var,
            exclude_slash_tmp=config.exclude_slash_tmp,
        )
        if not any(_path_is_within(workdir.resolve(), root) for root in writable_roots):
            writable_roots.append(workdir.resolve())
        sections.append(_macos_file_write_policy(writable_roots))
    if config.network_access == "enabled":
        sections.append("(allow network-outbound)\n(allow network-inbound)")
    return "\n".join(section for section in sections if section)


def _macos_file_write_policy(roots: list[Path]) -> str:
    lines = ["; allow writable roots"]
    for root in roots:
        path = _sbpl_string(str(root.resolve()))
        lines.append(f"(allow file-write* (subpath {path}))")
        lines.append(f"(allow file-write* (literal {path}))")
    return "\n".join(lines)


def _sbpl_string(value: str) -> str:
    return json.dumps(value)


def _spawn_process(argv: list[str], *, cwd: Path, tty: bool) -> tuple[subprocess.Popen[Any], int | None]:
    if tty:
        if pty is None:
            raise OSError("PTY execution is unavailable on this platform")
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        return process, master_fd

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=(sys.platform != "win32"),
    )
    return process, None


def _join_output(stdout: str, stderr: str) -> str:
    if stdout and stderr:
        return f"{stdout}\n[stderr]\n{stderr}"
    return stdout or stderr


def _decode_output_chunk(chunk: bytes | str) -> str:
    if isinstance(chunk, str):
        return chunk
    return chunk.decode("utf-8", errors="replace")


def _truncate_output(output: str, max_output_tokens: int) -> tuple[str, int]:
    approx_tokens = _approx_token_count(output)
    byte_budget = max(0, int(max_output_tokens)) * APPROX_BYTES_PER_TOKEN
    if len(output.encode("utf-8")) <= byte_budget:
        return output, approx_tokens
    return _formatted_truncate_text_tokens(output, max(0, int(max_output_tokens))), approx_tokens


def _formatted_truncate_text_tokens(text: str, max_tokens: int) -> str:
    if len(text.encode("utf-8")) <= max_tokens * APPROX_BYTES_PER_TOKEN:
        return text
    total_lines = len(text.splitlines())
    return f"Total output lines: {total_lines}\n\n{_truncate_middle_tokens(text, max_tokens)}"


def _truncate_middle_tokens(text: str, max_tokens: int) -> str:
    if not text:
        return ""
    max_bytes = max(0, max_tokens) * APPROX_BYTES_PER_TOKEN
    if max_bytes == 0:
        return _truncation_marker(_approx_token_count(text))
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    left_budget = max_bytes // 2
    right_budget = max_bytes - left_budget
    prefix, suffix = _split_text_for_byte_budget(text, left_budget, right_budget)
    removed_tokens = _approx_tokens_from_byte_count(len(text.encode("utf-8")) - max_bytes)
    return f"{prefix}{_truncation_marker(removed_tokens)}{suffix}"


def _split_text_for_byte_budget(text: str, left_budget: int, right_budget: int) -> tuple[str, str]:
    total_bytes = len(text.encode("utf-8"))
    tail_start_target = max(0, total_bytes - right_budget)
    prefix: list[str] = []
    suffix: list[str] = []
    byte_index = 0
    suffix_started = False
    for char in text:
        char_len = len(char.encode("utf-8"))
        char_start = byte_index
        char_end = byte_index + char_len
        if char_end <= left_budget:
            prefix.append(char)
        elif char_start >= tail_start_target:
            suffix_started = True
            suffix.append(char)
        elif suffix_started:
            suffix.append(char)
        byte_index = char_end
    return "".join(prefix), "".join(suffix)


def _truncation_marker(removed_tokens: int) -> str:
    return f"…{max(0, int(removed_tokens))} tokens truncated…"


def _approx_token_count(text: str) -> int:
    return _approx_tokens_from_byte_count(len(text.encode("utf-8")))


def _approx_tokens_from_byte_count(byte_count: int) -> int:
    byte_count = max(0, int(byte_count))
    return (byte_count + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN


def _clamp_exec_yield_time(yield_time_ms: int) -> int:
    return max(MIN_YIELD_TIME_MS, min(MAX_YIELD_TIME_MS, yield_time_ms))


def _write_stdin_yield_time(yield_time_ms: int, chars: str) -> int:
    time_ms = max(MIN_YIELD_TIME_MS, int(yield_time_ms))
    if chars:
        return min(MAX_YIELD_TIME_MS, time_ms)
    return max(MIN_EMPTY_YIELD_TIME_MS, min(DEFAULT_MAX_BACKGROUND_TERMINAL_TIMEOUT_MS, time_ms))


def _session_id_to_prune(sessions: dict[int, RunningCommand]) -> int | None:
    if not sessions:
        return None
    ordered = sorted(sessions.items(), key=lambda item: item[1].last_used, reverse=True)
    protected = {session_id for session_id, _ in ordered[:8]}
    lru = sorted(sessions.items(), key=lambda item: item[1].last_used)
    for session_id, running in lru:
        if session_id not in protected and running.process.poll() is not None:
            return session_id
    for session_id, _running in lru:
        if session_id not in protected:
            return session_id
    return None


def _wait_for_process_or_timeout(
    process: subprocess.Popen[str],
    yield_time_ms: int,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    deadline = time.monotonic() + max(yield_time_ms, 0) / 1000
    while process.poll() is None and time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _generate_chunk_id() -> str:
    alphabet = "0123456789abcdef"
    return "".join(random.choice(alphabet) for _ in range(6))


def _unified_exec_response_text(payload: dict[str, Any]) -> str:
    sections: list[str] = []
    chunk_id = payload.get("chunk_id")
    if chunk_id:
        sections.append(f"Chunk ID: {chunk_id}")
    sections.append(f"Wall time: {float(payload.get('wall_time_seconds') or 0):.4f} seconds")
    if "exit_code" in payload:
        sections.append(f"Process exited with code {payload['exit_code']}")
    if "session_id" in payload:
        sections.append(f"Process running with session ID {payload['session_id']}")
    if "original_token_count" in payload:
        sections.append(f"Original token count: {payload['original_token_count']}")
    sections.append("Output:")
    sections.append(str(payload.get("output") or ""))
    return "\n".join(sections)


def _data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _supports_image_input(config: VolleyConfig) -> bool:
    if config.model_supports_image_input is not None:
        return config.model_supports_image_input
    model_info = _model_catalog_info(config.model)
    if model_info is not None and isinstance(model_info.get("input_modalities"), list):
        return "image" in model_info["input_modalities"]
    return True


def _can_request_original_image_detail(config: VolleyConfig) -> bool:
    if config.model_supports_image_detail_original is not None:
        return config.model_supports_image_detail_original
    model_info = _model_catalog_info(config.model)
    if model_info is not None and "supports_image_detail_original" in model_info:
        return bool(model_info["supports_image_detail_original"])
    return False


def _model_catalog_info(model: str) -> dict[str, Any] | None:
    global _MODEL_CATALOG_CACHE
    if _MODEL_CATALOG_CACHE is None:
        _MODEL_CATALOG_CACHE = {}
        try:
            data = json.loads(MODEL_CATALOG_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        models = data.get("models") if isinstance(data, dict) else None
        if isinstance(models, list):
            for item in models:
                if isinstance(item, dict) and isinstance(item.get("slug"), str):
                    _MODEL_CATALOG_CACHE[item["slug"]] = item
    return _MODEL_CATALOG_CACHE.get(model)


def _load_image_for_prompt(path: Path, *, original: bool) -> dict[str, Any]:
    file_bytes = path.read_bytes()
    if Image is None:
        if original:
            image_url = _data_url(path)
            return {"image_url": image_url, "mime": mimetypes.guess_type(path.name)[0], "width": None, "height": None, "resized": False}
        raise ValueError("Pillow is required to process resized images")

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            image = opened.copy()
            source_format = (opened.format or "").upper()
            width, height = image.size
    except Exception as exc:  # Pillow raises several decode-specific exception types.
        raise ValueError(str(exc)) from exc

    preserve_source = source_format in {"PNG", "JPEG", "WEBP"}
    if original or (width <= MAX_PROMPT_IMAGE_DIMENSION and height <= MAX_PROMPT_IMAGE_DIMENSION):
        if preserve_source:
            mime = _image_format_mime(source_format)
            return {
                "image_url": _data_url_from_bytes(file_bytes, mime),
                "mime": mime,
                "width": width,
                "height": height,
                "resized": False,
            }
        encoded, mime = _encode_prompt_image(image, "PNG")
        return {
            "image_url": _data_url_from_bytes(encoded, mime),
            "mime": mime,
            "width": width,
            "height": height,
            "resized": False,
        }

    resized = image.copy()
    resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    resized.thumbnail((MAX_PROMPT_IMAGE_DIMENSION, MAX_PROMPT_IMAGE_DIMENSION), resampling)
    target_format = source_format if preserve_source else "PNG"
    encoded, mime = _encode_prompt_image(resized, target_format)
    return {
        "image_url": _data_url_from_bytes(encoded, mime),
        "mime": mime,
        "width": resized.width,
        "height": resized.height,
        "resized": True,
    }


def _encode_prompt_image(image: Any, image_format: str) -> tuple[bytes, str]:
    buffer = io.BytesIO()
    if image_format == "JPEG":
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        return buffer.getvalue(), "image/jpeg"
    if image_format == "WEBP":
        image.convert("RGBA").save(buffer, format="WEBP", lossless=True)
        return buffer.getvalue(), "image/webp"
    image.convert("RGBA").save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


def _image_format_mime(image_format: str) -> str:
    if image_format == "JPEG":
        return "image/jpeg"
    if image_format == "WEBP":
        return "image/webp"
    return "image/png"


def _data_url_from_bytes(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


_VOLLEY_PATCH_HEREDOC_START_LINES = {"<<EOF", "<<'EOF'", '<<"EOF"'}


def _looks_like_volley_freeform_patch(patch: str) -> bool:
    lines = patch.strip().splitlines()
    if not lines:
        return False
    if lines[0].strip() == "*** Begin Patch":
        return True
    return _has_volley_patch_heredoc_wrapper(lines)


def _has_volley_patch_heredoc_wrapper(lines: list[str]) -> bool:
    return (
        len(lines) >= 4
        and lines[0] in _VOLLEY_PATCH_HEREDOC_START_LINES
        and lines[-1].endswith("EOF")
    )


def _parse_volley_patch(patch: str) -> list[dict[str, Any]]:
    lines = patch.strip().splitlines()
    if _has_volley_patch_heredoc_wrapper(lines):
        lines = lines[1:-1]
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")
    changes: list[dict[str, Any]] = []
    index = 1
    if index < len(lines) - 1 and lines[index].lstrip().startswith("*** Environment ID: "):
        environment_id = lines[index].lstrip().removeprefix("*** Environment ID: ").strip()
        if not environment_id:
            raise ValueError("apply_patch environment_id cannot be empty")
        raise ValueError("apply_patch environment selection is unavailable for this turn")
    while index < len(lines) - 1:
        line = lines[index]
        header = line.strip()
        if header.startswith("*** Add File: "):
            path = header.removeprefix("*** Add File: ")
            index += 1
            content: list[str] = []
            while index < len(lines) - 1:
                if not lines[index].startswith("+"):
                    break
                content.append(lines[index][1:])
                index += 1
            changes.append({"type": "add", "path": path, "content": content})
            continue
        if header.startswith("*** Delete File: "):
            path = header.removeprefix("*** Delete File: ")
            changes.append({"type": "delete", "path": path})
            index += 1
            continue
        if header.startswith("*** Update File: "):
            path = header.removeprefix("*** Update File: ")
            index += 1
            move_path = None
            if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                move_path = lines[index].removeprefix("*** Move to: ")
                index += 1
            chunks: list[dict[str, Any]] = []
            parsed_hunk_lines: list[str] = []
            while index < len(lines) - 1:
                if lines[index].strip() == "":
                    index += 1
                    continue
                if lines[index].startswith("*"):
                    break
                chunk, consumed = _parse_update_file_chunk(
                    lines[index : len(lines) - 1],
                    allow_missing_context=not chunks,
                )
                chunks.append(chunk)
                parsed_hunk_lines.extend(lines[index : index + consumed])
                index += consumed
            if not chunks:
                raise ValueError(f"Invalid patch hunk: Update file hunk for path '{path}' is empty")
            changes.append(
                {
                    "type": "update",
                    "path": path,
                    "move_path": move_path,
                    "chunks": chunks,
                    "hunk_lines": parsed_hunk_lines,
                }
            )
            continue
        raise ValueError(f"unexpected patch line: {line}")
    return changes


def _parse_update_file_chunk(lines: list[str], *, allow_missing_context: bool) -> tuple[dict[str, Any], int]:
    if not lines:
        raise ValueError("Invalid patch hunk: Update hunk does not contain any lines")
    if lines[0] == "@@":
        change_context = None
        start_index = 1
    elif lines[0].startswith("@@ "):
        change_context = lines[0].removeprefix("@@ ")
        start_index = 1
    elif allow_missing_context:
        change_context = None
        start_index = 0
    else:
        raise ValueError(f"Invalid patch hunk: Expected update hunk to start with a @@ context marker, got: '{lines[0]}'")
    if start_index >= len(lines):
        raise ValueError("Invalid patch hunk: Update hunk does not contain any lines")

    old_lines: list[str] = []
    new_lines: list[str] = []
    parsed_lines = 0
    is_end_of_file = False
    for line in lines[start_index:]:
        if line == "*** End of File":
            if parsed_lines == 0:
                raise ValueError("Invalid patch hunk: Update hunk does not contain any lines")
            is_end_of_file = True
            parsed_lines += 1
            break
        if line == "":
            old_lines.append("")
            new_lines.append("")
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        else:
            if parsed_lines == 0:
                raise ValueError(
                    "Invalid patch hunk: Unexpected line found in update hunk: "
                    f"'{line}'. Every line should start with ' ' (context line), '+' (added line), or '-' (removed line)"
                )
            break
        parsed_lines += 1
    return (
        {
            "change_context": change_context,
            "old_lines": old_lines,
            "new_lines": new_lines,
            "is_end_of_file": is_end_of_file,
        },
        parsed_lines + start_index,
    )


@dataclass
class _PatchAffected:
    added: list[str]
    modified: list[str]
    deleted: list[str]
    changes: list[dict[str, Any]]
    approval: str | None = None

    def files(self) -> list[str]:
        return [*self.added, *self.modified, *self.deleted]


class _PatchApplicationError(ValueError):
    def __init__(self, message: str, affected: _PatchAffected):
        super().__init__(message)
        self.affected = affected


def _volley_patch_touched_paths(changes: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for change in changes:
        path = change.get("path")
        if isinstance(path, str):
            paths.append(path)
        move_path = change.get("move_path")
        if isinstance(move_path, str):
            paths.append(move_path)
    return list(dict.fromkeys(paths))


def _apply_volley_patch_changes(
    cwd: Path,
    changes: list[dict[str, Any]],
    *,
    writable_roots: list[Path] | None = None,
) -> _PatchAffected:
    _verify_volley_patch_changes(cwd, changes, writable_roots=writable_roots)
    affected = _PatchAffected(added=[], modified=[], deleted=[], changes=[])
    for change in changes:
        try:
            change_type = change["type"]
            path = _safe_writable_path(cwd, writable_roots, change["path"])
            if change_type == "add":
                overwritten_content = path.read_text(encoding="utf-8") if path.is_file() else None
                content = "\n".join(change["content"]) + "\n"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                affected.added.append(change["path"])
                affected.changes.append(
                    {
                        "path": change["path"],
                        "type": "add",
                        "content": content,
                        "overwritten_content": overwritten_content,
                        "additions": len(content.splitlines()),
                        "deletions": 0,
                    }
                )
            elif change_type == "delete":
                if not path.exists():
                    raise ValueError(f"Failed to delete file {path}: No such file or directory (os error 2)")
                if path.is_dir():
                    raise ValueError(f"Failed to delete file {path}")
                content = path.read_text(encoding="utf-8")
                path.unlink()
                affected.deleted.append(change["path"])
                affected.changes.append(
                    {
                        "path": change["path"],
                        "type": "delete",
                        "content": content,
                        "additions": 0,
                        "deletions": len(content.splitlines()),
                    }
                )
            elif change_type == "update":
                if not change["chunks"]:
                    raise ValueError(f"Invalid patch hunk: Update file hunk for path '{change['path']}' is empty")
                if not path.exists():
                    raise ValueError(f"Failed to read file to update {path}: No such file or directory (os error 2)")
                if path.is_dir():
                    raise ValueError(f"Failed to read file to update {path}: path is a directory")
                original = path.read_text(encoding="utf-8")
                updated = _apply_update_chunks(original, change["chunks"], path=change["path"])
                move_path = change.get("move_path")
                target_path = move_path or change["path"]
                additions, deletions = _line_delta_counts(original, updated)
                unified_diff = _unified_diff_text(original, updated, change["path"], target_path)
                overwritten_move_content = None
                if move_path:
                    target = _safe_writable_path(cwd, writable_roots, move_path)
                    overwritten_move_content = target.read_text(encoding="utf-8") if target.is_file() else None
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(updated, encoding="utf-8")
                    if path.is_dir():
                        raise ValueError(f"Failed to remove original {path}")
                    path.unlink()
                    affected.modified.append(move_path)
                else:
                    path.write_text(updated, encoding="utf-8")
                    affected.modified.append(change["path"])
                affected.changes.append(
                    {
                        "path": change["path"],
                        "type": "update",
                        "move_path": move_path,
                        "unified_diff": unified_diff,
                        "old_content": original,
                        "new_content": updated,
                        "overwritten_move_content": overwritten_move_content,
                        "additions": additions,
                        "deletions": deletions,
                    }
                )
            else:
                raise ValueError(f"unknown patch change type: {change_type}")
        except Exception as exc:
            if affected.changes:
                raise _PatchApplicationError(str(exc), affected) from exc
            raise
    return affected


def _verify_volley_patch_changes(
    cwd: Path,
    changes: list[dict[str, Any]],
    *,
    writable_roots: list[Path] | None,
) -> None:
    if not changes:
        raise ValueError("No files were modified.")
    for change in changes:
        change_type = change["type"]
        path = _safe_writable_path(cwd, writable_roots, change["path"])
        if change_type == "add":
            continue
        if change_type == "delete":
            if not path.exists():
                raise ValueError(f"Failed to delete file {path}: No such file or directory (os error 2)")
            if path.is_dir():
                raise ValueError(f"Failed to delete file {path}")
            path.read_text(encoding="utf-8")
            continue
        if change_type == "update":
            if not change["chunks"]:
                raise ValueError(f"Invalid patch hunk: Update file hunk for path '{change['path']}' is empty")
            if not path.exists():
                raise ValueError(f"Failed to read file to update {path}: No such file or directory (os error 2)")
            if path.is_dir():
                raise ValueError(f"Failed to read file to update {path}: path is a directory")
            original = path.read_text(encoding="utf-8")
            _apply_update_chunks(original, change["chunks"], path=change["path"])
            move_path = change.get("move_path")
            if move_path:
                _safe_writable_path(cwd, writable_roots, move_path)
            continue
        raise ValueError(f"unknown patch change type: {change_type}")


def _unified_diff_text(original: str, updated: str, old_path: str, new_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=old_path,
            tofile=new_path,
        )
    )


def _line_delta_counts(original: str, updated: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in difflib.ndiff(original.splitlines(), updated.splitlines()):
        if line.startswith("+ "):
            additions += 1
        elif line.startswith("- "):
            deletions += 1
    return additions, deletions


def _apply_update_chunks(original: str, chunks: list[dict[str, Any]], *, path: str) -> str:
    original_lines = original.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements = _compute_update_replacements(original_lines, path, chunks)
    new_lines = _apply_update_replacements(original_lines, replacements)
    if not new_lines or new_lines[-1] != "":
        new_lines.append("")
    return "\n".join(new_lines)


def _compute_update_replacements(
    original_lines: list[str],
    path: str,
    chunks: list[dict[str, Any]],
) -> list[tuple[int, int, list[str]]]:
    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0

    for chunk in chunks:
        change_context = chunk.get("change_context")
        if change_context is not None:
            context_index = _seek_sequence(original_lines, [change_context], line_index, eof=False)
            if context_index is None:
                raise ValueError(f"Failed to find context '{change_context}' in {path}")
            line_index = context_index + 1

        old_lines = list(chunk["old_lines"])
        if not old_lines:
            insertion_index = (
                len(original_lines) - 1
                if original_lines and original_lines[-1] == ""
                else len(original_lines)
            )
            replacements.append((insertion_index, 0, list(chunk["new_lines"])))
            continue

        pattern = old_lines
        new_segment = list(chunk["new_lines"])
        found = _seek_sequence(original_lines, pattern, line_index, eof=bool(chunk["is_end_of_file"]))

        if found is None and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_segment and new_segment[-1] == "":
                new_segment = new_segment[:-1]
            found = _seek_sequence(original_lines, pattern, line_index, eof=bool(chunk["is_end_of_file"]))

        if found is None:
            raise ValueError(f"Failed to find expected lines in {path}:\n" + "\n".join(chunk["old_lines"]))

        replacements.append((found, len(pattern), new_segment))
        line_index = found + len(pattern)

    replacements.sort(key=lambda replacement: replacement[0])
    return replacements


def _apply_update_replacements(
    lines: list[str],
    replacements: list[tuple[int, int, list[str]]],
) -> list[str]:
    updated = list(lines)
    for start_index, old_len, new_segment in reversed(replacements):
        del updated[start_index : start_index + old_len]
        updated[start_index:start_index] = new_segment
    return updated


def _seek_sequence(lines: list[str], pattern: list[str], start: int, *, eof: bool) -> int | None:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = len(lines) - len(pattern) if eof else start
    search_end = len(lines) - len(pattern)

    for index in range(search_start, search_end + 1):
        if lines[index : index + len(pattern)] == pattern:
            return index
    for index in range(search_start, search_end + 1):
        if all(lines[index + offset].rstrip() == item.rstrip() for offset, item in enumerate(pattern)):
            return index
    for index in range(search_start, search_end + 1):
        if all(lines[index + offset].strip() == item.strip() for offset, item in enumerate(pattern)):
            return index
    for index in range(search_start, search_end + 1):
        if all(
            _normalize_seek_line(lines[index + offset]) == _normalize_seek_line(item)
            for offset, item in enumerate(pattern)
        ):
            return index
    return None


_SEEK_NORMALIZATION_TABLE = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u00a0": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u202f": " ",
        "\u205f": " ",
        "\u3000": " ",
    }
)


def _normalize_seek_line(value: str) -> str:
    return value.strip().translate(_SEEK_NORMALIZATION_TABLE)


def _diff_patch_touched_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            if len(parts) >= 4:
                paths.extend([_strip_git_diff_prefix(parts[-2]), _strip_git_diff_prefix(parts[-1])])
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            path = _diff_file_header_path(line[4:])
            if path and path != "/dev/null":
                paths.append(_strip_git_diff_prefix(path))
    return list(dict.fromkeys(path for path in paths if path and path != "/dev/null"))


def _git_apply_invocation(cwd: Path, patch_path: Path) -> tuple[Path, list[str]]:
    git_root = _nearest_git_root(cwd)
    if git_root is None:
        return cwd, ["git", "apply", str(patch_path)]
    try:
        relative_cwd = cwd.resolve().relative_to(git_root.resolve())
    except ValueError:
        return cwd, ["git", "apply", str(patch_path)]
    argv = ["git", "apply"]
    if str(relative_cwd) not in {"", "."}:
        argv.append(f"--directory={relative_cwd.as_posix()}")
    argv.append(str(patch_path))
    return git_root, argv


def _nearest_git_root(cwd: Path) -> Path | None:
    path = cwd.resolve()
    while True:
        if (path / ".git").exists():
            return path
        if path.parent == path:
            return None
        path = path.parent


def _diff_file_header_path(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = value.split()
        return parts[0] if parts else ""
    return value.split("\t", 1)[0].strip()


def _strip_git_diff_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _workspace_write_roots(
    cwd: Path,
    writable_roots: tuple[Path | str, ...],
    *,
    include_tmp_roots: bool = False,
    exclude_tmpdir_env_var: bool = False,
    exclude_slash_tmp: bool = False,
) -> list[Path]:
    roots = [cwd.resolve()]
    for root in writable_roots:
        path = Path(root).expanduser()
        if not path.is_absolute():
            path = cwd / path
        roots.append(path.resolve())
    if include_tmp_roots and not exclude_slash_tmp:
        slash_tmp = Path("/tmp")
        if slash_tmp.is_absolute() and slash_tmp.is_dir():
            roots.append(slash_tmp.resolve())
    if include_tmp_roots and not exclude_tmpdir_env_var:
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir:
            tmpdir_path = Path(tmpdir).expanduser()
            if tmpdir_path.is_absolute():
                try:
                    roots.append(tmpdir_path.resolve())
                except OSError:
                    pass
    return list(dict.fromkeys(roots))


def _safe_writable_path(cwd: Path, writable_roots: list[Path] | None, path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve()
    if writable_roots is not None and not any(_path_is_within(resolved, root) for root in writable_roots):
        raise ValueError(f"path escapes writable workspace: {path}")
    return resolved


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _format_apply_patch_success(affected: _PatchAffected) -> str:
    lines = ["Success. Updated the following files:"]
    lines.extend(f"A {path}" for path in affected.added)
    lines.extend(f"M {path}" for path in affected.modified)
    lines.extend(f"D {path}" for path in affected.deleted)
    return "\n".join(lines) + "\n"
