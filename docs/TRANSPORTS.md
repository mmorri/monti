# Provider transport status

Auth (login/refresh) is shipped for all eight providers. Chat transports are
only implemented where the wire format is proven — never invented. A tier
whose transport is missing fails closed with a clear error naming the
provider.

| Provider | Auth | Chat transport |
|---|---|---|
| `kimi` | Device authorization grant (opencodex port; global kimi.ai default, CN kimi.com via `domain`) | OpenAI-compatible `POST {api.kimi.ai/coding/v1}/chat/completions`, SSE. |
| `anthropic` | PKCE browser (opencodex port) | Anthropic Messages API (`POST /v1/messages`, SSE) with the OAuth beta headers the opencodex adapter applies (`claude-code-20250219,oauth-2025-04-20`). |
| `muse` | RFC 8628 device flow at `auth.meta.com` + subscription-key mint at `api.meta.ai/muse-code/key` (CLIProxyAPI port) | OpenAI-compatible `POST {base_url from mint}/chat/completions`, SSE. |
| `zai` | Interactive GLM Coding Plan key via `monti login zai` only (plan-key exception, see SPEC.md; no portable OAuth exists) | OpenAI-compatible `POST {api.z.ai/api/coding/paas/v4}/chat/completions`, SSE. |
| `openai` | PKCE browser (opencodex port) | Responses API `POST {chatgpt.com/backend-api}/codex/responses` with Codex CLI identity headers; chat↔Responses translation in `providers/responses.py`. |
| `copilot` | GitHub device flow + `copilot_internal/v2/token` exchange (opencodex port) | OpenAI-compatible `POST {api.githubcopilot.com}/chat/completions`, SSE. |
| `xai` | OIDC discovery + PKCE browser (opencodex port) | OpenAI-compatible `POST {cli-chat-proxy.grok.com/v1}/chat/completions`, SSE. |
| `windsurf` | Browser loopback OAuth at windsurf.com → `register.windsurf.com` RegisterUser → long-lived account key; per-RPC `user_jwt` minted via GetUserJwt (opencode-windsurf-auth port) | Connect-streaming `POST {server.codeium.com}/exa.api_server_pb.ApiServerService/GetChatMessage` (gzip protobuf frames); wire helpers in `src/model_router/proto_wire.py`. Model catalog via GetCascadeModelConfigs. |
| `cursor` | PKCE browser + poll (opencodex port) | NOT PORTED — re-evaluated 2026-10-06: a **working, verified** reverse-engineered client exists (`eisbaw/cursor_api_demo`, Python, chat + streaming + checksum cipher confirmed against Cursor 2.6.22) but it carries **no license**, so its code cannot be ported. Independent blockers remain: `api2.cursor.sh` is HTTP/2-only (this router is stdlib-only; no HTTP/2 client) and requests require the obfuscated `x-cursor-checksum` cipher plus IDE machine IDs. A port would need a licensed reference or a clean-room HTTP/2 + checksum implementation; auth stays working, chat fails closed. Revisit if Cursor ships a model-level chat RPC on their official `sdk.v1` bridge. |

Default `config.yaml` points `fast` at `kimi` and `strong` at `anthropic`
so both tiers work end to end today with flat-rate subscriptions. Use
`monti models` to list each logged-in provider's live catalog, and pin any
model per tier via `config.yaml`, `--model`, or `/model`.
