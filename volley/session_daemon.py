from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from dataclasses import dataclass
from dataclasses import fields
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import SteerInputError, VolleySession
from .types import VolleyConfig, VolleyEvent


LIVE_DIR_NAME = "live"
EVENT_LOG_MAX_ITEMS = 20_000
STARTED_WORKER_PROCESSES: list[subprocess.Popen[Any]] = []
CONFIG_WIRE_OMIT_FIELDS = frozenset(
    {
        "approval_provider",
        "hook_provider",
        "request_user_input_provider",
        "memory_state_store",
        "memory_rate_limit_provider",
    }
)
CONFIG_WIRE_TUPLE_FIELDS = frozenset(
    {
        "writable_roots",
        "request_user_input_available_modes",
        "web_search_content_types",
        "input_images",
    }
)


@dataclass(frozen=True)
class LiveSessionInfo:
    thread_id: str
    pid: int
    socket_path: Path
    registry_path: Path
    rollout_path: Path
    cwd: Path
    log_path: Path
    updated_at: float
    running: bool = False


class PersistentSessionUnavailable(RuntimeError):
    pass


def live_sessions_dir(volley_home: Path | str) -> Path:
    return Path(volley_home).expanduser().resolve() / LIVE_DIR_NAME


def live_socket_path(thread_id: str) -> Path:
    try:
        user = str(os.getuid())
    except AttributeError:  # pragma: no cover - Unix-only feature.
        user = "user"
    root = Path("/tmp") if Path("/tmp").exists() else Path(tempfile.gettempdir())
    socket_dir = root / f"volley-{user}"
    socket_dir.mkdir(parents=True, exist_ok=True)
    return socket_dir / f"{thread_id[:18]}.sock"


def config_to_wire(config: VolleyConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in fields(VolleyConfig):
        if field.name in CONFIG_WIRE_OMIT_FIELDS:
            continue
        payload[field.name] = _wire_value(getattr(config, field.name))
    return payload


def config_from_wire(payload: Any) -> VolleyConfig:
    if not isinstance(payload, dict):
        raise ValueError("worker config payload must be an object")
    kwargs: dict[str, Any] = {}
    valid_fields = {field.name for field in fields(VolleyConfig)}
    for key, value in payload.items():
        if key not in valid_fields or key in CONFIG_WIRE_OMIT_FIELDS:
            continue
        restored = _unwire_value(value)
        if key in CONFIG_WIRE_TUPLE_FIELDS and isinstance(restored, list):
            restored = tuple(restored)
        kwargs[key] = restored
    return VolleyConfig(**kwargs)


def _wire_value(value: Any) -> Any:
    if isinstance(value, Path):
        return {"__path__": str(value)}
    if isinstance(value, tuple):
        return [_wire_value(item) for item in value]
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _unwire_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__path__"}:
            return str(value["__path__"])
        return {key: _unwire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwire_value(item) for item in value]
    return value


def _write_worker_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def start_persistent_session_worker(
    session: VolleySession,
    *,
    resume_rollout_path: Path | str | None = None,
) -> LiveSessionInfo:
    """Start a detached worker process that owns this VolleySession.

    The worker keeps model/tool state in memory and exposes a local Unix socket.
    The parent process can die without killing the worker. The worker is a fresh
    Python interpreter instead of a post-fork child; macOS crashes forked Python
    children when Objective-C runtime initialization is in progress.
    """

    if sys.platform == "win32":
        raise PersistentSessionUnavailable("persistent interactive sessions require Unix domain sockets")

    thread_id = session.state.thread_id
    live_dir = live_sessions_dir(session.config.resolved_volley_home())
    live_dir.mkdir(parents=True, exist_ok=True)
    socket_path = live_socket_path(thread_id)
    registry_path = live_dir / f"{thread_id}.json"
    log_path = live_dir / f"{thread_id}.log"
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    rollout_path = session.state.rollout_path()
    cwd = session.config.resolved_cwd()
    payload_path = live_dir / f"{thread_id}.worker.json"
    _write_worker_payload(
        payload_path,
        {
            "config": config_to_wire(session.config),
            "thread_id": thread_id,
            "socket_path": str(socket_path),
            "registry_path": str(registry_path),
            "log_path": str(log_path),
            "resume_rollout_path": str(resume_rollout_path) if resume_rollout_path is not None else None,
        },
    )
    module_name = (__package__ or "volley").split(".", 1)[0]
    argv = [sys.executable, "-m", module_name, "__persistent-worker", str(payload_path)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=Path.cwd(),
            start_new_session=(sys.platform != "win32"),
            close_fds=True,
        )
    STARTED_WORKER_PROCESSES.append(process)

    info = LiveSessionInfo(
        thread_id=thread_id,
        pid=process.pid,
        socket_path=socket_path,
        registry_path=registry_path,
        rollout_path=rollout_path,
        cwd=cwd,
        log_path=log_path,
        updated_at=time.time(),
        running=False,
    )
    _wait_for_socket(socket_path, process.pid, registry_path=registry_path, log_path=log_path)
    return info


def run_persistent_session_worker(payload_path: Path | str) -> int:
    payload_file = Path(payload_path)
    try:
        payload = json.loads(payload_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be an object")
        config = config_from_wire(payload.get("config"))
        thread_id = str(payload["thread_id"])
        socket_path = Path(str(payload["socket_path"]))
        registry_path = Path(str(payload["registry_path"]))
        log_path = Path(str(payload["log_path"]))
        resume_rollout_path = payload.get("resume_rollout_path")
        if resume_rollout_path:
            session = VolleySession.resume_from_rollout(str(resume_rollout_path), config)
        else:
            session = VolleySession(config)
            session.state.thread_id = thread_id
        worker = PersistentSessionWorker(
            session,
            socket_path=socket_path,
            registry_path=registry_path,
            log_path=log_path,
        )
        worker.run()
        return 0
    except BaseException as exc:
        try:
            with payload_file.with_suffix(".crash.log").open("a", encoding="utf-8") as handle:
                handle.write(f"worker crashed: {type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        print(f"worker crashed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


def list_live_sessions(volley_home: Path | str, *, include_idle: bool = False) -> list[LiveSessionInfo]:
    live_dir = live_sessions_dir(volley_home)
    if not live_dir.exists():
        return []
    out: list[LiveSessionInfo] = []
    for path in sorted(live_dir.glob("*.json"), key=lambda item: _safe_mtime(item), reverse=True):
        info = _read_live_session_info(path)
        if info is None:
            continue
        if not _pid_is_alive(info.pid) or not info.socket_path.exists():
            _cleanup_live_session_files(info)
            continue
        if not include_idle and not info.running:
            continue
        out.append(info)
    return out


def resolve_live_session(
    volley_home: Path | str,
    selector: str | None = None,
    *,
    include_idle: bool = False,
) -> LiveSessionInfo | None:
    sessions = list_live_sessions(volley_home, include_idle=include_idle)
    if not sessions:
        return None
    if not selector:
        return sessions[0]
    for info in sessions:
        if info.thread_id == selector or selector in info.thread_id:
            return info
    return None


class PersistentSessionClient:
    def __init__(self, socket_path: Path | str):
        self.socket_path = Path(socket_path)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.messages: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.thread_id = ""
        self.rollout_path = ""
        self.control = False
        self.running = False
        self.closed = threading.Event()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        self._sock = sock
        thread = threading.Thread(target=self._read_loop, daemon=True)
        thread.start()

    def send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        sock = self._sock
        if sock is None:
            raise BrokenPipeError("persistent session client is not connected")
        with self._send_lock:
            sock.sendall(data)

    def close(self) -> None:
        self.closed.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buffer = b""
        try:
            while not self.closed.is_set():
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw:
                        continue
                    try:
                        message = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        self._record_client_state(message)
                        self.messages.put(message)
        finally:
            self.closed.set()

    def _record_client_state(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "hello":
            self.thread_id = str(message.get("thread_id") or "")
            self.rollout_path = str(message.get("rollout_path") or "")
            self.control = bool(message.get("control"))
            self.running = bool(message.get("running"))
            return
        if kind == "control":
            self.control = bool(message.get("control"))
            return
        if kind == "state":
            self.running = bool(message.get("running"))


class PersistentSessionWorker:
    def __init__(
        self,
        session: VolleySession,
        *,
        socket_path: Path,
        registry_path: Path,
        log_path: Path,
    ) -> None:
        self.session = session
        self.socket_path = socket_path
        self.registry_path = registry_path
        self.log_path = log_path
        self._stop = threading.Event()
        self._event_cond = threading.Condition()
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._next_seq = 1
        self._clients_lock = threading.Lock()
        self._client_count = 0
        self._client_senders: dict[str, tuple[socket.socket, threading.Lock]] = {}
        self._turn_lock = threading.RLock()
        self._turn_running = False
        self._interrupt_requested = False
        self._queued_prompts: "queue.Queue[str]" = queue.Queue()
        self._request_input_cond = threading.Condition()
        self._request_input_answers: dict[str, Any] = {}
        self._pending_request_input_questions: dict[str, list[dict[str, Any]]] = {}
        self._install_request_user_input_provider()

    def run(self) -> None:
        self._prepare_process()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(8)
        server.settimeout(0.2)
        atexit.register(self._cleanup)
        self._write_registry()
        self._emit_state()
        try:
            while not self._stop.is_set():
                try:
                    conn, _addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                thread.start()
        finally:
            try:
                server.close()
            except OSError:
                pass
            self._cleanup()

    def _prepare_process(self) -> None:
        try:
            os.setsid()
        except OSError:
            pass
        for sig in ("SIGHUP", "SIGPIPE"):
            value = getattr(signal, sig, None)
            if value is None:
                continue
            try:
                signal.signal(value, signal.SIG_IGN)
            except Exception:
                pass

        def stop_handler(_signum: int, _frame: Any) -> None:
            self.shutdown()

        for sig in ("SIGTERM", "SIGINT"):
            value = getattr(signal, sig, None)
            if value is None:
                continue
            try:
                signal.signal(value, stop_handler)
            except Exception:
                pass

    def _install_request_user_input_provider(self) -> None:
        config = replace(self.session.config, request_user_input_provider=self._request_user_input)
        self.session.config = config
        self.session.state.config = config
        self.session.tools.config = config

    def _request_user_input(self, questions: list[dict[str, Any]]) -> dict[str, Any] | None:
        request_id = str(uuid.uuid4())
        with self._request_input_cond:
            self._pending_request_input_questions[request_id] = questions
        self._emit_daemon_event(
            "daemon.request_user_input",
            request_id=request_id,
            questions=questions,
        )
        with self._request_input_cond:
            while not self._stop.is_set():
                if request_id in self._request_input_answers:
                    answer = self._request_input_answers.pop(request_id)
                    self._pending_request_input_questions.pop(request_id, None)
                    return answer if isinstance(answer, dict) else None
                self._request_input_cond.wait(timeout=0.5)
            self._pending_request_input_questions.pop(request_id, None)
        return None

    def _handle_client(self, conn: socket.socket) -> None:
        client_id = str(uuid.uuid4())
        send_lock = threading.Lock()
        control = self._register_client(client_id, conn, send_lock)
        writer_stop = threading.Event()
        with self._event_cond:
            replay_until_seq = self._next_seq - 1
        self._send_json(
            conn,
            {
                "type": "hello",
                "thread_id": self.session.state.thread_id,
                "rollout_path": str(self.session.state.rollout_path()),
                "control": control,
                "running": self._turn_running,
                "replay_until_seq": replay_until_seq,
                "status": self._status_payload(),
            },
            send_lock=send_lock,
        )
        writer = threading.Thread(
            target=self._client_event_writer,
            args=(conn, send_lock, writer_stop),
            daemon=True,
        )
        writer.start()
        buffer = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        self._handle_command(
                            payload,
                            client_id=client_id,
                            conn=conn,
                            send_lock=send_lock,
                        )
        except OSError:
            pass
        finally:
            writer_stop.set()
            self._unregister_client(client_id)
            try:
                conn.close()
            except OSError:
                pass
            self._shutdown_if_idle_without_clients()

    def _client_event_writer(
        self,
        conn: socket.socket,
        send_lock: threading.Lock,
        stop: threading.Event,
    ) -> None:
        next_seq = 1
        while not stop.is_set() and not self._stop.is_set():
            with self._event_cond:
                available = [(seq, event) for seq, event in self._events if seq >= next_seq]
                if not available:
                    self._event_cond.wait(timeout=0.5)
                    continue
            for seq, event in available:
                if stop.is_set():
                    return
                if not self._should_send_message_to_client(event):
                    next_seq = seq + 1
                    continue
                ok = self._send_json(conn, {"seq": seq, **event}, send_lock=send_lock)
                if not ok:
                    stop.set()
                    return
                next_seq = seq + 1

    def _handle_command(
        self,
        payload: dict[str, Any],
        *,
        client_id: str,
        conn: socket.socket,
        send_lock: threading.Lock,
    ) -> None:
        command = str(payload.get("type") or "")
        if command == "detach":
            return
        if command == "shutdown":
            self.shutdown()
            return
        if command == "interrupt":
            self._request_interrupt()
            return
        if command == "request_user_input_response":
            request_id = str(payload.get("request_id") or "")
            with self._request_input_cond:
                if request_id in self._pending_request_input_questions:
                    self._request_input_answers[request_id] = payload.get("answer")
                    self._request_input_cond.notify_all()
            return
        if command == "submit":
            text = str(payload.get("text") or "")
            self.submit(text)
            return
        if command == "slash":
            self.handle_slash(str(payload.get("text") or ""))

    def submit(self, text: str) -> None:
        prompt = text.strip()
        if not prompt:
            return
        with self._turn_lock:
            if self._turn_running:
                if self._interrupt_requested:
                    self._queued_prompts.put(text)
                    self._emit_daemon_event("daemon.pending_input", text=text, active=False)
                    return
                try:
                    self.session.steer_input(text)
                    self._emit_daemon_event("daemon.pending_input", text=text, active=True)
                except SteerInputError:
                    self._queued_prompts.put(text)
                    self._emit_daemon_event("daemon.pending_input", text=text, active=False)
                return
            self._start_turn_locked(text)

    def handle_slash(self, text: str) -> None:
        command, _, rest = text.lstrip()[1:].partition(" ")
        command = command.lower()
        if command in {"quit", "exit", "shutdown"}:
            self.shutdown()
            return
        if command == "interrupt":
            self._request_interrupt()
            return
        if command == "rollout":
            self._emit_daemon_event("daemon.notice", message=f"Current rollout path: {self.session.state.rollout_path()}")
            return
        if command == "status":
            status = "running" if self._turn_running else "idle"
            self._emit_daemon_event(
                "daemon.notice",
                message=(
                    f"Persistent session {self.session.state.thread_id} is {status}. "
                    f"Rollout: {self.session.state.rollout_path()}"
                ),
            )
            return
        if command == "stop":
            interrupt_all = getattr(self.session.tools, "interrupt_all", None)
            if callable(interrupt_all):
                interrupt_all()
            self._emit_daemon_event("daemon.notice", message="Stopped background terminals.")
            return
        if command == "compact":
            with self._turn_lock:
                if self._turn_running:
                    self._queued_prompts.put(text)
                    self._emit_daemon_event("daemon.notice", message="/compact queued until the active turn finishes.")
                    return
                self._start_compact_locked(rest.strip() or None)
            return
        self._emit_daemon_event(
            "daemon.notice",
            message=f"Command '/{command}' is not available in persistent session mode yet.",
        )

    def _start_turn_locked(self, prompt: str) -> None:
        self._turn_running = True
        self._interrupt_requested = False
        self._write_registry()
        self._emit_state()
        self._emit_daemon_event("daemon.user_message", text=prompt)
        thread = threading.Thread(target=self._run_turn, args=(prompt,), daemon=True)
        thread.start()

    def _start_compact_locked(self, prompt: str | None) -> None:
        self._turn_running = True
        self._write_registry()
        self._emit_state()
        thread = threading.Thread(target=self._run_compact, args=(prompt,), daemon=True)
        thread.start()

    def _run_turn(self, prompt: str) -> None:
        try:
            for event in self.session.stream(prompt):
                self._emit_event(event)
        except Exception as exc:
            self._emit_daemon_event("daemon.error", message=f"{type(exc).__name__}: {exc}")
        finally:
            self._finish_turn_and_maybe_continue()

    def _run_compact(self, prompt: str | None) -> None:
        try:
            for event in self.session.stream_compact(prompt):
                self._emit_event(event)
        except Exception as exc:
            self._emit_daemon_event("daemon.error", message=f"{type(exc).__name__}: {exc}")
        finally:
            self._finish_turn_and_maybe_continue()

    def _finish_turn_and_maybe_continue(self) -> None:
        should_check_idle = False
        with self._turn_lock:
            self._turn_running = False
            self._interrupt_requested = False
            self._write_registry()
            self._emit_state()
            try:
                next_prompt = self._queued_prompts.get_nowait()
            except queue.Empty:
                should_check_idle = True
                next_prompt = None
            if next_prompt is None:
                pass
            elif next_prompt.lstrip().startswith("/compact"):
                _, _, rest = next_prompt.lstrip()[1:].partition(" ")
                self._start_compact_locked(rest.strip() or None)
                return
            else:
                self._start_turn_locked(next_prompt)
                return
        if should_check_idle:
            self._shutdown_if_idle_without_clients()

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self.session.interrupt()
        except Exception:
            pass
        interrupt_all = getattr(self.session.tools, "interrupt_all", None)
        if callable(interrupt_all):
            try:
                interrupt_all()
            except Exception:
                pass
        with self._request_input_cond:
            self._request_input_cond.notify_all()
        with self._event_cond:
            self._event_cond.notify_all()
        self._emit_daemon_event("daemon.shutdown", message="Persistent session worker stopped.")

    def _request_interrupt(self) -> None:
        with self._turn_lock:
            self._interrupt_requested = True
        self.session.interrupt()

    def _shutdown_if_idle_without_clients(self) -> None:
        with self._turn_lock:
            if self._turn_running or not self._queued_prompts.empty():
                return
        with self._clients_lock:
            if self._client_count > 0:
                return
        self.shutdown()

    def _emit_event(self, event: VolleyEvent) -> None:
        self._append_event(event.to_dict())
        if event.type in {"token_count", "thread.goal.updated", "thread.goal.cleared"}:
            self._emit_state()

    def _emit_state(self) -> None:
        self._append_message({"type": "state", "running": self._turn_running, "status": self._status_payload()})

    def _status_payload(self) -> dict[str, Any]:
        active_context: int | None = None
        active_context_estimated = True
        session_context: int | None = None
        session_context_estimated = True
        session_reasoning: int | None = None
        context_window: int | None = None
        try:
            active_context, active_context_estimated = self.session.state.active_context_token_status()
        except Exception:
            pass
        try:
            session_context, session_context_estimated = self.session.state.session_context_token_status()
        except Exception:
            pass
        try:
            session_reasoning = self.session.state.session_reasoning_usage_tokens()
        except Exception:
            pass
        try:
            context_window = self.session.config.resolved_model_context_window()
        except Exception:
            pass
        return {
            "auth_label": self._auth_label(),
            "fast_status": self._fast_status(),
            "goal_status": self._goal_status(),
            "active_context_tokens": active_context,
            "active_context_estimated": active_context_estimated,
            "session_context_tokens": session_context,
            "session_context_estimated": session_context_estimated,
            "session_reasoning_tokens": session_reasoning,
            "context_window": context_window,
        }

    def _auth_label(self) -> str | None:
        client = getattr(self.session, "model_client", None)
        active = getattr(client, "auth_display_name", None)
        if isinstance(active, str) and active:
            return active
        type_name = type(client).__name__ if client is not None else ""
        if type_name == "ScriptedResponsesModel":
            return "fake model"
        if type_name == "ChatGPTCodexSubscriptionModel":
            return "ChatGPT"
        if type_name == "OpenAIResponsesModel":
            return "API key"
        if type_name == "GeminiGenerateContentModel":
            return "Gemini API key"
        return None

    def _fast_status(self) -> str | None:
        try:
            service_tier = self.session.config.resolved_service_tier()
            if service_tier == "priority" and self.session.config.resolved_model_supports_fast_mode():
                return "Fast on"
            if service_tier:
                return str(service_tier)
        except Exception:
            pass
        return None

    def _goal_status(self) -> str | None:
        runtime = getattr(self.session, "goals", None)
        get_goal = getattr(runtime, "get_goal", None)
        if not callable(get_goal):
            return None
        try:
            goal = get_goal()
        except Exception:
            return None
        if goal is None:
            return None
        status = str(getattr(goal, "status", "") or "")
        if not status:
            return None
        return f"Goal {status.replace('_', ' ')}"

    def _emit_daemon_event(self, event_type: str, **payload: Any) -> None:
        self._append_event({"type": event_type, **payload})

    def _append_event(self, event: dict[str, Any]) -> None:
        self._append_message({"type": "event", "event": event})

    def _append_message(self, message: dict[str, Any]) -> None:
        with self._event_cond:
            seq = self._next_seq
            self._next_seq += 1
            self._events.append((seq, message))
            if len(self._events) > EVENT_LOG_MAX_ITEMS:
                self._events = self._events[-EVENT_LOG_MAX_ITEMS:]
            self._event_cond.notify_all()
        self._append_worker_event_log(seq, message)

    def _append_worker_event_log(self, seq: int, message: dict[str, Any]) -> None:
        try:
            path = self.registry_path.with_suffix(".events.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"seq": seq, **message}, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass

    def _register_client(
        self,
        client_id: str,
        conn: socket.socket,
        send_lock: threading.Lock,
    ) -> bool:
        with self._clients_lock:
            self._client_count += 1
            self._client_senders[client_id] = (conn, send_lock)
            return True

    def _unregister_client(self, client_id: str) -> None:
        with self._clients_lock:
            self._client_senders.pop(client_id, None)
            self._client_count = max(0, self._client_count - 1)

    def _send_json(
        self,
        conn: socket.socket,
        payload: dict[str, Any],
        *,
        send_lock: threading.Lock,
    ) -> bool:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        try:
            with send_lock:
                conn.sendall(data)
            return True
        except OSError:
            return False

    def _should_send_message_to_client(self, message: dict[str, Any]) -> bool:
        if message.get("type") != "event":
            return True
        event = message.get("event")
        if not isinstance(event, dict):
            return True
        if event.get("type") != "daemon.request_user_input":
            return True
        request_id = str(event.get("request_id") or "")
        with self._request_input_cond:
            return request_id in self._pending_request_input_questions

    def _write_registry(self) -> None:
        payload = {
            "thread_id": self.session.state.thread_id,
            "pid": os.getpid(),
            "socket_path": str(self.socket_path),
            "rollout_path": str(self.session.state.rollout_path()),
            "cwd": str(self.session.config.resolved_cwd()),
            "log_path": str(self.log_path),
            "updated_at": time.time(),
            "updated_at_iso": datetime.now().astimezone().isoformat(),
            "running": self._turn_running,
        }
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.registry_path)

    def _cleanup(self) -> None:
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            self.registry_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _detach_child_stdio(log_path: Path) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null_fd, 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
    finally:
        for fd in (log_fd, null_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _wait_for_socket(
    socket_path: Path,
    pid: int,
    *,
    registry_path: Path | None = None,
    log_path: Path | None = None,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists() and (registry_path is None or registry_path.exists()):
            return
        if not _pid_is_alive(pid):
            details = _tail_text(log_path) if log_path is not None else ""
            suffix = f": {details}" if details else ""
            raise PersistentSessionUnavailable(f"persistent session worker exited before opening {socket_path}{suffix}")
        time.sleep(0.02)
    details = _tail_text(log_path) if log_path is not None else ""
    suffix = f": {details}" if details else ""
    raise PersistentSessionUnavailable(f"timed out waiting for persistent session worker at {socket_path}{suffix}")


def _tail_text(path: Path | None, limit: int = 4000) -> str:
    if path is None:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def _read_live_session_info(path: Path) -> LiveSessionInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LiveSessionInfo(
            thread_id=str(data["thread_id"]),
            pid=int(data["pid"]),
            socket_path=Path(str(data["socket_path"])),
            registry_path=path,
            rollout_path=Path(str(data["rollout_path"])),
            cwd=Path(str(data["cwd"])),
            log_path=Path(str(data.get("log_path") or path.with_suffix(".log"))),
            updated_at=float(data.get("updated_at") or _safe_mtime(path)),
            running=bool(data.get("running")),
        )
    except Exception:
        return None


def _cleanup_live_session_files(info: LiveSessionInfo) -> None:
    for path in (info.socket_path, info.registry_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
