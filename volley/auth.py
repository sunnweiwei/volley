from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal


AuthModeSelection = Literal["auto", "api_key", "chatgpt"]

DEFAULT_CHATGPT_BACKEND_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_CHATGPT_CODEX_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_OAUTH_ISSUER = "https://auth.openai.com"
DEFAULT_LOGIN_PORT = 1455
CHATGPT_OAUTH_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR = "VOLLEY_REFRESH_TOKEN_URL_OVERRIDE"
LEGACY_REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR = "CODEX_REFRESH_TOKEN_URL_OVERRIDE"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ORIGINATOR = "codex_cli_rs"
TOKEN_REFRESH_INTERVAL_DAYS = 8


@dataclass(frozen=True)
class VolleyAuthSnapshot:
    mode: str
    source_path: Path | None
    api_key: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    account_id: str | None = None
    email: str | None = None
    plan_type: str | None = None
    is_fedramp_account: bool = False
    last_refresh: datetime | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_chatgpt(self) -> bool:
        return self.mode in {"chatgpt", "chatgptAuthTokens"}

    @property
    def auth_home(self) -> Path | None:
        return self.source_path.parent if self.source_path is not None else None

    def access_token_expiration(self) -> datetime | None:
        return _jwt_expiration(self.access_token)

    def needs_proactive_refresh(self, now: datetime | None = None) -> bool:
        if not self.is_chatgpt or not self.refresh_token:
            return False
        now = now or datetime.now(timezone.utc)
        expires_at = self.access_token_expiration()
        if expires_at is not None:
            return expires_at <= now
        if self.last_refresh is None:
            return False
        return self.last_refresh < now - timedelta(days=TOKEN_REFRESH_INTERVAL_DAYS)


@dataclass(frozen=True)
class ChatGPTRateLimitWindow:
    used_percent: float
    window_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True)
class ChatGPTCreditsSnapshot:
    has_credits: bool
    unlimited: bool
    balance: str | None


@dataclass(frozen=True)
class ChatGPTRateLimitSnapshot:
    limit_id: str | None
    limit_name: str | None
    primary: ChatGPTRateLimitWindow | None
    secondary: ChatGPTRateLimitWindow | None
    credits: ChatGPTCreditsSnapshot | None
    plan_type: str | None
    rate_limit_reached_type: str | None


def normalize_auth_mode(value: str | None) -> AuthModeSelection:
    if value is None or not value.strip():
        return "auto"
    key = value.strip().replace("-", "_").lower()
    if key in {"auto", "default"}:
        return "auto"
    if key in {"api", "apikey", "api_key", "openai_api_key"}:
        return "api_key"
    if key in {"chatgpt", "chatgpt_auth_tokens", "chatgptauthtokens"}:
        return "chatgpt"
    raise ValueError(f"unsupported auth mode `{value}`")


def resolve_auth_home(volley_home: Path | str | None = None, *, for_write: bool = False) -> Path:
    if volley_home is not None:
        return Path(volley_home).expanduser().resolve()
    configured = [
        os.environ.get("VOLLEY_AUTH_HOME"),
        os.environ.get("VOLLEY_HOME"),
        os.environ.get("VOLLEY_PY_HOME"),
    ]
    primary = next((Path(value).expanduser().resolve() for value in configured if value), None)
    if primary is not None:
        return primary
    default_home = Path("~/.volley-python").expanduser().resolve()
    if for_write:
        return default_home
    candidates = [
        default_home,
        Path("~/.volley").expanduser().resolve(),
        *legacy_auth_homes(),
    ]
    for candidate in candidates:
        if (candidate / "auth.json").exists():
            return candidate
    return default_home


def legacy_auth_homes() -> list[Path]:
    homes: list[Path] = []
    for value in (
        os.environ.get("CODEX_AUTH_HOME"),
        os.environ.get("CODEX_HOME"),
        os.environ.get("CODEX_PY_HOME"),
    ):
        if value:
            homes.append(Path(value).expanduser().resolve())
    homes.extend(
        [
            Path("~/.codex-python").expanduser().resolve(),
            Path("~/.codex").expanduser().resolve(),
        ]
    )
    out: list[Path] = []
    seen: set[Path] = set()
    for home in homes:
        if home not in seen:
            out.append(home)
            seen.add(home)
    return out


def auth_json_path(volley_home: Path | str | None = None, *, for_write: bool = False) -> Path:
    return resolve_auth_home(volley_home, for_write=for_write) / "auth.json"


def load_auth_snapshot(
    volley_home: Path | str | None = None,
    *,
    mode: AuthModeSelection | str = "auto",
) -> VolleyAuthSnapshot | None:
    requested_mode = normalize_auth_mode(mode)
    path = auth_json_path(volley_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if requested_mode == "chatgpt":
            raise RuntimeError(f"ChatGPT auth requested, but {path} does not exist")
        return None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    auth_mode = _normalize_stored_auth_mode(raw.get("auth_mode"))
    if requested_mode == "api_key":
        return _api_key_snapshot(path, raw)
    if requested_mode == "chatgpt":
        snapshot = _chatgpt_snapshot(path, raw, auth_mode)
        if snapshot is None:
            raise RuntimeError(f"ChatGPT auth requested, but {path} does not contain ChatGPT tokens")
        return snapshot

    if auth_mode in {"chatgpt", "chatgptAuthTokens"}:
        snapshot = _chatgpt_snapshot(path, raw, auth_mode)
        if snapshot is not None:
            return snapshot
    return _api_key_snapshot(path, raw)


def refresh_chatgpt_auth(snapshot: VolleyAuthSnapshot, *, timeout: float = 60) -> VolleyAuthSnapshot:
    if not snapshot.refresh_token:
        raise RuntimeError("ChatGPT auth cannot refresh because refresh_token is missing")
    if snapshot.source_path is None:
        raise RuntimeError("ChatGPT auth cannot refresh because source auth.json is unknown")
    raw = dict(snapshot.raw or {})
    tokens = dict(raw.get("tokens") or {})
    body = json.dumps(
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": snapshot.refresh_token,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        os.environ.get(REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR)
        or os.environ.get(LEGACY_REFRESH_TOKEN_URL_OVERRIDE_ENV_VAR)
        or REFRESH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "originator": ORIGINATOR,
            "User-Agent": f"{ORIGINATOR}/python-volley",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            refresh_response = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ChatGPT token refresh failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ChatGPT token refresh failed: {exc.reason}") from exc

    if not isinstance(refresh_response, dict):
        raise RuntimeError("ChatGPT token refresh returned a non-object JSON payload")
    for key in ("id_token", "access_token", "refresh_token"):
        value = refresh_response.get(key)
        if isinstance(value, str) and value:
            tokens[key] = value
    raw["tokens"] = tokens
    raw["auth_mode"] = "chatgpt"
    raw["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_auth_json(snapshot.source_path, raw)
    refreshed = load_auth_snapshot(snapshot.source_path.parent, mode="chatgpt")
    if refreshed is None:
        raise RuntimeError("ChatGPT token refresh did not produce a usable auth snapshot")
    return refreshed


@dataclass(frozen=True)
class DeviceCode:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval: int


def login_with_api_key(api_key: str, volley_home: Path | str | None = None) -> Path:
    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError("No API key provided")
    path = auth_json_path(volley_home, for_write=True)
    payload = {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": api_key,
    }
    _write_auth_json(path, payload)
    return path


def save_chatgpt_tokens(
    *,
    id_token: str,
    access_token: str,
    refresh_token: str,
    volley_home: Path | str | None = None,
    api_key: str | None = None,
) -> Path:
    auth_claims = _auth_claims_from_jwt(id_token)
    tokens = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    account_id = _string_or_none(auth_claims.get("chatgpt_account_id"))
    if account_id:
        tokens["account_id"] = account_id
    payload: dict[str, Any] = {
        "auth_mode": "chatgpt",
        "tokens": tokens,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if api_key:
        payload["OPENAI_API_KEY"] = api_key
    path = auth_json_path(volley_home, for_write=True)
    _write_auth_json(path, payload)
    return path


def build_authorize_url(
    *,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    forced_workspace_ids: list[str] | tuple[str, ...] | None = None,
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": CHATGPT_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": ORIGINATOR,
    }
    if forced_workspace_ids:
        query["allowed_workspace_id"] = ",".join(forced_workspace_ids)
    return f"{issuer.rstrip('/')}/oauth/authorize?{urllib.parse.urlencode(query)}"


def exchange_code_for_tokens(
    *,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    redirect_uri: str,
    code_verifier: str,
    code: str,
    timeout: float = 60,
) -> dict[str, str]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{issuer.rstrip('/')}/oauth/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "originator": ORIGINATOR,
            "User-Agent": f"{ORIGINATOR}/python-volley",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OAuth token exchange failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OAuth token exchange failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("OAuth token exchange returned a non-object JSON payload")
    missing = [key for key in ("id_token", "access_token", "refresh_token") if not isinstance(payload.get(key), str)]
    if missing:
        raise RuntimeError(f"OAuth token exchange response missing {', '.join(missing)}")
    return {
        "id_token": payload["id_token"],
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
    }


def run_browser_login(
    *,
    volley_home: Path | str | None = None,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    port: int = DEFAULT_LOGIN_PORT,
    open_browser: bool = True,
    timeout_seconds: int = 15 * 60,
    forced_workspace_ids: list[str] | tuple[str, ...] | None = None,
    on_start: Any | None = None,
) -> Path:
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)
    try:
        server = _LoginHTTPServer(("127.0.0.1", port), _LoginCallbackHandler)
    except OSError:
        if port != DEFAULT_LOGIN_PORT:
            raise
        server = _LoginHTTPServer(("127.0.0.1", 0), _LoginCallbackHandler)
    actual_port = int(server.server_address[1])
    redirect_uri = f"http://localhost:{actual_port}/auth/callback"
    auth_url = build_authorize_url(
        issuer=issuer,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
        forced_workspace_ids=forced_workspace_ids,
    )
    server.login_context = {
        "auth_url": auth_url,
        "volley_home": volley_home,
        "issuer": issuer,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "state": state,
        "forced_workspace_ids": tuple(forced_workspace_ids or ()),
        "done": threading.Event(),
        "result_path": None,
        "error": None,
    }
    if on_start is not None:
        on_start(actual_port, auth_url)
    if open_browser:
        webbrowser.open(auth_url)

    deadline = time.monotonic() + timeout_seconds
    server.timeout = 1
    try:
        while not server.login_context["done"].is_set():
            if time.monotonic() > deadline:
                raise TimeoutError("ChatGPT login timed out")
            server.handle_request()
    finally:
        server.server_close()

    if server.login_context["error"] is not None:
        raise RuntimeError(str(server.login_context["error"]))
    result_path = server.login_context["result_path"]
    if not isinstance(result_path, Path):
        raise RuntimeError("ChatGPT login completed without saving credentials")
    return result_path


def request_device_code(
    *,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    timeout: float = 60,
) -> DeviceCode:
    base_url = issuer.rstrip("/")
    payload = _http_json(
        f"{base_url}/api/accounts/deviceauth/usercode",
        {"client_id": client_id},
        timeout=timeout,
    )
    user_code = _string_or_none(payload.get("user_code")) or _string_or_none(payload.get("usercode"))
    device_auth_id = _string_or_none(payload.get("device_auth_id"))
    if not user_code or not device_auth_id:
        raise RuntimeError("device code response missing user_code or device_auth_id")
    interval_raw = payload.get("interval", 5)
    try:
        interval = max(1, int(interval_raw))
    except (TypeError, ValueError):
        interval = 5
    return DeviceCode(
        verification_url=f"{base_url}/codex/device",
        user_code=user_code,
        device_auth_id=device_auth_id,
        interval=interval,
    )


def complete_device_code_login(
    device_code: DeviceCode,
    *,
    volley_home: Path | str | None = None,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    timeout_seconds: int = 15 * 60,
    forced_workspace_ids: list[str] | tuple[str, ...] | None = None,
) -> Path:
    base_url = issuer.rstrip("/")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            payload = _http_json(
                f"{base_url}/api/accounts/deviceauth/token",
                {
                    "device_auth_id": device_code.device_auth_id,
                    "user_code": device_code.user_code,
                },
                timeout=60,
            )
            break
        except _DeviceCodePending:
            if time.monotonic() >= deadline:
                raise TimeoutError("device auth timed out after 15 minutes")
            time.sleep(min(device_code.interval, max(1, int(deadline - time.monotonic()))))

    authorization_code = _string_or_none(payload.get("authorization_code"))
    code_verifier = _string_or_none(payload.get("code_verifier"))
    if not authorization_code or not code_verifier:
        raise RuntimeError("device auth token response missing authorization_code or code_verifier")
    tokens = exchange_code_for_tokens(
        issuer=issuer,
        client_id=client_id,
        redirect_uri=f"{base_url}/deviceauth/callback",
        code_verifier=code_verifier,
        code=authorization_code,
    )
    _ensure_workspace_allowed(tokens["id_token"], forced_workspace_ids)
    return save_chatgpt_tokens(volley_home=volley_home, **tokens)


def run_device_code_login(
    *,
    volley_home: Path | str | None = None,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = CLIENT_ID,
    forced_workspace_ids: list[str] | tuple[str, ...] | None = None,
) -> Path:
    code = request_device_code(issuer=issuer, client_id=client_id)
    print(
        "\nFollow these steps to sign in with ChatGPT using device code authorization:\n"
        f"\n1. Open this link in your browser and sign in to your account\n   {code.verification_url}\n"
        f"\n2. Enter this one-time code (expires in 15 minutes)\n   {code.user_code}\n"
        "\nDevice codes are a common phishing target. Never share this code.\n"
    )
    return complete_device_code_login(
        code,
        volley_home=volley_home,
        issuer=issuer,
        client_id=client_id,
        forced_workspace_ids=forced_workspace_ids,
    )


def auth_status(volley_home: Path | str | None = None) -> dict[str, Any]:
    path = auth_json_path(volley_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "auth_home": str(path.parent),
            "auth_file": str(path),
            "logged_in": False,
            "auth_mode": None,
            "has_api_key": bool(os.environ.get("OPENAI_API_KEY")),
            "has_chatgpt_tokens": False,
        }
    if not isinstance(raw, dict):
        return {
            "auth_home": str(path.parent),
            "auth_file": str(path),
            "logged_in": False,
            "auth_mode": "invalid",
            "has_api_key": False,
            "has_chatgpt_tokens": False,
        }
    mode = _normalize_stored_auth_mode(raw.get("auth_mode"))
    chatgpt = _chatgpt_snapshot(path, raw, mode)
    api_key = _api_key_snapshot(path, raw)
    return {
        "auth_home": str(path.parent),
        "auth_file": str(path),
        "logged_in": bool((chatgpt and chatgpt.access_token) or (api_key and api_key.api_key)),
        "auth_mode": mode,
        "has_api_key": bool(api_key and api_key.api_key),
        "has_chatgpt_tokens": bool(chatgpt and chatgpt.access_token),
        "account_id": _mask_identifier(chatgpt.account_id) if chatgpt else None,
        "email": chatgpt.email if chatgpt else None,
        "plan_type": chatgpt.plan_type if chatgpt else None,
        "last_refresh": chatgpt.last_refresh.isoformat().replace("+00:00", "Z") if chatgpt and chatgpt.last_refresh else None,
    }


def fetch_chatgpt_rate_limits(
    volley_home: Path | str | None = None,
    *,
    base_url: str | None = None,
    timeout: float = 10,
) -> list[ChatGPTRateLimitSnapshot]:
    """Fetch Volley ChatGPT usage windows using the official backend shape."""

    snapshot = load_auth_snapshot(volley_home, mode="chatgpt")
    if snapshot is None:
        raise RuntimeError("ChatGPT auth is required to read rate limits")
    snapshot = _refresh_snapshot_if_needed(snapshot, timeout=timeout)
    try:
        return _fetch_chatgpt_rate_limits_once(snapshot, base_url=base_url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and snapshot.refresh_token:
            snapshot = refresh_chatgpt_auth(snapshot, timeout=timeout)
            return _fetch_chatgpt_rate_limits_once(snapshot, base_url=base_url, timeout=timeout)
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_http_status_error("ChatGPT rate limits request", exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ChatGPT rate limits request failed: {exc.reason}") from exc


def parse_chatgpt_rate_limit_payload(payload: dict[str, Any]) -> list[ChatGPTRateLimitSnapshot]:
    """Map Codex subscription usage JSON to Volley snapshots."""

    if not isinstance(payload, dict):
        raise RuntimeError("rate-limit payload must be a JSON object")
    plan_type = _string_or_none(payload.get("plan_type"))
    reached_type = None
    reached_payload = payload.get("rate_limit_reached_type")
    if isinstance(reached_payload, dict):
        reached_type = _string_or_none(reached_payload.get("type"))
    snapshots = [
        _rate_limit_snapshot_from_details(
            limit_id="volley",
            limit_name=None,
            details=_dict_or_none(payload.get("rate_limit")),
            credits=_dict_or_none(payload.get("credits")),
            plan_type=plan_type,
            rate_limit_reached_type=reached_type,
        )
    ]
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for item in additional:
            if not isinstance(item, dict):
                continue
            snapshots.append(
                _rate_limit_snapshot_from_details(
                    limit_id=_string_or_none(item.get("metered_feature")) or _string_or_none(item.get("limit_id")),
                    limit_name=_string_or_none(item.get("limit_name")),
                    details=_dict_or_none(item.get("rate_limit")),
                    credits=None,
                    plan_type=plan_type,
                    rate_limit_reached_type=None,
                )
            )
    return snapshots


def chatgpt_codex_subscription_base_url(configured: str | None = None) -> str:
    base = (
        os.environ.get("VOLLEY_CHATGPT_AGENT_BASE_URL")
        or os.environ.get("VOLLEY_CHATGPT_CODEX_BASE_URL")
        or os.environ.get("CODEX_CHATGPT_CODEX_BASE_URL")
        or os.environ.get("CHATGPT_CODEX_BASE_URL")
        or configured
        or DEFAULT_CHATGPT_CODEX_SUBSCRIPTION_BASE_URL
    )
    base = base.strip().rstrip("/")
    if not base:
        return DEFAULT_CHATGPT_CODEX_SUBSCRIPTION_BASE_URL
    if base.endswith("/codex"):
        return base
    if base.endswith("/backend-api"):
        return f"{base}/codex"
    return base


def chatgpt_codex_base_url(configured: str | None = None) -> str:
    return chatgpt_codex_subscription_base_url(configured)


def chatgpt_backend_base_url(configured: str | None = None) -> str:
    base = (
        os.environ.get("VOLLEY_CHATGPT_BACKEND_BASE_URL")
        or os.environ.get("CODEX_CHATGPT_BACKEND_BASE_URL")
        or os.environ.get("CHATGPT_BACKEND_BASE_URL")
        or configured
        or DEFAULT_CHATGPT_BACKEND_BASE_URL
    )
    base = base.strip().rstrip("/")
    if not base:
        return DEFAULT_CHATGPT_BACKEND_BASE_URL
    if base.endswith("/backend-api/codex"):
        return base[: -len("/codex")]
    return base


def _refresh_snapshot_if_needed(snapshot: VolleyAuthSnapshot, *, timeout: float) -> VolleyAuthSnapshot:
    if not snapshot.needs_proactive_refresh():
        return snapshot
    try:
        return refresh_chatgpt_auth(snapshot, timeout=timeout)
    except RuntimeError:
        if snapshot.access_token_expiration() is not None:
            raise
        return snapshot


def _fetch_chatgpt_rate_limits_once(
    snapshot: VolleyAuthSnapshot,
    *,
    base_url: str | None,
    timeout: float,
) -> list[ChatGPTRateLimitSnapshot]:
    base = chatgpt_backend_base_url(base_url)
    path = "/wham/usage" if "/backend-api" in base else "/api/codex/usage"
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        headers=_chatgpt_backend_headers(snapshot),
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ChatGPT rate limits response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ChatGPT rate limits response was not a JSON object")
    return parse_chatgpt_rate_limit_payload(payload)


def _chatgpt_backend_headers(snapshot: VolleyAuthSnapshot) -> dict[str, str]:
    if not snapshot.access_token:
        raise RuntimeError("ChatGPT auth snapshot is missing an access token")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {snapshot.access_token}",
        "originator": ORIGINATOR,
        "User-Agent": f"{ORIGINATOR}/python-volley",
    }
    if snapshot.account_id:
        headers["ChatGPT-Account-Id"] = snapshot.account_id
    if snapshot.is_fedramp_account:
        headers["X-OpenAI-Fedramp"] = "true"
    return headers


def _rate_limit_snapshot_from_details(
    *,
    limit_id: str | None,
    limit_name: str | None,
    details: dict[str, Any] | None,
    credits: dict[str, Any] | None,
    plan_type: str | None,
    rate_limit_reached_type: str | None,
) -> ChatGPTRateLimitSnapshot:
    return ChatGPTRateLimitSnapshot(
        limit_id=limit_id,
        limit_name=limit_name,
        primary=_rate_limit_window_from_details(_dict_or_none((details or {}).get("primary_window"))),
        secondary=_rate_limit_window_from_details(_dict_or_none((details or {}).get("secondary_window"))),
        credits=_credits_snapshot_from_details(credits),
        plan_type=plan_type,
        rate_limit_reached_type=rate_limit_reached_type,
    )


def _rate_limit_window_from_details(details: dict[str, Any] | None) -> ChatGPTRateLimitWindow | None:
    if not details:
        return None
    used_percent = _float_or_none(details.get("used_percent"))
    if used_percent is None:
        return None
    limit_window_seconds = _int_or_none(details.get("limit_window_seconds"))
    window_minutes = None
    if limit_window_seconds is not None and limit_window_seconds > 0:
        window_minutes = (limit_window_seconds + 59) // 60
    resets_at = _int_or_none(details.get("reset_at"))
    return ChatGPTRateLimitWindow(
        used_percent=used_percent,
        window_minutes=window_minutes,
        resets_at=resets_at,
    )


def _credits_snapshot_from_details(details: dict[str, Any] | None) -> ChatGPTCreditsSnapshot | None:
    if not details:
        return None
    return ChatGPTCreditsSnapshot(
        has_credits=bool(details.get("has_credits")),
        unlimited=bool(details.get("unlimited")),
        balance=_string_or_none(details.get("balance")),
    )


def _http_status_error(operation: str, status_code: int, detail: str) -> str:
    detail = detail.strip()
    if len(detail) > 500:
        detail = detail[:500] + "..."
    return f"{operation} failed with HTTP {status_code}: {detail}" if detail else f"{operation} failed with HTTP {status_code}"


def _api_key_snapshot(path: Path, raw: dict[str, Any]) -> VolleyAuthSnapshot | None:
    api_key = raw.get("OPENAI_API_KEY")
    if not isinstance(api_key, str) or not api_key:
        api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return VolleyAuthSnapshot(mode="apikey", source_path=path, api_key=api_key, raw=raw)


def _chatgpt_snapshot(path: Path, raw: dict[str, Any], auth_mode: str) -> VolleyAuthSnapshot | None:
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = _string_or_none(tokens.get("access_token"))
    if not access_token:
        return None
    id_token = _extract_id_token(tokens.get("id_token"))
    claims = _jwt_payload(id_token) or {}
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    profile_claims = claims.get("https://api.openai.com/profile")
    if not isinstance(profile_claims, dict):
        profile_claims = {}
    account_id = (
        _string_or_none(tokens.get("account_id"))
        or _string_or_none(auth_claims.get("chatgpt_account_id"))
    )
    email = _string_or_none(claims.get("email")) or _string_or_none(profile_claims.get("email"))
    plan_type = _string_or_none(auth_claims.get("chatgpt_plan_type"))
    return VolleyAuthSnapshot(
        mode=auth_mode if auth_mode in {"chatgpt", "chatgptAuthTokens"} else "chatgpt",
        source_path=path,
        access_token=access_token,
        refresh_token=_string_or_none(tokens.get("refresh_token")),
        id_token=id_token,
        account_id=account_id,
        email=email,
        plan_type=plan_type,
        is_fedramp_account=bool(auth_claims.get("chatgpt_account_is_fedramp")),
        last_refresh=_parse_datetime(raw.get("last_refresh")),
        raw=raw,
    )


def _extract_id_token(raw: Any) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        return _string_or_none(raw.get("raw_jwt"))
    return None


def _normalize_stored_auth_mode(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return "apikey"
    key = raw.strip()
    lowered = key.replace("_", "").replace("-", "").lower()
    if lowered in {"apikey", "api"}:
        return "apikey"
    if lowered == "chatgptauthtokens":
        return "chatgptAuthTokens"
    if lowered == "agentidentity":
        return "agentIdentity"
    if lowered == "chatgpt":
        return "chatgpt"
    return key


def _jwt_expiration(jwt: str | None) -> datetime | None:
    payload = _jwt_payload(jwt)
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def _jwt_payload(jwt: str | None) -> dict[str, Any] | None:
    if not jwt:
        return None
    parts = jwt.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _auth_claims_from_jwt(jwt: str | None) -> dict[str, Any]:
    payload = _jwt_payload(jwt)
    auth = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
    return auth if isinstance(auth, dict) else {}


def _ensure_workspace_allowed(jwt: str, expected: list[str] | tuple[str, ...] | None) -> None:
    if not expected:
        return
    account_id = _string_or_none(_auth_claims_from_jwt(jwt).get("chatgpt_account_id"))
    if account_id is None:
        raise PermissionError(
            "Login is restricted to a specific workspace, but the token did not include chatgpt_account_id"
        )
    if account_id not in expected:
        raise PermissionError(f"Login is restricted to workspace id(s) {', '.join(expected)}")


def _generate_pkce() -> tuple[str, str]:
    verifier_bytes = secrets.token_bytes(64)
    code_verifier = _base64url(verifier_bytes)
    challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return code_verifier, _base64url(challenge)


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class _DeviceCodePending(RuntimeError):
    pass


def _http_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "originator": ORIGINATOR,
            "User-Agent": f"{ORIGINATOR}/python-volley",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in {403, 404} and "deviceauth/token" in url:
            raise _DeviceCodePending(detail) from exc
        raise RuntimeError(f"HTTP request failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTP request failed: {exc.reason}") from exc
    try:
        data = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("HTTP response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("HTTP response was not a JSON object")
    return data


class _LoginHTTPServer(http.server.HTTPServer):
    login_context: dict[str, Any]


class _LoginCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        context = self.server.login_context  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/auth/callback":
            self._send_html(404, "<h1>Not Found</h1>")
            return
        params = urllib.parse.parse_qs(parsed.query)
        try:
            state = (params.get("state") or [""])[0]
            if state != context["state"]:
                raise RuntimeError("State mismatch")
            error = (params.get("error") or [""])[0]
            if error:
                description = (params.get("error_description") or [""])[0]
                raise RuntimeError(description or error)
            code = (params.get("code") or [""])[0]
            if not code:
                raise RuntimeError("Missing authorization code")
            tokens = exchange_code_for_tokens(
                issuer=context["issuer"],
                client_id=context["client_id"],
                redirect_uri=context["redirect_uri"],
                code_verifier=context["code_verifier"],
                code=code,
            )
            _ensure_workspace_allowed(tokens["id_token"], context.get("forced_workspace_ids"))
            result_path = save_chatgpt_tokens(volley_home=context["volley_home"], **tokens)
            context["result_path"] = result_path
            self._send_html(
                200,
                "<h1>Successfully logged in</h1><p>You can close this browser window and return to Volley.</p>",
            )
        except Exception as exc:
            context["error"] = exc
            self._send_html(400, f"<h1>Login failed</h1><p>{_html_escape(str(exc))}</p>")
        finally:
            context["done"].set()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_html(self, status: int, body: str) -> None:
        encoded = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Volley Login</title></head>"
            f"<body>{body}</body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
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


def _mask_identifier(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return value[0] + "***" + value[-1]
    return value[:4] + "..." + value[-4:]


def _write_auth_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
