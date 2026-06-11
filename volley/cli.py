from __future__ import annotations

import argparse
import codecs
import datetime as _dt
import json
import os
import queue
import re
import shlex
import shutil
import select
import sys
try:
    import termios
except ImportError:  # pragma: no cover - Windows has no termios; raw-TTY paths degrade to line input.
    termios = None  # type: ignore[assignment]
import textwrap
import threading
import time
import tomllib
import unicodedata

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .core import VolleySession, SteerInputError, TurnInterrupted
from .goal import GOAL_STATUS_FROM_WIRE, GoalStatus, goal_summary
from .model import default_model_client
from .state import load_rollout_records, parse_command_actions, reconstruct_history_from_rollout
from .types import VolleyConfig, VolleyEvent, _model_catalog_info, normalize_service_tier
from . import types as _types


def _default_cli_syntax_theme() -> str:
    configured = os.environ.get("PY_VOLLEY_SYNTAX_THEME")
    if configured:
        return configured
    colorfgbg = os.environ.get("COLORFGBG", "")
    try:
        bg = int(colorfgbg.split(";")[-1])
    except (TypeError, ValueError):
        bg = -1
    if bg < 0 or bg in {7, 15} or bg >= 230:
        return "catppuccin-latte"
    return "catppuccin-mocha"


_CLI_SYNTAX_THEME = _default_cli_syntax_theme()
_TERMINAL_RESIZE_REFLOW_FALLBACK_MAX_ROWS = 1000


def _volley_module_name_from(module_name: str) -> str:
    module_name = module_name.split(".cli", 1)[0]
    return module_name or "volley"


def _volley_module_name() -> str:
    return _volley_module_name_from(__name__)


def _volley_module_prog(*args: str) -> str:
    return " ".join(["python", "-m", _volley_module_name(), *args])


def _set_raw_keep_opost(fd: int) -> None:
    """Like tty.setraw, but keep OPOST so output \\n is still translated to \\r\\n.

    Without OPOST, lines printed while a bottom-anchored prompt is rendered
    leave the cursor at an arbitrary column, causing the prompt to "jump"
    to the middle of the screen on the next redraw.
    """
    mode = termios.tcgetattr(fd)
    # iflag
    mode[0] &= ~(
        termios.BRKINT
        | termios.ICRNL
        | termios.INPCK
        | termios.ISTRIP
        | termios.IXON
    )
    # oflag — intentionally leave OPOST set
    # cflag
    mode[2] &= ~(termios.CSIZE | termios.PARENB)
    mode[2] |= termios.CS8
    # lflag
    mode[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        _print_cli_error(exc)
        return 1


def _main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if len(raw_argv) >= 2 and raw_argv[0] == "exec" and raw_argv[1] == "resume":
        return _main_exec_resume(raw_argv[2:])
    if len(raw_argv) >= 2 and raw_argv[0] == "exec" and raw_argv[1] == "fork":
        return _main_exec_fork(raw_argv[2:])
    if raw_argv and raw_argv[0] == "resume":
        return _main_resume_chat(raw_argv[1:], fork=False)
    if raw_argv and raw_argv[0] == "fork":
        return _main_resume_chat(raw_argv[1:], fork=True)
    if raw_argv and raw_argv[0] == "chat":
        return _main_chat(raw_argv[1:], prog=_volley_module_prog("chat"))
    if _should_route_to_chat(raw_argv):
        return _main_chat(raw_argv)

    parser = argparse.ArgumentParser(prog=_volley_module_prog())
    subparsers = parser.add_subparsers(dest="command")
    chat_parser = subparsers.add_parser("chat")
    chat_parser.add_argument("prompt", nargs="?")
    _add_exec_options(chat_parser)
    resume_parser = subparsers.add_parser("resume", help="resume a previous interactive session")
    resume_parser.add_argument("session_id", nargs="?")
    resume_parser.add_argument("--last", action="store_true")
    resume_parser.add_argument("--all", action="store_true", dest="all_cwds")
    _add_exec_options(resume_parser)
    fork_parser = subparsers.add_parser("fork", help="fork a previous interactive session")
    fork_parser.add_argument("session_id", nargs="?")
    fork_parser.add_argument("--last", action="store_true")
    fork_parser.add_argument("--all", action="store_true", dest="all_cwds")
    _add_exec_options(fork_parser)
    login_parser = subparsers.add_parser("login", help="sign in to ChatGPT for Volley")
    login_parser.add_argument("login_command", nargs="?", choices=["status"])
    login_parser.add_argument("--json", action="store_true", dest="login_json")
    login_parser.add_argument("--device-auth", action="store_true", help="sign in with a browser device code")
    login_parser.add_argument("--with-api-key", action="store_true", dest="with_api_key", help="read an OpenAI API key from stdin")
    login_parser.add_argument("--api-key", action="store_true", dest="with_api_key", help=argparse.SUPPRESS)
    login_parser.add_argument("--no-open-browser", action="store_true", help="print the login URL instead of opening a browser")
    login_parser.add_argument("--experimental_issuer", help=argparse.SUPPRESS)
    login_parser.add_argument("--experimental_client-id", dest="experimental_client_id", help=argparse.SUPPRESS)
    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("prompt", nargs="?")
    _add_exec_options(exec_parser)

    args = parser.parse_args(raw_argv)

    if args.command == "chat":
        return _main_chat(raw_argv[1:], prog=_volley_module_prog("chat"))

    if args.command == "login":
        return _main_login(args)

    if args.command != "exec":
        parser.print_help(sys.stderr)
        return 2

    try:
        config = _build_exec_config(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return _run_session(
        VolleySession(config),
        _read_prompt(args.prompt),
        json_events=args.json_events,
        color_mode=args.color,
    )


def _should_route_to_chat(raw_argv: list[str]) -> bool:
    if not raw_argv:
        return True
    if raw_argv[0] in {"-h", "--help", "exec", "resume", "fork", "login"}:
        return False
    return True


def _main_login(args: argparse.Namespace) -> int:
    from .auth import (
        CLIENT_ID,
        DEFAULT_OAUTH_ISSUER,
        auth_status,
        login_with_api_key,
        run_browser_login,
        run_device_code_login,
    )

    if args.login_command == "status":
        status = auth_status()
        if getattr(args, "login_json", False):
            print(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2))
            return 0 if status.get("logged_in") else 1
        print(f"Auth file: {status.get('auth_file')}")
        print(f"Mode: {status.get('auth_mode') or 'none'}")
        if status.get("has_chatgpt_tokens"):
            account = status.get("account_id") or "unknown"
            plan = status.get("plan_type") or "unknown"
            email = status.get("email") or "unknown"
            print(f"ChatGPT: logged in ({email}, {plan}, {account})")
        elif status.get("has_api_key"):
            print("API key: available")
        else:
            print(f"Not logged in. Run `{_volley_module_prog('login')}` once, or set OPENAI_API_KEY.")
            return 1
        if status.get("last_refresh"):
            print(f"Last refresh: {status['last_refresh']}")
        return 0

    issuer = getattr(args, "experimental_issuer", None) or DEFAULT_OAUTH_ISSUER
    client_id = getattr(args, "experimental_client_id", None) or CLIENT_ID
    if getattr(args, "login_json", False):
        print("--json is only supported for `login status`", file=sys.stderr)
        return 2
    if getattr(args, "with_api_key", False):
        if sys.stdin.isatty():
            print(
                "--with-api-key expects the API key on stdin. Try piping it, e.g. "
                "`printenv OPENAI_API_KEY | volley login --with-api-key`.",
                file=sys.stderr,
            )
            return 1
        print("Reading API key from stdin...", file=sys.stderr)
        api_key = sys.stdin.read().strip()
        if not api_key:
            print("No API key provided via stdin.", file=sys.stderr)
            return 1
        login_with_api_key(api_key)
        print("Successfully logged in", file=sys.stderr)
        return 0
    if getattr(args, "device_auth", False):
        run_device_code_login(issuer=issuer, client_id=client_id)
        print("Successfully logged in", file=sys.stderr)
        return 0

    def _print_login_start(port: int, url: str) -> None:
        print(
            f"Starting local login server on http://localhost:{port}.\n"
            "If your browser did not open, navigate to this URL to authenticate:\n\n"
            f"{url}\n\n"
            "On a remote or headless machine? Use `volley login --device-auth` instead.",
            file=sys.stderr,
        )

    run_browser_login(
        issuer=issuer,
        client_id=client_id,
        open_browser=not getattr(args, "no_open_browser", False),
        on_start=_print_login_start,
    )
    print("Successfully logged in", file=sys.stderr)
    return 0


def _main_exec_resume(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=_volley_module_prog("exec", "resume"))
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--all", action="store_true", dest="all_cwds")
    _add_exec_options(parser)
    args = parser.parse_args(argv)

    if args.last and args.prompt is None:
        args.prompt = args.session_id
        args.session_id = None

    try:
        config = _build_exec_config(args)
        rollout_path = _resolve_resume_rollout(args, config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if rollout_path is None:
        if args.session_id:
            print(f"No Volley rollout found for `{args.session_id}`", file=sys.stderr)
            return 1
        session = VolleySession(config)
    else:
        session = VolleySession.resume_from_rollout(rollout_path, config)
    return _run_session(session, _read_prompt(args.prompt), json_events=args.json_events, color_mode=args.color)


def _main_exec_fork(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=_volley_module_prog("exec", "fork"))
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--all", action="store_true", dest="all_cwds")
    _add_exec_options(parser)
    args = parser.parse_args(argv)

    if args.last and args.prompt is None:
        args.prompt = args.session_id
        args.session_id = None

    try:
        config = _build_exec_config(args)
        rollout_path = _resolve_resume_rollout(args, config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if rollout_path is None:
        selector = args.session_id or "--last"
        print(f"No Volley rollout found for `{selector}`", file=sys.stderr)
        return 1
    session = VolleySession.fork_from_rollout(rollout_path, config)
    return _run_session(session, _read_prompt(args.prompt), json_events=args.json_events, color_mode=args.color)


def _main_resume_chat(argv: list[str], *, fork: bool) -> int:
    name = "fork" if fork else "resume"
    parser = argparse.ArgumentParser(prog=_volley_module_prog(name))
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--all", action="store_true", dest="all_cwds")
    _add_exec_options(parser)
    args = parser.parse_args(argv)
    if args.json_events:
        print(f"`--json` is only supported for `exec`, not interactive {name}.", file=sys.stderr)
        return 2

    try:
        config = _build_exec_config(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if args.session_id or args.last:
            rollout_path = _resolve_resume_rollout(args, config)
        else:
            rollout_path = _prompt_rollout_picker(
                config,
                title="Fork a previous session" if fork else "Resume a previous session",
                all_cwds=args.all_cwds,
                color_mode=args.color,
            )
        if rollout_path is None:
            if args.session_id or args.last:
                selector = args.session_id or "--last"
                print(f"No Volley rollout found for `{selector}`", file=sys.stderr)
                return 1
            session = VolleySession(config)
        elif fork:
            session = VolleySession.fork_from_rollout(rollout_path, config)
        else:
            session = VolleySession.resume_from_rollout(rollout_path, config)
        return _run_chat(
            session,
            None,
            color_mode=args.color,
            replay_history=rollout_path is not None,
            history_source_path=rollout_path,
        )
    except Exception as exc:
        _print_cli_error(exc)
        return 1


def _main_chat(argv: list[str], *, prog: str | None = None) -> int:
    prog = prog or _volley_module_prog()
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("prompt", nargs="?")
    _add_exec_options(parser)
    args = parser.parse_args(argv)
    if args.json_events:
        print("`--json` is only supported for `exec`, not interactive chat.", file=sys.stderr)
        return 2

    try:
        config = _build_exec_config(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        session = VolleySession(config)
        initial_prompt = _normalize_optional_prompt(args.prompt)
        return _run_chat(session, initial_prompt, color_mode=args.color)
    except Exception as exc:
        _print_cli_error(exc)
        return 1


def _add_exec_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", "--experimental-json", action="store_true", dest="json_events")
    parser.add_argument("--model", "-m")
    parser.add_argument("--service-tier", choices=["fast", "priority", "flex"], default=None)
    parser.add_argument("--oss", action="store_true")
    parser.add_argument("--local-provider", dest="local_provider")
    parser.add_argument("--auth-mode", choices=["auto", "api-key", "api_key", "chatgpt"], default=None)
    parser.add_argument("--profile", "-p")
    parser.add_argument("--config", "-c", action="append", default=[], dest="config_overrides")
    parser.add_argument("--cd", "-C", dest="cwd")
    parser.add_argument("--sandbox", "-s", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--add-dir", action="append", default=[], dest="add_dirs")
    parser.add_argument("--ask-for-approval", choices=["untrusted", "on-failure", "on-request", "never"])
    bypass_group = parser.add_mutually_exclusive_group()
    bypass_group.add_argument("--dangerously-bypass-approvals-and-sandbox", "--yolo", action="store_true")
    bypass_group.add_argument("--full-auto", action="store_true", dest="removed_full_auto", help=argparse.SUPPRESS)
    parser.add_argument("--dangerously-bypass-hook-trust", action="store_true", dest="bypass_hook_trust")
    parser.add_argument("--ignore-user-config", action="store_true")
    parser.add_argument("--ignore-rules", action="store_true")
    parser.add_argument("--skip-git-repo-check", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--output-last-message", "-o")
    parser.add_argument("--output-schema")
    parser.add_argument("--image", "-i", action="append", default=[], dest="images")


def _build_exec_config(args: argparse.Namespace) -> VolleyConfig:
    if getattr(args, "removed_full_auto", False):
        print("warning: `--full-auto` is deprecated; use `--sandbox workspace-write` instead.", file=sys.stderr)
    output_schema = _load_output_schema(args.output_schema)
    cli_config = _load_cli_config(args)
    _configure_cli_syntax_theme(cli_config)
    oss_provider = _resolve_oss_provider(args, cli_config)
    model = _exec_model(args, cli_config, oss_provider)
    configured_provider = oss_provider or _string_config(cli_config, "model_provider")
    model_provider_id = configured_provider or _model_provider_for_model(model) or "openai"
    model_provider_config = _model_provider_config(cli_config, model_provider_id)
    sandbox = "danger-full-access" if args.dangerously_bypass_approvals_and_sandbox else args.sandbox
    approval_policy = "never" if args.dangerously_bypass_approvals_and_sandbox else args.ask_for_approval
    web_search = _web_search_settings(cli_config)
    auth_home = _string_config(cli_config, "auth_home")
    sandbox_workspace_write = cli_config.get("sandbox_workspace_write")
    sandbox_workspace_write_config = sandbox_workspace_write if isinstance(sandbox_workspace_write, dict) else {}
    writable_roots = [
        *_path_list_config(cli_config, "writable_roots"),
        *_path_list_config(sandbox_workspace_write_config, "writable_roots"),
        *args.add_dirs,
    ]
    config = VolleyConfig(
        model=model,
        model_provider_id=model_provider_id,
        session_source="exec" if getattr(args, "command", None) == "exec" else "cli",
        cwd=Path(args.cwd or _string_config(cli_config, "cwd") or "."),
        sandbox=sandbox or _sandbox_config(cli_config) or "workspace-write",
        approval_policy=approval_policy or _approval_config(cli_config) or "never",
        network_access="enabled"
        if _bool_config(sandbox_workspace_write_config, "network_access", False)
        else "restricted",
        writable_roots=tuple(Path(path) for path in writable_roots),
        exclude_tmpdir_env_var=_bool_config(sandbox_workspace_write_config, "exclude_tmpdir_env_var", False),
        exclude_slash_tmp=_bool_config(sandbox_workspace_write_config, "exclude_slash_tmp", False),
        volley_home=_default_volley_home(),
        auth_home=auth_home,
        auth_mode=_auth_mode_config(args, cli_config),
        chatgpt_base_url=_string_config(cli_config, "chatgpt_base_url"),
        openai_base_url=_string_config(cli_config, "openai_base_url") or _string_config(model_provider_config, "base_url"),
        gemini_base_url=_string_config(cli_config, "gemini_base_url")
        or _string_config(model_provider_config, "base_url"),
        json_events=args.json_events,
        output_last_message=args.output_last_message,
        skip_git_repo_check=args.skip_git_repo_check,
        ephemeral=args.ephemeral,
        include_web_search_tool=web_search[0],
        web_search_external_web_access=web_search[1],
        memory_tool_enabled=_bool_config(cli_config, "memory_tool_enabled", False)
        or _bool_nested_config(cli_config, ("features", "memory_tool"), False),
        memory_generate_memories=_bool_nested_config(cli_config, ("memories", "generate_memories"), True),
        memory_disable_on_external_context=_bool_nested_config(
            cli_config,
            ("memories", "disable_on_external_context"),
            _bool_nested_config(cli_config, ("memories", "no_memories_if_mcp_or_web_search"), False),
        ),
        use_memories=_bool_nested_config(cli_config, ("memories", "use_memories"), True),
        memory_max_raw_memories_for_consolidation=_int_nested_config(
            cli_config, ("memories", "max_raw_memories_for_consolidation"), 256
        ),
        memory_max_unused_days=_int_nested_config(cli_config, ("memories", "max_unused_days"), 30),
        memory_max_rollout_age_days=_int_nested_config(cli_config, ("memories", "max_rollout_age_days"), 10),
        memory_max_rollouts_per_startup=_int_nested_config(cli_config, ("memories", "max_rollouts_per_startup"), 2),
        memory_min_rollout_idle_hours=_int_nested_config(cli_config, ("memories", "min_rollout_idle_hours"), 6),
        model_reasoning_effort=_string_config(cli_config, "model_reasoning_effort"),
        model_reasoning_summary=_string_config(cli_config, "model_reasoning_summary"),
        model_verbosity=_string_config(cli_config, "model_verbosity"),
        service_tier=args.service_tier or _string_config(cli_config, "service_tier"),
        fast_mode_enabled=_bool_nested_config(cli_config, ("features", "fast_mode"), True),
        fast_default_opt_out=_bool_nested_config(
            cli_config,
            ("notices", "fast_default_opt_out"),
            _bool_config(cli_config, "fast_default_opt_out", False),
        ),
        account_plan_type=_local_account_plan_type(auth_home),
        model_stream_max_retries=_int_config(model_provider_config, "stream_max_retries"),
        show_raw_agent_reasoning=bool(oss_provider) or _bool_config(cli_config, "show_raw_agent_reasoning", False),
        bypass_hook_trust=args.bypass_hook_trust or _bool_config(cli_config, "bypass_hook_trust", False),
        include_environment_context=_bool_config(cli_config, "include_environment_context", True),
        include_permissions_instructions=_bool_config(cli_config, "include_permissions_instructions", True),
        goals_enabled=_bool_nested_config(
            cli_config,
            ("features", "goals"),
            _bool_config(cli_config, "goals", True),
        ),
        collaboration_mode=_collaboration_mode_config(cli_config),
        request_user_input_available_modes=_request_user_input_available_modes(cli_config),
        output_schema=output_schema,
        input_images=tuple(Path(path) for path in _parse_image_args(args.images)),
        remote_compaction=_remote_compaction_config(cli_config),
        terminal_resize_reflow_enabled=_bool_nested_config(cli_config, ("features", "terminal_resize_reflow"), True),
        terminal_resize_reflow_max_rows=_terminal_resize_reflow_max_rows_config(cli_config),
    )
    return config


def _run_session(session: VolleySession, prompt: str, *, json_events: bool, color_mode: str = "auto") -> int:
    if json_events:
        final = ""
        failed = False
        try:
            for event in session.stream(prompt):
                if event.type == "turn.completed":
                    final = str(event.payload.get("final_message", ""))
                elif event.type == "turn.failed":
                    failed = True
                print(event.to_json(), flush=True)
        except Exception as exc:
            if not failed:
                print(_session_failure_event(session, exc).to_json(), flush=True)
            return 1
        return 0 if final else 1

    return _run_session_human(session, prompt, color_mode=color_mode, print_final_to_stdout=True)


def _run_session_human(
    session: VolleySession,
    prompt: str,
    *,
    color_mode: str = "auto",
    print_final_to_stdout: bool,
    renderer: "_HumanEventRenderer | None" = None,
    install_request_user_input_provider: bool = True,
) -> int:
    if install_request_user_input_provider:
        _install_request_user_input_provider(
            session,
            _make_cli_request_user_input_provider(color_mode=color_mode),
        )
    renderer = renderer or _HumanEventRenderer(color_mode=color_mode)
    final = ""
    failed = False
    try:
        for event in session.stream(prompt):
            renderer.render(event)
            if event.type == "turn.completed":
                final = str(event.payload.get("final_message", ""))
            elif event.type == "turn.aborted":
                failed = True
            elif event.type == "turn.failed":
                failed = True
                renderer.render_error(str(event.payload.get("error") or "turn failed"))
    except TurnInterrupted:
        failed = True
        renderer.render_interrupted()
    except Exception as exc:
        if not failed:
            event = _session_failure_event(session, exc)
            renderer.render_error(str(event.payload.get("error") or "turn failed"))
    renderer.finish(final, print_to_stdout=print_final_to_stdout)
    return 0 if final else 1


def _run_goal_continuations_human(
    session: VolleySession,
    *,
    color_mode: str = "auto",
    queued_prompts: deque[str] | None = None,
) -> int:
    exit_status = 0
    while not queued_prompts:
        runtime = getattr(session, "goals", None)
        candidate = getattr(runtime, "continuation_item_if_active", None)
        if not callable(candidate) or candidate() is None:
            return exit_status
        renderer = _HumanEventRenderer(color_mode=color_mode)
        final = ""
        failed = False
        try:
            for event in session.stream_goal_continuation():
                renderer.render(event)
                if event.type == "turn.completed":
                    final = str(event.payload.get("final_message", ""))
                elif event.type in {"turn.aborted", "turn.failed"}:
                    failed = True
                    if event.type == "turn.failed":
                        renderer.render_error(str(event.payload.get("error") or "turn failed"))
        except TurnInterrupted:
            failed = True
            renderer.render_interrupted()
        except Exception as exc:
            failed = True
            renderer.render_error(_exception_display_message(exc))
        renderer.finish(final, print_to_stdout=False)
        if failed or not final:
            return 1
        goal = runtime.get_goal() if hasattr(runtime, "get_goal") else None
        if goal is None or getattr(goal, "status", None) != "active":
            return exit_status


def _run_chat(
    session: VolleySession,
    initial_prompt: str | None,
    *,
    color_mode: str = "auto",
    replay_history: bool = False,
    history_source_path: Path | None = None,
) -> int:
    prompt = initial_prompt
    printed_transcript = _render_resumed_transcript(
        session,
        source_path=history_source_path,
        color_mode=color_mode,
    ) if replay_history else False
    if not replay_history and initial_prompt is None:
        _render_chat_startup_panel(session, color_mode=color_mode)
    exit_status = 0
    queued_prompts: deque[str] = deque()
    composer_draft = _ComposerDraft()
    while True:
        if prompt is None and queued_prompts:
            prompt = queued_prompts.popleft()
        if prompt is None:
            if printed_transcript:
                print(file=sys.stderr, flush=True)
            prompt = _read_interactive_prompt(
                color_mode=color_mode,
                initial_text=composer_draft.text,
                initial_cursor=composer_draft.cursor,
            )
            if prompt is not None:
                composer_draft.text = ""
                composer_draft.cursor = 0
        if prompt is None:
            return exit_status
        slash_result = _handle_interactive_slash_command(
            session,
            prompt,
            color_mode=color_mode,
            queued_prompts=queued_prompts,
        )
        if slash_result.handled:
            if slash_result.session is not None:
                session = slash_result.session
            if slash_result.printed_transcript:
                printed_transcript = True
            if slash_result.exit:
                return slash_result.status
            if slash_result.run_goal_continuation and slash_result.prompt is None:
                continuation_status = _run_goal_continuations_human(
                    session,
                    color_mode=color_mode,
                    queued_prompts=queued_prompts,
                )
                if continuation_status != 0:
                    exit_status = continuation_status
                printed_transcript = True
            prompt = slash_result.prompt
            continue
        if prompt.strip():
            if _interactive_turn_controls_available():
                status = _run_session_human_interactive(
                    session,
                    prompt,
                    color_mode=color_mode,
                    queued_prompts=queued_prompts,
                    composer_draft=composer_draft,
                )
            else:
                renderer = _HumanEventRenderer(color_mode=color_mode)
                renderer.render_user_message(prompt)
                status = _run_session_human(
                    session,
                    prompt,
                    color_mode=color_mode,
                    print_final_to_stdout=False,
                    renderer=renderer,
                )
            if status != 0:
                exit_status = status
            printed_transcript = True
            continuation_status = _run_goal_continuations_human(
                session,
                color_mode=color_mode,
                queued_prompts=queued_prompts,
            )
            if continuation_status != 0:
                exit_status = continuation_status
        prompt = None


def _render_chat_startup_panel(session: VolleySession, *, color_mode: str = "auto") -> None:
    if not sys.stderr.isatty():
        return
    style = _AnsiStyle(_should_use_color(color_mode))
    rows = _chat_startup_panel_rows(session)
    if not rows:
        return
    print("\n".join(_chat_startup_panel_lines(rows, style)), file=sys.stderr, flush=True)


def _chat_startup_panel_lines(rows: list[tuple[str, str]], style: "_AnsiStyle") -> list[str]:
    label_width = max(_visible_len(label) for label, _value in rows)
    content_lines = [f"{style.muted(label.ljust(label_width))}  {value}" for label, value in rows]
    visible_width = max(_visible_len(line) for line in content_lines)
    logo_lines = _chat_startup_logo_lines(style)
    logo_width = max((_visible_len(line) for line in logo_lines), default=0)
    inner_width = max(visible_width, logo_width, 68)
    panel_width = inner_width + 4
    top = style.composer_border("╭" + "─" * max(0, panel_width - 2) + "╮")
    bottom = style.composer_border("╰" + "─" * max(0, panel_width - 2) + "╯")
    lines = [top]
    lines.append(_chat_startup_panel_line("", inner_width, style))
    for logo_line in logo_lines:
        lines.append(_chat_startup_panel_line(_center_visible(logo_line, inner_width), inner_width, style))
    lines.append(_chat_startup_panel_line("", inner_width, style))
    for line in content_lines:
        lines.append(_chat_startup_panel_line(line, inner_width, style))
    lines.append(bottom)
    return lines


def _chat_startup_logo_lines(style: "_AnsiStyle") -> list[str]:
    logo = [
        "██╗   ██╗ ██████╗ ██╗     ██╗     ███████╗██╗   ██╗",
        "██║   ██║██╔═══██╗██║     ██║     ██╔════╝╚██╗ ██╔╝",
        "╚██╗ ██╔╝██║   ██║██║     ██║     █████╗   ╚████╔╝ ",
        " ╚████╔╝ ██║   ██║██║     ██║     ██╔══╝    ╚██╔╝  ",
        "  ╚██╔╝  ╚██████╔╝███████╗███████╗███████╗   ██║   ",
    ]
    return [style.accent_bold(line) for line in logo]


def _chat_startup_panel_line(content: str, inner_width: int, style: "_AnsiStyle") -> str:
    return f"{style.composer_border('│')} {_pad_visible(content, inner_width)} {style.composer_border('│')}"


def _center_visible(text: str, width: int) -> str:
    text_width = _visible_len(text)
    if text_width >= width:
        return text
    left = (width - text_width) // 2
    return " " * left + text


def _chat_startup_panel_rows(session: VolleySession) -> list[tuple[str, str]]:
    config = session.config
    reasoning = config.resolved_reasoning() or {}
    effort = reasoning.get("effort") or "none"
    summary = reasoning.get("summary")
    model = f"{config.model} / {effort}"
    if summary:
        model = f"{model} / summary {summary}"
    auth = _session_auth_indicator(session, include_fallback=True) or "unknown"
    provider = config.model_provider_id
    if provider == "openai" and config.openai_base_url:
        provider = f"{provider} ({config.openai_base_url.rstrip('/')})"
    if provider == "gemini" and config.gemini_base_url:
        provider = f"{provider} ({config.gemini_base_url.rstrip('/')})"
    return [
        ("Model", model),
        ("Auth", auth),
        ("Provider", provider),
        ("Sandbox", f"{config.sandbox} · approvals {config.approval_policy}"),
        ("Thread", session.state.thread_id),
    ]


def _render_resumed_transcript(
    session: VolleySession,
    *,
    source_path: Path | None = None,
    color_mode: str = "auto",
) -> bool:
    lines: list[Any] = []
    renderer = _HumanEventRenderer(color_mode=color_mode, line_sink=lines.append)
    rendered = False
    records: list[dict[str, Any]] = []
    if source_path is not None:
        try:
            records = load_rollout_records(source_path)
        except Exception:
            records = []
    if not records:
        try:
            records = session.state.read_rollout_records()
        except Exception:
            records = []
    if records:
        rendered = _render_rollout_transcript_records(records, renderer)
    if not rendered:
        rendered = _render_history_transcript_items(session.state.history, renderer)
    if rendered:
        renderer._flush_exploration()
        _emit_resumed_transcript_lines(
            _retain_initial_history_replay_lines(
                lines,
                max_rows=_initial_history_replay_max_rows(session.config),
            )
        )
    return rendered


def _initial_history_replay_max_rows(config: VolleyConfig) -> int | None:
    if not config.terminal_resize_reflow_enabled:
        return None
    max_rows = config.terminal_resize_reflow_max_rows
    if max_rows == 0:
        return None
    if isinstance(max_rows, int) and max_rows > 0:
        return max_rows
    return _TERMINAL_RESIZE_REFLOW_FALLBACK_MAX_ROWS


def _retain_initial_history_replay_lines(lines: list[Any], *, max_rows: int | None) -> list[Any]:
    if max_rows is None or max_rows <= 0 or len(lines) <= max_rows:
        return list(lines)
    return list(lines[-max_rows:])


def _emit_resumed_transcript_lines(lines: list[Any]) -> None:
    for line in lines:
        if isinstance(line, _ConsoleWrite):
            sys.stderr.write(line.text)
            sys.stderr.flush()
        else:
            print(str(line), file=sys.stderr, flush=True)


def _render_rollout_transcript_records(records: list[dict[str, Any]], renderer: "_HumanEventRenderer") -> bool:
    rendered = False
    renderer._background_terminal_commands.update(_rollout_background_terminal_commands(records))
    for record in records:
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "response_item" and isinstance(payload, dict):
            rendered = _render_transcript_response_item(payload, renderer) or rendered
        elif record_type == "event_msg" and isinstance(payload, dict):
            rendered = _render_transcript_event_msg(payload, renderer) or rendered
    return rendered


def _render_history_transcript_items(history: list[dict[str, Any]], renderer: "_HumanEventRenderer") -> bool:
    rendered = False
    for item in history:
        if isinstance(item, dict):
            rendered = _render_transcript_response_item(item, renderer) or rendered
    return rendered


def _render_transcript_response_item(item: dict[str, Any], renderer: "_HumanEventRenderer") -> bool:
    item_type = item.get("type")
    if item_type == "message" and item.get("role") == "user":
        text = _user_item_text(item)
        if text and not _is_transcript_context_user_message(text):
            renderer.render_user_message(text)
            return True
        return False
    if item_type in {"message", "reasoning", "web_search_call"}:
        before = renderer._printed_any_cell
        renderer._render_item(item)
        return renderer._printed_any_cell != before
    return False


def _render_transcript_event_msg(payload: dict[str, Any], renderer: "_HumanEventRenderer") -> bool:
    event_type = str(payload.get("type") or "")
    if event_type in {"user_message", "agent_message", "web_search_end"}:
        return False
    if event_type == "plan_update":
        renderer.render(
            VolleyEvent(
                "tool.completed",
                {
                    "name": "update_plan",
                    "call_id": str(payload.get("call_id") or "resume-plan"),
                    "ok": True,
                    "metadata": {
                        "explanation": payload.get("explanation"),
                        "plan": payload.get("plan", []),
                    },
                },
            )
        )
        return True
    if event_type == "exec_command_end":
        command = _event_msg_command_text(payload)
        metadata = {
            "command": command,
            "exit_code": payload.get("exit_code"),
            "aggregated_output": payload.get("aggregated_output") or payload.get("stdout") or "",
            "output": payload.get("formatted_output") or payload.get("aggregated_output") or payload.get("stdout") or "",
            "stdout": payload.get("stdout") or payload.get("aggregated_output") or "",
            "stderr": payload.get("stderr") or "",
            "wall_time_seconds": _duration_seconds_from_payload(payload.get("duration")),
        }
        renderer.render(
            VolleyEvent(
                "tool.completed",
                {
                    "name": "exec_command",
                    "call_id": str(payload.get("call_id") or "resume-exec"),
                    "ok": _int_value(payload.get("exit_code")) == 0,
                    "metadata": metadata,
                },
            )
        )
        return True
    if event_type == "terminal_interaction":
        process_id = str(payload.get("process_id") or "")
        renderer.render_terminal_interaction(
            process_id,
            str(payload.get("stdin") or ""),
            command=renderer._background_terminal_commands.get(process_id),
        )
        return True
    if event_type == "patch_apply_end":
        success = bool(payload.get("success")) or str(payload.get("status") or "") == "completed"
        output = str(payload.get("stdout") if success else payload.get("stderr") or "")
        renderer.render(
            VolleyEvent(
                "tool.completed",
                {
                    "name": "apply_patch",
                    "call_id": str(payload.get("call_id") or "resume-patch"),
                    "ok": success,
                    "output": output,
                    "metadata": {"changes": payload.get("changes", [])},
                },
            )
        )
        return True
    if event_type == "view_image_tool_call":
        renderer.render(
            VolleyEvent(
                "tool.completed",
                {
                    "name": "view_image",
                    "call_id": str(payload.get("call_id") or "resume-image"),
                    "ok": True,
                    "metadata": {"path": payload.get("path")},
                },
            )
        )
        return True
    if event_type == "context_compacted":
        renderer.render_info_message("Context compacted")
        return True
    if event_type == "warning":
        message = str(payload.get("message") or "")
        if message:
            renderer.render(VolleyEvent("warning", {"message": message}))
            return True
    if event_type in {"warning", "error", "stream_error"}:
        message = str(payload.get("message") or payload.get("error") or "")
        if message:
            renderer.render_info_message(message)
            return True
    return False


def _event_msg_command_text(payload: dict[str, Any]) -> str:
    command = payload.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command if part is not None)
    if isinstance(command, str):
        return command
    return ""


def _rollout_background_terminal_commands(records: list[dict[str, Any]]) -> dict[str, str]:
    commands: dict[str, str] = {}
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") not in {"exec_command_begin", "exec_command_end"}:
            continue
        process_id = payload.get("process_id")
        if process_id is None:
            continue
        command = _event_msg_command_text(payload)
        if command:
            commands[str(process_id)] = _command_display(command)
    return commands


def _duration_seconds_from_payload(raw: Any) -> float:
    if isinstance(raw, dict):
        try:
            return float(raw.get("secs") or 0) + float(raw.get("nanos") or 0) / 1_000_000_000.0
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_transcript_context_user_message(text: str) -> bool:
    stripped = text.strip()
    return (
        _is_startup_context_preview(stripped)
        or stripped.startswith("<hook_context>")
        or stripped.startswith("<turn_aborted>")
        or stripped.startswith("<subagent_notification>")
        or stripped.startswith("<volley_memory>")
    )


def _run_session_human_interactive(
    session: VolleySession,
    prompt: str,
    *,
    color_mode: str = "auto",
    queued_prompts: deque[str] | None = None,
    composer_draft: _ComposerDraft | None = None,
) -> int:
    output: "queue.Queue[str]" = queue.Queue()
    status_tracker = _LiveTurnStatus()
    renderer = _HumanEventRenderer(color_mode=color_mode, line_sink=output.put, status_tracker=status_tracker)
    renderer.render_user_message(prompt)
    request_user_input_bridge = _RequestUserInputBridge()
    _install_request_user_input_provider(
        session,
        _make_cli_request_user_input_provider(
            color_mode=color_mode,
            bridge=request_user_input_bridge,
        ),
    )

    status: dict[str, int] = {"code": 1}

    def worker() -> None:
        try:
            status["code"] = _run_session_human(
                session,
                prompt,
                color_mode=color_mode,
                print_final_to_stdout=False,
                renderer=renderer,
                install_request_user_input_provider=False,
            )
        except Exception as exc:
            renderer.render_error(_exception_display_message(exc))
            status["code"] = 1

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _drain_output_queue(output)
    interrupted = False
    next_output_drain_at = 0.0
    output_drain_interval = 0.16
    with _TurnInputReader(
        enabled=sys.stdin.isatty() and sys.stderr.isatty(),
        color_mode=color_mode,
        initial_text=composer_draft.text if composer_draft is not None else "",
        initial_cursor=composer_draft.cursor if composer_draft is not None else None,
    ) as reader:
        reader.set_status(status_tracker.snapshot(session))
        while thread.is_alive():
            now = time.monotonic()
            if now >= next_output_drain_at:
                _drain_output_queue(output, input_reader=reader)
                next_output_drain_at = now + output_drain_interval
            if not reader.output_partial_line_open and output.empty():
                reader.set_status(status_tracker.snapshot(session))
            request = request_user_input_bridge.take_pending()
            if request is not None:
                reader.suspend()
                try:
                    response = _prompt_request_user_input(request.questions, color_mode=color_mode)
                finally:
                    reader.resume()
                request.resolve(response)
            for action in reader.poll():
                if action.kind == "interrupt" and not interrupted:
                    interrupted = True
                    pending_interrupt_prompts: list[str] = []
                    pending_interrupt_prompts = session.pop_pending_input_prompts_for_interrupt()
                    if pending_interrupt_prompts:
                        merged_prompt = "\n".join(pending_interrupt_prompts).strip()
                        if merged_prompt:
                            if queued_prompts is not None:
                                queued_prompts.appendleft(merged_prompt)
                            else:
                                session.queue_input_for_next_turn(merged_prompt)
                    session.interrupt()
                    if pending_interrupt_prompts:
                        renderer.render_pending_steer_interrupt()
                    else:
                        renderer.render_interrupted()
                    _drain_output_queue(output, input_reader=reader)
                    reader.render()
                elif action.kind == "submit" and action.text.strip():
                    if composer_draft is not None:
                        composer_draft.text = ""
                        composer_draft.cursor = 0
                    accepted = _submit_turn_input(session, action.text, queued_prompts=queued_prompts)
                    renderer.render_pending_input_preview(action.text, active=accepted)
                    _drain_output_queue(output, input_reader=reader)
            thread.join(timeout=0.03)
        if composer_draft is not None:
            composer_draft.text = reader.draft_text()
            composer_draft.cursor = reader.draft_cursor()
    thread.join(timeout=0.1)
    _drain_output_queue(output)
    outcome = "interrupted" if interrupted else "completed" if status["code"] == 0 else "failed"
    _print_finished_turn_status(
        status_tracker,
        session,
        color_mode=color_mode,
        outcome=outcome,
    )
    return status["code"]


def _run_compact_human_interactive(
    session: VolleySession,
    *,
    color_mode: str = "auto",
    queued_prompts: deque[str] | None = None,
) -> int:
    output: "queue.Queue[str]" = queue.Queue()
    status_tracker = _LiveTurnStatus(header="Compacting")
    renderer = _HumanEventRenderer(color_mode=color_mode, line_sink=output.put, status_tracker=status_tracker)
    completed = {"value": False}
    failed = {"value": False}

    def worker() -> None:
        try:
            for event in session.stream_compact():
                if event.type == "item.completed" and event.payload.get("compact"):
                    status_tracker.update(event)
                    continue
                renderer.render(event)
                if event.type == "context_compaction.completed":
                    completed["value"] = True
                elif event.type == "turn.failed":
                    failed["value"] = True
                    renderer.render_error(str(event.payload.get("error") or "turn failed"))
        except TurnInterrupted:
            failed["value"] = True
            renderer.render_interrupted()
        except Exception as exc:
            if not failed["value"]:
                event = _session_failure_event(session, exc)
                renderer.render_error(str(event.payload.get("error") or "turn failed"))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _drain_output_queue(output)
    interrupted = False
    with _TurnInputReader(enabled=sys.stdin.isatty() and sys.stderr.isatty(), color_mode=color_mode) as reader:
        reader.set_status(status_tracker.snapshot(session))
        while thread.is_alive():
            _drain_output_queue(output, input_reader=reader)
            if not reader.output_partial_line_open and output.empty():
                reader.set_status(status_tracker.snapshot(session))
            for action in reader.poll():
                if action.kind == "interrupt" and not interrupted:
                    interrupted = True
                    session.interrupt()
                    renderer.render_interrupted()
                    _drain_output_queue(output, input_reader=reader)
                    reader.render()
                elif action.kind == "submit" and action.text.strip():
                    accepted = _submit_turn_input(session, action.text, queued_prompts=queued_prompts)
                    renderer.render_pending_input_preview(action.text, active=accepted)
                    _drain_output_queue(output, input_reader=reader)
            thread.join(timeout=0.03)
    thread.join(timeout=0.1)
    _drain_output_queue(output)
    outcome = "interrupted" if interrupted else "compacted" if completed["value"] and not failed["value"] else "failed"
    _print_finished_turn_status(
        status_tracker,
        session,
        color_mode=color_mode,
        outcome=outcome,
    )
    return 0 if completed["value"] else 1


def _submit_turn_input(session: VolleySession, text: str, *, queued_prompts: deque[str] | None = None) -> bool:
    slash = _parse_interactive_slash(text)
    if slash is not None and not slash.command.available_during_task:
        if queued_prompts is not None:
            queued_prompts.append(text)
        else:
            session.queue_input_for_next_turn(text)
        return False
    parsed_name = _parse_slash_name(text.lstrip())
    if slash is None and parsed_name is not None and "/" not in parsed_name[0]:
        if queued_prompts is not None:
            queued_prompts.append(text)
        else:
            session.queue_input_for_next_turn(text)
        return False
    try:
        session.steer_input(text)
        return True
    except SteerInputError:
        if queued_prompts is not None:
            queued_prompts.append(text)
        else:
            session.queue_input_for_next_turn(text)
        return False


def _set_session_collaboration_mode(session: VolleySession, mode: str) -> None:
    config = replace(session.config, collaboration_mode=mode)
    session.config = config
    session.state.config = config
    session.tools.config = config


_MODEL_PICKER_VISIBLE_SLUGS = ("gpt-5.5", "gemini-3.5-flash")


def _list_known_models() -> list[dict[str, Any]]:
    _model_catalog_info("__warmup__")
    cache = getattr(_types, "_MODEL_CATALOG_CACHE", None) or {}
    out: list[dict[str, Any]] = []
    for slug in _MODEL_PICKER_VISIBLE_SLUGS:
        entry = cache.get(slug) or _model_catalog_info(slug)
        if not entry:
            continue
        if entry.get("visibility") == "hide":
            continue
        out.append(entry)
    return out


def _model_supported_efforts(model: str) -> list[str]:
    info = _model_catalog_info(model)
    raw = info.get("supported_reasoning_levels")
    efforts: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("effort"), str):
                efforts.append(item["effort"])
            elif isinstance(item, str):
                efforts.append(item)
    return efforts


def _model_provider_for_model(model: str) -> str | None:
    info = _model_catalog_info(model)
    provider = info.get("provider")
    if not provider and info:
        return "openai"
    return provider if isinstance(provider, str) and provider else None


def _set_session_model(session: VolleySession, model: str, effort: str | None) -> None:
    provider = _model_provider_for_model(model) or session.config.model_provider_id
    config = replace(
        session.config,
        model=model,
        model_provider_id=provider,
        model_reasoning_effort=effort,
    )
    session.config = config
    session.state.config = config
    session.tools.config = config
    session.model_client = default_model_client(config)


def _set_session_service_tier(session: VolleySession, service_tier: str | None) -> None:
    config = replace(
        session.config,
        service_tier=normalize_service_tier(service_tier),
        fast_default_opt_out=service_tier is None,
    )
    session.config = config
    session.state.config = config
    session.tools.config = config


def _interactive_model_picker(
    models: list[dict[str, Any]],
    current_model: str,
    current_effort: str,
    *,
    color_mode: str = "auto",
) -> tuple[str, str] | None:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        for i, entry in enumerate(models, 1):
            slug = entry.get("slug", "")
            efforts = _model_supported_efforts(slug) or [str(entry.get("default_reasoning_level") or "medium")]
            marker = " *" if slug == current_model else ""
            print(f"  [{i}] {slug}{marker}  efforts: {', '.join(efforts)}", file=sys.stderr, flush=True)
        return None

    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
    except Exception:
        return None

    style = _AnsiStyle(_should_use_color(color_mode))
    rows: list[tuple[str, list[str]]] = []
    for entry in models:
        slug = str(entry.get("slug", ""))
        efforts = _model_supported_efforts(slug)
        if not efforts:
            default_level = entry.get("default_reasoning_level")
            efforts = [str(default_level)] if default_level else ["medium"]
        rows.append((slug, efforts))

    selected = next((i for i, (s, _e) in enumerate(rows) if s == current_model), 0)
    effort_idx = [0] * len(rows)
    for i, (slug, efforts) in enumerate(rows):
        if slug == current_model and current_effort in efforts:
            effort_idx[i] = efforts.index(current_effort)
        else:
            default_level = str(_model_catalog_info(slug).get("default_reasoning_level") or "medium")
            if default_level in efforts:
                effort_idx[i] = efforts.index(default_level)

    rendered_rows = 0

    def render(lines: list[str]) -> None:
        nonlocal rendered_rows
        cols = _terminal_columns()
        _clear_prompt_lines(rendered_rows)
        print("\n".join(lines), end="", file=sys.stderr, flush=True)
        rendered_rows = _prompt_screen_rows(lines, cols)

    def clear() -> None:
        nonlocal rendered_rows
        _clear_prompt_lines(rendered_rows)
        rendered_rows = 0

    def lines() -> list[str]:
        out = [
            f"{style.bold('Select model')}  (current: {current_model} / {current_effort})",
            f"{style.dim('  ↑/↓ model  ←/→ reasoning effort  Enter confirm  Esc cancel')}",
            "",
        ]
        for i, (slug, efforts) in enumerate(rows):
            cur_eff = efforts[effort_idx[i]]
            if len(efforts) > 1:
                left = "◂" if effort_idx[i] > 0 else " "
                right = "▸" if effort_idx[i] < len(efforts) - 1 else " "
                eff_part = f"{left} {cur_eff} {right}"
            else:
                eff_part = f"  {cur_eff}  "
            marker = "*" if slug == current_model else " "
            line = f"  {marker} {slug:<20} {eff_part}"
            if i == selected:
                line = style.bold("> " + line.lstrip())
            else:
                line = "  " + line.lstrip()
            out.append(line)
        return out

    try:
        _set_raw_keep_opost(fd)
        pending = b""
        while True:
            render(lines())
            chunk, pending = _read_tty_chunk(fd, pending)
            if chunk == b"":
                return None
            if chunk == b"\x03":
                return None
            if chunk in {b"\r", b"\n"}:
                slug, efforts = rows[selected]
                return slug, efforts[effort_idx[selected]]
            if chunk == b"\x1b":
                sequence, pending = _read_escape_sequence(fd, pending)
                if sequence == b"\x1b":
                    return None
                if sequence in {b"\x1b[A", b"\x1bOA"}:
                    selected = (selected - 1) % len(rows)
                elif sequence in {b"\x1b[B", b"\x1bOB"}:
                    selected = (selected + 1) % len(rows)
                elif sequence in {b"\x1b[D", b"\x1bOD"}:
                    _slug, efforts = rows[selected]
                    if len(efforts) > 1:
                        effort_idx[selected] = (effort_idx[selected] - 1) % len(efforts)
                elif sequence in {b"\x1b[C", b"\x1bOC"}:
                    _slug, efforts = rows[selected]
                    if len(efforts) > 1:
                        effort_idx[selected] = (effort_idx[selected] + 1) % len(efforts)
                continue
            if chunk == b"q":
                return None
    except KeyboardInterrupt:
        return None
    finally:
        clear()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass


def _handle_model_slash(session: VolleySession, rest: str, *, color_mode: str = "auto") -> None:
    tokens = rest.split()
    current_model = session.config.model
    current_effort = session.config.model_reasoning_effort or _model_catalog_info(current_model).get("default_reasoning_level") or "medium"
    models = _list_known_models()

    if not tokens:
        if not models:
            print("No model catalog entries found.", file=sys.stderr, flush=True)
            return
        choice = _interactive_model_picker(models, current_model, current_effort, color_mode=color_mode)
        if choice is None:
            print("Cancelled.", file=sys.stderr, flush=True)
            return
        slug, effort = choice
        _set_session_model(session, slug, effort)
        print(f"Model set to {slug} (reasoning effort: {effort}).", file=sys.stderr, flush=True)
        return

    if tokens[0] in {"effort", "reasoning"} and len(tokens) == 2:
        effort = tokens[1]
        supported = _model_supported_efforts(current_model)
        if supported and effort not in supported:
            print(f"Effort '{effort}' not supported by {current_model}. Supported: {', '.join(supported)}", file=sys.stderr, flush=True)
            return
        _set_session_model(session, current_model, effort)
        print(f"Reasoning effort set to {effort} (model: {current_model}).", file=sys.stderr, flush=True)
        return

    name = tokens[0]
    info = _model_catalog_info(name)
    known_slugs = {str(e.get("slug", "")) for e in models}
    if known_slugs and name not in known_slugs:
        print(f"Unknown model '{name}'. Known: {', '.join(sorted(known_slugs))}", file=sys.stderr, flush=True)
        return
    if len(tokens) == 1:
        supported = _model_supported_efforts(name)
        if current_effort in supported:
            effort = current_effort
        else:
            effort = str(info.get("default_reasoning_level") or "medium")
    elif len(tokens) == 2:
        effort = tokens[1]
        supported = _model_supported_efforts(name)
        if supported and effort not in supported:
            print(f"Effort '{effort}' not supported by {name}. Supported: {', '.join(supported)}", file=sys.stderr, flush=True)
            return
    else:
        print("Usage: /model | /model <name> [<effort>] | /model effort <effort>", file=sys.stderr, flush=True)
        return
    _set_session_model(session, name, effort)
    print(f"Model set to {name} (reasoning effort: {effort}).", file=sys.stderr, flush=True)


class _RequestUserInputRequest:
    def __init__(self, questions: list[dict[str, Any]]) -> None:
        self.questions = questions
        self._event = threading.Event()
        self._response: dict[str, Any] | None = None

    def resolve(self, response: dict[str, Any] | None) -> None:
        self._response = response
        self._event.set()

    def wait(self) -> dict[str, Any] | None:
        self._event.wait()
        return self._response


class _RequestUserInputBridge:
    def __init__(self) -> None:
        self._requests: "queue.Queue[_RequestUserInputRequest]" = queue.Queue()

    def ask(self, questions: list[dict[str, Any]]) -> dict[str, Any] | None:
        request = _RequestUserInputRequest(questions)
        self._requests.put(request)
        return request.wait()

    def take_pending(self) -> _RequestUserInputRequest | None:
        try:
            return self._requests.get_nowait()
        except queue.Empty:
            return None


def _make_cli_request_user_input_provider(
    *,
    color_mode: str,
    bridge: _RequestUserInputBridge | None = None,
) -> Callable[[list[dict[str, Any]]], dict[str, Any] | None]:
    def provider(questions: list[dict[str, Any]]) -> dict[str, Any] | None:
        if bridge is not None:
            return bridge.ask(questions)
        return _prompt_request_user_input(questions, color_mode=color_mode)

    setattr(provider, "_python_volley_cli_request_user_input_provider", True)
    return provider


def _install_request_user_input_provider(
    session: VolleySession,
    provider: Callable[[list[dict[str, Any]]], dict[str, Any] | None],
) -> None:
    current = session.config.request_user_input_provider
    if current is not None and not getattr(current, "_python_volley_cli_request_user_input_provider", False):
        return
    config = replace(session.config, request_user_input_provider=provider)
    session.config = config
    session.state.config = config
    session.tools.config = config


_REQUEST_USER_INPUT_OTHER_LABEL = "None of the above"
_REQUEST_USER_INPUT_OTHER_DESCRIPTION = "Optionally, add details in notes (tab)."


@dataclass(frozen=True)
class _RequestUserInputOption:
    label: str
    description: str
    is_other: bool = False


def _prompt_request_user_input(questions: list[dict[str, Any]], *, color_mode: str = "auto") -> dict[str, Any] | None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return None
    style = _AnsiStyle(_should_use_color(color_mode))
    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
    except Exception:
        return None

    answers: dict[str, dict[str, list[str]]] = {}
    rendered_rows = 0

    def render(lines: list[str]) -> None:
        nonlocal rendered_rows
        cols = _terminal_columns()
        _clear_prompt_lines(rendered_rows)
        print("\n".join(lines), end="", file=sys.stderr, flush=True)
        rendered_rows = _prompt_screen_rows(lines, cols)

    def clear() -> None:
        nonlocal rendered_rows
        _clear_prompt_lines(rendered_rows)
        rendered_rows = 0

    try:
        _set_raw_keep_opost(fd)
        sys.stderr.write("\033[?2004h")
        sys.stderr.flush()
        pending = b""
        for index, question in enumerate(questions, start=1):
            if not isinstance(question, dict):
                continue
            question_id = str(question.get("id") or "")
            if not question_id:
                continue
            result, pending = _read_request_user_input_answer(
                question,
                question_index=index,
                question_count=len(questions),
                fd=fd,
                pending=pending,
                style=style,
                render=render,
            )
            if result is None:
                return None
            answers[question_id] = {"answers": result}
        clear()
        return {"answers": answers}
    except KeyboardInterrupt:
        raise
    finally:
        clear()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass
        sys.stderr.write("\033[?2004l")
        sys.stderr.flush()


def _read_request_user_input_answer(
    question: dict[str, Any],
    *,
    question_index: int,
    question_count: int,
    fd: int,
    pending: bytes,
    style: "_AnsiStyle",
    render: Callable[[list[str]], None],
) -> tuple[list[str] | None, bytes]:
    options = _request_user_input_options(question)
    selected = 0
    notes = ""
    mode = "options" if options else "notes"
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        render(
            _request_user_input_selector_lines(
                question,
                options=options,
                selected=selected,
                notes=notes,
                mode=mode,
                question_index=question_index,
                question_count=question_count,
                style=style,
            )
        )
        chunk, pending = _read_tty_chunk(fd, pending)
        if chunk == b"":
            return None, pending
        if chunk == b"\x03":
            raise KeyboardInterrupt
        if chunk in {b"\x7f", b"\b"}:
            if mode == "notes" and notes:
                notes = notes[:-1]
            elif mode == "notes" and options:
                mode = "options"
            continue
        if chunk == b"\t":
            if options:
                mode = "notes" if mode == "options" else "options"
            continue
        if chunk in {b"\r", b"\n"}:
            tail = decoder.decode(b"", final=True)
            if tail:
                notes += tail.replace("\r\n", "\n").replace("\r", "\n")
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            if chunk == b"\n" and mode == "notes":
                notes += "\n"
                continue
            if mode == "options" and options:
                if options[selected].is_other:
                    mode = "notes"
                    continue
                return _request_user_input_values_for_selection(options[selected], notes), pending
            if options:
                return _request_user_input_values_for_selection(options[selected], notes), pending
            text = notes.strip()
            return ([text] if text else []), pending
        if chunk == b"\x1b":
            sequence, pending = _read_escape_sequence(fd, pending)
            if sequence.startswith(b"\x1b[200~"):
                pasted, pending = _read_bracketed_paste(fd, sequence, pending)
                notes += pasted.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                mode = "notes"
                continue
            if sequence in {b"\x1b[A", b"\x1bOA"} and options:
                selected = (selected - 1) % len(options)
                continue
            if sequence in {b"\x1b[B", b"\x1bOB"} and options:
                selected = (selected + 1) % len(options)
                continue
            if sequence == b"\x1b":
                if mode == "notes" and notes:
                    notes = ""
                    mode = "options" if options else "notes"
                    continue
                return None, pending
            continue
        text = decoder.decode(chunk, final=False)
        if not text:
            continue
        if mode == "options" and options:
            stripped = text.strip()
            if len(stripped) == 1 and stripped.isdigit():
                index = int(stripped) - 1
                if 0 <= index < len(options):
                    selected = index
                    if options[selected].is_other:
                        mode = "notes"
                    continue
            mode = "notes"
        notes += text.replace("\r\n", "\n").replace("\r", "\n")


def _request_user_input_options(question: dict[str, Any]) -> list[_RequestUserInputOption]:
    options: list[_RequestUserInputOption] = []
    raw_options = question.get("options")
    if isinstance(raw_options, list):
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()
            if not label:
                continue
            options.append(
                _RequestUserInputOption(
                    label=label,
                    description=str(option.get("description") or "").strip(),
                )
            )
    if options and question.get("isOther"):
        options.append(
            _RequestUserInputOption(
                label=_REQUEST_USER_INPUT_OTHER_LABEL,
                description=_REQUEST_USER_INPUT_OTHER_DESCRIPTION,
                is_other=True,
            )
        )
    return options


def _request_user_input_selector_lines(
    question: dict[str, Any],
    *,
    options: list[_RequestUserInputOption],
    selected: int,
    notes: str,
    mode: str,
    question_index: int,
    question_count: int,
    style: "_AnsiStyle",
) -> list[str]:
    lines = [f"{style.marker()} {style.bold('Questions')}"]
    if question_count > 1:
        lines.append(f"  {style.dim(f'Question {question_index}/{question_count}')}")
    header = str(question.get("header") or "").strip()
    text = str(question.get("question") or "").strip()
    if header:
        lines.append(f"  {style.bold(header)}")
    if text:
        wrapped_question = textwrap.wrap(text, width=max(20, _terminal_columns() - 4)) or [text]
        lines.extend(f"  {line}" for line in wrapped_question)
    for index, option in enumerate(options):
        marker = "›" if index == selected else " "
        number = index + 1
        label = style.cyan(option.label) if index == selected else option.label
        prefix = f"  {marker} {number}. "
        row = f"{prefix}{label}"
        if option.description:
            row = f"{row} {style.dim(option.description)}"
        wrapped_row = _wrap_ansi_line(row, max(20, _terminal_safe_width(_terminal_columns())))
        if len(wrapped_row) > 1:
            indent = " " * _visible_width(prefix)
            wrapped_row = [wrapped_row[0], *[f"{indent}{line}" for line in wrapped_row[1:]]]
        lines.extend(wrapped_row)
    if mode == "notes":
        label = "Other" if options and options[selected].is_other else "Notes"
        visible_notes = "*" * len(notes) if question.get("isSecret") else notes
        note_lines = visible_notes.split("\n") or [""]
        first = note_lines[0]
        lines.append(f"  {style.bold(label + ':')} {first}")
        lines.extend(f"    {line}" for line in note_lines[1:])
    tips = "↑/↓ select | enter choose | tab notes | esc cancel"
    if mode == "notes":
        tips = "enter submit | tab choices | esc clear/cancel"
    lines.append(f"  {style.dim(tips)}")
    return lines


def _request_user_input_values_for_selection(option: _RequestUserInputOption, notes: str) -> list[str]:
    values = [option.label]
    note = notes.strip()
    if note:
        values.append(f"user_note: {note}")
    return values


def _request_user_input_question_lines(question: dict[str, Any], style: "_AnsiStyle") -> list[str]:
    lines: list[str] = []
    header = str(question.get("header") or "").strip()
    text = str(question.get("question") or "").strip()
    if header:
        lines.append(f"  {style.bold(header)}")
    if text:
        lines.append(f"  {text}")
    for index, option in enumerate(_request_user_input_options(question), start=1):
        if option.description:
            lines.append(f"    {index}. {style.cyan(option.label)} {style.dim(option.description)}")
        else:
            lines.append(f"    {index}. {style.cyan(option.label)}")
    return lines


def _request_user_input_answer_values(question: dict[str, Any], answer_text: str) -> list[str]:
    text = answer_text.strip()
    if not text:
        return []
    labels = [option.label for option in _request_user_input_options(question)]
    if labels and text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(labels):
            return [labels[index]]
    if labels:
        return [f"user_note: {text}"]
    return [text]


def _request_user_input_answer_list(answer: Any) -> list[str]:
    if not isinstance(answer, dict):
        return []
    values = answer.get("answers")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if isinstance(value, str) and value]


def _split_request_user_input_answer_values(values: list[str]) -> tuple[list[str], str | None]:
    options: list[str] = []
    note: str | None = None
    for value in values:
        if value.startswith("user_note: "):
            note = value[len("user_note: ") :]
        else:
            options.append(value)
    return options, note


@dataclass(frozen=True)
class _ConsoleWrite:
    text: str
    partial_line_open: bool = False
    live_op: str | None = None


@dataclass
class _ComposerDraft:
    text: str = ""
    cursor: int = 0


def _drain_output_queue(output: "queue.Queue[Any]", *, input_reader: "_TurnInputReader | None" = None) -> None:
    items: list[Any] = []
    while True:
        try:
            items.append(output.get_nowait())
        except queue.Empty:
            break
    if not items:
        return
    items = _coalesce_console_writes(items)
    synchronized = _should_synchronize_terminal_update(input_reader)
    if synchronized:
        sys.stderr.write("\033[?2026h")
        sys.stderr.flush()
    try:
        if input_reader is not None:
            input_reader.clear()
        for item in items:
            if isinstance(item, _ConsoleWrite):
                sys.stderr.write(item.text)
                sys.stderr.flush()
                if input_reader is not None:
                    input_reader.output_partial_line_open = item.partial_line_open
            else:
                print(item, file=sys.stderr, flush=True)
                if input_reader is not None:
                    input_reader.output_partial_line_open = False
        if input_reader is not None and not input_reader.output_partial_line_open:
            input_reader.render()
    finally:
        if synchronized:
            sys.stderr.write("\033[?2026l")
            sys.stderr.flush()


def _should_synchronize_terminal_update(input_reader: "_TurnInputReader | None") -> bool:
    if input_reader is None or not getattr(input_reader, "enabled", False):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def _coalesce_console_writes(items: list[Any]) -> list[Any]:
    coalesced: list[Any] = []
    run: list[_ConsoleWrite] = []

    def flush_run() -> None:
        nonlocal run
        if run:
            coalesced.extend(_coalesce_console_write_run(run))
            run = []

    for item in items:
        if isinstance(item, _ConsoleWrite):
            run.append(item)
            continue
        flush_run()
        coalesced.append(item)
    flush_run()
    return coalesced


def _coalesce_console_write_run(run: list[_ConsoleWrite]) -> list[_ConsoleWrite]:
    if len(run) <= 1:
        return list(run)
    if any(item.live_op not in {"live_clear", "live_panel"} for item in run):
        return list(run)
    first_clear = next((item for item in run if item.live_op == "live_clear"), None)
    last_panel = next((item for item in reversed(run) if item.live_op == "live_panel"), None)
    if first_clear is not None and last_panel is not None:
        return [first_clear, last_panel]
    if last_panel is not None:
        return [last_panel]
    if first_clear is not None:
        return [first_clear]
    return list(run)


@dataclass(frozen=True)
class _TurnInputAction:
    kind: str
    text: str = ""


@dataclass(frozen=True)
class _LiveTurnStatusSnapshot:
    header: str
    elapsed_seconds: int
    auth_label: str | None
    goal_status: str | None
    active_context_tokens: int | None
    active_context_estimated: bool
    session_context_tokens: int | None
    session_context_estimated: bool
    session_reasoning_tokens: int | None
    context_window: int | None
    finished: bool = False
    outcome: str | None = None
    fast_status: str | None = None
    details: str | None = None
    animation_millis: int = 0


class _LiveTurnStatus:
    def __init__(self, *, header: str = "Working") -> None:
        self._started_at = time.monotonic()
        self._header = header
        self._details: str | None = None
        self._background_terminal_commands: dict[str, str] = {}
        self._lock = threading.Lock()

    def update(self, event: Any) -> None:
        event_type = getattr(event, "type", "")
        payload = getattr(event, "payload", {})
        payload = payload if isinstance(payload, dict) else {}
        with self._lock:
            if event_type == "context_compaction.started":
                self._header = "Compacting"
                self._details = None
            elif event_type == "context_compaction.completed":
                self._header = "Working"
                self._details = None
            elif event_type == "model.request":
                self._header = "Compacting" if payload.get("compact") else "Working"
                self._details = None
            elif event_type == "stream_error":
                self._header = "Reconnecting"
                self._details = None
            elif event_type == "tool.completed" and payload.get("name") == "exec_command":
                metadata = payload.get("metadata")
                meta = metadata if isinstance(metadata, dict) else {}
                session_id = meta.get("session_id")
                command = meta.get("command") or meta.get("cmd")
                if session_id is not None and isinstance(command, str) and command:
                    self._background_terminal_commands[str(session_id)] = _command_display(command)
            elif event_type == "tool.started" and payload.get("name") == "write_stdin":
                arguments = payload.get("arguments")
                args = arguments if isinstance(arguments, dict) else {}
                if args.get("chars"):
                    self._header = "Working"
                    self._details = None
                else:
                    self._header = "Waiting for background terminal"
                    session_id = args.get("session_id")
                    self._details = (
                        self._background_terminal_commands.get(str(session_id))
                        if session_id is not None
                        else None
                    )
            elif event_type in {"tool.started", "tool.completed", "item.completed", "model.response"} and not payload.get("compact"):
                if self._header in {"Compacting", "Reconnecting"}:
                    self._header = "Working"
                    self._details = None
                elif self._header != "Waiting for background terminal":
                    self._details = None

    def snapshot(
        self,
        session: VolleySession,
        *,
        finished: bool = False,
        outcome: str | None = None,
    ) -> _LiveTurnStatusSnapshot:
        with self._lock:
            header = self._header
            details = self._details
            elapsed_raw = max(0.0, time.monotonic() - self._started_at)
            elapsed = int(elapsed_raw)
            animation_millis = int(elapsed_raw * 1000)
        (
            active_context,
            active_context_estimated,
            session_context,
            session_context_estimated,
            session_reasoning,
            context_window,
        ) = _session_context_status(session)
        return _LiveTurnStatusSnapshot(
            header=header,
            elapsed_seconds=elapsed,
            auth_label=_session_auth_indicator(session, include_fallback=False),
            fast_status=_fast_status_indicator(session.config, include_off=False),
            goal_status=_goal_status_indicator(session),
            active_context_tokens=active_context,
            active_context_estimated=active_context_estimated,
            session_context_tokens=session_context,
            session_context_estimated=session_context_estimated,
            session_reasoning_tokens=session_reasoning,
            context_window=context_window,
            finished=finished,
            outcome=outcome,
            details=details,
            animation_millis=animation_millis,
        )


class _TurnInputReader:
    def __init__(
        self,
        *,
        enabled: bool,
        color_mode: str = "auto",
        initial_text: str = "",
        initial_cursor: int | None = None,
    ) -> None:
        self.enabled = enabled
        self._fd: int | None = None
        self._old_attrs: list[Any] | None = None
        self._style = _AnsiStyle(_should_use_color(color_mode))
        self._buffer = initial_text
        cursor = len(initial_text) if initial_cursor is None else initial_cursor
        self._cursor = max(0, min(len(initial_text), cursor))
        self._pending = b""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._kill_buffer = ""
        self._rendered_lines = 0
        self._rendered_rows_below_cursor = 0
        self.output_partial_line_open = False
        self._status_lines: list[str] = []
        self._status_key = ""
        self._defer_render = False
        self._dirty = False
        self._slash_selection = 0
        self._slash_key = ""
        self._slash_dismissed_for: str | None = None

    def __enter__(self) -> "_TurnInputReader":
        if not self.enabled:
            return self
        try:
            self._fd = sys.stdin.fileno()
            self._old_attrs = termios.tcgetattr(self._fd)
            _set_raw_keep_opost(self._fd)
            sys.stderr.write("\033[?2004h")
            sys.stderr.flush()
        except Exception:
            self.enabled = False
            self._fd = None
            self._old_attrs = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.clear()
        if self._fd is not None and self._old_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except Exception:
                pass
        if self.enabled:
            sys.stderr.write("\033[?2004l")
            sys.stderr.flush()

    def poll(self) -> list[_TurnInputAction]:
        if not self.enabled or self._fd is None:
            return []
        actions: list[_TurnInputAction] = []
        self._defer_render = True
        try:
            while True:
                try:
                    readable, _, _ = select.select([self._fd], [], [], 0)
                except Exception:
                    break
                if not readable and not self._pending:
                    break
                chunk, self._pending = _read_tty_chunk(self._fd, self._pending)
                if chunk == b"":
                    break
                action = self._handle_chunk(chunk)
                if action is not None:
                    actions.append(action)
        finally:
            self._defer_render = False
            if self._dirty:
                self.render()
        return actions

    def render(self) -> None:
        if not self.enabled:
            return
        cols = _terminal_columns()
        display_buffer, display_cursor = _prompt_visible_text_window(
            self._buffer,
            self._cursor,
            max_lines=_composer_visible_body_line_limit(),
        )
        prompt_lines = _prompt_display_lines(display_buffer, self._style, width=cols, boxed=True)
        slash_lines = _slash_palette_display_lines(
            self._buffer,
            self._cursor,
            selected_index=self._synced_slash_selection(),
            dismissed_for=self._slash_dismissed_for,
            style=self._style,
            width=cols,
        )
        status_block = _composer_status_block(self._status_lines)
        lines = [*status_block, *prompt_lines, *slash_lines]
        self.clear()
        print("\n".join(lines), end="", file=sys.stderr, flush=True)
        self._rendered_lines = _prompt_screen_rows(lines, cols)
        status_rows = _prompt_screen_rows(status_block, cols)
        prompt_top_rows = _composer_prompt_top_rows(display_buffer, boxed=True)
        cursor_row = _prompt_cursor_screen_row(
            display_buffer,
            display_cursor,
            self._rendered_lines,
            cols,
            prefix_rows=status_rows + prompt_top_rows,
        )
        self._rendered_rows_below_cursor = max(0, self._rendered_lines - 1 - cursor_row)
        _move_prompt_cursor(
            display_buffer,
            display_cursor,
            self._rendered_lines,
            cols,
            prefix_rows=status_rows + prompt_top_rows,
        )
        self._dirty = False

    def set_status(self, snapshot: _LiveTurnStatusSnapshot | None) -> None:
        if not self.enabled:
            return
        lines = _live_status_display_lines(snapshot, self._style) if snapshot is not None else []
        key = "\n".join(lines)
        if key == self._status_key:
            return
        self._status_lines = lines
        self._status_key = key
        self._request_render()

    def clear(self) -> None:
        if not self.enabled:
            return
        _clear_prompt_lines(self._rendered_lines, rows_below_cursor=self._rendered_rows_below_cursor)
        self._rendered_lines = 0
        self._rendered_rows_below_cursor = 0
        self._dirty = False

    def draft_text(self) -> str:
        return self._buffer

    def draft_cursor(self) -> int:
        return self._cursor

    def suspend(self) -> None:
        if not self.enabled or self._fd is None:
            return
        self.clear()
        if self._old_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except Exception:
                pass
        sys.stderr.write("\033[?2004l")
        sys.stderr.flush()

    def resume(self) -> None:
        if not self.enabled or self._fd is None:
            return
        try:
            _set_raw_keep_opost(self._fd)
        except Exception:
            return
        sys.stderr.write("\033[?2004h")
        sys.stderr.flush()
        self.render()

    def _request_render(self) -> None:
        self._dirty = True
        if not self._defer_render:
            self.render()

    def _handle_chunk(self, chunk: bytes) -> _TurnInputAction | None:
        if chunk == b"\x03":
            raise KeyboardInterrupt
        control_update = _apply_prompt_control_key(self._buffer, self._cursor, chunk, self._kill_buffer)
        if control_update is not None:
            self._buffer, self._cursor, self._kill_buffer = control_update
            self._sync_slash_after_edit()
            self._request_render()
            return None
        if chunk in {b"\x7f", b"\b"}:
            if self._cursor > 0:
                self._buffer = self._buffer[: self._cursor - 1] + self._buffer[self._cursor :]
                self._cursor -= 1
                self._sync_slash_after_edit()
                self._request_render()
            return None
        if chunk == b"\t":
            if self._complete_slash_selection(trailing_space=True):
                return None
            return None
        if chunk == b"\r":
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._insert(tail)
            accepted = _slash_selected_completion_text(
                self._buffer,
                self._cursor,
                self._slash_selection,
                dismissed_for=self._slash_dismissed_for,
                trailing_space=False,
            )
            text = _normalize_optional_prompt(accepted if accepted is not None else self._buffer) or ""
            self._buffer = ""
            self._cursor = 0
            self._slash_selection = 0
            self._slash_key = ""
            self._slash_dismissed_for = None
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            # Force an immediate redraw of the (now empty) prompt so the
            # transcript above remains clean before the caller prints output.
            self.render()
            return _TurnInputAction("submit", text)
        if chunk == b"\n":
            self._insert("\n")
            return None
        if chunk == b"\x1b":
            sequence, self._pending = _read_escape_sequence(self._fd, self._pending)
            if sequence.startswith(b"\x1b[200~"):
                pasted, self._pending = _read_bracketed_paste(self._fd, sequence, self._pending)
                self._insert(pasted.decode("utf-8", errors="replace"))
                return None
            handled = self._handle_escape_sequence(sequence)
            if handled:
                return None
            if sequence == b"\x1b":
                return _TurnInputAction("interrupt")
            return None
        text = self._decoder.decode(chunk, final=False)
        if text:
            if text == "/" and self._complete_slash_selection(trailing_space=True):
                return None
            self._insert(text)
        return None

    def _insert(self, text: str) -> None:
        if not text:
            return
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._buffer = self._buffer[: self._cursor] + normalized + self._buffer[self._cursor :]
        self._cursor += len(normalized)
        self._sync_slash_after_edit()
        self._request_render()

    def _handle_escape_sequence(self, sequence: bytes) -> bool:
        rows = self._current_slash_rows()
        if rows:
            if sequence in {b"\x1b[A", b"\x1bOA"}:
                self._slash_selection = (self._slash_selection - 1) % len(rows)
                self._request_render()
                return True
            if sequence in {b"\x1b[B", b"\x1bOB"}:
                self._slash_selection = (self._slash_selection + 1) % len(rows)
                self._request_render()
                return True
            if sequence == b"\x1b":
                self._slash_dismissed_for = _slash_first_line(self._buffer)
                self._request_render()
                return True
        updated = _apply_prompt_escape_sequence(self._buffer, self._cursor, sequence)
        if updated is None:
            return False
        self._buffer, self._cursor = updated
        self._sync_slash_after_edit()
        self._request_render()
        return True

    def _current_slash_rows(self) -> list["_SlashPaletteRow"]:
        key = _slash_palette_key(self._buffer, self._cursor, self._slash_dismissed_for)
        if key != self._slash_key:
            self._slash_key = key
            self._slash_selection = 0
        rows = _slash_palette_rows_for_buffer(
            self._buffer,
            self._cursor,
            dismissed_for=self._slash_dismissed_for,
        )
        if rows:
            self._slash_selection = max(0, min(self._slash_selection, len(rows) - 1))
        return rows

    def _synced_slash_selection(self) -> int:
        self._current_slash_rows()
        return self._slash_selection

    def _sync_slash_after_edit(self) -> None:
        if self._slash_dismissed_for is not None and _slash_first_line(self._buffer) != self._slash_dismissed_for:
            self._slash_dismissed_for = None
        self._current_slash_rows()

    def _complete_slash_selection(self, *, trailing_space: bool) -> bool:
        completed = _slash_selected_completion_text(
            self._buffer,
            self._cursor,
            self._slash_selection,
            dismissed_for=self._slash_dismissed_for,
            trailing_space=trailing_space,
        )
        if completed is None:
            return False
        self._buffer = completed
        self._cursor = min(len(self._buffer), _slash_completed_cursor(completed))
        self._slash_dismissed_for = _slash_first_line(self._buffer)
        self._sync_slash_after_edit()
        self._request_render()
        return True


def _interactive_turn_controls_available() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _run_compact_human(session: VolleySession, *, color_mode: str = "auto") -> int:
    renderer = _HumanEventRenderer(color_mode=color_mode)
    completed = False
    failed = False
    try:
        for event in session.stream_compact():
            if event.type == "item.completed" and event.payload.get("compact"):
                continue
            if event.type == "context_compaction.completed":
                completed = True
            if event.type == "turn.failed":
                failed = True
                renderer.render_error(str(event.payload.get("error") or "turn failed"))
            else:
                renderer.render(event)
    except Exception as exc:
        if not failed:
            event = _session_failure_event(session, exc)
            renderer.render_error(str(event.payload.get("error") or "turn failed"))
    return 0 if completed else 1


def _session_failure_event(session: VolleySession, exc: Exception) -> VolleyEvent:
    message = _exception_display_message(exc)
    try:
        return session.state.emit("turn.failed", error=message)
    except Exception:
        return VolleyEvent("turn.failed", {"error": message})


def _exception_display_message(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return type(exc).__name__
    if isinstance(exc, RuntimeError):
        return message
    return f"{type(exc).__name__}: {message}"


def _print_cli_error(exc: Exception) -> None:
    print(f"ERROR: {_exception_display_message(exc)}", file=sys.stderr)


def _read_interactive_prompt(
    *,
    color_mode: str = "auto",
    initial_text: str = "",
    initial_cursor: int | None = None,
) -> str | None:
    if sys.stdin.isatty() and sys.stderr.isatty():
        prompt = _read_tty_prompt(
            color_mode=color_mode,
            initial_text=initial_text,
            initial_cursor=initial_cursor,
        )
        if prompt is not None:
            return prompt

    style = _AnsiStyle(_should_use_color(color_mode))
    if sys.stdin.isatty() and sys.stderr.isatty():
        print(f"{style.bold('›')} ", end="", file=sys.stderr, flush=True)
    else:
        print(f"{style.bold('›')} ", end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if line == "":
        if sys.stdin.isatty() and sys.stderr.isatty():
            print(file=sys.stderr)
        return None
    if sys.stdin.isatty() and sys.stderr.isatty():
        print("\033[1A\033[2K", end="", file=sys.stderr, flush=True)
    return line.rstrip("\n").replace("\r\n", "\n").replace("\r", "\n")


def _read_tty_prompt(
    *,
    color_mode: str = "auto",
    initial_text: str = "",
    initial_cursor: int | None = None,
) -> str | None:
    style = _AnsiStyle(_should_use_color(color_mode))
    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
    except Exception:
        return None

    buffer = initial_text
    requested_cursor = len(initial_text) if initial_cursor is None else initial_cursor
    cursor = max(0, min(len(initial_text), requested_cursor))
    kill_buffer = ""
    rendered_lines = 0
    rendered_rows_below_cursor = 0
    slash_selection = 0
    slash_key = ""
    slash_dismissed_for: str | None = None
    state = {"dirty": False, "defer": False}

    def current_slash_rows() -> list["_SlashPaletteRow"]:
        nonlocal slash_selection, slash_key
        key = _slash_palette_key(buffer, cursor, slash_dismissed_for)
        if key != slash_key:
            slash_key = key
            slash_selection = 0
        rows = _slash_palette_rows_for_buffer(buffer, cursor, dismissed_for=slash_dismissed_for)
        if rows:
            slash_selection = max(0, min(slash_selection, len(rows) - 1))
        return rows

    def sync_slash_after_edit() -> None:
        nonlocal slash_dismissed_for
        if slash_dismissed_for is not None and _slash_first_line(buffer) != slash_dismissed_for:
            slash_dismissed_for = None
        current_slash_rows()

    def complete_slash_selection(*, trailing_space: bool) -> bool:
        nonlocal buffer, cursor, slash_dismissed_for
        completed = _slash_selected_completion_text(
            buffer,
            cursor,
            slash_selection,
            dismissed_for=slash_dismissed_for,
            trailing_space=trailing_space,
        )
        if completed is None:
            return False
        buffer = completed
        cursor = min(len(buffer), _slash_completed_cursor(completed))
        slash_dismissed_for = _slash_first_line(buffer)
        sync_slash_after_edit()
        state["dirty"] = True
        return True

    def render() -> None:
        nonlocal rendered_lines, rendered_rows_below_cursor
        cols = _terminal_columns()
        display_buffer, display_cursor = _prompt_visible_text_window(
            buffer,
            cursor,
            max_lines=_composer_visible_body_line_limit(),
        )
        lines = _prompt_display_lines(display_buffer, style, width=cols, boxed=True)
        lines.extend(
            _slash_palette_display_lines(
                buffer,
                cursor,
                selected_index=slash_selection,
                dismissed_for=slash_dismissed_for,
                style=style,
                width=cols,
            )
        )
        _clear_prompt_lines(rendered_lines, rows_below_cursor=rendered_rows_below_cursor)
        print("\n".join(lines), end="", file=sys.stderr, flush=True)
        rendered_lines = _prompt_screen_rows(lines, cols)
        prompt_top_rows = _composer_prompt_top_rows(display_buffer, boxed=True)
        cursor_row = _prompt_cursor_screen_row(
            display_buffer,
            display_cursor,
            rendered_lines,
            cols,
            prefix_rows=prompt_top_rows,
        )
        rendered_rows_below_cursor = max(0, rendered_lines - 1 - cursor_row)
        _move_prompt_cursor(display_buffer, display_cursor, rendered_lines, cols, prefix_rows=prompt_top_rows)
        state["dirty"] = False

    def request_render() -> None:
        state["dirty"] = True
        if not state["defer"]:
            render()

    sys.stderr.write("\033[?2004h")
    sys.stderr.flush()
    try:
        _set_raw_keep_opost(fd)
        render()
        pending = b""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            # Drain everything currently available in one burst before
            # rendering, so a multi-kilobyte paste only redraws once.
            state["defer"] = True
            try:
                while True:
                    chunk, pending = _read_tty_chunk(fd, pending)
                    if chunk == b"":
                        if state["dirty"]:
                            render()
                        _clear_prompt_lines(rendered_lines, rows_below_cursor=rendered_rows_below_cursor)
                        print(file=sys.stderr, flush=True)
                        return None
                    if chunk == b"\x03":
                        raise KeyboardInterrupt
                    if chunk == b"\x04" and not buffer:
                        if state["dirty"]:
                            render()
                        _clear_prompt_lines(rendered_lines, rows_below_cursor=rendered_rows_below_cursor)
                        return None
                    control_update = _apply_prompt_control_key(buffer, cursor, chunk, kill_buffer)
                    if control_update is not None:
                        buffer, cursor, kill_buffer = control_update
                        sync_slash_after_edit()
                        state["dirty"] = True
                        if not _has_pending_input(fd, pending):
                            break
                        continue
                    if chunk in {b"\x7f", b"\b"}:
                        if cursor > 0:
                            buffer = buffer[: cursor - 1] + buffer[cursor:]
                            cursor -= 1
                            sync_slash_after_edit()
                            state["dirty"] = True
                        if not _has_pending_input(fd, pending):
                            break
                        continue
                    if chunk == b"\t":
                        complete_slash_selection(trailing_space=True)
                        if not _has_pending_input(fd, pending):
                            break
                        continue
                    if chunk == b"\r":
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            normalized = tail.replace("\r\n", "\n").replace("\r", "\n")
                            buffer = buffer[:cursor] + normalized + buffer[cursor:]
                            cursor += len(normalized)
                            sync_slash_after_edit()
                        accepted = _slash_selected_completion_text(
                            buffer,
                            cursor,
                            slash_selection,
                            dismissed_for=slash_dismissed_for,
                            trailing_space=False,
                        )
                        result = _normalize_optional_prompt(accepted if accepted is not None else buffer) or ""
                        _clear_prompt_lines(rendered_lines, rows_below_cursor=rendered_rows_below_cursor)
                        rendered_lines = 0
                        rendered_rows_below_cursor = 0
                        return result
                    if chunk == b"\n":
                        buffer = buffer[:cursor] + "\n" + buffer[cursor:]
                        cursor += 1
                        sync_slash_after_edit()
                        state["dirty"] = True
                        if not _has_pending_input(fd, pending):
                            break
                        continue
                    if chunk == b"\x1b":
                        sequence, pending = _read_escape_sequence(fd, pending)
                        if sequence.startswith(b"\x1b[200~"):
                            pasted, pending = _read_bracketed_paste(fd, sequence, pending)
                            decoded = pasted.decode("utf-8", errors="replace")
                            normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
                            buffer = buffer[:cursor] + normalized + buffer[cursor:]
                            cursor += len(normalized)
                            sync_slash_after_edit()
                            state["dirty"] = True
                        else:
                            rows = current_slash_rows()
                            if rows and sequence in {b"\x1b[A", b"\x1bOA"}:
                                slash_selection = (slash_selection - 1) % len(rows)
                                state["dirty"] = True
                            elif rows and sequence in {b"\x1b[B", b"\x1bOB"}:
                                slash_selection = (slash_selection + 1) % len(rows)
                                state["dirty"] = True
                            elif rows and sequence == b"\x1b":
                                slash_dismissed_for = _slash_first_line(buffer)
                                state["dirty"] = True
                            else:
                                updated = _apply_prompt_escape_sequence(buffer, cursor, sequence)
                                if updated is not None:
                                    buffer, cursor = updated
                                    sync_slash_after_edit()
                                    state["dirty"] = True
                        if sequence.startswith(b"\x1b[200~"):
                            state["dirty"] = True
                        if not _has_pending_input(fd, pending):
                            break
                        continue
                    decoded = decoder.decode(chunk, final=False)
                    if decoded:
                        if decoded == "/" and complete_slash_selection(trailing_space=True):
                            if not _has_pending_input(fd, pending):
                                break
                            continue
                        normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
                        buffer = buffer[:cursor] + normalized + buffer[cursor:]
                        cursor += len(normalized)
                        sync_slash_after_edit()
                        state["dirty"] = True
                    if not _has_pending_input(fd, pending):
                        break
            finally:
                state["defer"] = False
            if state["dirty"]:
                render()
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass
        sys.stderr.write("\033[?2004l")
        sys.stderr.flush()


_COMPOSER_PLACEHOLDER = "Ask Volley to do anything"
_VOLLEY_ACCENT_RGB = (34, 211, 180)
_VOLLEY_MUTED_RGB = (120, 138, 150)
_VOLLEY_BORDER_RGB = (74, 109, 118)
_USER_MESSAGE_BG_RGB = (244, 244, 244)
_USER_MESSAGE_FG_RGB = (28, 28, 28)
_COMPOSER_MAX_VISIBLE_BODY_LINES = 12
_COMPOSER_MIN_VISIBLE_BODY_LINES = 4


def _composer_visible_body_line_limit() -> int:
    rows = shutil.get_terminal_size((100, 24)).lines
    available = max(_COMPOSER_MIN_VISIBLE_BODY_LINES, rows - 8)
    return max(_COMPOSER_MIN_VISIBLE_BODY_LINES, min(_COMPOSER_MAX_VISIBLE_BODY_LINES, available))


def _prompt_visible_text_window(text: str, cursor: int, *, max_lines: int) -> tuple[str, int]:
    cursor = max(0, min(cursor, len(text)))
    lines = text.split("\n")
    max_lines = max(1, max_lines)
    if len(lines) <= max_lines:
        return text, cursor

    before_cursor = text[:cursor]
    cursor_line = before_cursor.count("\n")
    cursor_col = len(before_cursor.rsplit("\n", 1)[-1])
    marker_slots = 0
    data_slots = max_lines
    start = 0
    end = len(lines)

    for _ in range(3):
        data_slots = max(1, max_lines - marker_slots)
        start = min(
            max(0, cursor_line - data_slots + 1),
            max(0, len(lines) - data_slots),
        )
        end = min(len(lines), start + data_slots)
        next_marker_slots = (1 if start > 0 else 0) + (1 if end < len(lines) else 0)
        if next_marker_slots == marker_slots:
            break
        marker_slots = next_marker_slots

    display_lines: list[str] = []
    if start > 0:
        display_lines.append(_prompt_omitted_lines_marker(start, "above"))
    display_line_index = len(display_lines) + cursor_line - start
    display_lines.extend(lines[start:end])
    if end < len(lines):
        display_lines.append(_prompt_omitted_lines_marker(len(lines) - end, "below"))

    if not 0 <= display_line_index < len(display_lines):
        display_line_index = max(0, min(display_line_index, len(display_lines) - 1))
        cursor_col = len(display_lines[display_line_index])
    else:
        cursor_col = min(cursor_col, len(display_lines[display_line_index]))

    display_cursor = 0
    for line in display_lines[:display_line_index]:
        display_cursor += len(line) + 1
    display_cursor += cursor_col
    return "\n".join(display_lines), display_cursor


def _prompt_omitted_lines_marker(count: int, direction: str) -> str:
    noun = "line" if count == 1 else "lines"
    return f"... {count} {noun} {direction} ..."


def _prompt_display_lines(
    text: str,
    style: "_AnsiStyle",
    *,
    width: int | None = None,
    boxed: bool = False,
) -> list[str]:
    prompt_prefix = (
        f"{style.composer_bold('›')}{style.user_message(' ')}"
        if boxed
        else f"{style.bold('›')} "
    )
    if text == "":
        rendered = prompt_prefix
        rendered += style.composer_dim(_COMPOSER_PLACEHOLDER) if boxed else style.dim(_COMPOSER_PLACEHOLDER)
        return _composer_block_lines([rendered], style=style, width=width) if boxed else [rendered]
    lines = text.split("\n")
    content_width = _prompt_content_width(width) if boxed else None
    rendered: list[str] = []
    for line_index, line in enumerate(lines):
        chunks = (
            [chunk.text for chunk in _prompt_line_chunks(line, content_width)]
            if content_width is not None
            else [line]
        )
        for chunk_index, chunk in enumerate(chunks):
            is_first_prompt_line = line_index == 0 and chunk_index == 0
            prefix = prompt_prefix if is_first_prompt_line else (style.composer("  ") if boxed else "  ")
            body = style.composer(chunk) if boxed else chunk
            rendered.append(f"{prefix}{body}")
    if boxed:
        rendered = _composer_block_lines(rendered, style=style, width=width)
    return rendered


@dataclass(frozen=True)
class _PromptLineChunk:
    text: str
    start: int
    end: int


def _prompt_content_width(width: int | None) -> int:
    target = _composer_box_width(width)
    return max(1, target - 2) if target > 0 else 1


def _prompt_line_chunks(line: str, width: int | None) -> list[_PromptLineChunk]:
    if width is None or width <= 0:
        return [_PromptLineChunk(line, 0, len(line))]
    if line == "":
        return [_PromptLineChunk("", 0, 0)]
    chunks: list[_PromptLineChunk] = []
    current = ""
    current_width = 0
    start = 0
    for index, char in enumerate(line):
        char_width = _display_width(char)
        if current and char_width > 0 and current_width + char_width > width:
            chunks.append(_PromptLineChunk(current, start, index))
            current = ""
            current_width = 0
            start = index
        current += char
        current_width += char_width
    chunks.append(_PromptLineChunk(current, start, len(line)))
    return chunks


def _composer_block_lines(text_lines: list[str], *, style: "_AnsiStyle", width: int | None) -> list[str]:
    # Upstream ChatComposer reserves a composer rect with top/bottom padding:
    # textarea.desired_height(inner_width) + 2. Render the same shape in the
    # inline fallback so the input appears as a region, not a one-row stripe.
    blank = _composer_blank_line(style=style, width=width)
    body = [_composer_box_line(line, style=style, width=width) for line in text_lines]
    return [blank, *body, blank]


def _composer_blank_line(*, style: "_AnsiStyle", width: int | None) -> str:
    target = _composer_box_width(width)
    return style.user_message(" " * target) if target > 0 else style.user_message("")


def _composer_box_line(line: str, *, style: "_AnsiStyle", width: int | None) -> str:
    target = _composer_box_width(width)
    if target <= 0:
        return line
    padding = target - _visible_len(line)
    if padding <= 0:
        return line
    return f"{line}{style.user_message(' ' * padding)}"


def _user_message_blank_line(style: "_AnsiStyle", width: int) -> str:
    return _composer_blank_line(style=style, width=width)


def _user_message_box_line(line: str, style: "_AnsiStyle", width: int) -> str:
    return _composer_box_line(line, style=style, width=width)


def _composer_box_width(width: int | None) -> int:
    if width is None or width <= 0:
        return 0
    # Avoid writing into the terminal's last column; many terminals auto-wrap
    # after that cell, which would make prompt clearing leave artifacts.
    return max(1, width - 1)


def _composer_prompt_top_rows(text: str, *, boxed: bool) -> int:
    return 1 if boxed else 0


def _composer_status_block(status_lines: list[str]) -> list[str]:
    if not status_lines:
        return []
    # Terminal bottom-pane snapshots reserve a blank row above the live
    # status. The composer itself contributes the padded blank row below.
    return ["", *status_lines, ""]


def _live_status_display_lines(snapshot: _LiveTurnStatusSnapshot | None, style: "_AnsiStyle") -> list[str]:
    if snapshot is None:
        return []
    elapsed = _format_elapsed_compact(snapshot.elapsed_seconds)
    if snapshot.finished:
        parts = [style.dim(f"  {_finished_status_label(snapshot)} {elapsed}")]
    else:
        indicator = _activity_indicator(snapshot.animation_millis, style)
        header = _animated_status_header(snapshot.header, snapshot.animation_millis, style)
        prefix = f"{indicator} " if indicator else ""
        parts = [
            f"{prefix}{header} "
            f"{style.dim(f'({elapsed} • esc to interrupt)')}",
        ]
    metric_parts: list[str] = []
    if snapshot.goal_status:
        metric_parts.append(snapshot.goal_status)
    if snapshot.fast_status:
        metric_parts.append(snapshot.fast_status)
    if snapshot.active_context_tokens is not None:
        ctx = _format_tokens_compact(snapshot.active_context_tokens)
        if snapshot.context_window:
            ctx = f"{ctx}/{_format_tokens_compact(snapshot.context_window)}"
        metric_parts.append(f"ctx {ctx}")
    if snapshot.session_context_tokens is not None:
        metric_parts.append(
            f"session {_format_tokens_compact(snapshot.session_context_tokens)}"
        )
    if snapshot.session_reasoning_tokens is not None:
        metric_parts.append(f"reasoning {_format_tokens_compact(snapshot.session_reasoning_tokens)}")
    if snapshot.auth_label:
        metric_parts.append(f"auth {snapshot.auth_label}")
    if metric_parts:
        parts.append(style.dim(" · " + " · ".join(metric_parts)))
    line = "".join(parts)
    width = _terminal_columns()
    safe_width = _terminal_safe_width(width)
    wrapped = _wrap_ansi_line(line, max(20, safe_width - 2))
    lines = wrapped if len(wrapped) <= 1 else [wrapped[0], *[f"  {style.dim(line)}" for line in wrapped[1:]]]
    if snapshot.details and not snapshot.finished:
        lines.append(_status_details_line(snapshot.details, style=style, width=width))
    return lines


def _activity_indicator(animation_millis: int, style: "_AnsiStyle") -> str:
    if not style.enabled:
        return ""
    blink_on = (max(0, int(animation_millis)) // 600) % 2 == 0
    return style.marker() if blink_on else style.marker_off()


def _animated_status_header(text: str, animation_millis: int, style: "_AnsiStyle") -> str:
    del animation_millis
    return style.bold(text)


def _status_details_line(details: str, *, style: "_AnsiStyle", width: int) -> str:
    collapsed = " ".join(str(details).split())
    prefix = "  └ "
    detail_width = max(1, _terminal_safe_width(width) - _visible_len(prefix))
    return f"{style.dim(prefix)}{style.dim(_truncate_display_text(collapsed, detail_width))}"


def _print_finished_turn_status(
    status_tracker: _LiveTurnStatus,
    session: VolleySession,
    *,
    color_mode: str,
    outcome: str,
) -> None:
    if not sys.stderr.isatty():
        return
    style = _AnsiStyle(_should_use_color(color_mode))
    lines = _live_status_display_lines(
        status_tracker.snapshot(session, finished=True, outcome=outcome),
        style,
    )
    if lines:
        print("", file=sys.stderr)
        print("\n".join(lines), file=sys.stderr, flush=True)


def _finished_status_label(snapshot: _LiveTurnStatusSnapshot) -> str:
    outcome = (snapshot.outcome or "completed").lower()
    if outcome == "interrupted":
        return "Interrupted after"
    if outcome == "failed":
        return "Failed after"
    if outcome == "compacted":
        return "Compacted for"
    if snapshot.header == "Compacting":
        return "Compacted for"
    return "Worked for"


def _session_auth_indicator(session: VolleySession, *, include_fallback: bool) -> str | None:
    client = getattr(session, "model_client", None)
    active = getattr(client, "auth_display_name", None)
    if not isinstance(active, str) or not active:
        type_name = type(client).__name__ if client is not None else ""
        if type_name == "ScriptedResponsesModel":
            active = "fake model"
        elif type_name == "ChatGPTCodexSubscriptionModel":
            active = "ChatGPT"
        elif type_name == "OpenAIResponsesModel":
            active = "API key"
    if not active:
        return None
    if include_fallback:
        active = _with_chatgpt_account_hint(session, active)
        fallback = getattr(client, "auth_fallback_display_name", None)
        if isinstance(fallback, str) and fallback:
            active = f"{active} · fallback {fallback}"
    return active


def _with_chatgpt_account_hint(session: VolleySession, label: str) -> str:
    if "chatgpt" not in label.lower():
        return label
    try:
        from .auth import auth_status

        status = auth_status(session.config.resolved_auth_home())
    except Exception:
        return label
    pieces: list[str] = []
    email = _mask_email(str(status.get("email") or ""))
    if email:
        pieces.append(email)
    plan = status.get("plan_type")
    if isinstance(plan, str) and plan:
        pieces.append(plan)
    account = status.get("account_id")
    if isinstance(account, str) and account:
        pieces.append(account)
    if not pieces:
        return label
    return f"{label} ({', '.join(pieces)})"


def _mask_email(value: str) -> str | None:
    if "@" not in value:
        return None
    local, domain = value.split("@", 1)
    if not local or not domain:
        return None
    if len(local) == 1:
        masked_local = local + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def _goal_status_indicator(session: VolleySession) -> str | None:
    runtime = getattr(session, "goals", None)
    get_goal = getattr(runtime, "get_goal", None)
    if not callable(get_goal):
        return None
    try:
        goal = get_goal()
    except Exception:
        return None
    if goal is None:
        return None
    status = GOAL_STATUS_FROM_WIRE.get(getattr(goal, "status", ""), getattr(goal, "status", ""))
    if status == "active":
        return f"Pursuing goal ({_active_goal_footer_usage(goal)})"
    if status == "paused":
        return "Goal paused (/goal resume)"
    if status == "blocked":
        return "Goal blocked (/goal resume)"
    if status == "usage_limited":
        return "Goal hit usage limits (/goal resume)"
    if status == "budget_limited":
        usage = _budget_limited_goal_footer_usage(goal)
        return f"Goal unmet ({usage})" if usage else "Goal abandoned"
    if status == "complete":
        return f"Goal achieved ({_complete_goal_footer_usage(goal)})"
    return None


def _fast_status_indicator(config: VolleyConfig, *, include_off: bool) -> str | None:
    service_tier = config.resolved_service_tier()
    if service_tier == "priority" and config.resolved_model_supports_fast_mode():
        return "Fast on"
    if include_off and config.fast_mode_enabled and config.resolved_model_supports_fast_mode():
        return "Fast off"
    if include_off and service_tier:
        return service_tier
    return None


def _active_goal_footer_usage(goal: Any) -> str:
    token_budget = getattr(goal, "token_budget", None)
    tokens_used = int(getattr(goal, "tokens_used", 0) or 0)
    if token_budget is not None:
        return f"{_format_tokens_compact(tokens_used)} / {_format_tokens_compact(int(token_budget))}"
    elapsed = int(getattr(goal, "time_used_seconds", 0) or 0)
    return _format_goal_elapsed_seconds(elapsed)


def _budget_limited_goal_footer_usage(goal: Any) -> str:
    token_budget = getattr(goal, "token_budget", None)
    if token_budget is None:
        return ""
    tokens_used = int(getattr(goal, "tokens_used", 0) or 0)
    return f"{_format_tokens_compact(tokens_used)} / {_format_tokens_compact(int(token_budget))} tokens"


def _complete_goal_footer_usage(goal: Any) -> str:
    token_budget = getattr(goal, "token_budget", None)
    tokens_used = int(getattr(goal, "tokens_used", 0) or 0)
    if token_budget is not None:
        return f"{_format_tokens_compact(tokens_used)} tokens"
    elapsed = int(getattr(goal, "time_used_seconds", 0) or 0)
    return _format_goal_elapsed_seconds(elapsed)


def _session_context_status(session: VolleySession) -> tuple[int | None, bool, int | None, bool, int | None, int | None]:
    active_context: int | None = None
    active_context_estimated = True
    session_context: int | None = None
    session_context_estimated = True
    session_reasoning: int | None = None
    context_window: int | None = None
    try:
        active_context, active_context_estimated = session.state.active_context_token_status()
    except Exception:
        pass
    try:
        session_context, session_context_estimated = session.state.session_context_token_status()
    except Exception:
        pass
    try:
        session_reasoning = session.state.session_reasoning_usage_tokens()
    except Exception:
        pass
    try:
        context_window = session.config.resolved_model_context_window()
    except Exception:
        pass
    return active_context, active_context_estimated, session_context, session_context_estimated, session_reasoning, context_window


def _format_elapsed_compact(elapsed_seconds: int) -> str:
    seconds = max(0, int(elapsed_seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes = seconds // 60
        remainder = seconds % 60
        return f"{minutes}m {remainder:02}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remainder = seconds % 60
    return f"{hours}h {minutes:02}m {remainder:02}s"


def _format_tokens_compact(value: int | float) -> str:
    value = max(0, int(value))
    if value == 0:
        return "0"
    if value < 1_000:
        return str(value)
    scaled = float(value)
    suffix = "K"
    if value >= 1_000_000_000_000:
        scaled = scaled / 1_000_000_000_000.0
        suffix = "T"
    elif value >= 1_000_000_000:
        scaled = scaled / 1_000_000_000.0
        suffix = "B"
    elif value >= 1_000_000:
        scaled = scaled / 1_000_000.0
        suffix = "M"
    else:
        scaled = scaled / 1_000.0
    decimals = 2 if scaled < 10.0 else 1 if scaled < 100.0 else 0
    formatted = f"{scaled:.{decimals}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _visible_width(text: str) -> int:
    """Return the number of terminal columns occupied by `text`.

    Strips ANSI CSI escapes; counts CJK wide / fullwidth chars as 2 columns;
    treats combining marks and other control chars as zero-width.
    """
    cleaned = _ANSI_CSI_RE.sub("", text)
    width = 0
    for ch in cleaned:
        if ch in ("\r", "\n"):
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C") or cat in ("Mn", "Me"):
            continue
        ea = unicodedata.east_asian_width(ch)
        width += 2 if ea in ("W", "F") else 1
    return width


def _terminal_columns() -> int:
    try:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        cols = 80
    return cols if cols > 0 else 80


def _prompt_screen_rows(lines: list[str], cols: int) -> int:
    """Total screen rows the rendered prompt will occupy, accounting for wrap."""
    if cols <= 0:
        return max(1, len(lines))
    total = 0
    for line in lines:
        w = _visible_width(line)
        total += 1 if w == 0 else (w + cols - 1) // cols
    return total


def _move_prompt_cursor(text: str, cursor: int, rendered_lines: int, cols: int, *, prefix_rows: int = 0) -> None:
    if rendered_lines <= 0 or cols <= 0:
        return
    row, col = _prompt_cursor_position(text, cursor, cols)
    row = _prompt_cursor_screen_row(text, cursor, rendered_lines, cols, prefix_rows=prefix_rows)
    rows_up = max(0, rendered_lines - 1 - row)
    sys.stderr.write("\r")
    if rows_up:
        sys.stderr.write(f"\033[{rows_up}A")
    if col:
        sys.stderr.write(f"\033[{col}C")
    sys.stderr.flush()


def _prompt_cursor_screen_row(text: str, cursor: int, rendered_lines: int, cols: int, *, prefix_rows: int = 0) -> int:
    if rendered_lines <= 0 or cols <= 0:
        return 0
    row, _ = _prompt_cursor_position(text, cursor, cols)
    return min(max(prefix_rows + row, 0), max(0, rendered_lines - 1))


def _prompt_cursor_position(text: str, cursor: int, cols: int) -> tuple[int, int]:
    cursor = max(0, min(cursor, len(text)))
    before = text[:cursor]
    logical_line_index = before.count("\n")
    prefix_width = 2
    content_width = _prompt_content_width(cols)
    row = 0
    lines = text.split("\n")
    for line in lines[:logical_line_index]:
        row += len(_prompt_line_chunks(line, content_width))
    current_text = before.rsplit("\n", 1)[-1]
    cursor_col = len(current_text)
    current_line = lines[logical_line_index] if logical_line_index < len(lines) else ""
    chunks = _prompt_line_chunks(current_line, content_width)
    selected_index = 0
    for index, chunk in enumerate(chunks):
        if cursor_col <= chunk.end:
            selected_index = index
            break
    else:
        selected_index = max(0, len(chunks) - 1)
    if cursor_col == chunks[selected_index].start and selected_index > 0:
        selected_index -= 1
    chunk = chunks[selected_index]
    row += selected_index
    cursor_in_chunk = max(chunk.start, min(cursor_col, chunk.end))
    col = prefix_width + _display_width(current_line[chunk.start:cursor_in_chunk])
    return row, col


def _apply_prompt_escape_sequence(buffer: str, cursor: int, sequence: bytes) -> tuple[str, int] | None:
    if sequence in {b"\x1b[D", b"\x1bOD"}:
        return buffer, max(0, cursor - 1)
    if sequence in {b"\x1b[C", b"\x1bOC"}:
        return buffer, min(len(buffer), cursor + 1)
    if sequence in {b"\x1b[H", b"\x1b[1~", b"\x1bOH"}:
        return buffer, _line_start_index(buffer, cursor)
    if sequence in {b"\x1b[F", b"\x1b[4~", b"\x1bOF"}:
        return buffer, _line_end_index(buffer, cursor)
    if sequence == b"\x1b[3~":
        if cursor >= len(buffer):
            return buffer, cursor
        return buffer[:cursor] + buffer[cursor + 1 :], cursor
    if sequence == b"\x1b[A":
        return buffer, _move_cursor_vertical(buffer, cursor, -1)
    if sequence == b"\x1b[B":
        return buffer, _move_cursor_vertical(buffer, cursor, 1)
    return None


def _apply_prompt_control_key(buffer: str, cursor: int, chunk: bytes, kill_buffer: str) -> tuple[str, int, str] | None:
    cursor = max(0, min(cursor, len(buffer)))
    if chunk == b"\x01":  # Ctrl+A
        bol = _line_start_index(buffer, cursor)
        if cursor == bol and bol > 0:
            return buffer, _line_start_index(buffer, bol - 1), kill_buffer
        return buffer, bol, kill_buffer
    if chunk == b"\x05":  # Ctrl+E
        eol = _line_end_index(buffer, cursor)
        if cursor == eol and eol < len(buffer):
            return buffer, _line_end_index(buffer, cursor + 1), kill_buffer
        return buffer, eol, kill_buffer
    if chunk == b"\x04":  # Ctrl+D
        if cursor >= len(buffer):
            return buffer, cursor, kill_buffer
        return buffer[:cursor] + buffer[cursor + 1 :], cursor, kill_buffer
    if chunk == b"\x0b":  # Ctrl+K
        eol = _line_end_index(buffer, cursor)
        if cursor == eol:
            end = eol + 1 if eol < len(buffer) else eol
        else:
            end = eol
        if end <= cursor:
            return buffer, cursor, kill_buffer
        killed = buffer[cursor:end]
        return buffer[:cursor] + buffer[end:], cursor, killed
    if chunk == b"\x15":  # Ctrl+U
        bol = _line_start_index(buffer, cursor)
        if cursor == bol:
            start = bol - 1 if bol > 0 else bol
        else:
            start = bol
        if start >= cursor:
            return buffer, cursor, kill_buffer
        killed = buffer[start:cursor]
        return buffer[:start] + buffer[cursor:], start, killed
    if chunk == b"\x19":  # Ctrl+Y
        if not kill_buffer:
            return buffer, cursor, kill_buffer
        return buffer[:cursor] + kill_buffer + buffer[cursor:], cursor + len(kill_buffer), kill_buffer
    return None


def _line_start_index(text: str, cursor: int) -> int:
    return text.rfind("\n", 0, cursor) + 1


def _line_end_index(text: str, cursor: int) -> int:
    index = text.find("\n", cursor)
    return len(text) if index == -1 else index


def _move_cursor_vertical(text: str, cursor: int, direction: int) -> int:
    start = _line_start_index(text, cursor)
    column = cursor - start
    if direction < 0:
        if start == 0:
            return cursor
        previous_end = start - 1
        previous_start = _line_start_index(text, previous_end)
        return min(previous_start + column, previous_end)
    current_end = _line_end_index(text, cursor)
    if current_end >= len(text):
        return cursor
    next_start = current_end + 1
    next_end = _line_end_index(text, next_start)
    return min(next_start + column, next_end)


def _clear_prompt_lines(line_count: int, *, rows_below_cursor: int = 0) -> None:
    if line_count <= 0:
        return
    if rows_below_cursor > 0:
        sys.stderr.write("\r")
        sys.stderr.write(f"\033[{rows_below_cursor}B")
    if line_count > 1:
        sys.stderr.write(f"\r\033[{line_count - 1}A")
    else:
        sys.stderr.write("\r")
    for index in range(line_count):
        sys.stderr.write("\r\033[2K")
        if index < line_count - 1:
            sys.stderr.write("\033[1B")
    if line_count > 1:
        sys.stderr.write(f"\r\033[{line_count - 1}A")
    else:
        sys.stderr.write("\r")
    sys.stderr.flush()


def _read_tty_chunk(fd: int, pending: bytes) -> tuple[bytes, bytes]:
    if pending:
        return pending[:1], pending[1:]
    try:
        data = os.read(fd, 4096)
    except Exception:
        return b"", b""
    return data[:1], data[1:]


def _has_pending_input(fd: int, pending: bytes) -> bool:
    """True if more bytes are already available without blocking.

    Used by readers to keep processing a burst (paste, long voice input)
    before redrawing the prompt, so a multi-kilobyte paste redraws once
    instead of once per byte.
    """
    if pending:
        return True
    try:
        readable, _, _ = select.select([fd], [], [], 0)
    except Exception:
        return False
    return bool(readable)


def _read_escape_sequence(fd: int, pending: bytes) -> tuple[bytes, bytes]:
    sequence = b"\x1b"
    deadline = time.monotonic() + 0.03
    while time.monotonic() < deadline:
        if pending:
            sequence += pending[:1]
            pending = pending[1:]
            split_at = _complete_prompt_escape_sequence_length(sequence)
            if split_at is not None:
                return sequence[:split_at], sequence[split_at:] + pending
            continue
        readable, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not readable:
            break
        data = os.read(fd, 1024)
        if not data:
            break
        pending += data
    return sequence, pending


_COMPLETE_PROMPT_ESCAPE_SEQUENCES = (
    b"\x1b[200~",
    b"\x1b[D",
    b"\x1b[C",
    b"\x1b[A",
    b"\x1b[B",
    b"\x1b[H",
    b"\x1b[F",
    b"\x1b[1~",
    b"\x1b[3~",
    b"\x1b[4~",
    b"\x1bOA",
    b"\x1bOB",
    b"\x1bOD",
    b"\x1bOC",
    b"\x1bOH",
    b"\x1bOF",
)


def _complete_prompt_escape_sequence_length(sequence: bytes) -> int | None:
    for known in _COMPLETE_PROMPT_ESCAPE_SEQUENCES:
        if sequence.startswith(known):
            return len(known)
    return None


def _read_bracketed_paste(fd: int, sequence: bytes, pending: bytes) -> tuple[bytes, bytes]:
    start = b"\x1b[200~"
    end = b"\x1b[201~"
    data = sequence[len(start) :] if sequence.startswith(start) else b""
    while True:
        marker = data.find(end)
        if marker != -1:
            pasted = data[:marker]
            rest = data[marker + len(end) :]
            return pasted, rest + pending
        if pending:
            data += pending
            pending = b""
            continue
        chunk = os.read(fd, 1024)
        if not chunk:
            return data, b""
        data += chunk


def _normalize_optional_prompt(prompt: str | None) -> str | None:
    if prompt is None:
        return None
    return prompt.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class _SlashCommandDef:
    name: str
    description: str
    supports_inline_args: bool = False
    available_during_task: bool = True
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedSlashCommand:
    name: str
    rest: str
    command: _SlashCommandDef


@dataclass(frozen=True)
class _InteractiveSlashResult:
    handled: bool
    exit: bool = False
    status: int = 0
    prompt: str | None = None
    session: VolleySession | None = None
    run_goal_continuation: bool = False
    printed_transcript: bool = False


@dataclass(frozen=True)
class _SlashPaletteRow:
    display_name: str
    command: _SlashCommandDef
    description: str


_SLASH_COMMANDS: tuple[_SlashCommandDef, ...] = (
    _SlashCommandDef("model", "choose what model and reasoning effort to use", available_during_task=False),
    _SlashCommandDef("fast", "Fastest inference with increased plan usage", available_during_task=False),
    _SlashCommandDef("ide", "include current selection, open files, and other context from your IDE", supports_inline_args=True),
    _SlashCommandDef("permissions", "choose what Volley is allowed to do", available_during_task=False),
    _SlashCommandDef("keymap", "remap TUI shortcuts", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("vim", "toggle Vim mode for the composer", available_during_task=False),
    _SlashCommandDef("setup-default-sandbox", "set up elevated agent sandbox", available_during_task=False),
    _SlashCommandDef("sandbox-add-read-dir", "let sandbox read a directory", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("experimental", "toggle experimental features", available_during_task=False),
    _SlashCommandDef("approve", "approve one retry of a recent auto-review denial"),
    _SlashCommandDef("memories", "configure memory use and generation", available_during_task=False),
    _SlashCommandDef("skills", "use skills to improve how Volley performs specific tasks"),
    _SlashCommandDef("hooks", "view and manage lifecycle hooks"),
    _SlashCommandDef("review", "review my current changes and find issues", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("rename", "rename the current thread", supports_inline_args=True),
    _SlashCommandDef("new", "start a new chat during a conversation", available_during_task=False),
    _SlashCommandDef("resume", "resume a saved chat", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("fork", "fork the current chat", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("init", "create an AGENTS.md file with instructions for Volley", available_during_task=False),
    _SlashCommandDef("compact", "summarize conversation to prevent hitting the context limit", available_during_task=False),
    _SlashCommandDef("plan", "switch to Plan mode", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("goal", "set or view the goal for a long-running task", supports_inline_args=True),
    _SlashCommandDef("agent", "switch the active agent thread"),
    _SlashCommandDef("side", "start a side conversation in an ephemeral fork", supports_inline_args=True),
    _SlashCommandDef("btw", "start a side conversation in an ephemeral fork", supports_inline_args=True),
    _SlashCommandDef("multi-agents", "switch the active agent thread", aliases=("subagents",)),
    _SlashCommandDef("copy", "copy last response as markdown"),
    _SlashCommandDef("raw", "toggle raw scrollback mode for copy-friendly terminal selection", supports_inline_args=True),
    _SlashCommandDef("diff", "show git diff (including untracked files)"),
    _SlashCommandDef("mention", "mention a file"),
    _SlashCommandDef("status", "show current session configuration and token usage"),
    _SlashCommandDef("debug-config", "show config layers and requirement sources for debugging"),
    _SlashCommandDef("title", "configure which items appear in the terminal title"),
    _SlashCommandDef("statusline", "configure which items appear in the status line"),
    _SlashCommandDef("theme", "choose a syntax highlighting theme", supports_inline_args=True, available_during_task=False),
    _SlashCommandDef("pets", "choose or hide the terminal pet", supports_inline_args=True, available_during_task=False, aliases=("pet",)),
    _SlashCommandDef("mcp", "list configured MCP tools; use /mcp verbose for details", supports_inline_args=True),
    _SlashCommandDef("apps", "manage apps"),
    _SlashCommandDef("plugins", "browse plugins"),
    _SlashCommandDef("logout", "log out of Volley", available_during_task=False),
    _SlashCommandDef("quit", "exit Volley"),
    _SlashCommandDef("exit", "exit Volley"),
    _SlashCommandDef("feedback", "send logs to maintainers"),
    _SlashCommandDef("rollout", "print the rollout file path"),
    _SlashCommandDef("ps", "list background terminals"),
    _SlashCommandDef("stop", "stop all background terminals", aliases=("clean",)),
    _SlashCommandDef("clear", "clear the terminal and start a new chat", available_during_task=False),
    _SlashCommandDef("personality", "choose a communication style for Volley", available_during_task=False),
    _SlashCommandDef("realtime", "toggle realtime voice mode (experimental)"),
    _SlashCommandDef("settings", "configure realtime microphone/speaker"),
    _SlashCommandDef("test-approval", "test approval request"),
    _SlashCommandDef("debug-m-drop", "DO NOT USE", available_during_task=False),
    _SlashCommandDef("debug-m-update", "DO NOT USE", available_during_task=False),
)

_SLASH_COMMAND_BY_NAME: dict[str, _SlashCommandDef] = {
    alias: command
    for command in _SLASH_COMMANDS
    for alias in (command.name, *command.aliases)
}

_SLASH_IMPLEMENTED_NAMES = {
    "model",
    "fast",
    "new",
    "resume",
    "fork",
    "init",
    "compact",
    "plan",
    "goal",
    "status",
    "theme",
    "quit",
    "exit",
    "rollout",
    "ps",
    "stop",
    "clear",
}
_SLASH_AVAILABLE_COMMAND_BY_NAME: dict[str, _SlashCommandDef] = {
    alias: command
    for command in _SLASH_COMMANDS
    if command.name in _SLASH_IMPLEMENTED_NAMES
    for alias in (command.name, *command.aliases)
}

_SLASH_POPUP_ALIAS_NAMES = {"quit", "btw"}
_SLASH_POPUP_HIDDEN_NAMES = {"apps"}
_SLASH_POPUP_MAX_ROWS = 8


def _slash_first_line(buffer: str) -> str:
    return buffer.split("\n", 1)[0]


def _slash_command_under_cursor(buffer: str, cursor: int) -> tuple[str, str, str, int] | None:
    first_line_end = buffer.find("\n")
    if first_line_end == -1:
        first_line_end = len(buffer)
    if cursor > first_line_end:
        return None
    first_line = buffer[:first_line_end]
    if not first_line.startswith("/"):
        return None
    name_start = 1
    rest_of_line = first_line[name_start:]
    whitespace = next((idx for idx, char in enumerate(rest_of_line) if char.isspace()), None)
    name_end = len(first_line) if whitespace is None else name_start + whitespace
    if cursor > name_end:
        return None
    name = first_line[name_start:name_end]
    rest_start = name_end
    for index in range(name_end, len(first_line)):
        if not first_line[index].isspace():
            rest_start = index
            break
    else:
        rest_start = name_end
    rest = first_line[rest_start:]
    return name, rest, first_line, name_end


def _slash_palette_key(buffer: str, cursor: int, dismissed_for: str | None = None) -> str:
    parsed = _slash_command_under_cursor(buffer, cursor)
    if parsed is None:
        return ""
    name, rest, first_line, _ = parsed
    if dismissed_for == first_line:
        return ""
    return f"{name}\0{rest}\0{first_line}"


def _slash_palette_rows_for_buffer(
    buffer: str,
    cursor: int,
    *,
    dismissed_for: str | None = None,
) -> list[_SlashPaletteRow]:
    parsed = _slash_command_under_cursor(buffer, cursor)
    if parsed is None:
        return []
    name, rest, first_line, _ = parsed
    if dismissed_for == first_line:
        return []
    if not name and rest:
        return []
    return _slash_palette_rows_for_name(name)


def _slash_palette_rows_for_name(name: str) -> list[_SlashPaletteRow]:
    query = name.strip().lower()
    if not query:
        return [
            _SlashPaletteRow(command.name, command, command.description)
            for command in _SLASH_COMMANDS
            if _slash_command_visible_in_popup(command, default_list=True)
        ]

    exact: list[_SlashPaletteRow] = []
    prefix: list[_SlashPaletteRow] = []
    fuzzy: list[_SlashPaletteRow] = []
    seen: set[str] = set()
    for command in _SLASH_COMMANDS:
        if not _slash_command_visible_in_popup(command, default_list=False):
            continue
        candidates = (command.name, *command.aliases)
        lowered = [candidate.lower() for candidate in candidates]
        row = _SlashPaletteRow(command.name, command, command.description)
        if any(candidate == query for candidate in lowered):
            if command.name not in seen:
                exact.append(row)
                seen.add(command.name)
            continue
        if any(candidate.startswith(query) for candidate in lowered):
            if command.name not in seen:
                prefix.append(row)
                seen.add(command.name)
            continue
        if any(_is_subsequence(query, candidate) for candidate in lowered):
            if command.name not in seen:
                fuzzy.append(row)
                seen.add(command.name)
    if exact or prefix:
        return [*exact, *prefix]
    return fuzzy


def _slash_command_visible_in_popup(command: _SlashCommandDef, *, default_list: bool) -> bool:
    if command.name not in _SLASH_IMPLEMENTED_NAMES:
        return False
    if command.name in _SLASH_POPUP_HIDDEN_NAMES or command.name.startswith("debug"):
        return False
    if default_list and command.name in _SLASH_POPUP_ALIAS_NAMES:
        return False
    return True


def _is_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return True
    iterator = iter(haystack)
    return all(char in iterator for char in needle)


def _slash_palette_display_lines(
    buffer: str,
    cursor: int,
    *,
    selected_index: int,
    dismissed_for: str | None,
    style: "_AnsiStyle",
    width: int,
) -> list[str]:
    rows = _slash_palette_rows_for_buffer(buffer, cursor, dismissed_for=dismissed_for)
    if not rows:
        return []
    selected_index = max(0, min(selected_index, len(rows) - 1))
    start = _slash_visible_start(selected_index, len(rows), _SLASH_POPUP_MAX_ROWS)
    visible_rows = rows[start : start + _SLASH_POPUP_MAX_ROWS]
    name_width = max(_visible_len(f"/{row.display_name}") for row in visible_rows)
    lines = [""]
    for offset, row in enumerate(visible_rows):
        index = start + offset
        lines.extend(
            _slash_palette_row_lines(
                row,
                name_width=name_width,
                selected=index == selected_index,
                style=style,
                width=width,
            )
        )
    return lines


def _slash_visible_start(selected_index: int, total: int, max_rows: int) -> int:
    if total <= max_rows:
        return 0
    half = max_rows // 2
    return max(0, min(selected_index - half, total - max_rows))


def _slash_palette_row_lines(
    row: _SlashPaletteRow,
    *,
    name_width: int,
    selected: bool,
    style: "_AnsiStyle",
    width: int,
) -> list[str]:
    plain_name = f"/{row.display_name}"
    padded_name = _pad_visible(plain_name, name_width)
    prefix = f"  {padded_name}  "
    safe_width = _terminal_safe_width(width)
    desc_width = max(12, safe_width - _visible_len(prefix))
    desc_lines = _wrap_ansi_line(row.description, desc_width)
    rendered: list[str] = []
    for index, desc in enumerate(desc_lines):
        line_prefix = prefix if index == 0 else f"  {' ' * name_width}  "
        desc_text = style.dim(desc) if desc else ""
        line = f"{line_prefix}{desc_text}".rstrip()
        if selected:
            line = style.inverse(_pad_visible(line, min(safe_width, max(_visible_len(line), 1))))
        rendered.append(line)
    return rendered


def _pad_visible(text: str, width: int) -> str:
    padding = max(0, width - _visible_len(text))
    return text + (" " * padding)


def _slash_selected_completion_text(
    buffer: str,
    cursor: int,
    selected_index: int,
    *,
    dismissed_for: str | None = None,
    trailing_space: bool,
) -> str | None:
    parsed = _slash_command_under_cursor(buffer, cursor)
    if parsed is None:
        return None
    _, _, first_line, name_end = parsed
    rows = _slash_palette_rows_for_buffer(buffer, cursor, dismissed_for=dismissed_for)
    if not rows:
        return None
    row = rows[max(0, min(selected_index, len(rows) - 1))]
    replacement = f"/{row.display_name}"
    first_line_end = buffer.find("\n")
    if first_line_end == -1:
        first_line_end = len(buffer)
    completed_first_line = replacement + first_line[name_end:]
    if trailing_space and (
        len(completed_first_line) == len(replacement)
        or not completed_first_line[len(replacement)].isspace()
    ):
        completed_first_line = f"{replacement} " + completed_first_line[len(replacement):]
    return completed_first_line + buffer[first_line_end:]


def _slash_completed_cursor(buffer: str) -> int:
    first_line_end = buffer.find("\n")
    if first_line_end == -1:
        return len(buffer)
    return first_line_end


def _parse_slash_name(line: str) -> tuple[str, str] | None:
    first_line = line.split("\n", 1)[0]
    stripped = first_line.removeprefix("/")
    if stripped == first_line:
        return None
    name = stripped
    rest = ""
    for index, char in enumerate(stripped):
        if char.isspace():
            name = stripped[:index]
            rest = stripped[index:].lstrip()
            break
    if not name:
        return None
    return name, rest


def _parse_interactive_slash(prompt: str) -> _ParsedSlashCommand | None:
    parsed = _parse_slash_name(prompt.lstrip())
    if parsed is None:
        return None
    name, rest = parsed
    if "/" in name:
        return None
    command = _SLASH_AVAILABLE_COMMAND_BY_NAME.get(name.lower())
    if command is None:
        return None
    if rest and not command.supports_inline_args:
        return None
    return _ParsedSlashCommand(name=name.lower(), rest=rest, command=command)


def _handle_interactive_slash_command(
    session: VolleySession,
    prompt: str,
    *,
    color_mode: str = "auto",
    queued_prompts: deque[str] | None = None,
) -> _InteractiveSlashResult:
    value = prompt.lstrip()
    if value.strip() == "/help":
        _print_chat_help()
        return _InteractiveSlashResult(True)
    if value == "/default" or value.startswith("/default ") or value == "/code" or value.startswith("/code "):
        raw = "/default" if value.startswith("/default") else "/code"
        remainder = value[len(raw) :].lstrip() or None
        _set_session_collaboration_mode(session, "Default")
        print("Switched to Default mode.", file=sys.stderr, flush=True)
        return _InteractiveSlashResult(True, prompt=remainder)

    parsed_name = _parse_slash_name(value)
    slash = _parse_interactive_slash(value)
    if slash is None:
        if parsed_name is not None and "/" not in parsed_name[0]:
            name, _ = parsed_name
            command = _SLASH_COMMAND_BY_NAME.get(name.lower())
            if command is not None and command.name not in _SLASH_IMPLEMENTED_NAMES:
                print(
                    f"Command '/{name}' is not available in this Python CLI.",
                    file=sys.stderr,
                    flush=True,
                )
                return _InteractiveSlashResult(True)
            print(
                f"Unrecognized command '/{name}'. Type \"/\" for a list of supported commands.",
                file=sys.stderr,
                flush=True,
            )
            return _InteractiveSlashResult(True)
        return _InteractiveSlashResult(False)

    command = slash.command.name
    if command in {"exit", "quit"}:
        return _InteractiveSlashResult(True, exit=True, status=0)
    if command == "clear":
        _clear_terminal()
        return _InteractiveSlashResult(True, session=_new_chat_session(session))
    if command == "new":
        fresh = _new_chat_session(session)
        print(f"Started new chat {fresh.state.thread_id}.", file=sys.stderr, flush=True)
        return _InteractiveSlashResult(True, session=fresh)
    if command == "compact":
        if _interactive_turn_controls_available():
            _run_compact_human_interactive(session, color_mode=color_mode, queued_prompts=queued_prompts)
        else:
            _run_compact_human(session, color_mode=color_mode)
        return _InteractiveSlashResult(True)
    if command == "plan":
        _set_session_collaboration_mode(session, "Plan")
        print("Switched to Plan mode.", file=sys.stderr, flush=True)
        return _InteractiveSlashResult(True, prompt=slash.rest.strip() or None)
    if command == "resume":
        resumed = _interactive_resume_session(session, slash.rest.strip(), color_mode=color_mode)
        return _InteractiveSlashResult(True, session=resumed, printed_transcript=resumed is not None)
    if command == "fork":
        forked = _interactive_fork_session(session, slash.rest.strip(), color_mode=color_mode)
        return _InteractiveSlashResult(True, session=forked, printed_transcript=forked is not None)
    if command == "goal":
        should_continue = _handle_goal_slash(session, slash.rest.strip(), color_mode=color_mode)
        return _InteractiveSlashResult(True, run_goal_continuation=should_continue)
    if command == "status":
        _print_chat_status(session, color_mode=color_mode)
        return _InteractiveSlashResult(True)
    if command == "rollout":
        print(f"Current rollout path: {session.state.rollout_path()}", file=sys.stderr, flush=True)
        return _InteractiveSlashResult(True)
    if command == "ps":
        _handle_ps_slash(session)
        return _InteractiveSlashResult(True)
    if command == "stop":
        _handle_stop_slash(session)
        return _InteractiveSlashResult(True)
    if command == "init":
        init_target = session.config.resolved_cwd() / "AGENTS.md"
        if init_target.exists():
            print("AGENTS.md already exists here. Skipping /init to avoid overwriting it.", file=sys.stderr, flush=True)
            return _InteractiveSlashResult(True)
        return _InteractiveSlashResult(True, prompt=_read_init_command_prompt())
    if command == "fast":
        _handle_fast_slash(session, slash.rest, color_mode=color_mode)
        return _InteractiveSlashResult(True)
    if command == "model":
        _handle_model_slash(session, slash.rest, color_mode=color_mode)
        return _InteractiveSlashResult(True)
    if command == "theme":
        _handle_theme_slash(slash.rest)
        return _InteractiveSlashResult(True)
    if command == "raw":
        arg = slash.rest.strip().lower()
        if arg and arg not in {"on", "off"}:
            print("Usage: /raw [on|off]", file=sys.stderr, flush=True)
        else:
            print("Command '/raw' is not available in this Python CLI.", file=sys.stderr, flush=True)
        return _InteractiveSlashResult(True)

    print(
        f"Command '/{slash.name}' is not available in this Python CLI.",
        file=sys.stderr,
        flush=True,
    )
    return _InteractiveSlashResult(True)


def _handle_fast_slash(session: VolleySession, rest: str, *, color_mode: str = "auto") -> None:
    style = _AnsiStyle(_should_use_color(color_mode))
    if rest.strip():
        print("Usage: /fast", file=sys.stderr, flush=True)
        return
    command = _fast_service_tier_command(session.config)
    if command is None:
        print("Fast mode is not available for the current model.", file=sys.stderr, flush=True)
        return
    current = session.config.resolved_service_tier()
    next_tier = None if current == command["id"] else command["id"]
    _set_session_service_tier(session, next_tier)
    _persist_service_tier_selection(next_tier)
    label = next_tier or "default"
    print(f"Service tier set to {style.bold(label)}", file=sys.stderr, flush=True)


def _fast_service_tier_command(config: VolleyConfig) -> dict[str, str] | None:
    if not config.resolved_model_supports_fast_mode():
        return None
    for tier in config.resolved_model_service_tiers():
        if tier.get("id") == "priority" or tier.get("name", "").lower() == "fast":
            return {
                "id": normalize_service_tier(tier["id"]) or tier["id"],
                "name": tier.get("name", "fast").lower(),
                "description": tier.get("description") or "Fastest inference with increased plan usage",
            }
    return {
        "id": "priority",
        "name": "fast",
        "description": "Fastest inference with increased plan usage",
    }


def _persist_service_tier_selection(service_tier: str | None) -> None:
    path = _default_config_path()
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return
    persisted_value = "fast" if service_tier == "priority" else service_tier
    next_text = _update_service_tier_toml(text, persisted_value)
    next_text = _update_notices_fast_default_opt_out_toml(next_text, service_tier is None)
    if next_text == text:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(next_text, encoding="utf-8")
    except OSError:
        return


def _update_service_tier_toml(text: str, service_tier: str | None) -> str:
    lines = text.splitlines(keepends=True)
    in_root = True
    key_pattern = re.compile(r"^(\s*)service_tier\s*=")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_root = False
        if in_root and key_pattern.match(line):
            if service_tier is None:
                del lines[index]
            else:
                newline = "\n" if line.endswith("\n") else ""
                lines[index] = f'service_tier = "{service_tier}"{newline}'
            return "".join(lines)
    if service_tier is None:
        return text
    insert_at = next(
        (index for index, line in enumerate(lines) if line.strip().startswith("[") and line.strip().endswith("]")),
        len(lines),
    )
    prefix = "" if insert_at == 0 or (insert_at > 0 and lines[insert_at - 1].endswith("\n")) else "\n"
    lines.insert(insert_at, f'{prefix}service_tier = "{service_tier}"\n')
    return "".join(lines)


def _update_notices_fast_default_opt_out_toml(text: str, opt_out: bool) -> str:
    lines = text.splitlines(keepends=True)
    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[notices]":
            section_start = index
            section_end = len(lines)
            continue
        if section_start is not None and index > section_start and stripped.startswith("[") and stripped.endswith("]"):
            section_end = index
            break
    value = "true" if opt_out else "false"
    key_pattern = re.compile(r"^(\s*)fast_default_opt_out\s*=")
    if section_start is not None:
        for index in range(section_start + 1, section_end):
            if key_pattern.match(lines[index]):
                newline = "\n" if lines[index].endswith("\n") else ""
                lines[index] = f"fast_default_opt_out = {value}{newline}"
                return "".join(lines)
        insert_line = section_end
        prefix = "" if insert_line == 0 or lines[insert_line - 1].endswith("\n") else "\n"
        lines.insert(insert_line, f"{prefix}fast_default_opt_out = {value}\n")
        return "".join(lines)
    suffix = "" if not text or text.endswith("\n") else "\n"
    spacer = "" if not text else "\n"
    return f"{text}{suffix}{spacer}[notices]\nfast_default_opt_out = {value}\n"


def _handle_goal_slash(session: VolleySession, rest: str, *, color_mode: str = "auto") -> bool:
    style = _AnsiStyle(_should_use_color(color_mode))
    renderer = _HumanEventRenderer(color_mode=color_mode)
    runtime = getattr(session, "goals", None)
    available = getattr(runtime, "tools_available", None)
    if not callable(available) or not available():
        renderer.render_info_message("Usage: /goal <objective>", "Goals need a saved session. This session is temporary.")
        return False
    rest = rest.strip()
    if not rest:
        goal = runtime.get_goal()
        if goal is None:
            renderer.render_info_message("Usage: /goal <objective>", "No goal is currently set.")
            return False
        _print_goal_summary(goal, style)
        return False
    try:
        command, remainder = _parse_goal_command(rest)
        if command == "clear":
            cleared, events = runtime.clear_goal_external()
            _emit_goal_runtime_events(session, events)
            if cleared:
                renderer.render_info_message("Goal cleared")
            else:
                renderer.render_info_message("No goal to clear", "This thread does not currently have a goal.")
            return False
        if command in {"pause", "resume"}:
            status: GoalStatus = "paused" if command == "pause" else "active"
            goal, events = runtime.set_goal_external(status=status)
            _emit_goal_runtime_events(session, events)
            renderer.render_info_message(f"Goal {_goal_status_label(goal.status)}", goal_summary(goal))
            return goal.status == "active"
        if command == "edit":
            if not remainder.strip():
                renderer.render_info_message("Usage: /goal edit <objective>")
                return False
            goal, events = runtime.set_goal_external(objective=remainder.strip())
            _emit_goal_runtime_events(session, events)
            renderer.render_info_message(f"Goal {_goal_status_label(goal.status)}", goal_summary(goal))
            return goal.status == "active"
        objective = rest
        existing = runtime.get_goal()
        replace = existing is not None and existing.status == "complete"
        if existing is not None and existing.status != "complete":
            if not _confirm_replace_goal(existing.objective, objective, color_mode=color_mode):
                renderer.render_info_message("Cancelled.")
                return False
            replace = True
        goal, events = runtime.set_goal_external(objective=objective, status="active", token_budget=None, replace_existing=replace)
        _emit_goal_runtime_events(session, events)
        renderer.render_info_message(f"Goal {_goal_status_label(goal.status)}", goal_summary(goal))
        return goal.status == "active"
    except Exception as exc:
        renderer.render_error(f"Failed to update thread goal: {_exception_display_message(exc)}")
        return False


def _parse_goal_command(rest: str) -> tuple[str | None, str]:
    first, _, remainder = rest.partition(" ")
    lowered = first.lower()
    if lowered in {"edit", "pause", "resume", "clear"}:
        return lowered, remainder
    return None, rest


def _emit_goal_runtime_events(session: VolleySession, events: tuple[Any, ...]) -> None:
    for event in events:
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if isinstance(event_type, str) and isinstance(payload, dict):
            session.state.emit(event_type, **payload)


def _print_goal_summary(goal: Any, style: "_AnsiStyle") -> None:
    print(style.bold("Goal"), file=sys.stderr)
    print(f"{style.dim('Status:')} {_goal_status_label(goal.status)}", file=sys.stderr)
    print(f"{style.dim('Objective:')} {goal.objective}", file=sys.stderr)
    print(f"{style.dim('Time used:')} {_format_goal_elapsed_seconds(goal.time_used_seconds)}", file=sys.stderr)
    print(f"{style.dim('Tokens used:')} {_format_tokens_compact(goal.tokens_used)}", file=sys.stderr)
    if goal.token_budget is not None:
        print(f"{style.dim('Token budget:')} {_format_tokens_compact(goal.token_budget)}", file=sys.stderr)
    if goal.status == "active":
        hint = "Commands: /goal edit, /goal pause, /goal clear"
    elif goal.status in {"paused", "blocked", "usage_limited"}:
        hint = "Commands: /goal edit, /goal resume, /goal clear"
    else:
        hint = "Commands: /goal edit, /goal clear"
    print(file=sys.stderr)
    print(style.dim(hint), file=sys.stderr, flush=True)


def _confirm_replace_goal(current: str, new: str, *, color_mode: str = "auto") -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    print("Replace goal?", file=sys.stderr)
    print(f"Current objective: {current}", file=sys.stderr)
    print(f"New objective: {new}", file=sys.stderr)
    print("Replace current goal? [y/N] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip().lower()
    return answer in {"y", "yes"}


def _goal_status_label(status: str) -> str:
    return {
        "active": "active",
        "paused": "paused",
        "blocked": "blocked",
        "usage_limited": "usage limited",
        "budget_limited": "limited by budget",
        "complete": "complete",
    }.get(GOAL_STATUS_FROM_WIRE.get(status, status), status)


def _format_goal_elapsed_seconds(seconds: int) -> str:
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


def _handle_ps_slash(session: VolleySession) -> None:
    rows = _background_terminal_rows(session)
    if not rows:
        print("No background terminals.", file=sys.stderr, flush=True)
        return
    print("Background terminals:", file=sys.stderr)
    for session_id, status, tty, command in rows:
        tty_label = "tty" if tty else "pipe"
        command_label = _truncate_display_text(_command_display(command), max(20, _terminal_columns() - 24))
        print(f"  {session_id} · {status} · {tty_label} · {command_label}", file=sys.stderr)
    sys.stderr.flush()


def _handle_stop_slash(session: VolleySession) -> None:
    count = len(_background_terminal_rows(session))
    interrupt_all = getattr(session.tools, "interrupt_all", None)
    if callable(interrupt_all):
        interrupt_all()
    if count:
        print(f"Stopped {count} background terminal{'s' if count != 1 else ''}.", file=sys.stderr, flush=True)
    else:
        print("No background terminals.", file=sys.stderr, flush=True)


def _background_terminal_rows(session: VolleySession) -> list[tuple[int, str, bool, str]]:
    runtime = getattr(session, "tools", None)
    sessions = getattr(runtime, "_sessions", None)
    if not isinstance(sessions, dict):
        return []
    lock = getattr(runtime, "_session_lock", None)
    if lock is not None:
        with lock:
            items = list(sessions.items())
    else:
        items = list(sessions.items())
    rows: list[tuple[int, str, bool, str]] = []
    for session_id, running in items:
        process = getattr(running, "process", None)
        returncode = process.poll() if process is not None else None
        status = "running" if returncode is None else f"exit {returncode}"
        rows.append(
            (
                int(session_id),
                status,
                bool(getattr(running, "tty", False)),
                str(getattr(running, "command", "")),
            )
        )
    return rows


def _interactive_resume_session(session: VolleySession, rest: str, *, color_mode: str = "auto") -> VolleySession | None:
    rollout_path = _resolve_interactive_rollout_selector(
        rest,
        session.config,
        title="Resume saved chat",
        color_mode=color_mode,
    )
    if rollout_path is None:
        return None
    resumed = VolleySession.resume_from_rollout(
        rollout_path,
        session.config,
        model_client=session.model_client,
    )
    _render_resumed_transcript(resumed, source_path=rollout_path, color_mode=color_mode)
    return resumed


def _interactive_fork_session(session: VolleySession, rest: str, *, color_mode: str = "auto") -> VolleySession | None:
    if rest:
        rollout_path = _resolve_interactive_rollout_selector(
            rest,
            session.config,
            title="Fork saved chat",
            color_mode=color_mode,
        )
        if rollout_path is None:
            return None
        forked = VolleySession.fork_from_rollout(
            rollout_path,
            session.config,
            model_client=session.model_client,
        )
        _render_resumed_transcript(forked, source_path=rollout_path, color_mode=color_mode)
    else:
        forked = _fork_session_in_memory(session)
    print(
        f"Forked chat {forked.state.thread_id} from {forked.state.forked_from_id or 'current context'}",
        file=sys.stderr,
        flush=True,
    )
    return forked


def _fork_session_in_memory(session: VolleySession) -> VolleySession:
    forked = VolleySession(session.config, model_client=session.model_client)
    forked.state.history = deepcopy(session.state.history)
    forked.state._rollout_seed_history = deepcopy(session.state.history)
    forked.state.forked_from_id = session.state.thread_id
    forked.state.previous_turn_settings = deepcopy(session.state.previous_turn_settings)
    forked.state.reference_context_item = deepcopy(session.state.reference_context_item)
    forked.state.last_token_usage = deepcopy(session.state.last_token_usage)
    forked.state.total_token_usage = session.state.total_token_usage
    forked.state.session_reasoning_tokens = session.state.session_reasoning_tokens
    forked.state.context_carryover_tokens = session.state.context_carryover_tokens
    forked.state.context_carryover_estimated = session.state.context_carryover_estimated
    forked._initial_context_recorded = getattr(session, "_initial_context_recorded", False)
    return forked


def _new_chat_session(session: VolleySession) -> VolleySession:
    return VolleySession(session.config, model_client=session.model_client)


def _resolve_interactive_rollout_selector(
    rest: str,
    config: VolleyConfig,
    *,
    title: str,
    color_mode: str = "auto",
) -> Path | None:
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        print(f"Invalid command arguments: {exc}", file=sys.stderr, flush=True)
        return None
    all_cwds = False
    last = False
    selector: str | None = None
    for token in tokens:
        if token == "--all":
            all_cwds = True
        elif token == "--last":
            last = True
        elif selector is None:
            selector = token
        else:
            print("Usage: /resume [--last|SESSION_ID|ROLLOUT_PATH] [--all]", file=sys.stderr, flush=True)
            return None
    if selector is None and not last:
        return _prompt_rollout_picker(config, title=title, all_cwds=all_cwds, color_mode=color_mode)
    args = argparse.Namespace(session_id=selector, last=last, all_cwds=all_cwds)
    rollout_path = _resolve_resume_rollout(args, config)
    if rollout_path is None:
        missing = selector or "--last"
        print(f"No Volley rollout found for `{missing}`", file=sys.stderr, flush=True)
    return rollout_path


@dataclass
class _RolloutPickerRow:
    path: Path
    preview: str
    thread_id: str
    created_at: float
    updated_at: float
    cwd: str | None
    git_branch: str | None = None


def _prompt_rollout_picker(
    config: VolleyConfig,
    *,
    title: str,
    all_cwds: bool,
    color_mode: str = "auto",
) -> Path | None:
    rows = _rollout_picker_rows(config)
    filtered = _filter_rollout_picker_rows(rows, cwd=config.resolved_cwd(), show_all=all_cwds, query="", sort_key="updated")
    if not filtered:
        print("No saved Volley chats found.", file=sys.stderr, flush=True)
        return None
    if sys.stdin.isatty() and sys.stderr.isatty():
        return _interactive_rollout_picker(
            rows,
            title=title,
            cwd=config.resolved_cwd(),
            initial_all_cwds=all_cwds,
            color_mode=color_mode,
        )
    choices = filtered[:10]
    print(title, file=sys.stderr)
    for index, row in enumerate(choices, start=1):
        print(f"  {index}. {_format_rollout_picker_item(row.path)}", file=sys.stderr)
    print("Select a chat number, or press Enter to cancel: ", end="", file=sys.stderr, flush=True)
    if not sys.stdin.isatty():
        print(file=sys.stderr, flush=True)
        return None
    raw = sys.stdin.readline().strip()
    if not raw:
        print("Cancelled.", file=sys.stderr, flush=True)
        return None
    try:
        index = int(raw)
    except ValueError:
        print(f"Invalid selection `{raw}`.", file=sys.stderr, flush=True)
        return None
    if index < 1 or index > len(choices):
        print(f"Invalid selection `{raw}`.", file=sys.stderr, flush=True)
        return None
    return choices[index - 1].path


def _rollout_picker_rows(config: VolleyConfig) -> list[_RolloutPickerRow]:
    rows: list[_RolloutPickerRow] = []
    for home in _session_search_homes(config):
        for path in _iter_rollout_paths(home):
            reconstruction = _safe_reconstruct_rollout(path)
            if reconstruction is None:
                continue
            meta = reconstruction.session_meta if isinstance(reconstruction.session_meta, dict) else {}
            raw_cwd = meta.get("cwd")
            cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
            rows.append(
                _RolloutPickerRow(
                    path=path,
                    preview=_rollout_preview_text(reconstruction.history),
                    thread_id=_rollout_thread_id(meta) or _rollout_thread_id_from_path(path) or "unknown",
                    created_at=_rollout_created_at(meta, path),
                    updated_at=_safe_mtime(path),
                    cwd=cwd,
                    git_branch=_rollout_git_branch(path),
                )
            )
    return rows


def _filter_rollout_picker_rows(
    rows: list[_RolloutPickerRow],
    *,
    cwd: Path,
    show_all: bool,
    query: str,
    sort_key: str,
) -> list[_RolloutPickerRow]:
    normalized_query = _normalize_picker_query(query)
    filtered: list[_RolloutPickerRow] = []
    for row in rows:
        if not show_all and not _picker_row_cwd_matches(row, cwd):
            continue
        if normalized_query and normalized_query not in _normalize_picker_query(_rollout_picker_search_text(row)):
            continue
        filtered.append(row)
    if sort_key == "created":
        return sorted(filtered, key=lambda row: (row.created_at, row.updated_at, row.path.name), reverse=True)
    return sorted(filtered, key=lambda row: (row.updated_at, row.created_at, row.path.name), reverse=True)


def _interactive_rollout_picker(
    rows: list[_RolloutPickerRow],
    *,
    title: str,
    cwd: Path,
    initial_all_cwds: bool,
    color_mode: str = "auto",
) -> Path | None:
    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
    except Exception:
        return None

    style = _AnsiStyle(_should_use_color(color_mode))
    selected = 0
    offset = 0
    query = ""
    show_all = initial_all_cwds
    sort_key = "updated"
    density = "comfortable"
    toolbar_focus = "filter"
    expanded = False
    rendered_rows = 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def current_rows() -> list[_RolloutPickerRow]:
        return _filter_rollout_picker_rows(rows, cwd=cwd, show_all=show_all, query=query, sort_key=sort_key)

    def clamp_selection(filtered: list[_RolloutPickerRow]) -> None:
        nonlocal selected, offset
        if not filtered:
            selected = 0
            offset = 0
            return
        selected = min(max(selected, 0), len(filtered) - 1)
        visible = _rollout_picker_visible_count(density)
        if selected < offset:
            offset = selected
        elif selected >= offset + visible:
            offset = selected - visible + 1
        offset = min(max(offset, 0), max(0, len(filtered) - visible))

    def render(lines: list[str]) -> None:
        nonlocal rendered_rows
        cols = _terminal_columns()
        _clear_prompt_lines(rendered_rows)
        print("\n".join(lines), end="", file=sys.stderr, flush=True)
        rendered_rows = _prompt_screen_rows(lines, cols)

    def clear() -> None:
        nonlocal rendered_rows
        _clear_prompt_lines(rendered_rows)
        rendered_rows = 0

    def reset_view() -> None:
        nonlocal selected, offset
        selected = 0
        offset = 0

    try:
        _set_raw_keep_opost(fd)
        pending = b""
        while True:
            filtered = current_rows()
            clamp_selection(filtered)
            render(
                _rollout_picker_display_lines(
                    filtered,
                    title=title,
                    style=style,
                    cwd=cwd,
                    show_all=show_all,
                    query=query,
                    sort_key=sort_key,
                    selected=selected,
                    offset=offset,
                    density=density,
                    toolbar_focus=toolbar_focus,
                    expanded=expanded,
                )
            )
            chunk, pending = _read_tty_chunk(fd, pending)
            if chunk == b"":
                return None
            if chunk == b"\x03":
                return None
            if chunk in {b"\r", b"\n"}:
                filtered = current_rows()
                if not filtered:
                    return None
                return filtered[selected].path
            if chunk in {b"\x7f", b"\b"}:
                if query:
                    query = query[:-1]
                    reset_view()
                continue
            if chunk == b"\t":
                toolbar_focus = "sort" if toolbar_focus == "filter" else "filter"
                continue
            if chunk == b"\x0f":
                density = "dense" if density == "comfortable" else "comfortable"
                clamp_selection(current_rows())
                continue
            if chunk == b"\x05":
                expanded = not expanded
                continue
            if chunk == b"\x1b":
                sequence, pending = _read_escape_sequence(fd, pending)
                if sequence == b"\x1b":
                    if query:
                        query = ""
                        reset_view()
                        continue
                    return None
                if sequence in {b"\x1b[A", b"\x1bOA"}:
                    selected = max(0, selected - 1)
                elif sequence in {b"\x1b[B", b"\x1bOB"}:
                    selected = min(max(0, len(current_rows()) - 1), selected + 1)
                elif sequence in {b"\x1b[5~"}:
                    selected = max(0, selected - _rollout_picker_visible_count(density))
                elif sequence in {b"\x1b[6~"}:
                    selected = min(max(0, len(current_rows()) - 1), selected + _rollout_picker_visible_count(density))
                elif sequence in {b"\x1b[D", b"\x1bOD", b"\x1b[C", b"\x1bOC"}:
                    if toolbar_focus == "filter":
                        show_all = not show_all
                    else:
                        sort_key = "created" if sort_key == "updated" else "updated"
                    reset_view()
                continue
            text = decoder.decode(chunk, final=False)
            if text and all(not unicodedata.category(ch).startswith("C") for ch in text):
                query += text
                reset_view()
    except KeyboardInterrupt:
        return None
    finally:
        clear()
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass


def _rollout_picker_display_lines(
    rows: list[_RolloutPickerRow],
    *,
    title: str,
    style: "_AnsiStyle",
    cwd: Path,
    show_all: bool,
    query: str,
    sort_key: str,
    selected: int,
    offset: int,
    density: str,
    toolbar_focus: str,
    expanded: bool,
) -> list[str]:
    cols = _terminal_columns()
    width = max(40, cols)
    out = [
        style.bold(title),
        _rollout_picker_search_line(
            width,
            style=style,
            query=query,
            show_all=show_all,
            sort_key=sort_key,
            toolbar_focus=toolbar_focus,
        ),
        "",
    ]
    if not rows:
        out.append(style.dim("  No matching saved chats. Type to search, press Esc to cancel, or switch Filter to All."))
    else:
        visible_count = _rollout_picker_visible_count(density)
        if offset > 0:
            out.append(style.dim(f"  \u2191 {offset} more"))
        visible = rows[offset : offset + visible_count]
        for visible_index, row in enumerate(visible):
            index = offset + visible_index
            out.extend(
                _rollout_picker_row_lines(
                    row,
                    style=style,
                    width=width,
                    selected=index == selected,
                    density=density,
                    expanded=expanded and index == selected,
                    cwd=cwd,
                )
            )
            if density == "comfortable" and visible_index < len(visible) - 1:
                out.append("")
        remaining = len(rows) - (offset + len(visible))
        if remaining > 0:
            out.append(style.dim(f"  \u2193 {remaining} more"))
    out.append(_rollout_picker_separator(width, rows=rows, selected=selected, style=style))
    out.append(style.dim("enter resume  esc cancel/clear search  tab focus sort/filter  \u2190/\u2192 change option"))
    out.append(style.dim("ctrl+o dense/comfortable  ctrl+e expand  \u2191/\u2193 browse"))
    return out


def _rollout_picker_search_line(
    width: int,
    *,
    style: "_AnsiStyle",
    query: str,
    show_all: bool,
    sort_key: str,
    toolbar_focus: str,
) -> str:
    search = f"Search: {query}" if query else "Type to search"
    filter_label = f"Filter:[{'All' if show_all else 'Cwd'}]"
    sort_label = f"Sort:[{'Created' if sort_key == 'created' else 'Updated'}]"
    if toolbar_focus == "filter":
        filter_label = style.bold(filter_label)
    else:
        filter_label = style.dim(filter_label)
    if toolbar_focus == "sort":
        sort_label = style.bold(sort_label)
    else:
        sort_label = style.dim(sort_label)
    toolbar = f"{filter_label}  {sort_label}"
    gap = max(1, width - _visible_len(search) - _visible_len(toolbar))
    return f"{style.dim(search) if not query else search}{' ' * gap}{toolbar}"


def _rollout_picker_row_lines(
    row: _RolloutPickerRow,
    *,
    style: "_AnsiStyle",
    width: int,
    selected: bool,
    density: str,
    expanded: bool,
    cwd: Path,
) -> list[str]:
    marker = "\u276f " if selected else "  "
    if density == "dense":
        label_width = max(16, width - 14)
        preview = _truncate_display_text(row.preview, label_width)
        line = f"{marker}{_format_picker_relative_time(row.updated_at):>10}  {preview}"
        return [style.yellow(line) if selected else line]

    preview_width = max(20, width - 2)
    preview = _truncate_display_text(row.preview, preview_width)
    head = f"{marker}{preview}"
    if selected:
        head = style.yellow(head)
    meta = _rollout_picker_meta_line(row, cwd=cwd, width=max(12, width - 2))
    lines = [head, style.dim(f"  {meta}")]
    if expanded:
        details = [
            f"id: {row.thread_id}",
            f"path: {row.path}",
        ]
        for detail in details:
            lines.append(style.dim("  " + _truncate_display_text(detail, max(10, width - 2))))
    return lines


def _rollout_picker_meta_line(row: _RolloutPickerRow, *, cwd: Path, width: int) -> str:
    parts = [_format_picker_relative_time(row.updated_at)]
    if row.cwd:
        cwd_label = "." if _picker_row_cwd_matches(row, cwd) else _short_path_display(row.cwd)
        parts.append(cwd_label)
    if row.git_branch:
        parts.append(row.git_branch)
    meta = "  \u00b7  ".join(parts)
    return _truncate_display_text(meta, width)


def _rollout_picker_separator(width: int, *, rows: list[_RolloutPickerRow], selected: int, style: "_AnsiStyle") -> str:
    if not rows:
        return style.dim("\u2500" * width)
    total = len(rows)
    position = min(max(selected + 1, 1), total)
    percent = int(round(position * 100 / total))
    label = f"{position} / {total} \u00b7 {percent}%"
    left_width = max(0, width - _visible_len(label) - 1)
    return style.dim("\u2500" * left_width + " " + label)


def _rollout_picker_visible_count(density: str) -> int:
    try:
        terminal_lines = shutil.get_terminal_size((100, 24)).lines
    except Exception:
        terminal_lines = 24
    body_lines = max(5, terminal_lines - 7)
    if density == "dense":
        return max(5, min(18, body_lines))
    return max(3, min(8, body_lines // 3))


def _format_rollout_picker_item(path: Path) -> str:
    reconstruction = _safe_reconstruct_rollout(path)
    if reconstruction is None:
        thread_id = _rollout_thread_id_from_path(path) or "unknown"
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(_safe_mtime(path)))
        return f"{thread_id} \u00b7 {stamp} \u00b7 unknown cwd"
    meta = reconstruction.session_meta if isinstance(reconstruction.session_meta, dict) else {}
    thread_id = _rollout_thread_id(meta) or _rollout_thread_id_from_path(path) or "unknown"
    cwd = "unknown cwd"
    raw_cwd = meta.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd:
        cwd = raw_cwd
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(_safe_mtime(path)))
    preview = _truncate_display_text(_rollout_preview_text(reconstruction.history), 48)
    return f"{preview} \u00b7 {thread_id} \u00b7 {stamp} \u00b7 {cwd}"


def _rollout_preview_text(history: list[dict[str, Any]]) -> str:
    for role in ("user", "assistant"):
        for item in history:
            if item.get("type") != "message" or item.get("role") != role:
                continue
            text = _message_item_text(item)
            if text and not _is_startup_context_preview(text):
                return _normalize_picker_preview(text)
    return "(no message yet)"


def _is_startup_context_preview(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.startswith("<environment_context>")
        or stripped.startswith("<user_instructions>")
        or stripped.startswith("<subagent_notification>")
    )


def _message_item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                pieces.append(text)
            elif isinstance(part.get("input_text"), str):
                pieces.append(str(part["input_text"]))
        elif isinstance(part, str):
            pieces.append(part)
    return "\n".join(piece for piece in pieces if piece)


def _normalize_picker_preview(text: str) -> str:
    preview = " ".join(text.strip().split())
    return preview or "(no message yet)"


def _rollout_picker_search_text(row: _RolloutPickerRow) -> str:
    return " ".join(
        value
        for value in [row.preview, row.thread_id, row.cwd or "", row.git_branch or "", str(row.path)]
        if value
    )


def _normalize_picker_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _picker_row_cwd_matches(row: _RolloutPickerRow, cwd: Path) -> bool:
    if not row.cwd:
        return False
    try:
        return Path(row.cwd).expanduser().resolve() == cwd.resolve()
    except OSError:
        return False


def _rollout_thread_id_from_path(path: Path) -> str | None:
    name = path.stem
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", name)
    return match.group(1) if match else None


def _rollout_created_at(meta: dict[str, Any], path: Path) -> float:
    raw = meta.get("timestamp")
    if isinstance(raw, str):
        parsed = _parse_iso_timestamp(raw)
        if parsed is not None:
            return parsed
    return _safe_mtime(path)


def _parse_iso_timestamp(raw: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _rollout_git_branch(path: Path) -> str | None:
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            if not isinstance(record, dict) or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            git = payload.get("git")
            if isinstance(git, dict):
                branch = git.get("branch")
                if isinstance(branch, str) and branch:
                    return branch
            return None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _format_picker_relative_time(timestamp: float) -> str:
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 86400 * 30:
        return f"{delta // 86400}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _short_path_display(path: str) -> str:
    try:
        candidate = Path(path).expanduser()
    except Exception:
        return path
    parts = candidate.parts
    if len(parts) <= 3:
        return str(candidate)
    return "\u2026/" + "/".join(parts[-2:])


def _truncate_display_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _visible_len(text) <= width:
        return text
    ellipsis = "\u2026"
    target = max(1, width - _visible_len(ellipsis))
    out = ""
    current = 0
    for char in text:
        char_width = _display_width(char)
        if current + char_width > target:
            break
        out += char
        current += char_width
    return out.rstrip() + ellipsis


def _terminal_interaction_command_snapshot(command: str, width: int) -> str:
    if width < 8:
        return ""
    single_line = " ".join(str(command or "").split())
    if not single_line:
        return ""
    return _truncate_display_text(single_line, width)


def _read_init_command_prompt() -> str:
    path = Path(__file__).resolve().parent / "assets" / "prompts" / "init_command.md"
    return path.read_text(encoding="utf-8")


def _print_chat_status(session: VolleySession, *, color_mode: str = "auto") -> None:
    style = _AnsiStyle(_should_use_color(color_mode))
    rate_limits, rate_limit_error = _status_rate_limits(session)
    print(
        "\n".join(
            _chat_status_panel_lines(
                session,
                style=style,
                rate_limits=rate_limits,
                rate_limit_error=rate_limit_error,
            )
        ),
        file=sys.stderr,
        flush=True,
    )


def _chat_status_panel_lines(
    session: VolleySession,
    *,
    style: "_AnsiStyle" | None = None,
    rate_limits: list[Any] | None = None,
    rate_limit_error: str | None = None,
    terminal_width: int | None = None,
) -> list[str]:
    style = style or _AnsiStyle(False)
    config = session.config
    reasoning = config.resolved_reasoning() or {}
    effort = str(reasoning.get("effort") or "none").lower()
    summary = str(reasoning.get("summary") or "auto").lower()
    model_details = f"reasoning {effort}, summaries {summary}"
    provider = _status_provider_display(config)
    directory = _status_directory_display(config.resolved_cwd())
    permissions = _status_permissions_display(config)
    account = _session_auth_indicator(session, include_fallback=True)
    thread_name = getattr(session.state, "thread_name", None)
    active_context, active_estimated = session.state.active_context_token_status()
    session_context, session_estimated = session.state.session_context_token_status()
    context_window = config.resolved_model_context_window()
    token_usage = _status_token_usage_display(session)
    labels = [
        "Model",
        "Model provider",
        "Fast mode",
        "Directory",
        "Permissions",
        "Agents.md",
        "Account",
        "Thread name",
        "Collaboration mode",
        "Session",
        "Forked from",
        "Token usage",
        "Context window",
        "Session context",
        "Reasoning tokens",
        "Rollout",
        *[label for label, _value in _status_rate_limit_rows(rate_limits, rate_limit_error)],
    ]
    label_width = max(_visible_len(label) for label in labels)
    terminal_width = terminal_width or _terminal_columns()
    available_inner_width = max(24, min(96, terminal_width - 4))
    field_lines: list[str] = [
        f"  >_ {style.bold('Volley')}",
        "",
        style.cyan("Visit https://chatgpt.com/volley/settings/usage for up-to-date"),
        style.cyan("information on rate limits and credits"),
        "",
    ]
    field_lines.extend(_status_field_lines("Model", f"{config.model} ({model_details})", label_width, available_inner_width))
    if provider:
        field_lines.extend(_status_field_lines("Model provider", provider, label_width, available_inner_width))
    fast_status = _fast_status_indicator(config, include_off=True)
    if fast_status:
        field_lines.extend(_status_field_lines("Fast mode", fast_status, label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Directory", directory, label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Permissions", permissions, label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Agents.md", _status_agents_summary(config.resolved_cwd()), label_width, available_inner_width))
    if account:
        field_lines.extend(_status_field_lines("Account", account, label_width, available_inner_width))
    if isinstance(thread_name, str) and thread_name.strip():
        field_lines.extend(_status_field_lines("Thread name", thread_name.strip(), label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Collaboration mode", config.collaboration_mode, label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Session", session.state.thread_id, label_width, available_inner_width))
    if session.state.forked_from_id:
        field_lines.extend(_status_field_lines("Forked from", session.state.forked_from_id, label_width, available_inner_width))
    field_lines.append("")
    if not _session_uses_chatgpt_auth(session) and token_usage:
        field_lines.extend(_status_field_lines("Token usage", token_usage, label_width, available_inner_width))
    if active_context is not None:
        field_lines.extend(
            _status_field_lines(
                "Context window",
                _status_context_display(active_context, active_estimated, context_window),
                label_width,
                available_inner_width,
            )
        )
    if session_context is not None:
        field_lines.extend(
            _status_field_lines(
                "Session context",
                _status_context_display(session_context, session_estimated, context_window),
                label_width,
                available_inner_width,
            )
        )
    reasoning_tokens = session.state.session_reasoning_usage_tokens()
    if reasoning_tokens is not None:
        field_lines.extend(_status_field_lines("Reasoning tokens", _format_tokens_compact(reasoning_tokens), label_width, available_inner_width))
    for label, value in _status_rate_limit_rows(rate_limits, rate_limit_error):
        field_lines.extend(_status_field_lines(label, value, label_width, available_inner_width))
    field_lines.extend(_status_field_lines("Rollout", str(session.state.rollout_path()), label_width, available_inner_width))
    return ["/status", "", *_box_lines(field_lines, terminal_width=terminal_width)]


def _status_rate_limits(session: VolleySession) -> tuple[list[Any] | None, str | None]:
    if not _session_uses_chatgpt_auth(session):
        return None, None
    try:
        from .auth import fetch_chatgpt_rate_limits

        return (
            fetch_chatgpt_rate_limits(
                session.config.resolved_auth_home(),
                base_url=session.config.chatgpt_base_url,
                timeout=3,
            ),
            None,
        )
    except Exception as exc:
        return None, _sanitize_status_limit_error(exc)


def _status_rate_limit_rows(rate_limits: list[Any] | None, error: str | None = None) -> list[tuple[str, str]]:
    if rate_limits:
        rows: list[tuple[str, str]] = []
        for snapshot in rate_limits:
            limit_id = str(getattr(snapshot, "limit_id", "") or "volley")
            prefix = "" if limit_id.lower() == "volley" else f"{limit_id} "
            primary = getattr(snapshot, "primary", None)
            secondary = getattr(snapshot, "secondary", None)
            if primary is not None:
                rows.append((_status_limit_label(primary, secondary=False, prefix=prefix), _status_limit_value(primary)))
            if secondary is not None:
                rows.append((_status_limit_label(secondary, secondary=True, prefix=prefix), _status_limit_value(secondary)))
            credits = getattr(snapshot, "credits", None)
            credit_row = _status_credit_row(credits)
            if credit_row:
                rows.append(credit_row)
        if rows:
            return rows
        return [("Limits", "not available for this account")]
    if error:
        return [("Limits", f"data not available yet ({error})")]
    return [("Limits", "data not available yet")]


def _status_limit_label(window: Any, *, secondary: bool, prefix: str) -> str:
    window_minutes = getattr(window, "window_minutes", None)
    label = _limit_label_for_window(window_minutes, secondary=secondary)
    if label == "5h":
        label = "5h"
    else:
        label = label[:1].upper() + label[1:]
    return f"{prefix}{label} limit"


def _limit_label_for_window(window_minutes: Any, *, secondary: bool) -> str:
    try:
        minutes = int(window_minutes)
    except (TypeError, ValueError):
        return "secondary usage" if secondary else "usage"
    expected = [
        (5 * 60, "5h"),
        (24 * 60, "daily"),
        (7 * 24 * 60, "weekly"),
        (30 * 24 * 60, "monthly"),
        (365 * 24 * 60, "annual"),
    ]
    for target, label in expected:
        if target * 0.95 <= max(0, minutes) <= target * 1.05:
            return label
    return "secondary usage" if secondary else "usage"


def _status_limit_value(window: Any) -> str:
    used = getattr(window, "used_percent", 0.0)
    try:
        remaining = (100.0 - float(used))
    except (TypeError, ValueError):
        remaining = 0.0
    remaining = max(0.0, min(100.0, remaining))
    value = f"{_status_limit_bar(remaining)} {remaining:.0f}% left"
    reset = _status_reset_display(getattr(window, "resets_at", None))
    return f"{value} (resets {reset})" if reset else value


def _status_limit_bar(percent_remaining: float) -> str:
    segments = 20
    filled = min(segments, max(0, round((percent_remaining / 100.0) * segments)))
    return "[" + ("█" * filled) + ("░" * (segments - filled)) + "]"


def _status_reset_display(resets_at: Any) -> str | None:
    try:
        timestamp = int(resets_at)
    except (TypeError, ValueError):
        return None
    dt = _dt.datetime.fromtimestamp(timestamp).astimezone()
    now = _dt.datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return f"{dt.strftime('%H:%M')} on {dt.strftime('%-d %b')}"


def _status_credit_row(credits: Any) -> tuple[str, str] | None:
    if credits is None or not bool(getattr(credits, "has_credits", False)):
        return None
    if bool(getattr(credits, "unlimited", False)):
        return ("Credits", "Unlimited")
    balance = str(getattr(credits, "balance", "") or "").strip()
    if not balance:
        return None
    try:
        value = float(balance)
    except ValueError:
        return None
    if value <= 0:
        return None
    return ("Credits", f"{int(round(value))} credits")


def _status_field_lines(label: str, value: str, label_width: int, inner_width: int) -> list[str]:
    prefix = "  " + _pad_visible(f"{label}:", label_width + 1) + " "
    value_width = max(8, inner_width - _visible_len(prefix))
    wrapped = _wrap_ansi_line(value, value_width)
    if not wrapped:
        return [prefix.rstrip()]
    continuation_prefix = " " * _visible_len(prefix)
    return [
        f"{prefix}{wrapped[0]}",
        *[f"{continuation_prefix}{line}" for line in wrapped[1:]],
    ]


def _box_lines(lines: list[str], *, terminal_width: int | None = None) -> list[str]:
    terminal_width = terminal_width or _terminal_columns()
    max_inner = max(24, min(96, terminal_width - 4))
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(_wrap_ansi_line(line, max_inner))
    inner_width = min(max_inner, max((_visible_len(line) for line in wrapped), default=0))
    boxed = ["╭" + ("─" * (inner_width + 2)) + "╮"]
    boxed.extend(f"│ {_pad_visible(line, inner_width)} │" for line in wrapped)
    boxed.append("╰" + ("─" * (inner_width + 2)) + "╯")
    return boxed


def _status_provider_display(config: VolleyConfig) -> str | None:
    provider = config.model_provider_id
    if provider == "openai" and not config.openai_base_url:
        return None
    if provider == "openai" and config.openai_base_url:
        return f"OpenAI - {config.openai_base_url.rstrip('/')}"
    if provider == "gemini" and config.gemini_base_url:
        return f"Gemini - {config.gemini_base_url.rstrip('/')}"
    return provider


def _status_directory_display(path: Path) -> str:
    try:
        home = Path.home().resolve()
        if path == home:
            return "~"
        try:
            rel = path.relative_to(home)
            return f"~/{rel}"
        except ValueError:
            return str(path)
    except Exception:
        return str(path)


def _status_permissions_display(config: VolleyConfig) -> str:
    approval = config.approval_policy
    if config.sandbox == "read-only":
        return f"Read Only ({approval})"
    if config.sandbox == "workspace-write":
        suffix = " with network access" if config.network_access == "enabled" else ""
        return f"Workspace{suffix} ({approval})"
    if config.sandbox == "danger-full-access":
        return "Full Access" if approval == "never" else f"No Sandbox ({approval})"
    return f"Custom ({config.sandbox}, {approval})"


def _status_agents_summary(cwd: Path) -> str:
    try:
        project_root = next((ancestor for ancestor in (cwd, *cwd.parents) if (ancestor / ".git").exists()), cwd)
        dirs = [cwd]
        while dirs[-1] != project_root and dirs[-1].parent != dirs[-1]:
            dirs.append(dirs[-1].parent)
        dirs.reverse()
        paths: list[Path] = []
        for directory in dirs:
            for filename in ("AGENTS.override.md", "AGENTS.md"):
                candidate = directory / filename
                if candidate.exists() and candidate.is_file() and candidate.read_text(encoding="utf-8").strip():
                    paths.append(candidate)
                    break
        if not paths:
            return "<none>"
        return ", ".join(_relative_status_path(path, cwd) for path in paths)
    except Exception:
        return "<none>"


def _relative_status_path(path: Path, cwd: Path) -> str:
    try:
        if path.parent == cwd:
            return path.name
        return str(path.relative_to(cwd))
    except ValueError:
        try:
            return os.path.relpath(path, cwd)
        except ValueError:
            return str(path)


def _status_token_usage_display(session: VolleySession) -> str | None:
    usage = session.state.last_token_usage or {}
    total = session.state.session_usage_tokens()
    if total is None:
        return None
    input_tokens = _usage_int(usage, "input_tokens")
    cached = _usage_int(usage, "cached_input_tokens")
    if cached is None:
        details = usage.get("input_tokens_details")
        if isinstance(details, dict):
            cached = _usage_int(details, "cached_tokens")
    output_tokens = _usage_int(usage, "output_tokens") or 0
    non_cached_input = max(0, (input_tokens or 0) - (cached or 0))
    return (
        f"{_format_tokens_compact(total)} total  "
        f"({_format_tokens_compact(non_cached_input)} input + {_format_tokens_compact(output_tokens)} output)"
    )


def _status_context_display(tokens: int, estimated: bool, context_window: int | None) -> str:
    marker = " approx" if estimated else ""
    if context_window:
        remaining = max(0, min(100, round(100 - (tokens / context_window) * 100)))
        return f"{remaining}% left ({_format_tokens_compact(tokens)} used / {_format_tokens_compact(context_window)}){marker}"
    return f"{_format_tokens_compact(tokens)}{marker}"


def _usage_int(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _session_uses_chatgpt_auth(session: VolleySession) -> bool:
    label = _session_auth_indicator(session, include_fallback=False)
    if not isinstance(label, str):
        return False
    return "chatgpt" in label.lower()


def _sanitize_status_limit_error(exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")
    message = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", message)
    message = re.sub(r"(sk-[A-Za-z0-9_-]{8,})", "[redacted-api-key]", message)
    if len(message) > 120:
        message = message[:117] + "..."
    return message or type(exc).__name__


def _interactive_command(prompt: str) -> str | None:
    value = prompt.strip()
    if value in {"/exit", "/quit", ":q"}:
        return "exit"
    if value == "/help":
        return "help"
    if value == "/clear":
        return "clear"
    if value == "/compact":
        return "compact"
    if value == "/plan":
        return "plan"
    if value in {"/default", "/code"}:
        return "default"
    return None


def _interactive_mode_command(prompt: str) -> tuple[str, str | None] | None:
    value = prompt.lstrip()
    for raw, mode in (("/plan", "Plan"), ("/default", "Default"), ("/code", "Default")):
        if value == raw:
            return mode, None
        if value.startswith(f"{raw} "):
            return mode, value[len(raw) :].lstrip() or None
    return None


def _print_chat_help() -> None:
    local_commands = [
        command
        for command in _SLASH_COMMANDS
        if command.name in _SLASH_IMPLEMENTED_NAMES and command.name not in _SLASH_POPUP_ALIAS_NAMES
    ]
    local = ", ".join(f"/{command.name}" for command in local_commands[:18])
    print(
        "Commands implemented locally: "
        f"{local}\n"
        "Unsupported reference commands are hidden from the chat menu until their Python CLI behavior is implemented.",
        file=sys.stderr,
        flush=True,
    )


def _handle_theme_slash(rest: str) -> None:
    raw = rest.strip()
    if not raw:
        print(
            f"Current syntax theme: {_CLI_SYNTAX_THEME}\n"
            "Use `/theme NAME` to switch for this Python CLI session. "
            "Common names: dracula, github, gruvbox-dark, gruvbox-light, monokai-extended, nord, solarized-dark, solarized-light, zenburn.",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        token = shlex.split(raw)[0]
    except ValueError as exc:
        print(f"Invalid /theme argument: {exc}", file=sys.stderr, flush=True)
        return
    if _set_cli_syntax_theme(token):
        print(f"Syntax theme set to {token}.", file=sys.stderr, flush=True)
    else:
        print(f"Unknown syntax theme `{token}` for the Python CLI highlighter.", file=sys.stderr, flush=True)


def _clear_terminal() -> None:
    if sys.stderr.isatty():
        print("\033[2J\033[H", end="", file=sys.stderr, flush=True)


class _HumanEventRenderer:
    def __init__(
        self,
        *,
        color_mode: str = "auto",
        line_sink: Callable[[Any], None] | None = None,
        status_tracker: _LiveTurnStatus | None = None,
    ) -> None:
        self._tool_arguments: dict[str, Any] = {}
        self._agent_nicknames: dict[str, str] = {}
        self._exec_calls: dict[str, _ExecDisplayCall] = {}
        self._background_terminal_commands: dict[str, str] = {}
        self._background_terminal_call_commands: dict[str, str] = {}
        self._background_terminal_session_call_ids: dict[str, str] = {}
        self._active_background_terminal_interactions: dict[str, dict[str, str]] = {}
        self._pending_background_terminal_waits: dict[str, dict[str, str]] = {}
        self._flushing_background_terminal_wait = False
        self._idle_background_terminal_call_ids: set[str] = set()
        self._rendered_background_terminal_interactions: set[str] = set()
        self._rendered_empty_background_waits: set[str] = set()
        self._live_exec_rendered: set[str] = set()
        self._live_exec_output_seen: set[str] = set()
        self._live_exec_output_text: dict[str, str] = {}
        self._live_exec_incremental_rendered: set[str] = set()
        self._live_exec_incremental_obscured: set[str] = set()
        self._live_exec_incremental_segment_output: dict[str, str] = {}
        self._live_exec_incremental_header_start: dict[str, int] = {}
        self._live_exec_incremental_header_rows: dict[str, int] = {}
        self._live_exec_incremental_total_rows: dict[str, int] = {}
        self._live_exec_incremental_visible_rows = 0
        self._live_exec_regions: dict[str, _LiveExecRegion] = {}
        self._live_exec_panel_rows = 0
        self._live_exec_panel_needs_leading_gap = False
        self._exploration_calls: list[_ExecDisplayCall] = []
        self._final_message: str = ""
        self._final_message_rendered = False
        self._interrupted_rendered = False
        self._printed_any_cell = False
        self._had_work_activity = False
        self._suspend_live_finish = 0
        self._style = _AnsiStyle(_should_use_color(color_mode))
        self._line_sink = line_sink
        self._status_tracker = status_tracker
        self._render_lock = threading.RLock()

    def render(self, event: Any) -> None:
        with self._render_lock:
            if self._status_tracker is not None:
                self._status_tracker.update(event)
            if event.type == "item.completed":
                self._render_item(event.payload.get("item"), pending_input=bool(event.payload.get("pending_input")))
            elif event.type == "tool.started":
                self._render_tool_started(event.payload)
            elif event.type == "exec_command.output_delta":
                self._render_exec_output_delta(event.payload)
            elif event.type == "tool.completed":
                self._render_tool_completed(event.payload)
            elif event.type == "context_compaction.completed":
                self.render_info_message("Context compacted")
            elif event.type == "warning":
                self.render_warning(str(event.payload.get("message") or ""))
            elif event.type == "stream_error":
                self._begin_cell()
                self._line(str(event.payload.get("message") or "Reconnecting..."))
            elif event.type == "turn.aborted":
                self.render_interrupted()
            elif event.type == "thread.goal.updated":
                goal = event.payload.get("goal")
                if isinstance(goal, dict):
                    self._render_goal_updated(goal)
            elif event.type == "thread.goal.cleared":
                self.render_info_message("Goal cleared")

    def render_error(self, message: str) -> None:
        with self._render_lock:
            self._begin_cell()
            self._line(f"ERROR: {message}")

    def render_info_message(self, message: str, hint: str | None = None) -> None:
        with self._render_lock:
            self._begin_cell()
            text = message
            if hint:
                text = f"{message} {self._style.dim(hint)}"
            self._emit_prefixed_lines(
                [text],
                first_prefix=f"{self._style.marker()} ",
                rest_prefix="  ",
            )

    def render_warning(self, message: str) -> None:
        with self._render_lock:
            if not message:
                return
            self._begin_cell()
            self._emit_prefixed_lines(
                [message],
                first_prefix=self._style.yellow("⚠ "),
                rest_prefix="  ",
                transform=self._style.yellow,
            )

    def render_interrupted(self) -> None:
        with self._render_lock:
            if self._interrupted_rendered:
                return
            self._interrupted_rendered = True
            self._begin_cell()
            self._line(
                f"{self._style.red('■')} "
                "Conversation interrupted - tell the model what to do differently. "
                "Something went wrong? Hit `/feedback` to report the issue."
            )

    def render_pending_steer_interrupt(self) -> None:
        with self._render_lock:
            if self._interrupted_rendered:
                return
            self._interrupted_rendered = True
            self.render_info_message("Model interrupted to submit steer instructions.")

    def render_user_message(self, text: str) -> None:
        with self._render_lock:
            normalized = text.rstrip("\r\n")
            if not normalized:
                return
            terminal_width = shutil.get_terminal_size((100, 24)).columns
            safe_width = _terminal_safe_width(terminal_width)
            lines: list[str] = []
            for raw_line in normalized.split("\n"):
                if raw_line == "":
                    lines.append("")
                    continue
                lines.extend(_wrap_ansi_line(raw_line, max(10, safe_width - 2)))
            self._begin_cell()
            if self._style.enabled:
                self._emit_user_message_block(lines, terminal_width=terminal_width)
                return
            self._emit_prefixed_lines(
                lines,
                first_prefix=self._style.dim(self._style.bold("› ")),
                rest_prefix="  ",
            )

    def _emit_user_message_block(self, lines: list[str], *, terminal_width: int) -> None:
        self._line(_user_message_blank_line(self._style, terminal_width))
        first = True
        for logical_line in lines:
            if logical_line == "":
                self._line(_user_message_blank_line(self._style, terminal_width))
                first = False
                continue
            prefix = (
                self._style.user_message_bold_dim("› ")
                if first
                else self._style.user_message("  ")
            )
            body = self._style.user_message(logical_line)
            self._line(_user_message_box_line(f"{prefix}{body}", self._style, terminal_width))
            first = False
        self._line(_user_message_blank_line(self._style, terminal_width))

    def render_pending_input_preview(self, text: str, *, active: bool) -> None:
        with self._render_lock:
            normalized = text.rstrip("\r\n")
            if not normalized:
                return
            terminal_width = shutil.get_terminal_size((100, 24)).columns
            safe_width = _terminal_safe_width(terminal_width)
            lines: list[str] = []
            for raw_line in normalized.split("\n"):
                if raw_line == "":
                    lines.append("")
                    continue
                lines.extend(_wrap_ansi_line(raw_line, max(10, safe_width - 4)))
            self._begin_cell()
            header = "Messages to be submitted after next tool call" if active else "Queued follow-up inputs"
            self._line(f"{self._style.marker()} {self._style.bold(header)}")
            self._emit_prefixed_lines(
                lines,
                first_prefix=self._style.dim("  ↳ "),
                rest_prefix=self._style.dim("    "),
                transform=self._style.dim,
            )

    def finish(self, final_message: str, *, print_to_stdout: bool = True) -> None:
        with self._render_lock:
            self._flush_exploration()
            self._final_message = final_message or self._final_message
            if not self._final_message:
                return
            if sys.stdout.isatty() and sys.stderr.isatty():
                if not self._final_message_rendered:
                    self._render_agent_message(self._final_message)
                return
            if not self._final_message_rendered:
                self._render_agent_message(self._final_message)
            if print_to_stdout:
                print(self._final_message, flush=True)

    def _render_item(self, item: Any, *, pending_input: bool = False) -> None:
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "user" and pending_input:
            self._flush_exploration()
            text = _user_item_text(item)
            if text:
                self.render_user_message(text)
            return
        if item_type == "message" and item.get("role") == "assistant":
            self._flush_exploration()
            text = _assistant_item_text(item)
            if text:
                self._final_message = text
                self._render_agent_message(text)
            return
        if item_type == "reasoning":
            self._flush_exploration()
            text = _reasoning_item_text(item)
            if text.strip():
                self._render_reasoning_message(text)
            return
        if item_type == "web_search_call":
            self._flush_exploration()
            self._begin_cell()
            self._render_web_search_cell(
                _web_search_query(item),
                completed=True,
                action=item.get("action") if isinstance(item.get("action"), dict) else None,
            )

    def _render_tool_started(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "")
        call_id = str(payload.get("call_id") or "")
        arguments = payload.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        if call_id:
            self._tool_arguments[call_id] = arguments

        if name in {"exec_command", "shell_command"}:
            command = str(args.get("cmd") or args.get("command") or "")
            call = _ExecDisplayCall(
                call_id=call_id,
                command=command,
                parsed=parse_command_actions(command),
                yield_time_ms=_int_value(args.get("yield_time_ms")),
            )
            self._exec_calls[call_id] = call
        elif name == "write_stdin":
            session_id = str(args.get("session_id") or "")
            event_call_id = self._background_terminal_session_call_ids.get(session_id, "")
            if event_call_id:
                stdin = str(args.get("chars") or "")
                if stdin:
                    self._flush_pending_background_terminal_wait(event_call_id)
                    self._rendered_empty_background_waits.discard(event_call_id)
                else:
                    existing_wait = self._pending_background_terminal_waits.get(event_call_id, {})
                    self._pending_background_terminal_waits[event_call_id] = {
                        "session_id": session_id,
                        "command": self._background_terminal_commands.get(session_id, ""),
                        "confirmed_running": existing_wait.get("confirmed_running", ""),
                    }
                self._idle_background_terminal_call_ids.discard(event_call_id)
                self._detach_live_exec_region(event_call_id, commit_if_last=True)
                if stdin:
                    self._clear_live_exec_metadata(event_call_id)
                self._active_background_terminal_interactions[event_call_id] = {
                    "session_id": session_id,
                    "stdin": stdin,
                    "command": self._background_terminal_commands.get(session_id, ""),
                }
                if event_call_id in self._live_exec_regions:
                    self._ensure_live_exec_region(
                        event_call_id,
                        kind="background",
                        command=self._background_terminal_commands.get(session_id, ""),
                        stdin=stdin,
                    )
            return
        elif name == "apply_patch":
            return
        elif name == "web_search":
            self._flush_exploration()
            self._begin_work_cell()
            query = str(args.get("query") or "")
            self._render_web_search_cell(query, completed=False)
        elif name == "wait_agent":
            self._render_collab_waiting_begin(args)
        elif name == "resume_agent":
            self._render_collab_resume_begin(args)
        # spawn_agent / send_input / close_agent render nothing while in progress,
        # matching the multi-agent protocol.

    def _render_tool_completed(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "")
        call_id = str(payload.get("call_id") or "")
        metadata = payload.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        arguments = self._tool_arguments.pop(call_id, None)
        ok = bool(payload.get("ok"))

        if name in {"exec_command", "shell_command", "write_stdin"}:
            self._render_command_completed(call_id, ok, payload, meta, arguments)
        elif name == "apply_patch":
            self._render_apply_patch_completed(ok, payload, meta, arguments)
        elif name == "update_plan":
            self._flush_exploration()
            self._begin_work_cell()
            self._render_plan(meta)
        elif name == "view_image":
            self._flush_exploration()
            self._begin_work_cell()
            path = meta.get("path")
            self._line(f"{self._style.bold('view image:')} {path}" if path else self._style.bold("view image"))
        elif name == "request_user_input":
            self._flush_exploration()
            self._begin_work_cell()
            if not self._render_request_user_input_result(meta, interrupted=not ok):
                if ok:
                    self._emit_prefixed_lines(
                        [f"{self._style.bold('Request user input')} {self._style.green('completed')}"],
                        first_prefix=f"{self._style.marker()} ",
                        rest_prefix="  ",
                    )
                else:
                    self._render_tool_failure(name, _tool_failure_output(payload, meta))
        elif name == "spawn_agent":
            self._render_collab_spawn_end(ok, meta, arguments)
        elif name == "send_input":
            self._render_collab_send_end(ok, meta, arguments)
        elif name == "resume_agent":
            self._render_collab_resume_end(ok, meta, arguments)
        elif name == "wait_agent":
            self._render_collab_waiting_end(ok, meta, arguments)
        elif name == "close_agent":
            self._render_collab_close_end(ok, meta, arguments)
        elif name in {"get_goal", "create_goal", "update_goal"}:
            if ok:
                self._flush_exploration()
                self._begin_work_cell()
                self._emit_prefixed_lines(
                    [f"{self._style.bold(_tool_display_title(name))} {self._style.green('completed')}"],
                    first_prefix=f"{self._style.marker()} ",
                    rest_prefix="  ",
                )
            else:
                self._render_tool_failure(name, _tool_failure_output(payload, meta))
        elif not ok:
            self._render_tool_failure(name, _tool_failure_output(payload, meta))

    # --- multi-agent (collab) tool rendering ----------------------------------
    # Each multi-agent event is rendered as its own cell
    # whose title carries a dim "• " prefix and whose details are indented under
    # a dim "  └ " gutter. spawn/send/close render only on completion; wait and
    # resume also render an in-progress ("Waiting for"/"Resuming") cell.

    _COLLAB_PROMPT_PREVIEW = 160
    _COLLAB_ERROR_PREVIEW = 160
    _COLLAB_RESPONSE_PREVIEW = 240

    def _collab_event(self, title: str, details: list[str]) -> None:
        self._flush_exploration()
        self._begin_work_cell()
        self._line(title)
        if details:
            self._emit_prefixed_lines(
                details,
                first_prefix=self._style.dim("  └ "),
                rest_prefix="    ",
            )

    def _collab_title_text(self, title: str) -> str:
        return f"{self._style.marker()} {self._style.bold(title)}"

    def _collab_title(
        self,
        prefix: str,
        agent_id: str | None,
        nickname: str | None,
        *,
        spawn_args: Any | None = None,
    ) -> str:
        title = f"{self._style.marker()} {self._style.bold(prefix)} {self._agent_label(agent_id, nickname)}"
        if spawn_args is not None:
            extra = self._collab_spawn_request(spawn_args)
            if extra:
                title = f"{title} {extra}"
        return title

    def _agent_label(self, agent_id: str | None, nickname: str | None) -> str:
        nick = nickname.strip() if isinstance(nickname, str) else ""
        if nick:
            return self._style.cyan(self._style.bold(nick))
        ident = (agent_id or "").strip()
        if ident:
            return self._style.cyan(ident)
        return self._style.cyan("agent")

    def _lookup_agent_nickname(self, agent_id: str | None) -> str | None:
        if not agent_id:
            return None
        return self._agent_nicknames.get(agent_id)

    def _collab_spawn_request(self, args: Any) -> str:
        args = args if isinstance(args, dict) else {}
        model = args.get("model")
        model = model.strip() if isinstance(model, str) else ""
        effort = args.get("reasoning_effort")
        effort = effort.strip() if isinstance(effort, str) else ""
        if model and effort:
            detail = f"({model} {effort})"
        elif model:
            detail = f"({model})"
        elif effort:
            detail = f"({effort})"
        else:
            return ""
        return self._style.magenta(detail)

    def _collab_prompt_text(self, args: Any) -> str:
        args = args if isinstance(args, dict) else {}
        message = args.get("message")
        if isinstance(message, str) and message.strip():
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
                name = item.get("name")
                prefix = f"{name}: " if isinstance(name, str) and name else ""
                chunks.append(f"{prefix}{item['path']}")
            elif isinstance(item.get("name"), str):
                chunks.append(item["name"])
        return "\n".join(chunk for chunk in chunks if chunk.strip())

    def _collab_prompt_details(self, args: Any) -> list[str]:
        trimmed = self._collab_prompt_text(args).strip()
        if not trimmed:
            return []
        return [_truncate_display_text(trimmed, self._COLLAB_PROMPT_PREVIEW)]

    def _collab_status_summary(self, status: Any, *, fallback_error: str | None = None) -> str:
        if isinstance(status, dict):
            if "completed" in status:
                summary = self._style.green("Completed")
                message = status.get("completed")
                preview = self._collab_preview(message, self._COLLAB_RESPONSE_PREVIEW)
                if preview:
                    summary = f"{summary}{self._style.dim(' - ')}{preview}"
                return summary
            if "errored" in status:
                message = status.get("errored")
                return self._collab_error_summary(message if isinstance(message, str) else "Agent errored")
        if status == "running":
            return self._style.cyan(self._style.bold("Running"))
        if status == "interrupted":
            return self._style.yellow("Interrupted")
        if status == "shutdown":
            return "Shutdown"
        if status == "not_found":
            return self._style.red("Not found")
        if status == "pending_init":
            return self._style.cyan("Pending init")
        if isinstance(status, str) and status:
            return status
        return self._collab_error_summary(fallback_error or "")

    def _collab_error_summary(self, error: str) -> str:
        summary = self._style.red("Error")
        preview = self._collab_preview(error, self._COLLAB_ERROR_PREVIEW)
        if preview:
            summary = f"{summary}{self._style.dim(' - ')}{preview}"
        return summary

    def _collab_preview(self, text: Any, limit: int) -> str:
        if not isinstance(text, str):
            return ""
        collapsed = " ".join(text.split())
        if not collapsed:
            return ""
        return _truncate_display_text(collapsed, limit)

    def _render_collab_spawn_end(self, ok: bool, meta: dict[str, Any], arguments: Any) -> None:
        agent_id = str(meta.get("agent_id") or "") or None
        nickname = meta.get("nickname")
        nickname = nickname if isinstance(nickname, str) and nickname.strip() else None
        if ok and agent_id:
            if nickname:
                self._agent_nicknames[agent_id] = nickname
            title = self._collab_title("Spawned", agent_id, nickname, spawn_args=arguments)
        else:
            title = self._collab_title_text("Agent spawn failed")
        self._collab_event(title, self._collab_prompt_details(arguments))

    def _render_collab_send_end(self, ok: bool, meta: dict[str, Any], arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        target = str(args.get("target") or "") or None
        if not target:
            return
        title = self._collab_title("Sent input to", target, self._lookup_agent_nickname(target))
        self._collab_event(title, self._collab_prompt_details(arguments))

    def _render_collab_resume_begin(self, arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        agent_id = str(args.get("id") or "") or None
        if not agent_id:
            return
        self._collab_event(
            self._collab_title("Resuming", agent_id, self._lookup_agent_nickname(agent_id)),
            [],
        )

    def _render_collab_resume_end(self, ok: bool, meta: dict[str, Any], arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        agent_id = str(args.get("id") or "") or None
        if not agent_id:
            return
        title = self._collab_title("Resumed", agent_id, self._lookup_agent_nickname(agent_id))
        detail = self._collab_status_summary(meta.get("status"), fallback_error="Agent resume failed")
        self._collab_event(title, [detail])

    def _render_collab_waiting_begin(self, arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        targets = args.get("targets")
        ids = [str(item) for item in targets] if isinstance(targets, list) else []
        if len(ids) == 1:
            title = self._collab_title("Waiting for", ids[0], self._lookup_agent_nickname(ids[0]))
            details: list[str] = []
        elif not ids:
            title = self._collab_title_text("Waiting for agents")
            details = []
        else:
            title = self._collab_title_text(f"Waiting for {len(ids)} agents")
            details = [self._agent_label(i, self._lookup_agent_nickname(i)) for i in ids]
        self._collab_event(title, details)

    def _render_collab_waiting_end(self, ok: bool, meta: dict[str, Any], arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        targets = args.get("targets")
        ids = [str(item) for item in targets] if isinstance(targets, list) else []
        statuses = meta.get("status") if isinstance(meta.get("status"), dict) else {}
        self._collab_event(self._collab_title_text("Finished waiting"), self._collab_wait_lines(ids, statuses))

    def _collab_wait_lines(self, ids: list[str], statuses: dict[str, Any]) -> list[str]:
        entries: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for tid in ids:
            if tid in statuses:
                entries.append((tid, statuses[tid]))
                seen.add(tid)
        extras = sorted((k, v) for k, v in statuses.items() if k not in seen)
        entries.extend(extras)
        if not entries:
            return ["No agents completed yet"]
        lines: list[str] = []
        for tid, status in entries:
            label = self._agent_label(tid, self._lookup_agent_nickname(tid))
            lines.append(f"{label}{self._style.dim(': ')}{self._collab_status_summary(status)}")
        return lines

    def _render_collab_close_end(self, ok: bool, meta: dict[str, Any], arguments: Any) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        target = str(args.get("target") or "") or None
        if not target:
            return
        title = self._collab_title("Closed", target, self._lookup_agent_nickname(target))
        self._collab_event(title, [])

    def _render_web_search_cell(
        self,
        query: str,
        *,
        completed: bool,
        action: dict[str, Any] | None = None,
    ) -> None:
        header = "Searched" if completed else "Searching the web"
        detail = _web_search_action_detail(action) if action is not None else ""
        text = " ".join(part for part in [self._style.bold(header), detail or query] if part)
        self._emit_prefixed_lines(
            [text],
            first_prefix=f"{self._style.marker()} ",
            rest_prefix="  ",
        )

    def _render_goal_updated(self, goal: dict[str, Any]) -> None:
        self._flush_exploration()
        self._begin_work_cell()
        status = _goal_status_label(str(goal.get("status") or ""))
        objective = str(goal.get("objective") or "")
        summary = f"Goal {status}"
        details: list[str] = []
        if objective:
            details.append(f"Objective: {objective}")
        if goal.get("timeUsedSeconds"):
            details.append(f"Time: {_format_goal_elapsed_seconds(int(goal.get('timeUsedSeconds') or 0))}.")
        if goal.get("tokenBudget") is not None:
            details.append(
                f"Tokens: {_format_tokens_compact(int(goal.get('tokensUsed') or 0))}/{_format_tokens_compact(int(goal.get('tokenBudget') or 0))}."
            )
        self._emit_prefixed_lines(
            [self._style.bold(summary), *details],
            first_prefix=f"{self._style.marker()} ",
            rest_prefix="  ",
        )

    def _render_command_completed(
        self,
        call_id: str,
        ok: bool,
        payload: dict[str, Any],
        meta: dict[str, Any],
        arguments: Any,
    ) -> None:
        if str(payload.get("name") or "") == "write_stdin":
            args = arguments if isinstance(arguments, dict) else {}
            session_id = str(args.get("session_id") or meta.get("session_id") or "")
            command = str(meta.get("command") or self._background_terminal_commands.get(session_id) or "")
            event_call_id = str(
                meta.get("event_call_id")
                or self._background_terminal_session_call_ids.get(session_id)
                or call_id
            )
            if ok:
                output = _visible_command_output(meta, payload)
                should_render_interaction = bool(str(args.get("chars") or "").strip()) or bool(output.strip())
                process_still_running = _int_value(meta.get("exit_code")) is None
                has_live_region = (
                    event_call_id in self._live_exec_regions
                    or event_call_id in self._live_exec_output_seen
                )
                if (
                    should_render_interaction
                    and not process_still_running
                    and not has_live_region
                    and event_call_id not in self._rendered_background_terminal_interactions
                ):
                    self.render_terminal_interaction(session_id, str(args.get("chars") or ""), command=command)
                self._render_write_stdin_exec_completion(
                    call_id,
                    ok,
                    payload,
                    meta,
                    command=command,
                    session_id=session_id,
                    stdin=str(args.get("chars") or ""),
                )
            else:
                # Mirror the success path: surface the interaction header (which
                # terminal + the input we sent) followed by the error rendered as a
                # dim output block, instead of a bare unrendered "write_stdin: failed".
                chars = str(args.get("chars") or "")
                output = _tool_failure_output(payload, meta)
                has_terminal_context = bool(command) or event_call_id in self._background_terminal_call_commands
                should_render_interaction = has_terminal_context and (bool(chars.strip()) or bool(output.strip()))
                if should_render_interaction and event_call_id not in self._rendered_background_terminal_interactions:
                    self.render_terminal_interaction(session_id, chars, command=command)
                elif should_render_interaction:
                    self._flush_exploration()
                    self._begin_work_cell()
                else:
                    self._render_tool_failure("write_stdin", output)
                    self._active_background_terminal_interactions.pop(event_call_id, None)
                    self._rendered_background_terminal_interactions.discard(event_call_id)
                    return
                self._render_output_block(output or "write_stdin failed")
            self._active_background_terminal_interactions.pop(event_call_id, None)
            self._rendered_background_terminal_interactions.discard(event_call_id)
            return
        call = self._exec_calls.pop(call_id, None)
        if call is None:
            command = str(meta.get("command") or "")
            call = _ExecDisplayCall(call_id=call_id, command=command, parsed=parse_command_actions(command))
        exit_code = meta.get("exit_code")
        exit_value = _int_value(exit_code)
        session_id = meta.get("session_id")
        if session_id is not None and exit_value is None and call.command:
            self._background_terminal_commands[str(session_id)] = _command_display(call.command)
            self._background_terminal_call_commands[call_id] = _command_display(call.command)
            self._background_terminal_session_call_ids[str(session_id)] = call_id
        live_output_seen = call_id in self._live_exec_output_seen
        output = _visible_command_output(meta, payload)
        incremental_live_output = call_id in self._live_exec_incremental_rendered
        if live_output_seen and exit_value is None:
            self._mark_background_terminal_idle(call_id)
            return
        if live_output_seen and incremental_live_output and exit_value is not None:
            call.output = self._remaining_live_exec_output(call_id, output)
            call.exit_code = exit_value
            call.duration_ms = _duration_ms(meta.get("wall_time_seconds"))
            self._rewrite_incremental_live_exec_header(call, running=False, ok=ok)
            if exit_value is not None:
                self._background_terminal_call_commands.pop(call_id, None)
                self._idle_background_terminal_call_ids.discard(call_id)
            self._detach_live_exec_region(call_id, commit_if_last=True)
            self._clear_live_exec_metadata(call_id)
            self._render_live_exec_panel_if_needed()
            return
        elif live_output_seen and self._live_exec_has_other_regions(call_id):
            call.output = self._live_exec_final_output(call_id, output)
            self._detach_live_exec_region(call_id, commit_if_last=False)
            suppress_empty_output = False
        elif live_output_seen:
            call.output = output or self._live_exec_output_text.get(call_id, "")
            self._detach_live_exec_region(call_id, commit_if_last=False)
            suppress_empty_output = not bool(call.output.strip())
        else:
            call.output = output
            suppress_empty_output = False
        call.exit_code = exit_value
        call.duration_ms = _duration_ms(meta.get("wall_time_seconds"))
        if exit_value is None and not live_output_seen and not call.output.strip():
            self._mark_background_terminal_idle(call_id)
            return
        if exit_value is not None and call.is_exploration and not live_output_seen:
            self._exploration_calls.append(call)
            self._clear_live_exec(call_id)
            return
        if exit_value is not None:
            self._flush_pending_background_terminal_wait(call_id)
        self._flush_exploration()
        self._render_exec_call(
            call,
            running=exit_value is None,
            ok=ok,
            suppress_empty_output=suppress_empty_output,
        )
        if exit_value is not None:
            self._background_terminal_call_commands.pop(call_id, None)
            self._idle_background_terminal_call_ids.discard(call_id)
            self._pending_background_terminal_waits.pop(call_id, None)
            self._rendered_empty_background_waits.discard(call_id)
            self._clear_live_exec_metadata(call_id)
        else:
            self._mark_background_terminal_idle(call_id)
        self._render_live_exec_panel_if_needed()

    def _render_exec_output_delta(self, payload: dict[str, Any]) -> None:
        call_id = str(payload.get("call_id") or "")
        delta = str(payload.get("delta") or "")
        if not call_id or not delta:
            return
        interaction = self._active_background_terminal_interactions.get(call_id)
        if interaction is None and call_id in self._idle_background_terminal_call_ids:
            return
        call = self._exec_calls.get(call_id)
        if interaction is None and call is not None and call.is_exploration:
            return
        if (
            interaction is None
            and call is not None
            and call.yield_time_ms is not None
            and call.yield_time_ms < 1000
            and call_id not in self._live_exec_rendered
        ):
            return
        first_output = call_id not in self._live_exec_output_seen
        if interaction is not None:
            self._pending_background_terminal_waits.pop(call_id, None)
            self._rendered_empty_background_waits.discard(call_id)
        if call_id not in self._live_exec_rendered:
            if interaction is not None:
                self._ensure_live_exec_region(
                    call_id,
                    kind="background",
                    command=interaction.get("command", ""),
                    stdin=interaction.get("stdin", ""),
                )
                self._rendered_background_terminal_interactions.add(call_id)
            else:
                if call is None:
                    command = self._background_terminal_call_commands.get(call_id, "")
                    call = _ExecDisplayCall(call_id=call_id, command=command, parsed=parse_command_actions(command))
                self._ensure_live_exec_region(call_id, kind="exec", command=call.command)
        elif interaction is not None:
            self._ensure_live_exec_region(
                call_id,
                kind="background",
                command=interaction.get("command", ""),
                stdin=interaction.get("stdin", ""),
            )
        self._live_exec_rendered.add(call_id)
        self._live_exec_output_seen.add(call_id)
        previous_output = self._live_exec_output_text.get(call_id, "")
        self._live_exec_output_text[call_id] = previous_output + delta
        self._rendered_empty_background_waits.discard(call_id)
        if not self._is_live_exec_tail_region(call_id):
            self._freeze_live_exec_region(call_id)
            return
        self._render_output_delta_block(call_id, previous_output, self._live_exec_output_text[call_id], first=first_output)

    def render_terminal_interaction(self, session_id: str, stdin: str, *, command: str | None = None) -> None:
        with self._render_lock:
            self._flush_exploration()
            self._begin_work_cell()
            command_display = command or self._background_terminal_commands.get(str(session_id), "")
            if not stdin:
                self._render_terminal_interaction_header("•", "Waited for background terminal", command_display)
                return
            self._render_terminal_interaction_header("↳", "Interacted with background terminal", command_display)
            input_lines = stdin.rstrip("\n").splitlines()
            if input_lines:
                self._emit_prefixed_lines(
                    input_lines,
                    first_prefix=self._style.dim("  └ "),
                    rest_prefix=self._style.dim("    "),
                )

    def _flush_pending_background_terminal_wait(self, call_id: str | None = None) -> None:
        if self._flushing_background_terminal_wait:
            return
        if call_id is None:
            call_ids = list(self._pending_background_terminal_waits)
        else:
            call_ids = [call_id]
        self._flushing_background_terminal_wait = True
        try:
            for pending_call_id in call_ids:
                pending = self._pending_background_terminal_waits.pop(pending_call_id, None)
                if not pending:
                    continue
                command = pending.get("command", "")
                if not command:
                    session_id = pending.get("session_id", "")
                    command = self._background_terminal_commands.get(session_id, "")
                if pending_call_id in self._rendered_empty_background_waits:
                    continue
                self.render_terminal_interaction(pending.get("session_id", ""), "", command=command)
                self._rendered_background_terminal_interactions.add(pending_call_id)
                self._rendered_empty_background_waits.add(pending_call_id)
        finally:
            self._flushing_background_terminal_wait = False

    def _render_terminal_interaction_header(self, marker: str, label: str, command_display: str) -> None:
        prefix = f"{self._style.dim(marker)} "
        line = self._style.bold(label)
        command_snapshot = _terminal_interaction_command_snapshot(
            command_display,
            shutil.get_terminal_size((100, 24)).columns - _visible_len(prefix) - _visible_len(label) - 3,
        )
        if command_snapshot:
            line += f" {self._style.dim('·')} {self._style.dim(command_snapshot)}"
        self._emit_prefixed_lines([line], first_prefix=prefix, rest_prefix="  ")

    def _render_write_stdin_exec_completion(
        self,
        call_id: str,
        ok: bool,
        payload: dict[str, Any],
        meta: dict[str, Any],
        *,
        command: str,
        session_id: str,
        stdin: str = "",
    ) -> None:
        exit_value = _int_value(meta.get("exit_code"))
        output = _visible_command_output(meta, payload)
        event_call_id = str(meta.get("event_call_id") or call_id)
        live_output_seen = event_call_id in self._live_exec_output_seen
        if exit_value is None:
            if live_output_seen:
                self._mark_background_terminal_idle(event_call_id, preserve_live_metadata=True)
                return
            if output.strip():
                self._pending_background_terminal_waits.pop(event_call_id, None)
                self._ensure_live_exec_region(event_call_id, kind="background", command=command, stdin=stdin)
                self._live_exec_rendered.add(event_call_id)
                self._live_exec_output_seen.add(event_call_id)
                self._live_exec_output_text[event_call_id] = self._live_exec_output_text.get(event_call_id, "") + output
                self._render_live_exec_panel()
                self._mark_background_terminal_idle(event_call_id, preserve_live_metadata=True)
                return
            if stdin.strip():
                self._ensure_live_exec_region(event_call_id, kind="background", command=command, stdin=stdin)
                self._live_exec_rendered.add(event_call_id)
                self._render_live_exec_panel()
                self._mark_background_terminal_idle(event_call_id, preserve_live_metadata=True)
                return
            pending_wait = self._pending_background_terminal_waits.get(event_call_id)
            if pending_wait is not None:
                pending_wait["confirmed_running"] = "1"
            self._mark_background_terminal_idle(event_call_id, preserve_live_metadata=True)
            return
        if session_id:
            self._background_terminal_commands.pop(session_id, None)
            self._background_terminal_session_call_ids.pop(session_id, None)
        pending_wait = self._pending_background_terminal_waits.get(event_call_id)
        if pending_wait is not None and pending_wait.get("confirmed_running") == "1":
            self._flush_pending_background_terminal_wait(event_call_id)
        else:
            self._pending_background_terminal_waits.pop(event_call_id, None)
        call = _ExecDisplayCall(
            call_id=event_call_id,
            command=command,
            parsed=parse_command_actions(command),
        )
        incremental_live_output = event_call_id in self._live_exec_incremental_rendered
        if live_output_seen and incremental_live_output:
            call.output = self._remaining_live_exec_output(event_call_id, output)
            call.exit_code = exit_value
            call.duration_ms = _duration_ms(meta.get("wall_time_seconds"))
            self._rewrite_incremental_live_exec_header(call, running=False, ok=ok and exit_value == 0)
            self._background_terminal_call_commands.pop(event_call_id, None)
            self._idle_background_terminal_call_ids.discard(event_call_id)
            self._detach_live_exec_region(event_call_id, commit_if_last=True)
            self._clear_live_exec_metadata(event_call_id)
            self._render_live_exec_panel_if_needed()
            return
        elif live_output_seen and self._live_exec_has_other_regions(event_call_id):
            call.output = self._live_exec_final_output(event_call_id, output)
            self._detach_live_exec_region(event_call_id, commit_if_last=False)
            suppress_empty_output = False
        elif live_output_seen:
            call.output = output or self._live_exec_output_text.get(event_call_id, "")
            self._detach_live_exec_region(event_call_id, commit_if_last=False)
            suppress_empty_output = not bool(call.output.strip())
        else:
            call.output = output
            suppress_empty_output = False
        call.exit_code = exit_value
        call.duration_ms = _duration_ms(meta.get("wall_time_seconds"))
        self._render_exec_call(
            call,
            running=False,
            ok=ok and exit_value == 0,
            suppress_empty_output=suppress_empty_output,
        )
        self._background_terminal_call_commands.pop(event_call_id, None)
        self._idle_background_terminal_call_ids.discard(event_call_id)
        self._pending_background_terminal_waits.pop(event_call_id, None)
        self._rendered_empty_background_waits.discard(event_call_id)
        self._clear_live_exec_metadata(event_call_id)
        self._render_live_exec_panel_if_needed()

    def _render_apply_patch_completed(
        self,
        ok: bool,
        payload: dict[str, Any],
        meta: dict[str, Any],
        arguments: Any,
    ) -> None:
        self._flush_exploration()
        self._begin_work_cell()
        if ok:
            changes = _file_change_display_from_metadata(meta)
            if not changes:
                changes = _file_change_display_from_patch(arguments)
            if changes:
                self._render_file_changes(changes)
                return
            self._line(f"{self._style.bold('patch:')} {self._style.green('completed')}")
            return
        # Failure: render patch apply failures consistently:
        # a magenta+bold title followed by the stderr rendered as a dim output block.
        self._line(self._style.magenta(self._style.bold("✘ Failed to apply patch")))
        output = str(payload.get("output") or "")
        if output.strip():
            self._render_output_block(output)

    def _render_file_changes(self, changes: list["_FileChangeDisplay"]) -> None:
        total_added = sum(change.additions for change in changes)
        total_deleted = sum(change.deletions for change in changes)
        if len(changes) == 1:
            change = changes[0]
            verb = {"add": "Added", "delete": "Deleted"}.get(change.kind, "Edited")
            self._line(
                f"{self._style.marker()} {self._style.bold(verb)} "
                f"{_file_change_path_label(change)} (+{change.additions} -{change.deletions})"
            )
            self._render_file_change_rows(change)
            return
        noun = "file" if len(changes) == 1 else "files"
        self._line(
            f"{self._style.marker()} {self._style.bold('Edited')} {len(changes)} {noun} "
            f"(+{total_added} -{total_deleted})"
        )
        for index, change in enumerate(changes):
            if index > 0:
                self._line("")
            self._line(
                f"{self._style.dim('  └')} {_file_change_path_label(change)} "
                f"(+{change.additions} -{change.deletions})"
            )
            self._render_file_change_rows(change)

    def _render_file_change_rows(self, change: "_FileChangeDisplay") -> None:
        width = max(1, max((row.line_number or 0 for row in change.rows), default=0))
        line_number_width = len(str(width))
        for row in change.rows:
            if row.kind == "ellipsis":
                self._line(f"{'':>{line_number_width + 5}}{self._style.dim('⋮')}")
                continue
            number = "" if row.line_number is None else str(row.line_number)
            sign = row.kind if row.kind in {"+", "-"} else " "
            rendered = f"    {number:>{line_number_width}} {sign}{row.text}"
            if row.kind == "+":
                rendered = self._style.green(rendered)
            elif row.kind == "-":
                rendered = self._style.red(rendered)
            self._emit_prefixed_lines([rendered], first_prefix="", rest_prefix="      ")

    def _render_exec_call(
        self,
        call: "_ExecDisplayCall",
        *,
        running: bool,
        ok: bool,
        suppress_empty_output: bool = False,
    ) -> None:
        self._begin_work_cell()
        for row in self._exec_call_header_rows(call, running=running, ok=ok, terminal_width=_terminal_columns()):
            self._line(row)
        if call.output.strip():
            self._render_output_block(call.output)
        elif not running and not suppress_empty_output:
            self._emit_prefixed_lines(
                [self._style.dim("(no output)")],
                first_prefix=self._style.dim("  └ "),
                rest_prefix="    ",
            )

    def _flush_exploration(self) -> None:
        if not self._exploration_calls:
            return
        calls = self._exploration_calls
        self._exploration_calls = []
        self._begin_work_cell()
        self._line(f"{self._style.marker()} {self._style.bold('Explored')}")
        rows: list[tuple[str, str]] = []
        while calls:
            call = calls.pop(0)
            if call.reads_only:
                names = []
                for action in call.parsed:
                    if action.get("type") == "read":
                        name = str(action.get("name") or action.get("path") or action.get("cmd") or "")
                        if name and name not in names:
                            names.append(name)
                while calls and calls[0].reads_only:
                    next_call = calls.pop(0)
                    for action in next_call.parsed:
                        name = str(action.get("name") or action.get("path") or action.get("cmd") or "")
                        if name and name not in names:
                            names.append(name)
                rows.append(("Read", ", ".join(names)))
                continue
            for action in call.parsed:
                action_type = action.get("type")
                if action_type == "read":
                    rows.append(("Read", str(action.get("name") or action.get("path") or action.get("cmd") or "")))
                elif action_type == "list_files":
                    rows.append(("List", str(action.get("path") or action.get("cmd") or "")))
                elif action_type == "search":
                    query = action.get("query")
                    path = action.get("path")
                    if query and path:
                        rows.append(("Search", f"{query} in {path}"))
                    else:
                        rows.append(("Search", str(query or action.get("cmd") or "")))
                else:
                    rows.append(("Run", str(action.get("cmd") or call.command)))
        for index, (title, text) in enumerate(rows):
            gutter = self._style.dim("  └ " if index == 0 else "    ")
            title_prefix = f"{gutter}{self._style.cyan(title)} "
            continuation = f"{self._style.dim('    ')}{' ' * (_visible_len(title) + 1)}"
            self._emit_prefixed_lines([text], first_prefix=title_prefix, rest_prefix=continuation)

    def _render_output_block(self, output: str) -> None:
        lines = output.rstrip("\n").splitlines()
        if not lines:
            return
        head, tail, omitted = _truncate_middle_parts(lines, 5)
        self._emit_prefixed_lines(
            head,
            first_prefix=self._style.dim("  └ "),
            rest_prefix=self._style.dim("    "),
            transform=lambda segment: _dim_command_output_segment(segment, self._style),
        )
        if omitted:
            self._line(self._style.dim(f"    {_ellipsis_text(omitted, transcript_hint=True)}"))
            self._emit_prefixed_lines(
                tail,
                first_prefix=self._style.dim("    "),
                rest_prefix=self._style.dim("    "),
                transform=lambda segment: _dim_command_output_segment(segment, self._style),
            )

    def _render_tool_failure(self, name: str, output: str = "") -> None:
        self._flush_exploration()
        self._begin_work_cell()
        title = f"{_tool_display_title(name)} failed"
        self._emit_prefixed_lines(
            [self._style.bold(title)],
            first_prefix=f"{self._style.marker('red')} ",
            rest_prefix="  ",
        )
        if output.strip():
            self._render_output_block(output)

    def _ensure_live_exec_region(
        self,
        call_id: str,
        *,
        kind: str,
        command: str = "",
        stdin: str = "",
    ) -> None:
        region = self._live_exec_regions.get(call_id)
        if region is not None:
            if kind == "background":
                region.kind = "background"
            if command and not region.command:
                region.command = command
            if stdin and not region.stdin:
                region.stdin = stdin
            return
        if not self._live_exec_regions:
            continuing_background = (
                kind == "background"
                and call_id in self._live_exec_incremental_rendered
                and call_id in self._live_exec_output_seen
                and call_id not in self._live_exec_incremental_obscured
            )
            if not continuing_background:
                self._flush_exploration()
                self._begin_work_cell()
        self._live_exec_regions[call_id] = _LiveExecRegion(
            call_id=call_id,
            kind=kind,
            command=command,
            stdin=stdin,
        )

    def _render_output_delta_block(self, call_id: str, previous_output: str, output: str, *, first: bool) -> None:
        if not output or call_id not in self._live_exec_regions:
            return
        self._render_incremental_live_exec_region(call_id, previous_output, output, first=first)

    def _render_incremental_live_exec_region(
        self,
        call_id: str,
        previous_output: str,
        output: str,
        *,
        first: bool,
    ) -> None:
        region = self._live_exec_regions.get(call_id)
        if region is None:
            return
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        obscured = call_id in self._live_exec_incremental_obscured
        if first or call_id not in self._live_exec_incremental_rendered or obscured:
            header_rows = self._live_exec_region_header_rows(region, terminal_width)
            segment_output = output if not obscured else _output_delta_since(previous_output, output)
            output_rows = _live_output_display_rows(segment_output, self._style, terminal_width)
            rows = [*header_rows, *output_rows]
            self._emit_live_incremental_rows(rows, call_id=call_id, header_rows=len(header_rows))
            self._live_exec_incremental_rendered.add(call_id)
            self._live_exec_incremental_obscured.discard(call_id)
            self._live_exec_incremental_segment_output[call_id] = segment_output
            self._live_exec_incremental_header_rows[call_id] = len(header_rows)
            self._live_exec_incremental_total_rows[call_id] = len(rows)
            return
        previous_segment_output = self._live_exec_incremental_segment_output.get(call_id, previous_output)
        segment_output = previous_segment_output + _output_delta_since(previous_output, output)
        old_rows = _live_output_display_rows(previous_segment_output, self._style, terminal_width)
        new_rows = _live_output_display_rows(segment_output, self._style, terminal_width)
        if _rows_have_prefix(new_rows, old_rows):
            self._emit_live_incremental_rows(new_rows[len(old_rows) :], call_id=call_id)
            self._live_exec_incremental_segment_output[call_id] = segment_output
            self._live_exec_incremental_total_rows[call_id] = (
                self._live_exec_incremental_total_rows.get(call_id, 0) + max(0, len(new_rows) - len(old_rows))
            )
            return
        if old_rows and new_rows and len(old_rows) == len(new_rows):
            self._write(f"\r\033[1A\r\033[2K{new_rows[-1]}\n", partial_line_open=False)
            self._live_exec_incremental_segment_output[call_id] = segment_output
            return
        self._emit_live_incremental_rows(new_rows[-max(1, min(3, len(new_rows))) :], call_id=call_id)
        self._live_exec_incremental_segment_output[call_id] = segment_output
        self._live_exec_incremental_total_rows[call_id] = (
            self._live_exec_incremental_header_rows.get(call_id, 0) + len(new_rows)
        )

    def _emit_live_incremental_rows(self, rows: list[str], *, call_id: str, header_rows: int = 0) -> None:
        if not rows:
            return
        if header_rows > 0:
            self._live_exec_incremental_header_start[call_id] = self._live_exec_incremental_visible_rows
            self._live_exec_incremental_header_rows[call_id] = header_rows
        self._suspend_live_finish += 1
        try:
            for row in rows:
                self._line(row)
        finally:
            self._suspend_live_finish -= 1
        self._live_exec_incremental_visible_rows += len(rows)

    def _rewrite_incremental_live_exec_header(self, call: "_ExecDisplayCall", *, running: bool, ok: bool) -> None:
        header_start = self._live_exec_incremental_header_start.get(call.call_id)
        header_rows = self._live_exec_incremental_header_rows.get(call.call_id, 0)
        if header_start is None or header_rows <= 0:
            return
        rows = self._exec_call_header_rows(
            call,
            running=running,
            ok=ok,
            terminal_width=shutil.get_terminal_size((100, 24)).columns,
        )
        rows = rows[:header_rows]
        if not rows:
            return
        rows_from_cursor_to_header = max(0, self._live_exec_incremental_visible_rows - header_start)
        if rows_from_cursor_to_header <= 0:
            return
        parts = [f"\r\033[{rows_from_cursor_to_header}A"]
        for row in rows:
            parts.append(f"\r\033[2K{row}\n")
        remaining_rows = max(0, rows_from_cursor_to_header - len(rows))
        if remaining_rows:
            parts.append(f"\033[{remaining_rows}B")
        parts.append("\r")
        self._write("".join(parts), partial_line_open=False)

    def _render_live_exec_panel(self) -> None:
        if not self._live_exec_regions:
            self._clear_live_exec_panel()
            return
        rows = self._live_exec_panel_display_rows()
        self._clear_live_exec_panel()
        if not rows:
            return
        leading_gap = self._live_exec_panel_needs_leading_gap
        self._live_exec_panel_needs_leading_gap = False
        text = ("\n" if leading_gap else "") + "\n".join(rows) + "\n"
        self._write(text, partial_line_open=False, live_op="live_panel")
        self._live_exec_panel_rows = len(rows) + (1 if leading_gap else 0)

    def _render_live_exec_panel_if_needed(self) -> None:
        if any(call_id not in self._live_exec_incremental_rendered for call_id in self._live_exec_regions):
            self._render_live_exec_panel()

    def _live_exec_panel_display_rows(self) -> list[str]:
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        rows: list[str] = []
        for region in self._live_exec_regions.values():
            rows.extend(self._live_exec_region_rows(region, terminal_width))
        return rows

    def _live_exec_region_rows(self, region: "_LiveExecRegion", terminal_width: int) -> list[str]:
        rows = self._live_exec_region_header_rows(region, terminal_width)
        output = self._live_exec_output_text.get(region.call_id, "")
        rows.extend(_live_output_display_rows(output, self._style, terminal_width))
        return rows

    def _live_exec_region_header_rows(self, region: "_LiveExecRegion", terminal_width: int) -> list[str]:
        rows: list[str] = []
        if region.kind == "background":
            marker = "↳" if region.stdin else "•"
            label = "Interacted with background terminal" if region.stdin else "Waited for background terminal"
            rows.extend(self._live_exec_background_header_rows(marker, label, region.command, terminal_width))
            if region.stdin:
                rows.extend(
                    self._prefixed_wrapped_rows(
                        region.stdin.rstrip("\n").splitlines(),
                        first_prefix=self._style.dim("  └ "),
                        rest_prefix=self._style.dim("    "),
                        terminal_width=terminal_width,
                    )
                )
        else:
            rows.extend(self._live_exec_command_header_rows(region.command, terminal_width))
        return rows

    def _live_exec_command_header_rows(self, command: str, terminal_width: int) -> list[str]:
        call = _ExecDisplayCall(call_id="", command=command, parsed=parse_command_actions(command))
        return self._exec_call_header_rows(
            call,
            running=True,
            ok=True,
            terminal_width=terminal_width,
        )

    def _exec_call_header_rows(
        self,
        call: "_ExecDisplayCall",
        *,
        running: bool,
        ok: bool,
        terminal_width: int,
    ) -> list[str]:
        command_lines = _command_display_lines(call.command, self._style)
        if running:
            title = "Running"
            bullet = self._style.marker("cyan")
        elif call.exit_code == 0 and ok:
            title = "Ran"
            bullet = self._style.marker("green")
        else:
            title = "Ran"
            bullet = self._style.marker("red")
        return self._prefixed_wrapped_rows(
            command_lines,
            first_prefix=f"{bullet} {self._style.bold(title)} ",
            rest_prefix=self._style.dim("  │ "),
            max_lines=5,
            ellipsis_prefix=self._style.dim("  │ "),
            terminal_width=terminal_width,
        )

    def _live_exec_background_header_rows(
        self,
        marker: str,
        label: str,
        command_display: str,
        terminal_width: int,
    ) -> list[str]:
        prefix = f"{self._style.marker()} " if marker == "•" else f"{self._style.dim(marker)} "
        line = self._style.bold(label)
        command_snapshot = _terminal_interaction_command_snapshot(
            command_display,
            terminal_width - _visible_len(prefix) - _visible_len(label) - 3,
        )
        if command_snapshot:
            line += f" {self._style.dim('·')} {self._style.dim(command_snapshot)}"
        return self._prefixed_wrapped_rows(
            [line],
            first_prefix=prefix,
            rest_prefix="  ",
            terminal_width=terminal_width,
        )

    def _prefixed_wrapped_rows(
        self,
        lines: list[str],
        *,
        first_prefix: str,
        rest_prefix: str,
        terminal_width: int,
        max_lines: int | None = None,
        ellipsis_prefix: str | None = None,
    ) -> list[str]:
        rendered: list[tuple[str, str]] = []
        first_physical_line = True
        for logical_line in lines:
            if logical_line == "":
                rendered.append((rest_prefix if rendered else first_prefix, ""))
                first_physical_line = False
                continue
            line_first = first_physical_line
            prefix = first_prefix if line_first else rest_prefix
            available_width = max(10, _terminal_safe_width(terminal_width) - _visible_len(prefix))
            for segment in _wrap_ansi_line(logical_line, available_width):
                rendered.append((first_prefix if line_first else rest_prefix, segment))
                line_first = False
                first_physical_line = False
        if max_lines is not None and len(rendered) > max_lines:
            keep = max(1, max_lines - 1)
            rows = [f"{prefix}{segment}" for prefix, segment in rendered[:keep]]
            rows.append(f"{ellipsis_prefix or rest_prefix}{self._style.dim(_ellipsis_text(len(rendered) - keep))}")
            return rows
        return [f"{prefix}{segment}" for prefix, segment in rendered]

    def _clear_live_exec_panel(self, *, leading_gap_after_clear: bool = False) -> None:
        if self._live_exec_panel_rows <= 0:
            self._live_exec_panel_rows = 0
            return
        self._write(
            _clear_rendered_block_sequence(self._live_exec_panel_rows),
            partial_line_open=False,
            live_op="live_clear",
        )
        self._live_exec_panel_rows = 0
        if leading_gap_after_clear:
            self._live_exec_panel_needs_leading_gap = True

    def _live_exec_has_other_regions(self, call_id: str) -> bool:
        return any(active_call_id != call_id for active_call_id in self._live_exec_regions)

    def _is_live_exec_tail_region(self, call_id: str) -> bool:
        if call_id not in self._live_exec_regions:
            return False
        return next(reversed(self._live_exec_regions), None) == call_id

    def _live_exec_final_output(self, call_id: str, output: str) -> str:
        rendered = self._live_exec_output_text.get(call_id, "")
        if not rendered:
            return output
        remaining = self._remaining_live_exec_output(call_id, output)
        return rendered + remaining

    def _remaining_live_exec_output(self, call_id: str, output: str) -> str:
        if not output:
            return ""
        rendered = self._live_exec_output_text.get(call_id, "")
        if not rendered:
            return output
        if output.startswith(rendered):
            return output[len(rendered):]
        if output.strip() in rendered.strip():
            return ""
        return output

    def _detach_live_exec_region(self, call_id: str, *, commit_if_last: bool) -> None:
        if call_id not in self._live_exec_regions:
            return
        other_regions = any(active_call_id != call_id for active_call_id in self._live_exec_regions)
        if self._live_exec_panel_rows > 0:
            if commit_if_last and not other_regions:
                self._live_exec_panel_rows = 0
            else:
                self._clear_live_exec_panel()
        self._live_exec_regions.pop(call_id, None)

    def _freeze_live_exec_region(self, call_id: str, *, obscure_for_future_delta: bool = False) -> None:
        self._detach_live_exec_region(call_id, commit_if_last=False)
        self._live_exec_incremental_rendered.discard(call_id)
        if obscure_for_future_delta and call_id in self._live_exec_output_seen:
            self._live_exec_incremental_obscured.add(call_id)
        else:
            self._live_exec_incremental_obscured.discard(call_id)
        self._live_exec_incremental_segment_output.pop(call_id, None)
        self._live_exec_incremental_header_start.pop(call_id, None)
        self._live_exec_incremental_header_rows.pop(call_id, None)
        self._live_exec_incremental_total_rows.pop(call_id, None)

    def _clear_live_exec_metadata(self, call_id: str) -> None:
        self._live_exec_rendered.discard(call_id)
        self._live_exec_output_seen.discard(call_id)
        self._live_exec_output_text.pop(call_id, None)
        self._live_exec_incremental_rendered.discard(call_id)
        self._live_exec_incremental_obscured.discard(call_id)
        self._live_exec_incremental_segment_output.pop(call_id, None)
        self._live_exec_incremental_header_start.pop(call_id, None)
        self._live_exec_incremental_header_rows.pop(call_id, None)
        self._live_exec_incremental_total_rows.pop(call_id, None)

    def _mark_background_terminal_idle(self, call_id: str, *, preserve_live_metadata: bool = False) -> None:
        if not call_id:
            return
        self._idle_background_terminal_call_ids.add(call_id)
        self._detach_live_exec_region(call_id, commit_if_last=True)
        if not preserve_live_metadata:
            self._clear_live_exec_metadata(call_id)

    def _clear_live_exec(self, call_id: str) -> None:
        self._detach_live_exec_region(call_id, commit_if_last=True)
        self._clear_live_exec_metadata(call_id)

    def _finish_live_output_line(self, call_id: str) -> None:
        self._detach_live_exec_region(call_id, commit_if_last=True)

    def _finish_all_live_output_lines(self) -> None:
        if self._suspend_live_finish > 0:
            return
        if self._live_exec_regions:
            self._clear_live_exec_panel()
            for call_id in list(self._live_exec_regions):
                self._freeze_live_exec_region(call_id, obscure_for_future_delta=True)
            self._live_exec_incremental_visible_rows = 0
            return
        for call_id in self._idle_background_terminal_call_ids:
            if call_id in self._live_exec_incremental_rendered:
                self._live_exec_incremental_obscured.add(call_id)

    def _render_plan(self, meta: dict[str, Any]) -> None:
        self._line(f"{self._style.marker()} {self._style.bold('Updated Plan')}")
        safe_width = _terminal_safe_width(shutil.get_terminal_size((100, 24)).columns)
        explanation = meta.get("explanation")
        indented: list[tuple[str, Any | None]] = []
        if isinstance(explanation, str) and explanation.strip():
            for rendered_line in _wrap_ansi_line(
                explanation.strip(),
                max(10, safe_width - 6),
            ):
                indented.append((rendered_line, lambda value, style=self._style: style.dim(style.italic(value))))
        plan = meta.get("plan")
        if isinstance(plan, list) and plan:
            for item in plan:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "")
                step = str(item.get("step") or "")
                if status == "completed":
                    marker_text = self._style.dim(self._style.strike("✔"))
                    render_step = lambda value, style=self._style: style.dim(style.strike(value))
                elif status == "in_progress":
                    marker_text = self._style.cyan(self._style.bold("□"))
                    render_step = lambda value, style=self._style: style.cyan(style.bold(value))
                else:
                    marker_text = self._style.dim("□")
                    render_step = self._style.dim
                wrapped = _wrap_ansi_line(step, max(10, safe_width - 8))
                if wrapped:
                    indented.append((f"{marker_text} {render_step(wrapped[0])}", None))
                    for continuation in wrapped[1:]:
                        indented.append((f"  {continuation}", render_step))
            else:
                pass
        elif isinstance(plan, list):
            indented.append((self._style.dim(self._style.italic("(no steps provided)")), None))
        if not indented:
            return
        for index, (line, transform) in enumerate(indented):
            prefix = self._style.dim("  └ " if index == 0 else "    ")
            rendered = transform(line) if transform is not None else line
            self._line(f"{prefix}{rendered}")

    def _render_request_user_input_result(self, meta: dict[str, Any], *, interrupted: bool) -> bool:
        questions = meta.get("questions")
        answers = meta.get("answers")
        if not isinstance(questions, list) or not isinstance(answers, dict):
            return False
        total = len(questions)
        answered = sum(
            1
            for question in questions
            if isinstance(question, dict)
            and _request_user_input_answer_list(answers.get(str(question.get("id") or "")))
        )
        header = f"{self._style.marker()} {self._style.bold('Questions')} {self._style.dim(f'{answered}/{total} answered')}"
        if interrupted:
            header += f" {self._style.cyan('(interrupted)')}"
        self._line(header)
        for question in questions:
            if not isinstance(question, dict):
                continue
            question_text = str(question.get("question") or "")
            answer_values = _request_user_input_answer_list(answers.get(str(question.get("id") or "")))
            if not answer_values:
                question_text = f"{question_text} {self._style.dim('(unanswered)')}"
            self._emit_prefixed_lines([question_text], first_prefix="  • ", rest_prefix="    ")
            if not answer_values:
                continue
            if bool(question.get("isSecret") or question.get("is_secret")):
                self._emit_prefixed_lines(
                    ["••••••"],
                    first_prefix=self._style.dim("    answer: "),
                    rest_prefix=self._style.dim("            "),
                    transform=self._style.cyan,
                )
                continue
            options, note = _split_request_user_input_answer_values(answer_values)
            for option in options:
                self._emit_prefixed_lines(
                    [option],
                    first_prefix=self._style.dim("    answer: "),
                    rest_prefix=self._style.dim("            "),
                    transform=self._style.cyan,
                )
            if note:
                label = "    note: " if options else "    answer: "
                continuation = "          " if options else "            "
                self._emit_prefixed_lines(
                    [note],
                    first_prefix=self._style.dim(label),
                    rest_prefix=self._style.dim(continuation),
                    transform=self._style.cyan,
                )
        if interrupted and answered < total:
            self._emit_prefixed_lines(
                [f"interrupted with {total - answered} unanswered"],
                first_prefix=self._style.dim(self._style.cyan("  ↳ ")),
                rest_prefix=self._style.dim("    "),
                transform=lambda value, style=self._style: style.dim(style.cyan(value)),
            )
        return True

    def _render_agent_message(self, text: str) -> None:
        self._final_message_rendered = True
        if self._had_work_activity:
            self._render_final_separator()
            self._had_work_activity = False
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        safe_width = _terminal_safe_width(terminal_width)
        lines = _render_markdown_for_terminal(
            text,
            self._style,
            terminal_width=max(10, safe_width - 2),
        )
        if not lines:
            return
        self._begin_cell()
        self._emit_prefixed_lines(lines, first_prefix=f"{self._style.marker('magenta')} ", rest_prefix="  ")
        self._render_live_exec_panel_if_needed()

    def _render_reasoning_message(self, text: str) -> None:
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        safe_width = _terminal_safe_width(terminal_width)
        lines = _render_reasoning_for_terminal(text, self._style, terminal_width=max(10, safe_width - 2))
        if not lines:
            return
        self._begin_cell()
        self._emit_prefixed_lines(
            lines,
            first_prefix=f"{self._style.marker()} ",
            rest_prefix="  ",
            transform=self._style.dim,
        )
        self._render_live_exec_panel_if_needed()

    def _render_final_separator(self) -> None:
        self._begin_cell()
        width = shutil.get_terminal_size((100, 24)).columns
        self._line(self._style.dim("─" * max(20, _terminal_safe_width(width))))

    def _begin_work_cell(self) -> None:
        self._had_work_activity = True
        self._begin_cell()

    def _begin_cell(self) -> None:
        if (
            self._pending_background_terminal_waits
            and not self._flushing_background_terminal_wait
        ):
            self._flush_pending_background_terminal_wait()
        if self._printed_any_cell:
            self._line("")
        self._printed_any_cell = True

    def _emit_prefixed_lines(
        self,
        lines: list[str],
        *,
        first_prefix: str,
        rest_prefix: str,
        transform: Any | None = None,
    ) -> None:
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        safe_width = _terminal_safe_width(terminal_width)
        first_physical_line = True
        for logical_line in lines:
            if logical_line == "":
                self._line("")
                first_physical_line = False
                continue
            line_first = first_physical_line
            prefix = first_prefix if line_first else rest_prefix
            available_width = max(10, safe_width - _visible_len(prefix))
            for segment in _wrap_ansi_line(logical_line, available_width):
                prefix = first_prefix if line_first else rest_prefix
                rendered = transform(segment) if transform is not None else segment
                self._line(f"{prefix}{rendered}")
                line_first = False
                first_physical_line = False

    def _emit_limited_prefixed_lines(
        self,
        lines: list[str],
        *,
        first_prefix: str,
        rest_prefix: str,
        max_lines: int,
        ellipsis_prefix: str,
    ) -> None:
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        safe_width = _terminal_safe_width(terminal_width)
        rendered: list[tuple[str, str]] = []
        first_physical_line = True
        for logical_line in lines:
            line_first = first_physical_line
            prefix = first_prefix if line_first else rest_prefix
            available_width = max(10, safe_width - _visible_len(prefix))
            for segment in _wrap_ansi_line(logical_line, available_width):
                rendered.append((first_prefix if line_first else rest_prefix, segment))
                line_first = False
                first_physical_line = False
        if len(rendered) <= max_lines:
            for prefix, segment in rendered:
                self._line(f"{prefix}{segment}")
            return
        keep = max(1, max_lines - 1)
        for prefix, segment in rendered[:keep]:
            self._line(f"{prefix}{segment}")
        self._line(f"{ellipsis_prefix}{self._style.dim(_ellipsis_text(len(rendered) - keep))}")

    def _line(self, text: str) -> None:
        self._finish_all_live_output_lines()
        if self._line_sink is not None:
            self._line_sink(text)
            return
        print(text, file=sys.stderr, flush=True)

    def _write(self, text: str, *, partial_line_open: bool, live_op: str | None = None) -> None:
        if self._line_sink is not None:
            self._line_sink(_ConsoleWrite(text, partial_line_open=partial_line_open, live_op=live_op))
            return
        sys.stderr.write(text)
        sys.stderr.flush()


@dataclass
class _LiveExecRegion:
    call_id: str
    kind: str
    command: str = ""
    stdin: str = ""


@dataclass
class _ExecDisplayCall:
    call_id: str
    command: str
    parsed: list[dict[str, Any]]
    yield_time_ms: int | None = None
    output: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None

    @property
    def is_exploration(self) -> bool:
        return bool(self.parsed) and all(
            action.get("type") in {"read", "list_files", "search"} for action in self.parsed
        )

    @property
    def reads_only(self) -> bool:
        return bool(self.parsed) and all(action.get("type") == "read" for action in self.parsed)


@dataclass
class _DiffDisplayRow:
    kind: str
    line_number: int | None
    text: str = ""


@dataclass
class _FileChangeDisplay:
    kind: str
    path: str
    additions: int
    deletions: int
    rows: list[_DiffDisplayRow]
    move_path: str | None = None


def _file_change_path_label(change: _FileChangeDisplay) -> str:
    if change.move_path:
        return f"{change.path} → {change.move_path}"
    return change.path


def _file_change_display_from_metadata(meta: dict[str, Any]) -> list[_FileChangeDisplay]:
    changes = meta.get("changes")
    if isinstance(changes, dict):
        raw_changes = []
        for path, raw_change in changes.items():
            if not isinstance(raw_change, dict):
                continue
            normalized = dict(raw_change)
            normalized.setdefault("path", path)
            raw_changes.append(normalized)
    elif isinstance(changes, list):
        raw_changes = changes
    else:
        return []
    rendered: list[_FileChangeDisplay] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, dict):
            continue
        path = str(raw_change.get("path") or "")
        if not path:
            continue
        kind = str(raw_change.get("type") or "update")
        move_path_value = raw_change.get("move_path")
        move_path = str(move_path_value) if move_path_value else None
        if kind == "add":
            rows = _content_diff_rows(str(raw_change.get("content") or ""), "+")
        elif kind == "delete":
            rows = _content_diff_rows(str(raw_change.get("content") or ""), "-")
        else:
            rows = _unified_diff_rows(str(raw_change.get("unified_diff") or ""))
        additions = _int_or(raw_change.get("additions"), _count_rows(rows, "+"))
        deletions = _int_or(raw_change.get("deletions"), _count_rows(rows, "-"))
        rendered.append(
            _FileChangeDisplay(
                kind=kind,
                path=path,
                move_path=move_path,
                additions=additions,
                deletions=deletions,
                rows=rows,
            )
        )
    return rendered


def _visible_command_output(meta: dict[str, Any], payload: dict[str, Any]) -> str:
    if meta:
        for key in ("aggregated_output", "output"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if any(
            key in meta
            for key in (
                "chunk_id",
                "wall_time_seconds",
                "session_id",
                "exit_code",
                "stdout",
                "stderr",
                "aggregated_output",
            )
        ):
            return ""
    output = payload.get("output")
    if not isinstance(output, str) or not output.strip():
        return ""
    return _strip_unified_exec_response_metadata(output)


def _tool_failure_output(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    for source in (payload, meta):
        for key in ("output", "error", "message"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _tool_display_title(name: str) -> str:
    words = " ".join(part for part in name.replace("-", "_").split("_") if part)
    if not words:
        return "Tool"
    return words[:1].upper() + words[1:]


def _strip_unified_exec_response_metadata(text: str) -> str:
    if "\nOutput:\n" not in text:
        return text
    head, output = text.split("\nOutput:\n", 1)
    metadata_lines = head.splitlines()
    if not metadata_lines:
        return text
    known_prefixes = (
        "Chunk ID:",
        "Wall time:",
        "Process exited with code ",
        "Process running with session ID ",
        "Original token count:",
    )
    if all(line.startswith(known_prefixes) for line in metadata_lines):
        return output
    return text


def _file_change_display_from_patch(arguments: Any) -> list[_FileChangeDisplay]:
    if isinstance(arguments, str):
        patch = arguments
    elif isinstance(arguments, dict):
        patch = arguments.get("patch") if isinstance(arguments.get("patch"), str) else ""
    else:
        patch = ""
    if not patch.strip():
        return []
    if patch.lstrip().startswith("*** Begin Patch"):
        return _volley_patch_display_changes(patch)
    return _unified_patch_display_changes(patch)


def _content_diff_rows(content: str, kind: str) -> list[_DiffDisplayRow]:
    return [_DiffDisplayRow(kind, index, line) for index, line in enumerate(content.splitlines(), 1)]


def _unified_patch_display_changes(patch: str) -> list[_FileChangeDisplay]:
    changes: list[_FileChangeDisplay] = []
    old_path: str | None = None
    new_path: str | None = None
    hunk_lines: list[str] = []

    def flush() -> None:
        nonlocal old_path, new_path, hunk_lines
        if old_path is None and new_path is None:
            return
        old = _strip_diff_display_prefix(old_path or "")
        new = _strip_diff_display_prefix(new_path or "")
        if old == "/dev/null":
            kind = "add"
            path = new
            move_path = None
        elif new == "/dev/null":
            kind = "delete"
            path = old
            move_path = None
        else:
            kind = "update"
            path = old or new
            move_path = new if old and new and old != new else None
        rows = _unified_diff_rows("\n".join(hunk_lines) + ("\n" if hunk_lines else ""))
        changes.append(
            _FileChangeDisplay(
                kind=kind,
                path=path,
                move_path=move_path,
                additions=_count_rows(rows, "+"),
                deletions=_count_rows(rows, "-"),
                rows=rows,
            )
        )
        old_path = None
        new_path = None
        hunk_lines = []

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            continue
        if line.startswith("--- "):
            flush()
            old_path = _diff_header_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _diff_header_path(line[4:])
            continue
        if line.startswith("@@") or (hunk_lines and line.startswith((" ", "+", "-", "\\"))):
            hunk_lines.append(line)
    flush()
    return [change for change in changes if change.path]


def _volley_patch_display_changes(patch: str) -> list[_FileChangeDisplay]:
    changes: list[_FileChangeDisplay] = []
    current_path: str | None = None
    current_kind: str | None = None
    move_path: str | None = None
    rows: list[_DiffDisplayRow] = []
    old_ln = 1
    new_ln = 1

    def flush() -> None:
        nonlocal current_path, current_kind, move_path, rows, old_ln, new_ln
        if current_path and current_kind:
            changes.append(
                _FileChangeDisplay(
                    kind=current_kind,
                    path=current_path,
                    move_path=move_path,
                    additions=_count_rows(rows, "+"),
                    deletions=_count_rows(rows, "-"),
                    rows=rows,
                )
            )
        current_path = None
        current_kind = None
        move_path = None
        rows = []
        old_ln = 1
        new_ln = 1

    for line in patch.splitlines():
        if line.startswith("*** Add File: "):
            flush()
            current_path = line.removeprefix("*** Add File: ").strip()
            current_kind = "add"
            continue
        if line.startswith("*** Delete File: "):
            flush()
            current_path = line.removeprefix("*** Delete File: ").strip()
            current_kind = "delete"
            continue
        if line.startswith("*** Update File: "):
            flush()
            current_path = line.removeprefix("*** Update File: ").strip()
            current_kind = "update"
            continue
        if line.startswith("*** Move to: "):
            move_path = line.removeprefix("*** Move to: ").strip()
            continue
        if line.startswith("*** End Patch"):
            flush()
            break
        if current_kind == "add" and line.startswith("+"):
            rows.append(_DiffDisplayRow("+", new_ln, line[1:]))
            new_ln += 1
        elif current_kind == "update":
            if line.startswith("@@"):
                if rows:
                    rows.append(_DiffDisplayRow("ellipsis", None))
                continue
            if line.startswith("+"):
                rows.append(_DiffDisplayRow("+", new_ln, line[1:]))
                new_ln += 1
            elif line.startswith("-"):
                rows.append(_DiffDisplayRow("-", old_ln, line[1:]))
                old_ln += 1
            elif line.startswith(" "):
                rows.append(_DiffDisplayRow(" ", new_ln, line[1:]))
                old_ln += 1
                new_ln += 1
    return changes


def _unified_diff_rows(diff_text: str) -> list[_DiffDisplayRow]:
    rows: list[_DiffDisplayRow] = []
    old_ln = 1
    new_ln = 1
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            if in_hunk and rows:
                rows.append(_DiffDisplayRow("ellipsis", None))
            in_hunk = True
            old_ln, new_ln = _parse_hunk_line_numbers(line)
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            rows.append(_DiffDisplayRow("+", new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            rows.append(_DiffDisplayRow("-", old_ln, line[1:]))
            old_ln += 1
        elif line.startswith(" "):
            rows.append(_DiffDisplayRow(" ", new_ln, line[1:]))
            old_ln += 1
            new_ln += 1
    return rows


def _parse_hunk_line_numbers(line: str) -> tuple[int, int]:
    match = re.search(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@", line)
    if not match:
        return 1, 1
    return int(match.group("old")), int(match.group("new"))


def _count_rows(rows: list[_DiffDisplayRow], kind: str) -> int:
    return sum(1 for row in rows if row.kind == kind)


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _diff_header_path(value: str) -> str:
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


def _strip_diff_display_prefix(path: str) -> str:
    if path in {"/dev/null", "dev/null"}:
        return "/dev/null"
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


class _AnsiStyle:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def italic(self, text: str) -> str:
        return self._wrap("3", text)

    def strike(self, text: str) -> str:
        return self._wrap("9", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def blue(self, text: str) -> str:
        return self._wrap("34", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def magenta(self, text: str) -> str:
        return self._wrap("35", text)

    def accent(self, text: str) -> str:
        red, green, blue = _VOLLEY_ACCENT_RGB
        return self.fg_rgb(red, green, blue, text)

    def accent_bold(self, text: str) -> str:
        red, green, blue = _VOLLEY_ACCENT_RGB
        return self.fg_rgb_bold(red, green, blue, text)

    def muted(self, text: str) -> str:
        red, green, blue = _VOLLEY_MUTED_RGB
        return self.fg_rgb(red, green, blue, text)

    def marker(self, color: str = "dim") -> str:
        codes = {
            "dim": "1;90",
            "green": "1;32",
            "yellow": "1;33",
            "red": "1;31",
            "cyan": "1;36",
            "magenta": "1;35",
        }
        return self._wrap(codes.get(color, "1;90"), "•")

    def marker_off(self) -> str:
        return self._wrap("2;90", "•")

    def fg256(self, color: int, text: str) -> str:
        return self._wrap(f"38;5;{color}", text)

    def fg256_bold(self, color: int, text: str) -> str:
        return self._wrap(f"1;38;5;{color}", text)

    def fg_rgb(self, red: int, green: int, blue: int, text: str) -> str:
        return self._wrap(f"38;2;{red};{green};{blue}", text)

    def fg_rgb_bold(self, red: int, green: int, blue: int, text: str) -> str:
        return self._wrap(f"1;38;2;{red};{green};{blue}", text)

    def inverse(self, text: str) -> str:
        return self._wrap("7", text)

    def composer(self, text: str) -> str:
        return self.user_message(text)

    def composer_bold(self, text: str) -> str:
        return self.user_message_bold(text)

    def composer_dim(self, text: str) -> str:
        return self.user_message_dim(text)

    def composer_border(self, text: str) -> str:
        red, green, blue = _VOLLEY_BORDER_RGB
        return self.fg_rgb(red, green, blue, text)

    def user_message(self, text: str) -> str:
        return self._wrap(_user_message_style_code(), text)

    def user_message_bold(self, text: str) -> str:
        return self._wrap(f"1;{_user_message_style_code()}", text)

    def user_message_dim(self, text: str) -> str:
        return self._wrap(f"2;{_user_message_style_code()}", text)

    def user_message_bold_dim(self, text: str) -> str:
        return self._wrap(f"1;2;{_user_message_style_code()}", text)

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{code}m{text}\033[0m"


def _user_message_style_code() -> str:
    fg_r, fg_g, fg_b = _USER_MESSAGE_FG_RGB
    bg_r, bg_g, bg_b = _USER_MESSAGE_BG_RGB
    return f"38;2;{fg_r};{fg_g};{fg_b};48;2;{bg_r};{bg_g};{bg_b}"


def _should_use_color(color_mode: str) -> bool:
    if color_mode == "always":
        return True
    if color_mode == "never":
        return False
    return sys.stderr.isatty()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text: str) -> int:
    return _display_width(_ANSI_RE.sub("", text))


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _wrap_ansi_line(text: str, width: int) -> list[str]:
    if width <= 0 or _visible_len(text) <= width:
        return [text]
    wrapped: list[str] = []
    current = ""
    current_width = 0
    for chunk in re.findall(r"\s+|\S+\s*", text):
        chunk_width = _visible_len(chunk)
        if current and current_width + chunk_width > width:
            wrapped.append(current.rstrip())
            next_chunk = chunk.lstrip()
            if _visible_len(next_chunk) > width:
                pieces = _split_visible_chunk(next_chunk, width)
                wrapped.extend(piece.rstrip() for piece in pieces[:-1])
                current = pieces[-1].lstrip()
            else:
                current = next_chunk
            current_width = _visible_len(current)
            continue
        if not current and chunk_width > width:
            pieces = _split_visible_chunk(chunk, width)
            wrapped.extend(piece.rstrip() for piece in pieces[:-1])
            current = pieces[-1].lstrip()
            current_width = _visible_len(current)
            continue
        current += chunk
        current_width += chunk_width
    if current or not wrapped:
        wrapped.append(current.rstrip())
    return wrapped


def _terminal_safe_width(width: int) -> int:
    if width <= 1:
        return max(1, width)
    return width - 1


def _split_visible_chunk(text: str, width: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    current_width = 0
    active_sgr = ""
    index = 0
    while index < len(text):
        match = _ANSI_RE.match(text, index)
        if match:
            sequence = match.group(0)
            current += sequence
            active_sgr = _next_active_sgr(active_sgr, sequence)
            index = match.end()
            continue
        char = text[index]
        char_width = _display_width(char)
        if current_width + char_width > width and current:
            if active_sgr:
                current += "\033[0m"
            pieces.append(current)
            current = active_sgr
            current_width = 0
        current += char
        current_width += char_width
        index += 1
    if current:
        if active_sgr and not current.endswith("\033[0m"):
            current += "\033[0m"
        pieces.append(current)
    return pieces or [text]


def _next_active_sgr(active: str, sequence: str) -> str:
    match = _ANSI_RE.fullmatch(sequence)
    if match is None:
        return active
    params = sequence[2:-1]
    values = [value for value in params.split(";") if value != ""]
    if not values:
        values = ["0"]
    if any(value in {"0", "00", "39", "49"} for value in values):
        return ""
    return active + sequence


def _ellipsis_text(omitted: int, *, transcript_hint: bool = False) -> str:
    suffix = " (ctrl + t to view transcript)" if transcript_hint else ""
    return f"… +{omitted} lines{suffix}"


def _dim_command_output_segment(segment: str, style: "_AnsiStyle") -> str:
    if not segment:
        return segment
    return style.dim(_ANSI_RE.sub("", segment))


def _rows_have_prefix(rows: list[str], prefix: list[str]) -> bool:
    if len(prefix) > len(rows):
        return False
    return rows[: len(prefix)] == prefix


def _output_delta_since(previous_output: str, output: str) -> str:
    if not previous_output:
        return output
    if output.startswith(previous_output):
        return output[len(previous_output) :]
    if output.strip() in previous_output.strip():
        return ""
    return output


def _truncate_middle_parts(lines: list[str], max_lines: int) -> tuple[list[str], list[str], int]:
    if len(lines) <= max_lines:
        return lines, [], 0
    if max_lines <= 1:
        return [], [], len(lines)
    head_count = max(1, (max_lines - 1) // 2)
    tail_count = max(0, max_lines - 1 - head_count)
    omitted = max(0, len(lines) - head_count - tail_count)
    tail = lines[len(lines) - tail_count :] if tail_count else []
    return lines[:head_count], tail, omitted


def _clear_rendered_block_sequence(line_count: int) -> str:
    if line_count <= 0:
        return ""
    parts: list[str] = ["\r"]
    parts.append(f"\033[{line_count}A")
    for index in range(line_count):
        parts.append("\r\033[2K")
        if index < line_count - 1:
            parts.append("\033[1B")
    if line_count > 1:
        parts.append(f"\r\033[{line_count - 1}A")
    else:
        parts.append("\r")
    return "".join(parts)


def _live_output_display_rows(output: str, style: "_AnsiStyle", terminal_width: int) -> list[str]:
    first_prefix = style.dim("  └ ")
    rest_prefix = style.dim("    ")
    rows: list[str] = []
    first = True
    cleaned = output.replace("\r", "")
    logical_lines = cleaned.split("\n")
    if logical_lines and logical_lines[-1] == "":
        logical_lines.pop()
    for logical_line in logical_lines or [cleaned]:
        prefix = first_prefix if first else rest_prefix
        available = max(10, _terminal_safe_width(terminal_width) - _visible_len(prefix))
        wrapped = _wrap_ansi_line(logical_line, available)
        for index, segment in enumerate(wrapped):
            current_prefix = prefix if index == 0 else rest_prefix
            rows.append(f"{current_prefix}{_dim_command_output_segment(segment, style)}")
            first = False
    max_rows = 5
    if len(rows) <= max_rows:
        return rows
    head_count = 2
    tail_count = max(1, max_rows - head_count - 1)
    omitted = max(1, len(rows) - head_count - tail_count)
    marker = f"{rest_prefix}{style.dim(_ellipsis_text(omitted, transcript_hint=True))}"
    return [*rows[:head_count], marker, *rows[-tail_count:]]


def _render_markdown_for_terminal(
    text: str,
    style: _AnsiStyle,
    *,
    emphasis: bool = True,
    terminal_width: int | None = None,
) -> list[str]:
    lines: list[str] = []
    normalized_text = _unwrap_markdown_fences(text)
    raw_lines = normalized_text.splitlines() or [normalized_text]
    index = 0
    width = terminal_width or shutil.get_terminal_size((100, 24)).columns
    while index < len(raw_lines):
        raw_line = raw_lines[index]
        stripped = raw_line.strip()
        fence = _markdown_fence(stripped)
        if fence is not None:
            marker, info = fence
            code_lines: list[str] = []
            index += 1
            while index < len(raw_lines):
                closing = _markdown_fence(raw_lines[index].strip())
                if closing is not None and closing[0] == marker:
                    index += 1
                    break
                code_lines.append(raw_lines[index])
                index += 1
            lines.extend(_render_code_block_for_terminal("\n".join(code_lines), info, style))
            continue
        table = _markdown_table_at(raw_lines, index)
        if table is not None:
            rendered, alignments, consumed = table
            lines.extend(
                _render_markdown_table(
                    rendered,
                    style,
                    emphasis=emphasis,
                    terminal_width=width,
                    alignments=alignments,
                )
            )
            index += consumed
            continue
        if _is_markdown_rule(stripped):
            lines.append(style.dim("─" * max(20, width)))
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line)
        if heading:
            heading_text = _format_inline_markdown(heading.group(2), style, emphasis=emphasis)
            lines.append(style.bold(heading_text) if emphasis else heading_text)
            index += 1
            continue
        quote = re.match(r"^(\s*)>\s?(.*)$", raw_line)
        if quote:
            lines.append(
                f"{quote.group(1)}{style.dim('>')} "
                f"{style.dim(_format_inline_markdown(quote.group(2), style, emphasis=emphasis))}"
            )
            index += 1
            continue
        lines.append(_format_inline_markdown(raw_line, style, emphasis=emphasis))
        index += 1
    return lines


def _markdown_fence(stripped: str) -> tuple[str, str] | None:
    for marker in ("```", "~~~"):
        if stripped.startswith(marker):
            return marker, stripped[len(marker) :].strip()
    return None


def _render_code_block_for_terminal(code: str, info: str, style: _AnsiStyle) -> list[str]:
    lang = _code_fence_language(info)
    if lang:
        highlighted = _highlight_code_for_terminal(code, lang, style)
        if highlighted is not None:
            return highlighted
    plain = code.splitlines()
    if not plain:
        return [""]
    return plain


def _code_fence_language(info: str) -> str | None:
    if not info.strip():
        return None
    token = re.split(r"[, \t]+", info.strip(), maxsplit=1)[0]
    token = token.strip("{}.")
    if not token:
        return None
    aliases = {
        "csharp": "c#",
        "c-sharp": "c#",
        "golang": "go",
        "python3": "python",
        "shell": "bash",
        "sh": "bash",
        "zsh": "bash",
    }
    return aliases.get(token.lower(), token.lower())


def _highlight_code_for_terminal(code: str, lang: str, style: _AnsiStyle) -> list[str] | None:
    if not style.enabled or not code:
        return None
    if len(code.encode("utf-8")) > 512 * 1024 or len(code.splitlines()) > 10_000:
        return None
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
        from pygments.util import ClassNotFound
    except Exception:
        return None
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        try:
            lexer = get_lexer_for_filename(f"snippet.{lang}")
        except ClassNotFound:
            return None
    try:
        rendered = highlight(code, lexer, Terminal256Formatter(style=_pygments_style_name()))
    except Exception:
        return None
    rendered_lines = rendered.rstrip("\n").splitlines()
    if _terminal_background_is_light(default=True):
        rendered_lines = [_improve_code_highlight_contrast_for_light_background(line) for line in rendered_lines]
    return rendered_lines or [""]


def _terminal_background_is_light(*, default: bool) -> bool:
    colorfgbg = os.environ.get("COLORFGBG", "")
    try:
        bg = int(colorfgbg.split(";")[-1])
    except (TypeError, ValueError):
        return default
    luminance = _ansi_256_luminance(bg)
    if luminance is None:
        return default
    return luminance >= 0.62


_ANSI_256_FG_RE = re.compile(r"\033\[(?P<prefix>(?:\d+;)*)38;5;(?P<color>\d+)(?P<suffix>(?:;\d+)*)m")


def _improve_code_highlight_contrast_for_light_background(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        color = int(match.group("color"))
        if not _ansi_256_foreground_is_too_light_for_light_background(color):
            return match.group(0)
        return f"\033[{match.group('prefix')}38;5;238{match.group('suffix')}m"

    return _ANSI_256_FG_RE.sub(replace, line)


def _ansi_256_foreground_is_too_light_for_light_background(color: int) -> bool:
    # Several dark syntax themes render ordinary tokens as white/near-white
    # (for example Pygments material/monokai use 15). On Volley's light terminal
    # surface those become washed out, while accent colors should stay intact.
    if color in {7, 15, 231}:
        return True
    if 250 <= color <= 255:
        return True
    luminance = _ansi_256_luminance(color)
    return luminance is not None and luminance >= 0.92 and _ansi_256_is_neutral_gray(color)


def _ansi_256_luminance(color: int) -> float | None:
    rgb = _ansi_256_rgb(color)
    if rgb is None:
        return None
    red, green, blue = rgb
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255


def _ansi_256_is_neutral_gray(color: int) -> bool:
    rgb = _ansi_256_rgb(color)
    if rgb is None:
        return False
    red, green, blue = rgb
    return max(rgb) - min(rgb) <= 8 or (red == green == blue)


def _ansi_256_rgb(color: int) -> tuple[int, int, int] | None:
    basic = {
        0: (0, 0, 0),
        1: (128, 0, 0),
        2: (0, 128, 0),
        3: (128, 128, 0),
        4: (0, 0, 128),
        5: (128, 0, 128),
        6: (0, 128, 128),
        7: (192, 192, 192),
        8: (128, 128, 128),
        9: (255, 0, 0),
        10: (0, 255, 0),
        11: (255, 255, 0),
        12: (0, 0, 255),
        13: (255, 0, 255),
        14: (0, 255, 255),
        15: (255, 255, 255),
    }
    if color in basic:
        return basic[color]
    if 16 <= color <= 231:
        value = color - 16
        scale = [0, 95, 135, 175, 215, 255]
        red = scale[value // 36]
        green = scale[(value % 36) // 6]
        blue = scale[value % 6]
        return red, green, blue
    if 232 <= color <= 255:
        gray = 8 + (color - 232) * 10
        return gray, gray, gray
    return None


_VOLLEY_THEME_TO_PYGMENTS = {
    "1337": "native",
    "ansi": "default",
    "base16": "native",
    "base16-256": "native",
    "base16-eighties-dark": "paraiso-dark",
    "base16-mocha-dark": "paraiso-dark",
    "base16-ocean-dark": "native",
    "base16-ocean-light": "default",
    "catppuccin-frappe": "material",
    "catppuccin-latte": "friendly",
    "catppuccin-macchiato": "material",
    "catppuccin-mocha": "material",
    "coldark-cold": "friendly",
    "coldark-dark": "native",
    "dark-neon": "native",
    "dracula": "dracula",
    "github": "github-dark",
    "gruvbox-dark": "gruvbox-dark",
    "gruvbox-light": "gruvbox-light",
    "inspired-github": "github-dark",
    "monokai-extended": "monokai",
    "monokai-extended-bright": "monokai",
    "monokai-extended-light": "monokai",
    "monokai-extended-origin": "monokai",
    "nord": "nord",
    "one-half-dark": "one-dark",
    "one-half-light": "default",
    "solarized-dark": "solarized-dark",
    "solarized-light": "solarized-light",
    "sublime-snazzy": "monokai",
    "two-dark": "native",
    "zenburn": "zenburn",
}


def _pygments_style_name(name: str | None = None) -> str:
    theme = (name or _CLI_SYNTAX_THEME or "monokai").strip().lower()
    return _VOLLEY_THEME_TO_PYGMENTS.get(theme, theme)


def _set_cli_syntax_theme(name: str) -> bool:
    global _CLI_SYNTAX_THEME
    theme = name.strip()
    if not theme:
        return False
    try:
        from pygments.styles import get_style_by_name
    except Exception:
        _CLI_SYNTAX_THEME = theme
        return True
    try:
        get_style_by_name(_pygments_style_name(theme))
    except Exception:
        return False
    _CLI_SYNTAX_THEME = theme
    return True


def _render_reasoning_for_terminal(
    text: str,
    style: _AnsiStyle,
    *,
    terminal_width: int | None = None,
) -> list[str]:
    normalized = _flatten_reasoning_heading(text.strip())
    return _render_markdown_for_terminal(normalized, style, emphasis=False, terminal_width=terminal_width)


def _flatten_reasoning_heading(text: str) -> str:
    strong = re.match(r"^\*\*([^*\n]+)\*\*\s*\n\s*\n(.+)$", text, flags=re.DOTALL)
    if strong:
        return f"{strong.group(1).strip()}: {strong.group(2).lstrip()}"
    heading = re.match(r"^#{1,6}\s+(.+?)\s*\n\s*\n(.+)$", text, flags=re.DOTALL)
    if heading:
        return f"{heading.group(1).strip()}: {heading.group(2).lstrip()}"
    return text


def _format_inline_markdown(text: str, style: _AnsiStyle, *, emphasis: bool = True) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"`([^`\n]+)`", lambda match: style.cyan(match.group(1)) if emphasis else match.group(1), text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", lambda match: style.bold(match.group(1)) if emphasis else match.group(1), text)
    text = re.sub(r"__([^_\n]+)__", lambda match: style.bold(match.group(1)) if emphasis else match.group(1), text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"\1", text)
    return text


def _unwrap_markdown_fences(text: str) -> str:
    raw_lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    while index < len(raw_lines):
        line = raw_lines[index]
        stripped = line.strip().lower()
        fence = None
        for marker in ("```", "~~~"):
            if stripped.startswith(marker):
                info = stripped[len(marker) :].strip()
                if info in {"md", "markdown"}:
                    fence = marker
                break
        if fence is None:
            out.append(line)
            index += 1
            continue
        body: list[str] = []
        close_index = index + 1
        while close_index < len(raw_lines):
            if raw_lines[close_index].strip().startswith(fence):
                break
            body.append(raw_lines[close_index])
            close_index += 1
        if close_index < len(raw_lines) and _contains_markdown_table(body):
            out.extend(body)
            index = close_index + 1
        else:
            out.append(line)
            out.extend(body)
            if close_index < len(raw_lines):
                out.append(raw_lines[close_index])
                index = close_index + 1
            else:
                index = close_index
    return "".join(out)


def _contains_markdown_table(lines: list[str]) -> bool:
    plain = [line.rstrip("\n") for line in lines]
    return any(
        _markdown_table_at(plain, index) is not None
        for index in range(max(0, len(plain) - 1))
    )


def _markdown_table_at(lines: list[str], start: int) -> tuple[list[list[str]], list[str], int] | None:
    if start + 1 >= len(lines):
        return None
    header = _parse_table_row(lines[start])
    separator = _parse_table_row(lines[start + 1])
    if header is None or separator is None or not _is_table_separator_row(separator):
        return None
    alignments = [_table_alignment(cell) for cell in separator]
    rows = [header]
    index = start + 2
    while index < len(lines):
        row = _parse_table_row(lines[index])
        if row is None:
            break
        rows.append(row)
        index += 1
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    return normalized, alignments + ["left"] * (width - len(alignments)), index - start


def _parse_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [cell.strip() for cell in stripped.split("|")]
    return cells if len(cells) >= 2 else None


def _is_table_separator_row(cells: list[str]) -> bool:
    for cell in cells:
        if not re.fullmatch(r":?-{3,}:?", cell.strip()):
            return False
    return True


def _table_alignment(cell: str) -> str:
    stripped = cell.strip()
    if stripped.startswith(":") and stripped.endswith(":"):
        return "center"
    if stripped.endswith(":"):
        return "right"
    return "left"


@dataclass
class _TableColumnMetrics:
    max_width: int
    header_token_width: int
    body_token_width: int
    avg_words_per_cell: float
    avg_cell_width: float
    kind: str


def _render_markdown_table(
    rows: list[list[str]],
    style: _AnsiStyle,
    *,
    emphasis: bool = True,
    terminal_width: int | None = None,
    alignments: list[str] | None = None,
) -> list[str]:
    if not rows or not rows[0]:
        return []
    column_count = len(rows[0])
    normalized = [row[:column_count] + [""] * (column_count - len(row)) for row in rows]
    alignments = (alignments or ["left"] * column_count)[:column_count] + ["left"] * max(
        0,
        column_count - len(alignments or []),
    )
    widths = _table_column_widths(
        normalized,
        terminal_width or shutil.get_terminal_size((100, 24)).columns,
    )
    if widths is None:
        return _render_table_pipe_fallback(normalized, style, emphasis=emphasis)

    rendered: list[str] = []
    rendered.append(_render_table_border("┌", "┬", "┐", widths, style))
    rendered.extend(_render_table_row(normalized[0], widths, alignments, style, emphasis=emphasis, header=True))
    rendered.append(_render_table_border("├", "┼", "┤", widths, style))
    for row in normalized[1:]:
        rendered.extend(_render_table_row(row, widths, alignments, style, emphasis=emphasis, header=False))
    rendered.append(_render_table_border("└", "┴", "┘", widths, style))
    return rendered


def _table_column_widths(rows: list[list[str]], terminal_width: int) -> list[int] | None:
    column_count = len(rows[0])
    min_column_width = 3
    border_width = 1 + (column_count * 3)
    available_width = max(0, terminal_width - border_width)
    if available_width < column_count * min_column_width:
        return None
    metrics = _collect_table_metrics(rows)
    widths = [max(metric.max_width, min_column_width) for metric in metrics]
    if sum(widths) <= available_width:
        return widths
    floors = [_preferred_column_floor(metric, min_column_width) for metric in metrics]
    while sum(floors) > available_width:
        candidates = [
            index
            for index, floor in enumerate(floors)
            if floor > min_column_width
        ]
        if not candidates:
            break
        index = min(candidates, key=lambda idx: (0 if metrics[idx].kind == "narrative" else 1, floors[idx]))
        floors[index] -= 1
    while sum(widths) > available_width:
        candidates = [
            index
            for index, width in enumerate(widths)
            if width > floors[index]
        ]
        if not candidates:
            return None
        index = min(candidates, key=lambda idx: _table_shrink_key(idx, widths, floors, metrics))
        widths[index] -= 1
    return widths


def _collect_table_metrics(rows: list[list[str]]) -> list[_TableColumnMetrics]:
    column_count = len(rows[0])
    metrics: list[_TableColumnMetrics] = []
    for column in range(column_count):
        header = rows[0][column]
        body = [row[column] for row in rows[1:]]
        header_token_width = _longest_token_width(header)
        body_token_width = max((_longest_token_width(cell) for cell in body), default=0)
        max_width = max(_cell_display_width(cell) for cell in [header, *body])
        non_empty_body = [cell for cell in body if cell.strip()]
        if non_empty_body:
            avg_words = sum(len(cell.split()) for cell in non_empty_body) / len(non_empty_body)
            avg_width = sum(_display_width(_plain_cell_text(cell)) for cell in non_empty_body) / len(non_empty_body)
        else:
            avg_words = float(len(header.split()))
            avg_width = float(_display_width(_plain_cell_text(header)))
        if body_token_width >= 20 and avg_words <= 2.0:
            kind = "structured"
        elif avg_words >= 4.0 or avg_width >= 28.0:
            kind = "narrative"
        else:
            kind = "structured"
        metrics.append(
            _TableColumnMetrics(
                max_width=max_width,
                header_token_width=header_token_width,
                body_token_width=body_token_width,
                avg_words_per_cell=avg_words,
                avg_cell_width=avg_width,
                kind=kind,
            )
        )
    return metrics


def _preferred_column_floor(metrics: _TableColumnMetrics, min_column_width: int) -> int:
    if metrics.kind == "narrative":
        token_target = min(metrics.header_token_width, 10)
    else:
        token_target = max(metrics.header_token_width, min(metrics.body_token_width, 16))
    return min(metrics.max_width, max(min_column_width, token_target))


def _table_shrink_key(
    index: int,
    widths: list[int],
    floors: list[int],
    metrics: list[_TableColumnMetrics],
) -> tuple[int, int]:
    metric = metrics[index]
    slack = widths[index] - floors[index]
    kind_cost = 0 if metric.kind == "narrative" else 2
    header_guard = 3 if widths[index] <= metric.header_token_width else 0
    density_guard = 0 if metric.avg_words_per_cell >= 4.0 or metric.avg_cell_width >= 24.0 else 1
    return kind_cost + header_guard + density_guard, -slack


def _longest_token_width(text: str) -> int:
    return max((_display_width(token) for token in _plain_cell_text(text).split()), default=0)


def _cell_display_width(text: str) -> int:
    plain_lines = _plain_cell_text(text).splitlines() or [""]
    return max(_display_width(line) for line in plain_lines)


def _plain_cell_text(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    return text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")


def _render_table_border(left: str, sep: str, right: str, widths: list[int], style: _AnsiStyle) -> str:
    return style.dim(left + sep.join("─" * (width + 2) for width in widths) + right)


def _render_table_row(
    row: list[str],
    widths: list[int],
    alignments: list[str],
    style: _AnsiStyle,
    *,
    emphasis: bool,
    header: bool,
) -> list[str]:
    wrapped_cells = [_wrap_table_cell(cell, widths[index], style, emphasis=emphasis) for index, cell in enumerate(row)]
    row_height = max((len(cell_lines) for cell_lines in wrapped_cells), default=1)
    rendered: list[str] = []
    for line_index in range(row_height):
        parts = [style.dim("│")]
        for column, width in enumerate(widths):
            cell_line = wrapped_cells[column][line_index] if line_index < len(wrapped_cells[column]) else ""
            if header and emphasis and cell_line:
                cell_line = style.bold(cell_line)
            parts.append(" ")
            parts.append(_align_ansi(cell_line, width, alignments[column]))
            parts.append(" ")
            parts.append(style.dim("│"))
        rendered.append("".join(parts))
    return rendered


def _wrap_table_cell(text: str, width: int, style: _AnsiStyle, *, emphasis: bool) -> list[str]:
    raw = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    formatted = _format_inline_markdown(raw, style, emphasis=emphasis)
    lines: list[str] = []
    for logical_line in formatted.splitlines() or [""]:
        if not logical_line:
            lines.append("")
        else:
            lines.extend(_wrap_ansi_line(logical_line, width))
    return lines or [""]


def _align_ansi(text: str, width: int, alignment: str) -> str:
    remaining = max(0, width - _visible_len(text))
    if alignment == "right":
        return (" " * remaining) + text
    if alignment == "center":
        left = remaining // 2
        return (" " * left) + text + (" " * (remaining - left))
    return text + (" " * remaining)


def _render_table_pipe_fallback(rows: list[list[str]], style: _AnsiStyle, *, emphasis: bool) -> list[str]:
    rendered: list[str] = []
    for index, row in enumerate(rows):
        line = "| " + " | ".join(_plain_cell_text(cell).replace("|", "\\|") for cell in row) + " |"
        rendered.append(_format_inline_markdown(line, style, emphasis=emphasis))
        if index == 0:
            rendered.append("|" + "|".join("---" for _ in row) + "|")
    return rendered


def _is_markdown_rule(text: str) -> bool:
    if len(text) < 3:
        return False
    return all(char == "-" for char in text) or all(char == "*" for char in text) or all(char == "_" for char in text)


def _assistant_item_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    for part in content:
        if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _user_item_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    for part in content:
        if isinstance(part, dict) and part.get("type") in {"input_text", "text"} and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


def _reasoning_item_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("summary", "content"):
        value = item.get(key)
        if isinstance(value, str):
            chunks.append(value)
            continue
        if isinstance(value, list):
            for part in value:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif isinstance(part, str):
                    chunks.append(part)
    return "\n".join(chunk for chunk in chunks if chunk)


def _web_search_query(item: dict[str, Any]) -> str:
    action = item.get("action")
    if isinstance(action, dict) and isinstance(action.get("query"), str):
        return action["query"]
    query = item.get("query")
    return query if isinstance(query, str) else ""


def _web_search_action_detail(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "")
    if action_type == "search":
        query = action.get("query")
        if isinstance(query, str) and query:
            return query
        queries = action.get("queries")
        if isinstance(queries, list) and queries:
            first = str(queries[0])
            return f"{first} ..." if len(queries) > 1 and first else first
        return ""
    if action_type == "open_page":
        url = action.get("url")
        return url if isinstance(url, str) else ""
    if action_type == "find_in_page":
        pattern = action.get("pattern")
        url = action.get("url")
        if isinstance(pattern, str) and isinstance(url, str):
            return f"'{pattern}' in {url}"
        if isinstance(pattern, str):
            return f"'{pattern}'"
        return url if isinstance(url, str) else ""
    return ""


def _command_display(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) >= 3 and Path(tokens[0]).name in {"bash", "zsh", "sh"} and tokens[1] in {"-lc", "-c"}:
        return tokens[2]
    return command


def _command_display_lines(command: str, style: _AnsiStyle) -> list[str]:
    display = _command_display(command)
    if style.enabled:
        highlighted = _highlight_code_for_terminal(display, "bash", style)
        if highlighted is not None and not _bash_highlight_is_low_contrast(highlighted):
            return highlighted
        fallback = _highlight_shell_command_for_terminal(display, style)
        if fallback:
            return fallback
        if highlighted is not None:
            return highlighted
    return display.splitlines() or [display]


_SHELL_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
    |(?P<single>'(?:[^'\\]|\\.)*')
    |(?P<double>"(?:[^"\\]|\\.)*")
    |(?P<op>\|\||&&|[|;<>])
    |(?P<word>[^\s|;&<>]+)
    """,
    re.VERBOSE,
)


def _bash_highlight_is_low_contrast(lines: list[str]) -> bool:
    joined = "".join(lines)
    if not _ANSI_RE.search(joined):
        return True
    visible = _ANSI_RE.sub("", joined)
    if not visible.strip():
        return False
    stripped = joined.lstrip()
    if not stripped.startswith("\033[") or stripped.startswith(("\033[38;5;15m", "\033[38;5;252m", "\033[38;5;249m")):
        return True
    # Pygments maps many bash tokens to bright white for several themes.  The
    # syntax highlighter themes can vary, so keep shell syntax readable here.
    # more visible accents, so fall back when the highlighted command is mostly
    # reset/default/white spans.
    accent_sequences = {
        sequence
        for sequence in re.findall(r"\x1b\[([0-9;]*)m", joined)
        if sequence not in {"", "0", "00", "39", "39;00", "38;5;15", "38;5;15;01"}
    }
    return not accent_sequences


def _highlight_shell_command_for_terminal(script: str, style: _AnsiStyle) -> list[str]:
    if not style.enabled:
        return script.splitlines() or [script]
    rendered: list[str] = []
    heredoc_delimiter: str | None = None
    for line in script.splitlines() or [script]:
        if heredoc_delimiter is not None:
            rendered.append(style.fg256(186, line))
            if line.strip() == heredoc_delimiter:
                heredoc_delimiter = None
            continue
        rendered.append(_highlight_shell_command_line(line, style))
        heredoc_delimiter = _shell_heredoc_delimiter(line)
    return rendered


def _highlight_shell_command_line(line: str, style: _AnsiStyle) -> str:
    rendered: list[str] = []
    at_command = True
    for match in _SHELL_TOKEN_RE.finditer(line):
        kind = match.lastgroup or "word"
        token = match.group(0)
        if kind == "space":
            rendered.append(token)
            continue
        if kind in {"single", "double"}:
            rendered.append(style.fg256(186, token))
            at_command = False
            continue
        if kind == "op":
            rendered.append(style.fg256(117, token))
            at_command = True
            continue
        rendered.append(_highlight_shell_word(token, style, at_command=at_command))
        if not _is_shell_assignment(token):
            at_command = False
    return "".join(rendered)


def _highlight_shell_word(token: str, style: _AnsiStyle, *, at_command: bool) -> str:
    if _is_shell_assignment(token):
        key, value = token.split("=", 1)
        return f"{style.cyan(key)}{style.dim('=')}{_highlight_shell_assignment_value(value, style)}"
    if token.startswith(("-", "--")):
        return style.yellow(token)
    if token.startswith("$") or token.startswith("${"):
        return style.fg256(141, token)
    if at_command:
        return style.cyan(token)
    if _looks_like_path_or_file(token):
        return style.fg256(117, token)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token):
        return style.fg256(141, token)
    return token


def _highlight_shell_assignment_value(value: str, style: _AnsiStyle) -> str:
    if not value:
        return ""
    if value.startswith(('"', "'")):
        return style.fg256(186, value)
    if value.startswith("$"):
        return style.fg256(141, value)
    if _looks_like_path_or_file(value):
        return style.fg256(117, value)
    return value


def _is_shell_assignment(token: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None


def _looks_like_path_or_file(token: str) -> bool:
    if "/" in token or token.startswith(("./", "../", "~/")):
        return True
    return re.search(r"\.[A-Za-z0-9_+-]{1,8}$", token) is not None


def _shell_heredoc_delimiter(line: str) -> str | None:
    match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_-]*)\1", line)
    if match is None:
        return None
    return match.group(2)


def _duration_ms(raw: Any) -> int | None:
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return int(seconds * 1000)


def _duration_suffix(raw: Any) -> str:
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    millis = int(seconds * 1000)
    return f" in {millis}ms"


def _int_value(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _resolve_resume_rollout(args: argparse.Namespace, config: VolleyConfig) -> Path | None:
    if args.last:
        return _latest_rollout(config, cwd=config.resolved_cwd(), all_cwds=args.all_cwds)
    selector = args.session_id
    if not selector:
        return None
    explicit_path = _resolve_explicit_rollout_path(selector, config.resolved_cwd())
    if explicit_path is not None:
        return explicit_path
    return _find_rollout_by_thread_id(config, selector, cwd=config.resolved_cwd(), all_cwds=args.all_cwds)


def _resolve_explicit_rollout_path(selector: str, cwd: Path) -> Path | None:
    candidates = [Path(selector).expanduser()]
    if not candidates[0].is_absolute():
        candidates.append(cwd / candidates[0])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def _latest_rollout(config: VolleyConfig, *, cwd: Path, all_cwds: bool) -> Path | None:
    for path in _iter_session_search_rollouts(config):
        reconstruction = _safe_reconstruct_rollout(path)
        if reconstruction is None:
            continue
        if all_cwds or _rollout_cwd_matches(reconstruction.session_meta, cwd):
            return path
    return None


def _find_rollout_by_thread_id(config: VolleyConfig, selector: str, *, cwd: Path, all_cwds: bool) -> Path | None:
    for path in _iter_session_search_rollouts(config):
        reconstruction = _safe_reconstruct_rollout(path)
        if reconstruction is None:
            continue
        if not all_cwds and not _rollout_cwd_matches(reconstruction.session_meta, cwd):
            continue
        thread_id = _rollout_thread_id(reconstruction.session_meta)
        if thread_id == selector or selector in path.name:
            return path
    return None


def _iter_session_search_rollouts(config: VolleyConfig) -> list[Path]:
    paths: list[Path] = []
    for home in _session_search_homes(config):
        paths.extend(_iter_rollout_paths(home))
    return sorted(paths, key=lambda path: (_safe_mtime(path), path.name), reverse=True)


def _session_search_homes(config: VolleyConfig) -> list[Path]:
    return _unique_paths([config.resolved_volley_home(), *_legacy_session_homes()])


def _iter_rollout_paths(volley_home: Path) -> list[Path]:
    sessions = volley_home / "sessions"
    if not sessions.exists():
        return []
    paths = [path for path in sessions.glob("????/??/??/rollout-*.jsonl") if path.is_file()]
    return sorted(paths, key=lambda path: (_safe_mtime(path), path.name), reverse=True)


def _safe_reconstruct_rollout(path: Path):
    try:
        return reconstruct_history_from_rollout(path)
    except Exception:
        return None


def _rollout_thread_id(session_meta: dict | None) -> str | None:
    if not isinstance(session_meta, dict):
        return None
    value = session_meta.get("id")
    return str(value) if value else None


def _rollout_cwd_matches(session_meta: dict | None, cwd: Path) -> bool:
    if not isinstance(session_meta, dict):
        return False
    raw_cwd = session_meta.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return False
    try:
        return Path(raw_cwd).expanduser().resolve() == cwd.resolve()
    except OSError:
        return False


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_prompt(prompt_arg: str | None) -> str:
    stdin_text = ""
    if prompt_arg == "-" or not sys.stdin.isatty():
        stdin_text = sys.stdin.read()
    if prompt_arg and prompt_arg != "-":
        if stdin_text:
            return f"{prompt_arg}\n\n<stdin>\n{stdin_text}\n</stdin>"
        return prompt_arg
    return stdin_text


def _load_output_schema(path: str | None) -> dict | None:
    if path is None:
        return None
    schema_path = Path(path)
    try:
        value = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Failed to read output schema file {schema_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Output schema file {schema_path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Output schema file {schema_path} must contain a JSON object")
    return value


def _parse_image_args(values: list[str]) -> list[str]:
    paths: list[str] = []
    for value in values:
        paths.extend(part for part in value.split(",") if part)
    return paths


def _resolve_oss_provider(args: argparse.Namespace, config: dict) -> str | None:
    if not args.oss:
        return None
    provider = args.local_provider or _string_config(config, "oss_provider")
    if not provider:
        raise ValueError(
            "No default OSS provider configured. Use --local-provider=provider or set "
            "oss_provider to one of: lmstudio, ollama in config.toml"
        )
    if provider not in {"lmstudio", "ollama"}:
        raise ValueError(f"Invalid OSS provider `{provider}`; expected one of: lmstudio, ollama")
    return provider


def _exec_model(args: argparse.Namespace, config: dict, oss_provider: str | None) -> str:
    if args.model:
        return args.model
    if oss_provider:
        return _default_oss_model(oss_provider)
    return _string_config(config, "model") or VolleyConfig().model


def _default_oss_model(provider: str) -> str:
    if provider == "lmstudio":
        return "openai/gpt-oss-20b"
    return "gpt-oss:20b"


def _load_cli_config(args: argparse.Namespace) -> dict:
    config: dict = {}
    if not args.ignore_user_config:
        path = _default_config_path()
        if path.exists():
            try:
                config = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"Error loading config.toml: {exc}") from exc
    for override in args.config_overrides:
        key, value = _parse_config_override(override)
        _apply_dotted_config(config, key, value)
    profile_name = args.profile or _string_config(config, "profile")
    if not profile_name:
        return _without_profiles(config)
    profiles = config.get("profiles", {})
    if not isinstance(profiles, dict) or profile_name not in profiles or not isinstance(profiles[profile_name], dict):
        raise ValueError(f"Config profile `{profile_name}` was not found in config.toml")
    return _deep_merge(_without_profiles(config), profiles[profile_name])


def _default_volley_home() -> Path:
    return Path(os.environ.get("VOLLEY_HOME") or os.environ.get("VOLLEY_PY_HOME", "~/.volley-python")).expanduser()


def _legacy_session_homes() -> list[Path]:
    homes: list[Path] = []
    for value in (
        os.environ.get("CODEX_PY_HOME"),
        os.environ.get("CODEX_HOME"),
    ):
        if value:
            homes.append(Path(value).expanduser())
    homes.extend(
        [
            Path("~/.codex-python").expanduser(),
            Path("~/.codex").expanduser(),
        ]
    )
    return homes


def _unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def _default_config_path() -> Path:
    volley_home = os.environ.get("VOLLEY_HOME")
    if volley_home:
        return Path(volley_home).expanduser() / "config.toml"
    official = Path("~/.volley/config.toml").expanduser()
    if official.exists():
        return official
    return _default_volley_home() / "config.toml"


def _local_account_plan_type(auth_home: str | Path | None = None) -> str | None:
    try:
        from .auth import auth_status

        status = auth_status(auth_home)
    except Exception:
        return None
    plan = status.get("plan_type") if isinstance(status, dict) else None
    return plan if isinstance(plan, str) and plan else None


def _configure_cli_syntax_theme(config: dict) -> None:
    configured = _string_nested_config(config, ("tui", "theme"))
    if configured:
        _set_cli_syntax_theme(configured)


def _parse_config_override(raw: str) -> tuple[str, object]:
    key, separator, value = raw.partition("=")
    key = key.strip()
    if not separator or not key:
        raise ValueError(f"Invalid -c/--config override `{raw}`; expected key=value")
    value = value.strip()
    try:
        parsed = tomllib.loads(f"value = {value}")["value"]
    except tomllib.TOMLDecodeError:
        parsed = value.strip("\"'")
    return key, parsed


def _apply_dotted_config(config: dict, key: str, value: object) -> None:
    parts = [part for part in key.split(".") if part]
    if not parts:
        raise ValueError(f"Invalid empty config override key `{key}`")
    cursor = config
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


def _without_profiles(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != "profiles"}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _string_config(config: dict, key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _bool_config(config: dict, key: str, default: bool) -> bool:
    value = config.get(key)
    return value if isinstance(value, bool) else default


def _remote_compaction_config(config: dict) -> str:
    value = (_string_config(config, "remote_compaction") or os.environ.get("PY_VOLLEY_REMOTE_COMPACTION") or "auto").lower()
    if value not in {"auto", "off", "required"}:
        raise ValueError("remote_compaction must be one of: auto, off, required")
    return value


def _auth_mode_config(args: argparse.Namespace, config: dict) -> str:
    from .auth import normalize_auth_mode

    cli_value = getattr(args, "auth_mode", None)
    if cli_value:
        return normalize_auth_mode(cli_value)
    forced = _string_config(config, "forced_login_method")
    if forced:
        key = forced.replace("-", "_").lower()
        if key in {"chatgpt", "chat"}:
            return "chatgpt"
        if key in {"api", "api_key", "apikey"}:
            return "api_key"
    configured = _string_config(config, "auth_mode")
    return normalize_auth_mode(configured)


def _bool_nested_config(config: dict, path: tuple[str, ...], default: bool) -> bool:
    value: object = config
    for part in path:
        if not isinstance(value, dict):
            return default
        value = value.get(part)
    return value if isinstance(value, bool) else default


def _string_nested_config(config: dict, path: tuple[str, ...]) -> str | None:
    value: object = config
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, str) and value else None


def _int_nested_config(config: dict, path: tuple[str, ...], default: int) -> int:
    value: object = config
    for part in path:
        if not isinstance(value, dict):
            return default
        value = value.get(part)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _int_config(config: dict, key: str) -> int | None:
    value = config.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _terminal_resize_reflow_max_rows_config(config: dict) -> int | None:
    nested = _int_nested_config(config, ("tui", "terminal_resize_reflow_max_rows"), -1)
    if nested >= 0:
        return nested
    top_level = _int_config(config, "terminal_resize_reflow_max_rows")
    if top_level is not None and top_level >= 0:
        return top_level
    return None


def _model_provider_config(config: dict, provider_id: str) -> dict:
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(provider_id)
    return provider if isinstance(provider, dict) else {}


def _path_list_config(config: dict, key: str) -> list[str]:
    value = config.get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _sandbox_config(config: dict) -> str | None:
    value = _string_config(config, "sandbox_mode")
    return value if value in {"read-only", "workspace-write", "danger-full-access"} else None


def _approval_config(config: dict) -> str | None:
    value = _string_config(config, "approval_policy")
    return value if value in {"untrusted", "on-failure", "on-request", "never"} else None


def _collaboration_mode_config(config: dict) -> str:
    value = _string_config(config, "collaboration_mode") or _string_config(config, "mode")
    normalized = (value or "Default").replace("_", " ").replace("-", " ").strip().lower()
    modes = {
        "default": "Default",
        "plan": "Plan",
        "execute": "Execute",
        "pair programming": "Pair Programming",
        "pair": "Pair Programming",
    }
    return modes.get(normalized, "Default")


def _request_user_input_available_modes(config: dict) -> tuple[str, ...]:
    value = config.get("request_user_input_available_modes")
    if isinstance(value, list):
        modes = tuple(
            mode
            for raw in value
            if isinstance(raw, str)
            for mode in [_collaboration_mode_config({"collaboration_mode": raw})]
        )
        if modes:
            return modes
    if _bool_nested_config(config, ("features", "default_mode_request_user_input"), False):
        return ("Default", "Plan")
    return ("Plan",)


def _web_search_settings(config: dict) -> tuple[bool, bool]:
    value = config.get("web_search")
    if value in {False, "disabled"}:
        return (False, False)
    if value in {True, "live"}:
        return (True, True)
    return (True, False)


if __name__ == "__main__":
    raise SystemExit(main())
