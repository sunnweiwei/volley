from __future__ import annotations

import hashlib
import json
import os
import sys
import time

from datetime import datetime
from pathlib import Path
from typing import Any

from .types import ApprovalPolicy, VolleyConfig, NetworkAccess, SandboxMode


PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"

ASSET_HASHES = {
    "prompts/gpt_5_codex_prompt.md": "42842be69650ae563d212695e8d3f3591534908fd8ca33b63f742daf41f88b65",
    "prompts/gpt_5_2_prompt.md": "c9b2fa097ac69cae82c3d2ae12271083890a96521c55ad8dc14cae5168ad3f39",
    "prompts/gpt-5.2-codex_prompt.md": "1b3dd697043a7f613c691a3721a450251626685b3e97ac3ad3d35ea62758a31e",
    "prompts/prompt_with_apply_patch_instructions.md": "a78d24ea274453453cedc397095b1a1bfe7218fe884f4e75c6e828375dd42241",
    "prompts/compact/prompt.md": "ab0c334d4faca17e3afbb9b16967c1b2fdcc7242a9a0880af57949fa236d6d07",
    "prompts/compact/summary_prefix.md": "e9b088e794a6bb9082ac053fcc760bd818d7e720ee4bcdc72c6e480de7b7cb0e",
    "prompts/memories/read_path.md": "71ece7a2ab1c986caf93733320bedd518fefa57cda92bf4fa862df3f229ba46c",
    "prompts/memories/write/stage_one_system.md": "cf795e8a2f5f52d333af2613bf1ff79178112f5fd2161cc181a8ddf52e59da33",
    "prompts/memories/write/stage_one_input.md": "2e54c74909238022305c269c862910bb29509fda8b58ce671ef011f8d6453047",
    "prompts/memories/write/consolidation.md": "af0df49d83c5ccc08ad0cadcd2856b05ac33439533daec8ecd00d291a8ad3358",
    "prompts/memories/write/extensions/ad_hoc/instructions.md": "d36a36083d92f9d44efbd95e0e4b6e81d7d149e812f2bca2009b6dd4b8aa93e7",
    "grammars/apply_patch.lark": "d6367f4826ed608c424b0a308f3d6163527df63c22513d089b91863552f8bfeb",
}

MEMORY_STAGE_ONE_MODEL = "gpt-5.4-mini"
MEMORY_STAGE_ONE_REASONING_EFFORT = "low"
MEMORY_STAGE_TWO_MODEL = "gpt-5.4"
MEMORY_STAGE_TWO_REASONING_EFFORT = "medium"
MEMORY_STAGE_ONE_DEFAULT_ROLLOUT_TOKEN_LIMIT = 150_000
MEMORY_STAGE_ONE_CONTEXT_WINDOW_PERCENT = 70
DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95
MEMORY_PHASE2_WORKSPACE_DIFF_FILE = "phase2_workspace_diff.md"

_MEMORY_EXTENSIONS_FOLDER_STRUCTURE = """
Memory extensions (under {{ memory_extensions_root }}/):

- <extension_name>/instructions.md
  - Source-specific guidance for interpreting additional memory signals. If an
    extension folder exists, you must read its instructions.md to determine how to use this memory
    source.

If the user has any memory extensions, you MUST read the instructions for each extension to
determine how to use the memory source. If the workspace diff shows deleted extension resource files,
remove stale memories derived only from those resources. If it has no extension folders, continue
with the standard memory inputs only.
"""

_MEMORY_EXTENSIONS_PRIMARY_INPUTS = """
Optional source-specific inputs:
Under `{{ memory_extensions_root }}/`:

- `<extension_name>/instructions.md`
  - If extension folders exist, read each instructions.md first and follow it when interpreting
    that extension's memory source.

If the workspace diff shows deleted memory extension resources, use that extension-specific deletion
signal to remove stale memories derived only from those resources.
"""

_MODEL_PROMPT_ASSETS = {
    "gpt-5-codex": "gpt_5_codex_prompt.md",
    "gpt-5.2": "gpt_5_2_prompt.md",
    "gpt-5.2-codex": "gpt-5.2-codex_prompt.md",
}
_PERSONALITY_PLACEHOLDER = "{{ personality }}"
_MODEL_CATALOG: dict[str, dict[str, Any]] | None = None


def read_asset(relative_path: str) -> str:
    return (ASSETS_DIR / relative_path).read_text(encoding="utf-8")


def read_model_catalog_instructions(model: str) -> str | None:
    info = _model_catalog().get(model)
    if not info:
        return None
    model_messages = info.get("model_messages")
    if isinstance(model_messages, dict):
        template = model_messages.get("instructions_template")
        if isinstance(template, str):
            variables = model_messages.get("instructions_variables")
            personality = ""
            if isinstance(variables, dict) and isinstance(variables.get("personality_default"), str):
                personality = variables["personality_default"]
            return template.replace(_PERSONALITY_PLACEHOLDER, personality).rstrip()
    base = info.get("base_instructions")
    return base.rstrip() if isinstance(base, str) else None


def _model_catalog() -> dict[str, dict[str, Any]]:
    global _MODEL_CATALOG
    if _MODEL_CATALOG is not None:
        return _MODEL_CATALOG
    path = ASSETS_DIR / "models.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _MODEL_CATALOG = {}
        return _MODEL_CATALOG
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        _MODEL_CATALOG = {}
        return _MODEL_CATALOG
    _MODEL_CATALOG = {
        str(model["slug"]): model
        for model in models
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    return _MODEL_CATALOG


def asset_sha256(relative_path: str) -> str:
    return hashlib.sha256((ASSETS_DIR / relative_path).read_bytes()).hexdigest()


def verify_asset_hashes() -> dict[str, bool]:
    return {
        path: asset_sha256(path) == expected
        for path, expected in ASSET_HASHES.items()
    }


def build_base_instructions(
    *,
    prompt_asset: str,
    model: str | None = None,
    cwd: Path,
    sandbox: SandboxMode,
    approval_policy: ApprovalPolicy,
    volley_home: Path | None = None,
    memory_tool_enabled: bool = False,
    use_memories: bool = True,
) -> str:
    """Return the base `instructions` payload for a model request.

    The extra keyword parameters are kept for the public Python API, but
    permissions, AGENTS.md, environment, and Memory read context are emitted
    as initial context items instead of being mixed into `instructions`.
    """

    if prompt_asset and prompt_asset != "auto":
        return read_asset(f"prompts/{prompt_asset}").rstrip()
    if model:
        catalog_instructions = read_model_catalog_instructions(model)
        if catalog_instructions:
            return catalog_instructions
        mapped_asset = _MODEL_PROMPT_ASSETS.get(model)
        if mapped_asset:
            return read_asset(f"prompts/{mapped_asset}").rstrip()
    return read_asset("prompts/gpt_5_2_prompt.md").rstrip()


def build_initial_context_items(config: VolleyConfig, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    cwd_path = cwd or config.resolved_cwd()
    developer_sections: list[str] = []
    contextual_user_sections: list[str] = []

    if config.include_permissions_instructions:
        developer_sections.append(
            build_permissions_instructions(
                cwd=cwd_path,
                sandbox=config.sandbox,
                approval_policy=config.approval_policy,
                network_access=config.network_access,
                writable_roots=config.writable_roots,
            )
        )

    if config.memory_tool_enabled and config.use_memories:
        memory_instructions = build_memory_tool_developer_instructions(config.resolved_volley_home())
        if memory_instructions:
            developer_sections.append(memory_instructions.rstrip())

    agents_md = collect_agents_md(cwd_path)
    if agents_md:
        contextual_user_sections.append(
            build_user_instructions_context(agents_md, directory=str(cwd_path))
        )

    if config.include_environment_context:
        contextual_user_sections.append(
            build_environment_context(
                cwd_path,
                current_date=config.current_date,
                timezone=config.timezone,
            )
        )

    items: list[dict[str, Any]] = []
    if developer_sections:
        items.append(_message("developer", developer_sections))
    if contextual_user_sections:
        items.append(_message("user", contextual_user_sections))
    return items


def build_permissions_instructions(
    *,
    cwd: Path,
    sandbox: SandboxMode,
    approval_policy: ApprovalPolicy,
    network_access: NetworkAccess = "restricted",
    writable_roots: tuple[Path | str, ...] = (),
) -> str:
    sandbox_prompt = _permissions_asset("sandbox_mode", _sandbox_asset_name(sandbox)).rstrip()
    sandbox_prompt = sandbox_prompt.replace("{{network_access}}", network_access)

    approval_prompt = _permissions_asset("approval_policy", _approval_asset_name(approval_policy)).strip()
    sections = [sandbox_prompt, approval_prompt]
    roots_text = _writable_roots_text(cwd, sandbox, writable_roots)
    if roots_text:
        sections.append(roots_text)
    text = "\n\n".join(section for section in sections if section)
    return text if text.endswith("\n") else text + "\n"


def build_user_instructions_context(text: str, *, directory: str) -> str:
    return f"# AGENTS.md instructions for {directory}\n\n<INSTRUCTIONS>\n{text}\n</INSTRUCTIONS>"


def build_memory_tool_developer_instructions(volley_home: Path, summary_token_limit: int = 5_000) -> str | None:
    base_path = volley_home / "memories"
    memory_summary_path = base_path / "memory_summary.md"
    if not memory_summary_path.exists():
        return None
    memory_summary = memory_summary_path.read_text(encoding="utf-8").strip()
    if not memory_summary:
        return None
    memory_summary = _truncate_to_tokens(memory_summary, summary_token_limit)
    template = read_asset("prompts/memories/read_path.md")
    return template.replace("{{ base_path }}", str(base_path)).replace("{{ memory_summary }}", memory_summary)


def memory_stage_one_system_prompt() -> str:
    return read_asset("prompts/memories/write/stage_one_system.md")


def build_memory_stage_one_input_message(
    *,
    rollout_path: Path | str,
    rollout_cwd: Path | str,
    rollout_contents: str,
    model_context_window: int | None = None,
    effective_context_window_percent: int = DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
) -> str:
    rollout_token_limit = memory_stage_one_rollout_token_limit(
        model_context_window=model_context_window,
        effective_context_window_percent=effective_context_window_percent,
    )
    return _render_template(
        read_asset("prompts/memories/write/stage_one_input.md"),
        {
            "rollout_path": str(rollout_path),
            "rollout_cwd": str(rollout_cwd),
            "rollout_contents": _truncate_middle_to_tokens(rollout_contents, rollout_token_limit),
        },
    )


def memory_stage_one_rollout_token_limit(
    *,
    model_context_window: int | None = None,
    effective_context_window_percent: int = DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
) -> int:
    if model_context_window is None or model_context_window <= 0:
        return MEMORY_STAGE_ONE_DEFAULT_ROLLOUT_TOKEN_LIMIT
    effective_limit = (model_context_window * effective_context_window_percent) // 100
    return max(1, (effective_limit * MEMORY_STAGE_ONE_CONTEXT_WINDOW_PERCENT) // 100)


def build_memory_consolidation_prompt(memory_root: Path | str) -> str:
    memory_root_path = Path(memory_root)
    memory_extensions_root = memory_root_path / "extensions"
    if memory_extensions_root.is_dir():
        extension_values = {"memory_extensions_root": str(memory_extensions_root)}
        memory_extensions_folder_structure = _render_template(
            _MEMORY_EXTENSIONS_FOLDER_STRUCTURE,
            extension_values,
        )
        memory_extensions_primary_inputs = _render_template(
            _MEMORY_EXTENSIONS_PRIMARY_INPUTS,
            extension_values,
        )
    else:
        memory_extensions_folder_structure = ""
        memory_extensions_primary_inputs = ""
    return _render_template(
        read_asset("prompts/memories/write/consolidation.md"),
        {
            "memory_root": str(memory_root_path),
            "memory_extensions_folder_structure": memory_extensions_folder_structure,
            "memory_extensions_primary_inputs": memory_extensions_primary_inputs,
            "phase2_workspace_diff_file": MEMORY_PHASE2_WORKSPACE_DIFF_FILE,
        },
    )


def build_environment_context(
    cwd: Path,
    *,
    shell: str | None = None,
    current_date: str | None = None,
    timezone: str | None = None,
) -> str:
    lines = [
        "<environment_context>",
        f"  <cwd>{cwd}</cwd>",
        f"  <shell>{shell or _default_shell_name()}</shell>",
    ]
    date = current_date or default_current_date()
    tz = timezone or default_timezone()
    if date:
        lines.append(f"  <current_date>{date}</current_date>")
    if tz:
        lines.append(f"  <timezone>{tz}</timezone>")
    lines.append("</environment_context>")
    return "\n".join(lines)


def collect_agents_md(cwd: Path) -> str:
    cwd_path = cwd.resolve()
    project_root = _project_root(cwd_path)
    search_dirs = _path_from_root(project_root, cwd_path) if project_root else [cwd_path]

    chunks = []
    for directory in search_dirs:
        candidate = _agents_md_candidate(directory)
        if candidate is None:
            continue
        text = candidate.read_text(encoding="utf-8")
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks)


def default_current_date() -> str:
    return datetime.now().astimezone().date().isoformat()


def default_timezone() -> str:
    return os.environ.get("TZ") or time.tzname[0]


def _permissions_asset(kind: str, name: str) -> str:
    path = ASSETS_DIR / "prompts" / "permissions" / kind / f"{name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _sandbox_asset_name(sandbox: SandboxMode) -> str:
    return {
        "read-only": "read_only",
        "workspace-write": "workspace_write",
        "danger-full-access": "danger_full_access",
    }[sandbox]


def _approval_asset_name(policy: ApprovalPolicy) -> str:
    return {
        "never": "never",
        "on-request": "on_request",
        "on-failure": "on_failure",
        "untrusted": "unless_trusted",
    }[policy]


def _default_shell() -> str:
    explicit = os.environ.get("SHELL")
    if explicit:
        return explicit
    if sys.platform == "win32":
        return os.environ.get("ComSpec") or "cmd.exe"
    return "/bin/bash"


def _default_shell_name() -> str:
    return Path(_default_shell()).name


def _message(role: str, text_sections: list[str]) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": section} for section in text_sections],
    }


def _writable_roots_text(
    cwd: Path,
    sandbox: SandboxMode,
    writable_roots: tuple[Path | str, ...],
) -> str:
    if sandbox != "workspace-write":
        return ""
    roots = [cwd, *(Path(root).expanduser().resolve() for root in writable_roots)]
    rendered_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        rendered = str(root)
        if rendered in seen:
            continue
        seen.add(rendered)
        rendered_roots.append(f"`{rendered}`")
    if not rendered_roots:
        return ""
    if len(rendered_roots) == 1:
        return f" The writable root is {rendered_roots[0]}."
    return f" The writable roots are {', '.join(rendered_roots)}."


def _project_root(cwd: Path) -> Path | None:
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _path_from_root(root: Path, cwd: Path) -> list[Path]:
    dirs = [cwd]
    while dirs[-1] != root and dirs[-1].parent != dirs[-1]:
        dirs.append(dirs[-1].parent)
    dirs.reverse()
    return dirs


def _agents_md_candidate(directory: Path) -> Path | None:
    for filename in ("AGENTS.override.md", "AGENTS.md"):
        candidate = directory / filename
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _truncate_to_tokens(text: str, tokens: int) -> str:
    return text[: max(tokens, 0) * 4]


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", value)
    return rendered


def _truncate_middle_to_tokens(text: str, tokens: int) -> str:
    if not text:
        return ""
    max_bytes = max(tokens, 0) * 4
    if max_bytes > 0 and len(text.encode("utf-8")) <= max_bytes:
        return text
    if max_bytes == 0:
        return f"\u2026{_approx_token_count(text)} tokens truncated\u2026"

    left_budget = max_bytes // 2
    right_budget = max_bytes - left_budget
    prefix = _prefix_with_byte_budget(text, left_budget)
    suffix = _suffix_with_byte_budget(text, right_budget)
    removed_bytes = max(len(text.encode("utf-8")) - max_bytes, 0)
    removed_tokens = (removed_bytes + 3) // 4
    truncated = f"{prefix}\u2026{removed_tokens} tokens truncated\u2026{suffix}"
    return truncated if truncated != text else text


def _prefix_with_byte_budget(text: str, byte_budget: int) -> str:
    used = 0
    chars: list[str] = []
    for char in text:
        char_bytes = len(char.encode("utf-8"))
        if used + char_bytes > byte_budget:
            break
        chars.append(char)
        used += char_bytes
    return "".join(chars)


def _suffix_with_byte_budget(text: str, byte_budget: int) -> str:
    used = 0
    chars: list[str] = []
    for char in reversed(text):
        char_bytes = len(char.encode("utf-8"))
        if used + char_bytes > byte_budget:
            break
        chars.append(char)
        used += char_bytes
    chars.reverse()
    return "".join(chars)


def _approx_token_count(text: str) -> int:
    byte_count = len(text.encode("utf-8"))
    return (byte_count + 3) // 4
