# Subscription-Only Model Router — Build Spec

## Goal
A local service that routes coding-agent requests to the right model based on
task difficulty, authenticating **exclusively through the user's paid
subscriptions**. No API keys anywhere — not stored, not accepted, not even as
a fallback. This is a hard requirement, not a preference.

### Plan-key exception (added 2026-10, user-approved)
Narrowly scoped to flat-rate subscription plans that expose **no portable
OAuth flow**: currently the Z.ai GLM Coding Plan. The plan key may be
supplied **only** through interactive `monti login <provider>` (never from
config, environment, or request headers) and is stored in the same 0600
token store as OAuth credentials. It draws from subscription quota, not
per-token platform billing. It is not an allowlist for platform API keys —
those remain rejected everywhere, fail closed.

## Motivation
The user pays for a Cursor subscription (and may add xAI/Grok). He wants easy
tasks served by fast/cheap models and hard tasks by frontier models, with zero
per-token billing and zero API keys.

## Architecture

```
opencode (or any OpenAI-compatible coding agent)
   │  POST /v1/chat/completions  →  base_url = http://127.0.0.1:PORT/v1
   ▼
┌─ Router Proxy (localhost) ──────────────────────────┐
│ 1. Difficulty classifier                             │
│    - small fast model via subscription, or           │
│      lightweight heuristic fallback                  │
│    - outputs one of: trivial | easy | medium | hard  │
│ 2. Routing policy                                    │
│    - trivial → everyday, easy → moderate,            │
│      medium → high, hard → very_high                 │
│    - each tier is an ordered pool of provider/model  │
│      candidates (same quality, different wallets);  │
│      on failure try the next wallet, then climb      │
│ 3. Subscription auth layer                           │
│    - OAuth tokens only (Cursor, xAI/Grok)            │
│    - auto-refresh; clear "re-login required" errors  │
└──────────────────────────────────────────────────────┘
   │  provider-native or OpenAI-compatible requests
   ▼
Cursor subscription gateway / xAI subscription gateway
```

## Components

### 1. Subscription auth (hard requirement: no API keys)
- Reuse proven OAuth flows — do not invent new ones:
  - **Cursor**: standalone PKCE browser login
    (see `opencodex`: `ocx login cursor`; `claude-code-proxy`: `cursor auth login`)
  - **xAI/Grok**: OAuth via grok.com
    (see `claude-code-proxy`: `grok auth login`; `gajae-code`: `xai` login)
- Token storage: `~/.config/model-router/<provider>/auth.json`, file mode
  0600. Refresh automatically before expiry.
- On HTTP 401 from a provider: refresh once, retry once, then fail with a
  clear `RELOGIN_REQUIRED:<provider>` error — never silently degrade.
- The proxy **refuses to start** if no subscription auth is present.
- It must **never** read API keys from env vars or config (e.g. `XAI_API_KEY`,
  `OPENAI_API_KEY`). Fail closed: if a request path would need an API key,
  return an error instead.

### 2. Difficulty classifier
- Default: ask a small fast model (via subscription) with a tiny prompt such
  as: `Rate this coding task's difficulty as exactly one word: trivial, easy,
  medium, or hard.` Keep it cheap — truncate the task to the first ~2k characters.
- Fallback heuristic when the classifier is unreachable: keyword/length rules
  (e.g. "typo", "rename" → trivial; "bug", "implement" → medium; "design",
  "refactor", "architecture", "race condition" → hard; default → easy).
- Log every classification (timestamp, verdict, latency) for later tuning.

### 3. Routing policy (`config.yaml`)
```yaml
tiers:
  everyday:  [{ provider: zai,  model: "glm-5.3-flash" },
              { provider: kimi, model: "kimi-for-coding-highspeed" }]
  moderate:  [{ provider: kimi, model: "kimi-for-coding" }]
  high:      [{ provider: zai,  model: "glm-5.3" },
              { provider: kimi, model: "k3" },
              { provider: muse, model: "muse-spark-1.3" }]
  very_high: [{ provider: anthropic, model: "claude-fable-5-1" },
              { provider: windsurf,  model: "claude-fable-5-1" },
              { provider: openai,    model: "gpt-6-astra" }]
policy:
  default_mode: auto
  cooldown_seconds: 300
  # modes: auto | everyday-only | moderate-only | high-only | very-high-only
  #        | weak-first-escalate
```
- `auto`: classify each request, route by verdict.
- Within a tier, list order is wallet preference: the first logged-in,
  non-cooling candidate serves. A 429/quota refusal cools that wallet for
  `cooldown_seconds` (rotation skips it); a dead grant skips the wallet
  outright. Only then does the request climb one rung.
- `weak-first-escalate`: route normally, then climb one rung when the agent
  trajectory shows failure/retry loops (configurable: N consecutive tool
  errors or an explicit "this isn't working" signal). (Same pattern as
  Switchyard's `auto-esc`.)
- Manual override without restarting: virtual model aliases
  `router-auto`, `router-everyday`, `router-moderate`, `router-high`,
  `router-very-high` selectable in the agent's model picker, plus an
  `X-Router-Mode` request header.

### 4. Proxy server
- OpenAI-compatible endpoints:
  - `GET /v1/models` → lists `router-auto` plus the per-tier pins
    (`router-everyday`, `router-moderate`, `router-high`, `router-very-high`)
  - `POST /v1/chat/completions` → classify → route → proxy (SSE streaming)
- Translates between the OpenAI chat format and each provider's subscription
  gateway format as needed.
- Request log per call: timestamp, classifier verdict, chosen tier/model,
  latency, token counts (when the provider reports them), errors.

## Reuse — do not rebuild
- **opencode** as the coding agent (point its base URL at the proxy).
- **Auth flows**: port/adapt from `opencodex`, `gajae-code`, or
  `claude-code-proxy` (check each repo's license before copying code).
- **Routing ideas**: NeMo Switchyard bundle (classifier + escalation pattern).

## Acceptance criteria
1. `curl 127.0.0.1:PORT/v1/models` lists the five virtual router models with
   no API key configured anywhere on the machine.
2. `"Fix this typo in README.md"` routes to the everyday tier; `"Design a plugin
   architecture for a multi-tenant billing system"` routes to the very_high
   tier (verify in the request log).
3. Works with only a subset of subscriptions logged in; tiers without a ready
   wallet are reported per-candidate, and strict startup refuses until every
   tier has at least one (or `policy.require_all_tiers: false` allows
   degraded starts).
4. In `weak-first-escalate` mode, a failing attempt climbs one rung; a 429 on
   one wallet rotates to the next wallet in the same tier.
5. `grep -ri "api[_-]key" ~/.config/model-router` returns nothing; no key
   material exists on disk.

## Out of scope for v1
- Web UI / dashboard (structured logs are enough)
- Multi-user or team credential brokering
- Usage metering/billing beyond logs
- Non-coding agents
