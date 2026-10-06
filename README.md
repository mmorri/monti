# Monti

**A difficulty-based model router for coding agents, powered only by the
subscriptions you already pay for.** Every request is classified (trivial /
easy / hard), routed to a fast or strong tier, and served through your
existing Kimi, Anthropic, Meta Muse, Windsurf, OpenAI, or Z.ai subscription —
with OAuth login only. **No API keys anywhere**: not stored, not accepted,
not even as a fallback. Fail closed, always.

Monti is a standalone local service, not a plugin or a fork. It works with
[opencode](https://opencode.ai) (first-class, via a 10-line config) and any
OpenAI-compatible client — and it ships its own minimal terminal agent for
quick tasks.

## What it does

- **Classifies** each request's difficulty (model-based classifier with a
  heuristic fallback) and routes: trivial/easy → fast tier, hard → strong tier.
- **Fails over** automatically: a transient fast-tier failure (429/5xx/network)
  escalates once to the strong tier before any error reaches you.
- **Escalates sessions** (`weak-first-escalate`): start cheap, upgrade to the
  strong tier after repeated tool errors or an explicit "this isn't working".
- **Lists live model catalogs** per logged-in subscription, so pinning a model
  never means guessing IDs.
- **Keeps tokens safe**: `~/.config/model-router/<provider>/auth.json`,
  mode 0600, atomic writes, per-provider refresh locks, one-retry-on-401 then
  a clear `RELOGIN_REQUIRED:<provider>`.

## Provider support

| Provider | Login | Chat |
|---|---|---|
| `kimi` (kimi.ai, CN kimi.com optional) | device flow | ✅ |
| `anthropic` (Claude Pro/Max) | PKCE browser | ✅ |
| `muse` (Meta Muse Code) | device flow + key mint | ✅ |
| `windsurf` (Cognition) | browser OAuth | ✅ Connect-RPC |
| `openai` (ChatGPT/Codex) | PKCE browser | ✅ Responses API |
| `zai` (GLM Coding Plan) | plan key¹ | ✅ |
| `copilot` (GitHub Copilot) | device flow | ✅ |
| `xai` (Grok) | PKCE browser | ✅ |
| `cursor` | PKCE browser | chat not ported — fails closed (see [docs/TRANSPORTS.md](docs/TRANSPORTS.md)) |

¹ Narrow, documented exception: providers whose plans expose no portable
OAuth flow (currently Z.ai) accept the plan key interactively via
`monti login zai` only — never config or environment. See SPEC.md.

## Quickstart

```sh
git clone <this-repo> && cd monti
uv venv && uv pip install -e .

monti login kimi      # or anthropic / muse / windsurf / openai / zai
monti models          # live catalogs from every logged-in subscription
monti --mode fast "explain this project"
monti                 # interactive session (/help for commands)
```

## Use with opencode

```sh
model-router serve --config config.yaml
```

```json
{
  "provider": {
    "model-router": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://127.0.0.1:8787/v1" },
      "models": {
        "router-auto":   { "name": "Router · auto" },
        "router-fast":   { "name": "Router · fast" },
        "router-strong": { "name": "Router · strong" }
      }
    }
  }
}
```

Pick a router model in opencode's model picker, or send an
`X-Router-Mode: auto|fast-only|strong-only|weak-first-escalate` header from
any client.

## Modes

- `auto` (default): classify each request; hard goes strong.
- `fast-only` / `strong-only`: pin the tier.
- `weak-first-escalate`: start fast; escalate the session after N consecutive
  tool errors or an explicit signal ("this isn't working", …). Sessions are
  tracked via the `X-Router-Session` header.

## Security model

- Subscription OAuth tokens are the only credentials, stored mode 0600 and
  never logged; errors are status-only (no token material in URLs or bodies).
- Config containing anything key-shaped is rejected at load time, and a
  source-scan test fails the build on key-shaped strings or env reads.
- The terminal agent's file tools are workspace-confined; writes, edits, and
  shell commands require interactive approval (or explicit `--yes`).

## Logs

- `~/.config/model-router/logs/requests.jsonl` — per call: verdict, tier,
  model, latency, tokens, fallback, errors.
- `~/.config/model-router/logs/classifications.jsonl` — every verdict.

## Tests

```sh
uv run python -m pytest tests/ -q
```

## Documentation

- [SPEC.md](SPEC.md) — design spec, including the plan-key exception.
- [docs/TRANSPORTS.md](docs/TRANSPORTS.md) — per-provider transport status
  and the Cursor decision record.
- [THIRD_PARTY.md](THIRD_PARTY.md) — attributions for the MIT-licensed OAuth
  flows this project ports.

## Disclaimer

Monti authenticates with your personal subscriptions the same way the
providers' own CLI clients do. Some providers' terms of service may not
contemplate third-party clients using subscription tokens; you are responsible
for how you use your own accounts. Provider gateways change without notice —
when they do, transports here fail closed with clear errors instead of
silently degrading. This project is not affiliated with or endorsed by any
provider.
