# Monti

Terminal coding assistant with subscription-only model routing. Routes each request to
a fast or strong tier based on task difficulty, authenticating exclusively
through your paid subscriptions. **No API keys anywhere** — not stored, not
accepted, not even as a fallback.

## Setup

```sh
uv venv
uv pip install -e .
# or: python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

## Terminal interface

```sh
source .venv/bin/activate
monti login kimi
monti --mode fast                    # Interactive session with just Kimi
monti --mode fast "fix the tests"     # One-shot task
monti                               # Auto-routing session (both tiers required)
monti models
monti status
```

Monti calls subscription providers directly; no proxy server is needed for the
terminal interface. Responses stream as they arrive. Conversations are kept in
memory until you exit or clear them; they are not saved to disk.

Inside a session:

```text
/mode auto
/mode fast
/mode strong
/mode weak-first-escalate
/model fast kimi/kimi-latest
/model strong xai/grok-4.5
/models
/status
/clear
/help
/exit
```

The agent can list and read workspace files, write files, and run shell commands.
Writes show a content preview and require confirmation; shell commands also
require confirmation. File tools stay inside `--workspace` (the current directory
by default). Approved shell commands have normal operating-system access.
Use `--yes` to authorize writes and commands without prompting:

```sh
monti --mode fast --yes "fix the failing tests"
cat error.log | monti --mode fast "explain this failure"
monti --workspace /path/to/project --mode strong "review the code"
monti --mode fast --model kimi/kimi-latest "explain this project"
```

Piped input is appended to the task. Without a terminal, approval-dependent tools
are denied unless `--yes` is supplied. `--max-steps` limits model calls per task
(default 20). Ctrl-C interrupts a task; Ctrl-D exits the interactive session.
Instructions from a workspace-root `AGENTS.md` are included in the session.
Cursor and Codex login are available, but their chat transports are not implemented.

The `model-router` command remains available as an alias, including its existing
`login`, `logout`, `status`, and `serve` commands.

## Login (subscription OAuth — pick what you pay for)

```sh
uv run model-router login kimi       # device code (default fast tier)
uv run model-router login anthropic  # Claude Pro/Max (default strong tier)
uv run model-router login muse       # Meta Muse Code (device flow)
uv run model-router login openai     # ChatGPT/Codex
uv run model-router login zai        # GLM Coding Plan key (plan-key exception)
uv run model-router login windsurf   # Windsurf/Cognition browser OAuth
uv run model-router login cursor     # PKCE browser (chat transport pending)
uv run model-router login copilot    # GitHub Copilot device flow
uv run model-router login xai        # Grok subscription (optional)
uv run model-router status
```

`monti models` also lists the live model catalog of every logged-in
provider, so you can pin exactly the model you want per tier:

```sh
monti models                          # tiers + per-subscription catalogs
monti --model anthropic/claude-opus-4-5 --tier strong "review this design"
/model strong anthropic/claude-opus-4-5   # inside a session
```

Z.ai note: the GLM Coding Plan has no portable OAuth flow, so `monti login
zai` asks for the plan key from your z.ai dashboard (interactive only,
never config/env — see the plan-key exception in SPEC.md).

Tokens land in `~/.config/model-router/<provider>/auth.json` (mode 0600).

## Optional proxy server

```sh
uv run model-router serve --config config.yaml --port 8787
```

The proxy refuses to start unless every configured tier is logged in and
transport-ready — a router with missing pieces is not a router. Missing tiers
are named with the exact `model-router login <provider>` command to fix them.
To allow degraded starts during development, set
`policy.require_all_tiers: false` (tiers without auth or without a ported chat
transport — see `docs/TRANSPORTS.md` — are then logged as disabled and the
proxy still starts).

## Use from opencode

```json
{
  "provider": {
    "id": "provider.model-router",
    "npm": "@ai-sdk/openai-compatible",
    "options": { "baseURL": "http://127.0.0.1:8787/v1" },
    "models": {
      "router-auto":   { "name": "router-auto" },
      "router-fast":   { "name": "router-fast" },
      "router-strong": { "name": "router-strong" }
    }
  }
}
```

Pick `router-auto` / `router-fast` / `router-strong` in the model picker, or
send an `X-Router-Mode: auto|fast-only|strong-only|weak-first-escalate` header.

## Modes

- `auto` (default): classify each request (`trivial|easy|hard`); hard goes strong.
- `fast-only` / `strong-only`: pin the tier.
- `weak-first-escalate`: start fast; escalate the session to strong after N
  consecutive tool errors or an explicit signal ("this isn't working", ...).
  Sessions are tracked via the `X-Router-Session` header.

## Logs

- `~/.config/model-router/logs/requests.jsonl` — per call: verdict, tier,
  model, latency, tokens, errors.
- `~/.config/model-router/logs/classifications.jsonl` — every verdict.

## Tests

```sh
uv run python -m pytest tests/ -q
```
