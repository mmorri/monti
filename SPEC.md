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
│    - outputs one of: trivial | easy | hard           │
│ 2. Routing policy                                    │
│    - trivial/easy → fast tier model                  │
│    - hard         → strong tier model                │
│    - on failure   → escalate one tier and retry      │
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
  or hard.` Keep it cheap — truncate the task to the first ~2k characters.
- Fallback heuristic when the classifier is unreachable: keyword/length rules
  (e.g. "typo", "rename" → trivial; "design", "refactor", "architecture",
  "race condition" → hard; default → easy).
- Log every classification (timestamp, verdict, latency) for later tuning.

### 3. Routing policy (`config.yaml`)
```yaml
tiers:
  fast:   { provider: cursor, model: "<fast model id>" }
  strong:  { provider: xai,    model: "grok-4.5" }
policy:
  default_mode: auto
  # modes: auto | fast-only | strong-only | weak-first-escalate
```
- `auto`: classify each request, route by verdict.
- `weak-first-escalate`: try the fast tier first; if the agent trajectory
  shows failure/retry loops (configurable: N consecutive tool errors or an
  explicit "this isn't working" signal), escalate the session to the strong
  tier mid-task. (Same pattern as Switchyard's `auto-esc`.)
- Manual override without restarting: virtual model aliases
  `router-auto`, `router-fast`, `router-strong` selectable in the agent's
  model picker, plus an `X-Router-Mode` request header.

### 4. Proxy server
- OpenAI-compatible endpoints:
  - `GET /v1/models` → lists `router-auto`, `router-fast`, `router-strong`
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
1. `curl 127.0.0.1:PORT/v1/models` lists the three virtual router models with
   no API key configured anywhere on the machine.
2. `"Fix this typo in README.md"` routes to the fast tier; `"Design a plugin
   architecture for a multi-tenant billing system"` routes to the strong
   tier (verify in the request log).
3. Works with only the Cursor subscription logged in; xAI is optional and the
   proxy still starts (strong tier disabled with a clear log line).
4. In `weak-first-escalate` mode, a failing fast-tier attempt escalates to the
   strong tier.
5. `grep -ri "api[_-]key" ~/.config/model-router` returns nothing; no key
   material exists on disk.

## Out of scope for v1
- Web UI / dashboard (structured logs are enough)
- Multi-user or team credential brokering
- Usage metering/billing beyond logs
- Non-coding agents
