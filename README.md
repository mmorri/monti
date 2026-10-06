# Monti

**A difficulty-based model router for coding agents, powered only by the
subscriptions you already pay for.** Every request is classified (trivial /
easy / medium / hard) and routed to one of four tiers — everyday, moderate,
high, very_high — each an ordered pool of interchangeable candidates across
your Kimi, Anthropic, Meta Muse, Windsurf, OpenAI, and Z.ai subscriptions.
Same model on two subscriptions? List it twice: the router burns the wallet
you prefer and rotates when one cools down. OAuth login only.
**No API keys anywhere**: not stored, not accepted, not even as a fallback.
Fail closed, always.

Monti is a standalone local service, not a plugin or a fork. It works with
[opencode](https://opencode.ai) (first-class, via a 10-line config) and any
OpenAI-compatible client — and it ships its own minimal terminal agent for
quick tasks.

## What it does

- **Classifies free**: difficulty classification (trivial/easy/medium/hard)
  runs on a free-first pool — the Muse contributor tier (near-free quota in
  exchange for training on prompts; only ~2k task excerpts are sent) leads,
  with high-quota flash wallets behind it. Routing itself costs ~nothing.
- **Rotates wallets**: each tier is a pool of provider/model candidates in
  your preference order. A 429/quota refusal cools that wallet down for
  `cooldown_seconds` (default 300s) and the next wallet serves — same
  quality band, different subscription. Only then does the request climb
  to the next rung up. A dead grant (`RELOGIN_REQUIRED`) skips the whole
  wallet, never fails the tier.
- **Escalates sessions** (`weak-first-escalate`): start at the routed tier,
  climb one rung after repeated tool errors or an explicit "this isn't working".
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

Requires **Python 3.11+** on **macOS or Linux**.

**As a CLI tool** (recommended — no clone, no activation):

```sh
uv tool install git+https://github.com/mmorri/monti   # or: pipx install git+https://github.com/mmorri/monti
monti login kimi      # or anthropic / muse / windsurf / openai / zai
monti models          # live catalogs from every logged-in subscription
monti --mode moderate "explain this project"
monti                 # interactive session (/help for commands)
```

**From a checkout** (for development):

```sh
git clone https://github.com/mmorri/monti && cd monti
uv venv && uv pip install -e .      # or: python3 -m venv .venv && .venv/bin/pip install -e .
source .venv/bin/activate           # <- without this, `monti` is not on PATH
monti login kimi
```

## Configuration

Monti looks for `config.yaml` in the **current directory first**, then in
`~/.config/model-router/config.yaml` — so a one-time setup works from any
directory:

```sh
mkdir -p ~/.config/model-router
cp config.yaml ~/.config/model-router/config.yaml   # from a checkout
$EDITOR ~/.config/model-router/config.yaml          # pin your tiers
monti serve                                         # uses it anywhere
```

Tiers pin one model each; `monti models` lists every logged-in provider's
live catalog so you never guess a model ID. Without any config file Monti
uses built-in defaults (kimi fast lanes, muse high, anthropic top).

## Use with opencode

```sh
monti serve          # or: model-router serve --config <path>
```

```json
{
  "provider": {
    "model-router": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://127.0.0.1:8787/v1" },
      "models": {
        "router-auto":      { "name": "Router · auto" },
        "router-everyday":  { "name": "Router · everyday" },
        "router-moderate":  { "name": "Router · moderate" },
        "router-high":      { "name": "Router · high" },
        "router-very-high": { "name": "Router · very high" }
      }
    }
  }
}
```

Pick a router model in opencode's model picker, or send an
`X-Router-Mode: auto|everyday-only|moderate-only|high-only|very-high-only|weak-first-escalate`
header from any client.

## Modes

- `auto` (default): classify each request; trivial→everyday, easy→moderate,
  medium→high, hard→very_high.
- `everyday-only` / `moderate-only` / `high-only` / `very-high-only`: pin the tier.
- `weak-first-escalate`: start at the routed tier; climb one rung after N
  consecutive tool errors or an explicit signal ("this isn't working", …).
  Sessions are tracked via the `X-Router-Session` header.

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
