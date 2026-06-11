# Volley

Volley is a Python coding-agent framework built for decentralized multi-agent
collaboration. The first release ships a local CLI agent, persistent chat
sessions, tool execution, compaction, memories, and local sub-agent tools so
multiple coding agents can coordinate inside one workspace.

## Quick Start

```bash
git clone https://github.com/sunnweiwei/volley
cd volley
pip install -e .
export OPENAI_API_KEY=sk-...
volley
```

You can also run without installing:

```bash
python -m volley
```

## CLI

```bash
volley
volley exec --skip-git-repo-check "List the files."
volley exec --json --skip-git-repo-check "List the files."
volley resume --last
volley fork --last
```

Interactive chat supports multi-turn work, streaming output, slash commands
such as `/compact`, `/resume`, `/fork`, `/goal`, `/ps`, and `/stop`, plus
mid-turn interrupts and follow-up input.

## Python API

```python
from volley import VolleyConfig, VolleySession

session = VolleySession(VolleyConfig(cwd=".", sandbox="workspace-write"))
result = session.run("Inspect the project and report the next step.")
print(result.final_message)
```

`VolleySession` is stateful. Reuse the same session for multi-turn workflows,
or call `VolleySession.resume_from_rollout(...)` / `fork_from_rollout(...)` for
saved chats.

## Auth And Models

Volley currently supports OpenAI API keys, the existing OpenAI Codex
subscription auth path, and Gemini API keys. The OpenAI subscription transport
intentionally keeps the current backend endpoint and protocol unchanged for
baseline compatibility.

```bash
export OPENAI_API_KEY=sk-...
volley login status
```

For OpenAI subscription auth:

```bash
volley login
volley login status
```

For Gemini API auth:

```bash
export GEMINI_API_KEY=...
volley exec --model gemini-3.5-flash --skip-git-repo-check "Say hello."
```

You can also put `GEMINI_API_KEY=...` or `GOOGLE_API_KEY=...` in
`secrets/google.env` while developing locally. Gemini models use Volley's
prompt-based compaction path.

Volley writes its own runtime data under `VOLLEY_HOME`, `VOLLEY_PY_HOME`, or
`~/.volley-python` by default. New chats are stored under
`<volley-home>/sessions`.

For migration, Volley can read legacy saved chats from existing Codex Python
session directories when you run `resume`, `fork`, or the interactive picker.
New Volley chats are still written only to the Volley home directory.

## Implemented

- Local interactive CLI and one-shot `exec`
- OpenAI Responses API, ChatGPT subscription transport, and Gemini GenerateContent API
- Tool execution, `apply_patch`, image viewing, web search, planning, and local multi-agent tools
- Conversation persistence with resume/fork
- Context compaction, memory read/write, and `AGENTS.md` discovery
- Sandbox and approval-policy controls

Remote control is intentionally not included in this release. Volley will add
its own remote collaboration path later.

## Tests

```bash
python -m unittest discover tests
```

## License

Apache-2.0. See `LICENSE`.
