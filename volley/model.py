from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

from collections.abc import Sequence
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .auth import VolleyAuthSnapshot
from .auth import chatgpt_codex_subscription_base_url
from .auth import load_auth_snapshot
from .auth import normalize_auth_mode
from .auth import refresh_chatgpt_auth
from .types import ModelResponse, PromptRequest, _model_catalog_info
from .types import VolleyConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENAI_ENV_FILE = PROJECT_ROOT / "secrets" / "openai.env"
OPENAI_ORIGINATOR = "codex_cli_rs"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_ENV_FILES = tuple(
    base / "secrets" / name
    for base in (Path.cwd(), Path.cwd().parent, Path.cwd().parent.parent, PROJECT_ROOT, PROJECT_ROOT.parent)
    for name in ("google.env", "gemini.env")
)


@dataclass(frozen=True)
class ModelStreamEvent:
    type: str
    payload: dict[str, Any]


class ModelClient(Protocol):
    def create(self, request: PromptRequest) -> ModelResponse:
        """Create one model response."""

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        """Stream model response lifecycle events."""


class RemoteCompactionError(RuntimeError):
    """Raised when the provider compact endpoint cannot return replacement history."""


class OpenAIResponsesModel:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or load_openai_api_key()
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        self.auth_display_name = "API key" if self.api_key else "OpenAI SDK auth"
        self._active_responses: list[tuple[str | None, Any]] = []
        self._active_responses_lock = threading.Lock()

    def cancel(self, thread_id: str | None = None) -> None:
        self._close_active_responses(thread_id)

    def create(self, request: PromptRequest) -> ModelResponse:
        return collect_model_stream_events(self.stream(request))

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        try:
            from openai import OpenAI
        except ImportError:
            yield from self._stream_via_http(request)
            return

        client = OpenAI(api_key=self.api_key) if self.api_key else OpenAI()
        kwargs = request.to_responses_kwargs()
        extra_body = {}
        client_metadata = kwargs.pop("client_metadata", None)
        if client_metadata is not None:
            extra_body["client_metadata"] = client_metadata
        if extra_body:
            kwargs["extra_body"] = extra_body
        extra_headers = _responses_headers(request)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        response = client.responses.create(**kwargs)
        if request.stream:
            self._register_active_response(response, request.thread_id)
            try:
                yield from iter_model_stream_events(response)
            finally:
                self._unregister_active_response(response)
                _close_response(response)
            return
        data = _model_dump(response)
        yield from _scripted_stream_events(data)

    def _stream_via_http(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        """Fallback path used when the `openai` package is not installed.

        Sends the same payload as the SDK to POST {base_url}/responses, parsing
        SSE for stream=True and a single JSON body otherwise.
        """
        body_dict = request.to_responses_kwargs()
        body_dict = {k: v for k, v in body_dict.items() if v is not None}
        body = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if request.stream else "application/json",
        }
        headers.update(_responses_headers(request))
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if os.environ.get("OPENAI_ORGANIZATION"):
            headers["OpenAI-Organization"] = os.environ["OPENAI_ORGANIZATION"]
        if os.environ.get("OPENAI_PROJECT"):
            headers["OpenAI-Project"] = os.environ["OPENAI_PROJECT"]
        url = f"{self.base_url.rstrip('/')}/responses"
        http_request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(http_request, timeout=600)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"responses request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"responses request failed: {exc.reason}") from exc

        if not request.stream:
            with response:
                payload = json.loads(response.read().decode("utf-8"))
            yield from _scripted_stream_events(payload)
            return

        self._register_active_response(response, request.thread_id)
        try:
            with response:
                yield from iter_model_stream_events(_iter_sse_events(response))
        finally:
            self._unregister_active_response(response)
            _close_response(response)

    def _register_active_response(self, response: Any, owner: str | None = None) -> None:
        with self._active_responses_lock:
            self._active_responses.append((owner, response))

    def _unregister_active_response(self, response: Any) -> None:
        with self._active_responses_lock:
            self._active_responses = [item for item in self._active_responses if item[1] is not response]

    def _close_active_responses(self, owner: str | None = None) -> None:
        with self._active_responses_lock:
            if owner is None:
                pending = list(self._active_responses)
                self._active_responses.clear()
            else:
                pending = [item for item in self._active_responses if item[0] == owner]
                self._active_responses = [item for item in self._active_responses if item[0] != owner]
        for _owner, response in pending:
            _close_response(response)

    def compact(
        self,
        request: PromptRequest,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        installation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._compact_payload(request)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._compact_headers(
            session_id=session_id,
            thread_id=thread_id,
            installation_id=installation_id,
        )
        http_request = urllib.request.Request(
            self._compact_url(),
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=120) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RemoteCompactionError(f"remote compact failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RemoteCompactionError(f"remote compact failed: {exc.reason}") from exc

        try:
            data = json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RemoteCompactionError("remote compact returned invalid JSON") from exc
        output = data.get("output") if isinstance(data, dict) else None
        if not isinstance(output, list):
            raise RemoteCompactionError("remote compact response did not include an output list")
        return [_model_dump(item) for item in output]

    def _compact_payload(self, request: PromptRequest) -> dict[str, Any]:
        # The API-key compact endpoint rejects service_tier today.
        return replace(request, service_tier=None).to_compact_payload()

    def _compact_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses/compact"

    def _compact_headers(
        self,
        *,
        session_id: str | None,
        thread_id: str | None,
        installation_id: str | None,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if os.environ.get("OPENAI_ORGANIZATION"):
            headers["OpenAI-Organization"] = os.environ["OPENAI_ORGANIZATION"]
        if os.environ.get("OPENAI_PROJECT"):
            headers["OpenAI-Project"] = os.environ["OPENAI_PROJECT"]
        if installation_id:
            headers["x-codex-installation-id"] = installation_id
        if session_id:
            headers["session_id"] = session_id
            headers["session-id"] = session_id
        if thread_id:
            headers["thread_id"] = thread_id
            headers["thread-id"] = thread_id
        return headers


class ChatGPTCodexSubscriptionModel:
    """Responses transport for OpenAI's current Codex subscription auth path.

    The current OpenAI subscription endpoint uses the same Responses request
    body but is hosted under chatgpt.com/backend-api/codex.
    """

    def __init__(
        self,
        *,
        auth_snapshot: VolleyAuthSnapshot | None = None,
        auth_home: Path | str | None = None,
        base_url: str | None = None,
    ):
        self.auth_home = Path(auth_home).expanduser().resolve() if auth_home is not None else None
        self.auth_snapshot = auth_snapshot or self._load_auth()
        self.base_url = chatgpt_codex_subscription_base_url(base_url)
        self.auth_display_name = "ChatGPT"
        self._active_responses: list[tuple[str | None, Any]] = []
        self._active_responses_lock = threading.Lock()

    def cancel(self, thread_id: str | None = None) -> None:
        self._close_active_responses(thread_id)

    def create(self, request: PromptRequest) -> ModelResponse:
        return collect_model_stream_events(self.stream(request))

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        yield from self._stream_via_http(request, allow_refresh=True)

    def compact(
        self,
        request: PromptRequest,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        installation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._compact_payload(request)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers(
            request,
            accept="application/json",
            session_id=session_id,
            thread_id=thread_id,
            installation_id=installation_id,
        )
        response_body = self._urlopen_bytes(
            self._compact_url(),
            body,
            headers,
            timeout=120,
            allow_refresh=True,
            error_type=RemoteCompactionError,
        )
        try:
            data = json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RemoteCompactionError("remote compact returned invalid JSON") from exc
        output = data.get("output") if isinstance(data, dict) else None
        if not isinstance(output, list):
            raise RemoteCompactionError("remote compact response did not include an output list")
        return [_model_dump(item) for item in output]

    def _compact_payload(self, request: PromptRequest) -> dict[str, Any]:
        return request.to_compact_payload()

    def _stream_via_http(self, request: PromptRequest, *, allow_refresh: bool) -> Iterable[ModelStreamEvent]:
        body_dict = request.to_responses_kwargs()
        body_dict = {k: v for k, v in body_dict.items() if v is not None}
        body = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers(
            request,
            accept="text/event-stream" if request.stream else "application/json",
        )
        http_request = urllib.request.Request(self._responses_url(), data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(http_request, timeout=600)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and allow_refresh and self._refresh_auth_for_retry():
                yield from self._stream_via_http(request, allow_refresh=False)
                return
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ChatGPT responses request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ChatGPT responses request failed: {exc.reason}") from exc

        if not request.stream:
            with response:
                payload = json.loads(response.read().decode("utf-8"))
            yield from _scripted_stream_events(payload)
            return

        self._register_active_response(response, request.thread_id)
        try:
            with response:
                yield from iter_model_stream_events(_iter_sse_events(response))
        finally:
            self._unregister_active_response(response)
            _close_response(response)

    def _register_active_response(self, response: Any, owner: str | None = None) -> None:
        with self._active_responses_lock:
            self._active_responses.append((owner, response))

    def _unregister_active_response(self, response: Any) -> None:
        with self._active_responses_lock:
            self._active_responses = [item for item in self._active_responses if item[1] is not response]

    def _close_active_responses(self, owner: str | None = None) -> None:
        with self._active_responses_lock:
            if owner is None:
                pending = list(self._active_responses)
                self._active_responses.clear()
            else:
                pending = [item for item in self._active_responses if item[0] == owner]
                self._active_responses = [item for item in self._active_responses if item[0] != owner]
        for _owner, response in pending:
            _close_response(response)

    def _urlopen_bytes(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
        allow_refresh: bool,
        error_type: type[Exception],
    ) -> bytes:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and allow_refresh and self._refresh_auth_for_retry():
                refreshed_headers = dict(headers)
                refreshed_headers["Authorization"] = f"Bearer {self.auth_snapshot.access_token}"
                return self._urlopen_bytes(
                    url,
                    body,
                    refreshed_headers,
                    timeout=timeout,
                    allow_refresh=False,
                    error_type=error_type,
                )
            detail = exc.read().decode("utf-8", errors="replace")
            raise error_type(f"ChatGPT compact request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise error_type(f"ChatGPT compact request failed: {exc.reason}") from exc

    def _headers(
        self,
        request: PromptRequest,
        *,
        accept: str,
        session_id: str | None = None,
        thread_id: str | None = None,
        installation_id: str | None = None,
    ) -> dict[str, str]:
        self._refresh_stale_auth()
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "Authorization": f"Bearer {self.auth_snapshot.access_token}",
            "originator": OPENAI_ORIGINATOR,
            "User-Agent": f"{OPENAI_ORIGINATOR}/python-volley",
            "version": "python-volley",
        }
        if self.auth_snapshot.account_id:
            headers["ChatGPT-Account-ID"] = self.auth_snapshot.account_id
        headers.update(_responses_headers(request, session_id=session_id, thread_id=thread_id))
        if installation_id:
            headers["x-codex-installation-id"] = installation_id
        return headers

    def _load_auth(self) -> VolleyAuthSnapshot:
        snapshot = load_auth_snapshot(self.auth_home, mode="chatgpt")
        if snapshot is None:
            raise RuntimeError("ChatGPT auth requested, but no ChatGPT auth snapshot was found")
        return snapshot

    def _refresh_stale_auth(self) -> None:
        if self.auth_snapshot.needs_proactive_refresh():
            try:
                self.auth_snapshot = refresh_chatgpt_auth(self.auth_snapshot)
            except RuntimeError:
                if self.auth_snapshot.access_token_expiration() is not None:
                    raise

    def _refresh_auth_for_retry(self) -> bool:
        try:
            self.auth_snapshot = refresh_chatgpt_auth(self.auth_snapshot)
            return True
        except RuntimeError:
            return False

    def _responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"

    def _compact_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses/compact"


class FallbackModelClient:
    """Use ChatGPT account auth first, then API key auth for quota/budget failures.

    The fallback is intentionally limited to request-start failures where no
    model output has been yielded yet. That keeps the retry behavior legible:
    the same request is re-issued once through the API-key transport instead of
    mixing two partial model streams in a single turn.
    """

    def __init__(
        self,
        *,
        primary: ModelClient,
        fallback: ModelClient,
        primary_label: str = "ChatGPT",
        fallback_label: str = "API key",
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_label = primary_label
        self.fallback_label = fallback_label
        self._using_fallback = False
        self._last_fallback_reason: str | None = None
        self._try_primary_next_request = False

    @property
    def auth_display_name(self) -> str:
        return self.fallback_label if self._using_fallback else self.primary_label

    @property
    def auth_fallback_display_name(self) -> str | None:
        return None if self._using_fallback else self.fallback_label

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    @property
    def last_fallback_reason(self) -> str | None:
        return self._last_fallback_reason

    def retry_primary_on_next_request(self) -> None:
        """Probe the primary transport once on the next model request.

        Volley can fall back from ChatGPT subscription auth to an API key when
        the account budget is exhausted. That limit can later reset. Rather
        than polling on every request, the session arms this at the beginning
        of a new user turn so the first real sampling request tries ChatGPT
        once and only stays on the API key if the limit is still active.
        """

        if self._using_fallback:
            self._try_primary_next_request = True

    def create(self, request: PromptRequest) -> ModelResponse:
        if self._should_retry_primary_once():
            try:
                response = self.primary.create(request)
            except Exception as exc:
                if not _looks_like_account_limit_error(exc):
                    raise
                self._last_fallback_reason = str(exc).strip() or type(exc).__name__
                return self.fallback.create(request)
            self._restore_primary()
            return response
        try:
            return self._active_client().create(request)
        except Exception as exc:
            if self._using_fallback or not _looks_like_account_limit_error(exc):
                raise
            self._switch_to_fallback(exc)
            return self.fallback.create(request)

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        if self._should_retry_primary_once():
            yielded = False
            restored = False
            try:
                for event in self.primary.stream(request):
                    if not yielded:
                        yielded = True
                        restored = True
                        self._restore_primary()
                        yield ModelStreamEvent(
                            "warning",
                            {
                                "message": (
                                    f"{self.primary_label} limit appears recovered; "
                                    f"switched back from {self.fallback_label}."
                                )
                            },
                        )
                    yield event
                if not yielded:
                    self._restore_primary()
                return
            except Exception as exc:
                if restored or yielded or not _looks_like_account_limit_error(exc):
                    raise
                self._last_fallback_reason = str(exc).strip() or type(exc).__name__
            yield from self.fallback.stream(request)
            return

        yielded = False
        try:
            for event in self._active_client().stream(request):
                yielded = True
                yield event
            return
        except Exception as exc:
            if self._using_fallback or yielded or not _looks_like_account_limit_error(exc):
                raise
            self._switch_to_fallback(exc)
        yield ModelStreamEvent(
            "warning",
            {"message": f"{self.primary_label} limit reached; switched to {self.fallback_label}."},
        )
        yield from self.fallback.stream(request)

    def compact(
        self,
        request: PromptRequest,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        installation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        compact = getattr(self._active_client(), "compact", None)
        if not callable(compact):
            raise RemoteCompactionError("active model transport does not support remote compaction")
        try:
            return compact(
                request,
                session_id=session_id,
                thread_id=thread_id,
                installation_id=installation_id,
            )
        except Exception as exc:
            if self._using_fallback or not _looks_like_account_limit_error(exc):
                raise
            self._switch_to_fallback(exc)
        fallback_compact = getattr(self.fallback, "compact", None)
        if not callable(fallback_compact):
            raise RemoteCompactionError("fallback model transport does not support remote compaction")
        return fallback_compact(
            request,
            session_id=session_id,
            thread_id=thread_id,
            installation_id=installation_id,
        )

    def _active_client(self) -> ModelClient:
        return self.fallback if self._using_fallback else self.primary

    def _switch_to_fallback(self, exc: Exception) -> None:
        self._using_fallback = True
        self._try_primary_next_request = False
        self._last_fallback_reason = str(exc).strip() or type(exc).__name__

    def _restore_primary(self) -> None:
        self._using_fallback = False
        self._try_primary_next_request = False
        self._last_fallback_reason = None

    def _should_retry_primary_once(self) -> bool:
        if not self._using_fallback or not self._try_primary_next_request:
            return False
        self._try_primary_next_request = False
        return True


class GeminiGenerateContentModel:
    """Native Gemini API transport backed by generateContent."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or load_gemini_api_key()
        self.base_url = (
            base_url
            or os.environ.get("GEMINI_BASE_URL")
            or os.environ.get("GOOGLE_GEMINI_BASE_URL")
            or DEFAULT_GEMINI_BASE_URL
        ).rstrip("/")
        self.auth_display_name = "Gemini API key" if self.api_key else "Gemini"
        self._active_responses: list[tuple[str | None, Any]] = []
        self._active_responses_lock = threading.Lock()

    def cancel(self, thread_id: str | None = None) -> None:
        self._close_active_responses(thread_id)

    def create(self, request: PromptRequest) -> ModelResponse:
        return collect_model_stream_events(self.stream(request))

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        body_dict = _gemini_request_body(request)
        body = json.dumps(body_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers(accept="text/event-stream" if request.stream else "application/json")
        url = self._url(request.model, stream=request.stream)
        http_request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(http_request, timeout=600)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc

        self._register_active_response(response, request.thread_id)
        try:
            with response:
                if request.stream:
                    yield from _iter_gemini_stream_events(_iter_sse_events(response))
                    return
                payload = json.loads(response.read().decode("utf-8"))
                yield from _gemini_response_stream_events(payload)
        finally:
            self._unregister_active_response(response)
            _close_response(response)

    def _headers(self, *, accept: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
        }
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

    def _url(self, model: str, *, stream: bool) -> str:
        escaped = urllib.parse.quote(model, safe="")
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self.base_url}/models/{escaped}:{method}"

    def _register_active_response(self, response: Any, owner: str | None = None) -> None:
        with self._active_responses_lock:
            self._active_responses.append((owner, response))

    def _unregister_active_response(self, response: Any) -> None:
        with self._active_responses_lock:
            self._active_responses = [item for item in self._active_responses if item[1] is not response]

    def _close_active_responses(self, owner: str | None = None) -> None:
        with self._active_responses_lock:
            if owner is None:
                pending = list(self._active_responses)
                self._active_responses.clear()
            else:
                pending = [item for item in self._active_responses if item[0] == owner]
                self._active_responses = [item for item in self._active_responses if item[0] != owner]
        for _owner, response in pending:
            _close_response(response)


def _gemini_request_body(request: PromptRequest) -> dict[str, Any]:
    system_parts: list[dict[str, Any]] = []
    if request.instructions:
        system_parts.append({"text": request.instructions})
    contents: list[dict[str, Any]] = []
    call_names_by_id: dict[str, str] = {}
    for item in request.input:
        converted = _gemini_content_items(item, call_names_by_id, system_parts)
        contents.extend(converted)
    body: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    tools = _gemini_function_declarations(request.tools)
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    generation_config = _gemini_generation_config(request)
    if generation_config:
        body["generationConfig"] = generation_config
    return body


def _gemini_content_items(
    item: dict[str, Any],
    call_names_by_id: dict[str, str],
    system_parts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    item_type = item.get("type")
    if item_type == "message":
        role = str(item.get("role") or "user")
        parts = _gemini_message_parts(item)
        if role in {"developer", "system"}:
            system_parts.extend(parts)
            return []
        return [{"role": "model" if role == "assistant" else "user", "parts": parts or [{"text": ""}]}]
    if item_type in {"function_call", "custom_tool_call"}:
        call_id = str(item.get("call_id") or item.get("id") or "")
        name = str(item.get("name") or "tool")
        call_names_by_id[call_id] = name
        args = _gemini_function_args(item)
        part: dict[str, Any] = {"functionCall": {"name": name, "args": args}}
        if call_id:
            part["functionCall"]["id"] = call_id
        signature = item.get("gemini_thought_signature")
        if isinstance(signature, str) and signature:
            part["thoughtSignature"] = signature
        return [{"role": "model", "parts": [part]}]
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = str(item.get("call_id") or "")
        name = call_names_by_id.get(call_id) or str(item.get("name") or "tool")
        response: dict[str, Any] = {"result": _gemini_json_safe(item.get("output", ""))}
        function_response = {"name": name, "response": response}
        if call_id:
            function_response["id"] = call_id
        return [{"role": "user", "parts": [{"functionResponse": function_response}]}]
    if item_type == "reasoning":
        text = _gemini_reasoning_text(item)
        return [{"role": "model", "parts": [{"text": text}]}] if text else []
    if item_type == "web_search_call":
        action = item.get("action")
        if isinstance(action, dict):
            return [{"role": "model", "parts": [{"text": f"[web_search_call] {json.dumps(action, ensure_ascii=False)}"}]}]
    return []


def _gemini_message_parts(item: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in item.get("content", []):
        if not isinstance(part, dict):
            if part is not None:
                parts.append({"text": str(part)})
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
            continue
        if part_type == "input_image":
            image_part = _gemini_image_part(part)
            if image_part is not None:
                parts.append(image_part)
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append({"text": text})
    return parts


def _gemini_image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    image_url = part.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith("data:"):
        return None
    header, separator, data = image_url.partition(",")
    if not separator:
        return None
    mime_type = header[5:].split(";", 1)[0] or "image/png"
    return {"inlineData": {"mimeType": mime_type, "data": data}}


def _gemini_function_args(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") == "custom_tool_call":
        return {"patch": str(item.get("input") or "")}
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"input": arguments}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    return {}


def _gemini_function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type == "function":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            declaration: dict[str, Any] = {"name": name}
            description = tool.get("description")
            if isinstance(description, str) and description:
                declaration["description"] = description
            parameters = _gemini_schema(tool.get("parameters") if isinstance(tool.get("parameters"), dict) else None)
            if parameters:
                declaration["parameters"] = parameters
            declarations.append(declaration)
        elif tool_type == "custom" and tool.get("name") == "apply_patch":
            declarations.append(
                {
                    "name": "apply_patch",
                    "description": "Apply a source-code patch in the current workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patch": {"type": "string", "description": "Patch text to apply."},
                            "workdir": {"type": "string", "description": "Optional working directory."},
                        },
                        "required": ["patch"],
                    },
                }
            )
    return declarations


def _gemini_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    allowed = {"type", "description", "enum", "properties", "required", "items", "format", "nullable"}
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "type":
            if isinstance(value, list):
                non_null = [item for item in value if item != "null"]
                if non_null:
                    out["type"] = str(non_null[0])
                if "null" in value:
                    out["nullable"] = True
            elif isinstance(value, str):
                out["type"] = value
        elif key == "properties" and isinstance(value, dict):
            properties: dict[str, Any] = {}
            for prop_name, prop_schema in value.items():
                if isinstance(prop_name, str):
                    converted = _gemini_schema(prop_schema)
                    if converted:
                        properties[prop_name] = converted
            if properties:
                out["properties"] = properties
        elif key == "items":
            converted = _gemini_schema(value)
            if converted:
                out["items"] = converted
        elif key == "required" and isinstance(value, list):
            out["required"] = [str(item) for item in value if isinstance(item, str)]
        elif key == "enum" and isinstance(value, list):
            out["enum"] = [item for item in value if isinstance(item, (str, int, float, bool))]
        elif isinstance(value, (str, int, float, bool)):
            out[key] = value
    return out


def _gemini_generation_config(request: PromptRequest) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if request.output_schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseSchema"] = _gemini_schema(request.output_schema)
    return config


def _iter_gemini_stream_events(chunks: Iterable[dict[str, Any]]) -> Iterable[ModelStreamEvent]:
    text = ""
    text_started = False
    output: list[dict[str, Any]] = []
    response_id = ""
    usage: dict[str, Any] | None = None
    function_index = 0
    for chunk in chunks:
        response_id = str(chunk.get("responseId") or response_id)
        usage = _gemini_usage(chunk) or usage
        for part in _gemini_candidate_parts(chunk):
            if isinstance(part.get("text"), str):
                delta = part["text"]
                if not delta:
                    continue
                if not text_started:
                    text_started = True
                    yield ModelStreamEvent(
                        "item.started",
                        {
                            "item": {"type": "message", "role": "assistant", "content": []},
                            "item_id": "gemini-message-0",
                            "output_index": len(output),
                        },
                    )
                text += delta
                yield ModelStreamEvent(
                    "item.delta",
                    {
                        "item_id": "gemini-message-0",
                        "output_index": len(output),
                        "content_index": 0,
                        "delta": delta,
                        "raw_type": "gemini.text.delta",
                    },
                )
                continue
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                if text_started:
                    message_item = _gemini_message_item(text)
                    output.append(message_item)
                    yield ModelStreamEvent(
                        "item.completed",
                        {"item": message_item, "item_id": "gemini-message-0", "output_index": len(output) - 1},
                    )
                    text_started = False
                    text = ""
                item = _gemini_function_call_item(function_call, function_index, part)
                function_index += 1
                output.append(item)
                item_id = str(item.get("call_id") or f"gemini-function-{function_index}")
                yield ModelStreamEvent("item.started", {"item": item, "item_id": item_id, "output_index": len(output) - 1})
                yield ModelStreamEvent(
                    "item.delta",
                    {
                        "item_id": item_id,
                        "output_index": len(output) - 1,
                        "delta": item.get("arguments", ""),
                        "raw_type": "gemini.function_call_arguments.delta",
                    },
                )
                yield ModelStreamEvent("item.completed", {"item": item, "item_id": item_id, "output_index": len(output) - 1})
    if text_started:
        message_item = _gemini_message_item(text)
        output.append(message_item)
        yield ModelStreamEvent("item.completed", {"item": message_item, "item_id": "gemini-message-0", "output_index": len(output) - 1})
    if usage is not None:
        completed_response = {"id": response_id, "output": output, "usage": usage}
        yield _token_count_event(completed_response)
    yield ModelStreamEvent(
        "model.response",
        {
            "response_id": response_id,
            "response": {"id": response_id, "output": output, "usage": usage},
            "usage": usage,
            "raw_type": "gemini.response.completed",
        },
    )


def _gemini_response_stream_events(response: dict[str, Any]) -> Iterable[ModelStreamEvent]:
    usage = _gemini_usage(response)
    response_id = str(response.get("responseId") or "")
    payload: dict[str, Any] = {"id": response_id, "output": _gemini_output_items(response)}
    if usage is not None:
        payload["usage"] = usage
    yield from _scripted_stream_events(payload)


def _gemini_output_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    function_index = 0
    text_chunks: list[str] = []
    for part in _gemini_candidate_parts(response):
        if isinstance(part.get("text"), str):
            if part["text"]:
                text_chunks.append(part["text"])
            continue
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            if text_chunks:
                items.append(_gemini_message_item("".join(text_chunks)))
                text_chunks = []
            items.append(_gemini_function_call_item(function_call, function_index, part))
            function_index += 1
    if text_chunks:
        items.append(_gemini_message_item("".join(text_chunks)))
    return items


def _gemini_candidate_parts(response: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return []
    parts: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        raw_parts = content.get("parts")
        if isinstance(raw_parts, list):
            parts.extend(part for part in raw_parts if isinstance(part, dict))
    return parts


def _gemini_message_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _gemini_function_call_item(function_call: dict[str, Any], index: int, part: dict[str, Any]) -> dict[str, Any]:
    call_id = str(function_call.get("id") or f"gemini-call-{index}")
    args = function_call.get("args")
    if not isinstance(args, dict):
        args = {}
    item: dict[str, Any] = {
        "type": "function_call",
        "name": str(function_call.get("name") or ""),
        "call_id": call_id,
        "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
    }
    signature = part.get("thoughtSignature")
    if isinstance(signature, str) and signature:
        item["gemini_thought_signature"] = signature
    return item


def _gemini_usage(response: dict[str, Any]) -> dict[str, Any] | None:
    raw = response.get("usageMetadata")
    if not isinstance(raw, dict):
        return None
    input_tokens = int(raw.get("promptTokenCount") or 0)
    output_tokens = int(raw.get("candidatesTokenCount") or raw.get("outputTokenCount") or 0)
    total_tokens = int(raw.get("totalTokenCount") or (input_tokens + output_tokens))
    reasoning_tokens = int(raw.get("thoughtsTokenCount") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_output_tokens": reasoning_tokens,
    }


def _gemini_reasoning_text(item: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("summary", "content"):
        value = item.get(key)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks)


def _gemini_json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_gemini_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _gemini_json_safe(item) for key, item in value.items()}
    return str(value)


def _iter_sse_events(stream: Any) -> Iterable[dict[str, Any]]:
    """Parse an OpenAI Responses SSE stream into raw event dicts.

    Each event looks like:
        event: <name>
        data: <json>
        <blank line>
    The JSON payload already carries `type`, so the event name is informational.
    """
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, (bytes, bytearray)) else str(raw_line)
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        payload = "\n".join(data_lines)
        if payload and payload != "[DONE]":
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                pass


class ScriptedResponsesModel:
    """Deterministic model used by tests and CLI smoke runs."""

    def __init__(self, responses: Sequence[dict[str, Any]]):
        self.responses = list(responses)
        self.requests: list[PromptRequest] = []
        self.index = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "ScriptedResponsesModel | None":
        raw = os.environ.get("PY_VOLLEY_FAKE_RESPONSES")
        if not raw:
            return None
        return cls(json.loads(raw))

    def create(self, request: PromptRequest) -> ModelResponse:
        return collect_model_stream_events(self.stream(request))

    def stream(self, request: PromptRequest) -> Iterable[ModelStreamEvent]:
        response = self._next_response(request)
        if isinstance(response.get("events"), list):
            yield from iter_model_stream_events(response["events"])
        else:
            yield from _scripted_stream_events(response)

    def _next_response(self, request: PromptRequest) -> dict[str, Any]:
        with self._lock:
            self.requests.append(request)
            if self.index >= len(self.responses):
                raise RuntimeError("scripted model exhausted")
            response = self.responses[self.index]
            self.index += 1
        payload = dict(response)
        payload.setdefault("id", f"fake-{self.index}")
        return payload


def default_model_client(config: VolleyConfig | None = None) -> ModelClient:
    scripted = ScriptedResponsesModel.from_env()
    if scripted is not None:
        return scripted
    provider = (getattr(config, "model_provider_id", None) if config is not None else None) or "openai"
    if config is not None:
        model_info = _model_catalog_info(config.model)
        catalog_provider = model_info.get("provider")
        if isinstance(catalog_provider, str) and catalog_provider:
            provider = catalog_provider
        elif model_info:
            provider = "openai"
    if provider.lower() == "gemini":
        return GeminiGenerateContentModel(
            base_url=getattr(config, "gemini_base_url", None) if config is not None else None,
        )
    auth_mode = normalize_auth_mode(
        getattr(config, "auth_mode", None) if config is not None else os.environ.get("PY_VOLLEY_AUTH_MODE")
    )
    auth_home = getattr(config, "auth_home", None) if config is not None else None
    if auth_mode in {"auto", "chatgpt"}:
        try:
            snapshot = load_auth_snapshot(auth_home, mode=auth_mode)
        except RuntimeError:
            if auth_mode == "chatgpt":
                raise
            snapshot = None
        if snapshot is not None and snapshot.is_chatgpt:
            chatgpt_client = ChatGPTCodexSubscriptionModel(
                auth_snapshot=snapshot,
                auth_home=snapshot.auth_home,
                base_url=getattr(config, "chatgpt_base_url", None) if config is not None else None,
            )
            if auth_mode == "auto":
                api_key = _api_key_for_openai_transport(auth_home)
                if api_key:
                    return FallbackModelClient(
                        primary=chatgpt_client,
                        fallback=OpenAIResponsesModel(
                            api_key=api_key,
                            base_url=getattr(config, "openai_base_url", None) if config is not None else None,
                        ),
                    )
            return chatgpt_client
    return OpenAIResponsesModel(base_url=getattr(config, "openai_base_url", None) if config is not None else None)


def _api_key_for_openai_transport(auth_home: Path | str | None) -> str | None:
    try:
        snapshot = load_auth_snapshot(auth_home, mode="api_key")
    except RuntimeError:
        snapshot = None
    if snapshot is not None and snapshot.api_key:
        return snapshot.api_key
    return load_openai_api_key()


def _looks_like_account_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "budget",
        "quota",
        "insufficient_quota",
        "usage limit",
        "rate_limit",
        "rate limit",
        "billing",
        "subscription",
        "limit reached",
        "exceeded",
    )
    return any(marker in text for marker in markers)


def _responses_headers(
    request: PromptRequest,
    *,
    session_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, str]:
    resolved_session_id = session_id or request.session_id
    resolved_thread_id = thread_id or request.thread_id or request.prompt_cache_key
    headers: dict[str, str] = {}
    if resolved_session_id:
        headers["session-id"] = resolved_session_id
    if resolved_thread_id:
        headers["thread-id"] = resolved_thread_id
        headers["x-client-request-id"] = resolved_thread_id
    client_metadata = request.client_metadata or {}
    installation_id = client_metadata.get("x-codex-installation-id")
    if installation_id:
        headers["x-codex-installation-id"] = installation_id
    return headers


def load_openai_api_key(env_file: Path = DEFAULT_OPENAI_ENV_FILE) -> str | None:
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key

    values = load_env_file(env_file)
    key = values.get("OPENAI_API_KEY")
    if key:
        os.environ.setdefault("OPENAI_API_KEY", key)
    return key


def load_gemini_api_key(env_files: Sequence[Path] = DEFAULT_GEMINI_ENV_FILES) -> str | None:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env_key = os.environ.get(env_name)
        if env_key:
            return env_key

    for env_file in env_files:
        values = load_env_file(env_file)
        for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            key = values.get(env_name)
            if key:
                os.environ.setdefault(env_name, key)
                return key
    return None


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def collect_stream_response(events: Iterable[Any]) -> ModelResponse:
    return collect_model_stream_events(iter_model_stream_events(events))


def model_response_to_stream_events(response: ModelResponse) -> Iterable[ModelStreamEvent]:
    payload: dict[str, Any] = {"id": response.id, "output": response.output}
    raw_response = response.raw.get("response") if isinstance(response.raw, dict) else None
    if isinstance(raw_response, dict) and "usage" in raw_response:
        payload["usage"] = raw_response["usage"]
    yield from _scripted_stream_events(payload)


def collect_model_stream_events(events: Iterable[ModelStreamEvent]) -> ModelResponse:
    raw_events: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    completed_response: dict[str, Any] | None = None
    response_id = ""

    for event in events:
        data = {"type": event.type, **event.payload}
        raw_events.append(data)
        if event.type == "item.completed" and isinstance(event.payload.get("item"), dict):
            output.append(event.payload["item"])
        elif event.type == "model.response":
            response_id = str(event.payload.get("response_id") or response_id)
            response = event.payload.get("response")
            if isinstance(response, dict):
                completed_response = response
        elif event.type == "model.failed":
            response = event.payload.get("response")
            if isinstance(response, dict):
                completed_response = response

    if completed_response is not None:
        completed_output = completed_response.get("output")
        if isinstance(completed_output, list):
            output = [_model_dump(item) for item in completed_output]
        response_id = str(completed_response.get("id") or response_id)

    return ModelResponse(id=response_id, output=output, raw={"events": raw_events, "response": completed_response})


def iter_model_stream_events(events: Iterable[Any]) -> Iterable[ModelStreamEvent]:
    for event in events:
        data = _model_dump(event)
        event_type = str(data.get("type") or "")
        if event_type == "response.output_item.added" and isinstance(data.get("item"), dict):
            yield ModelStreamEvent(
                "item.started",
                {
                    "item": data["item"],
                    "item_id": _event_item_id(data),
                    "output_index": data.get("output_index"),
                    "raw_type": event_type,
                },
            )
        elif event_type == "response.output_item.done" and isinstance(data.get("item"), dict):
            yield ModelStreamEvent(
                "item.completed",
                {
                    "item": data["item"],
                    "item_id": _event_item_id(data),
                    "output_index": data.get("output_index"),
                    "raw_type": event_type,
                },
            )
        elif event_type.endswith(".delta") and "delta" in data:
            yield ModelStreamEvent(
                "item.delta",
                {
                    "item_id": _event_item_id(data),
                    "output_index": data.get("output_index"),
                    "content_index": data.get("content_index"),
                    "summary_index": data.get("summary_index"),
                    "delta": data.get("delta"),
                    "raw_type": event_type,
                },
            )
        elif event_type == "response.completed" and isinstance(data.get("response"), dict):
            response = data["response"]
            yield _token_count_event(response)
            yield ModelStreamEvent(
                "model.response",
                {
                    "response_id": str(response.get("id", "")),
                    "response": response,
                    "usage": response.get("usage"),
                    "raw_type": event_type,
                },
            )
        elif event_type in {"response.failed", "response.incomplete"}:
            response = data.get("response") if isinstance(data.get("response"), dict) else None
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                yield _token_count_event(response)
            yield ModelStreamEvent(
                "model.failed",
                {
                    "response_id": str(response.get("id", "")) if isinstance(response, dict) else "",
                    "response": response,
                    "error": _response_failure_error(event_type, data, response),
                    "raw_type": event_type,
                },
            )


_NON_RETRYABLE_RESPONSE_FAILED_CODES = {
    "context_length_exceeded",
    "insufficient_quota",
    "usage_not_included",
    "invalid_prompt",
    "cyber_policy",
    "server_is_overloaded",
    "slow_down",
}


def _response_failure_error(event_type: str, data: dict[str, Any], response: dict[str, Any] | None) -> Any:
    raw_error = data.get("error")
    if raw_error is None and isinstance(response, dict):
        raw_error = response.get("error")

    if event_type == "response.incomplete":
        reason = "unknown"
        if isinstance(response, dict):
            details = response.get("incomplete_details")
            if isinstance(details, dict) and isinstance(details.get("reason"), str):
                reason = details["reason"]
        message = f"Incomplete response returned, reason: {reason}"
        if isinstance(raw_error, dict):
            error = dict(raw_error)
            error.setdefault("message", message)
            error.setdefault("retryable", True)
            return error
        return {"message": str(raw_error) if isinstance(raw_error, str) and raw_error else message, "retryable": True}

    if isinstance(raw_error, dict):
        error = dict(raw_error)
        code = str(error.get("code") or "")
        error.setdefault("message", "response.failed event received")
        error.setdefault("retryable", code not in _NON_RETRYABLE_RESPONSE_FAILED_CODES)
        return error
    if isinstance(raw_error, str) and raw_error:
        return {"message": raw_error, "retryable": True}
    return {"message": "response.failed event received", "retryable": True}


def _scripted_stream_events(response: dict[str, Any]) -> Iterable[ModelStreamEvent]:
    response_id = str(response.get("id", ""))
    output = [_model_dump(item) for item in response.get("output", [])]
    for index, item in enumerate(output):
        item_id = str(item.get("id") or item.get("call_id") or f"item-{index}")
        yield ModelStreamEvent("item.started", {"item": item, "item_id": item_id, "output_index": index})
        for delta in _scripted_item_deltas(item):
            yield ModelStreamEvent("item.delta", {"item_id": item_id, "output_index": index, **delta})
        yield ModelStreamEvent("item.completed", {"item": item, "item_id": item_id, "output_index": index})
    completed_response = {"id": response_id, "output": output}
    if "usage" in response:
        completed_response["usage"] = response["usage"]
        yield _token_count_event(completed_response)
    yield ModelStreamEvent(
        "model.response",
        {
            "response_id": response_id,
            "response": completed_response,
            "usage": completed_response.get("usage"),
        },
    )


def _scripted_item_deltas(item: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if item.get("type") == "message":
        for content_index, part in enumerate(item.get("content", [])):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                yield {"content_index": content_index, "delta": part["text"], "raw_type": "response.output_text.delta"}
    elif item.get("type") == "reasoning":
        for key in ("summary", "content"):
            value = item.get(key)
            if isinstance(value, str):
                yield {"delta": value, "raw_type": "response.reasoning_summary_text.delta", "summary_index": 0}
            elif isinstance(value, list):
                for summary_index, part in enumerate(value):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        yield {
                            "delta": part["text"],
                            "raw_type": "response.reasoning_summary_text.delta",
                            "summary_index": summary_index,
                        }
                    elif isinstance(part, str):
                        yield {
                            "delta": part,
                            "raw_type": "response.reasoning_summary_text.delta",
                            "summary_index": summary_index,
                        }
    elif item.get("type") == "function_call" and isinstance(item.get("arguments"), str):
        yield {"delta": item["arguments"], "raw_type": "response.function_call_arguments.delta"}
    elif item.get("type") == "custom_tool_call" and isinstance(item.get("input"), str):
        yield {"delta": item["input"], "raw_type": "response.custom_tool_call_input.delta"}


def _token_count_event(response: dict[str, Any]) -> ModelStreamEvent:
    usage = response.get("usage")
    return ModelStreamEvent(
        "token_count",
        {"usage": usage if isinstance(usage, dict) else None},
    )


def _event_item_id(data: dict[str, Any]) -> str:
    item = data.get("item")
    if isinstance(item, dict):
        value = item.get("id") or item.get("call_id")
        if value is not None:
            return str(value)
    for key in ("item_id", "id"):
        if data.get(key) is not None:
            return str(data[key])
    output_index = data.get("output_index")
    return f"item-{output_index}" if output_index is not None else ""


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "dict"):
        return value.dict()
    raise TypeError(f"cannot convert response object to dict: {type(value)!r}")
