# Provider transport status

Auth (login/refresh) is shipped for all eight providers. Chat transports are
only implemented where the wire format is proven — never invented. A tier
whose transport is missing fails closed with a clear error naming the
provider.

| Provider | Auth | Chat transport |
|---|---|---|
| `kimi` | Device authorization grant (opencodex port) | OpenAI-compatible `POST {api.kimi.com/coding/v1}/chat/completions`, SSE. |
| `anthropic` | PKCE browser (opencodex port) | Anthropic Messages API (`POST /v1/messages`, SSE) with the OAuth beta headers the opencodex adapter applies (`claude-code-20250219,oauth-2025-04-20`). |
| `muse` | RFC 8628 device flow at `auth.meta.com` + subscription-key mint at `api.meta.ai/muse-code/key` (CLIProxyAPI port) | OpenAI-compatible `POST {base_url from mint}/chat/completions`, SSE. |
| `zai` | Interactive GLM Coding Plan key via `monti login zai` only (plan-key exception, see SPEC.md; no portable OAuth exists) | OpenAI-compatible `POST {api.z.ai/api/coding/paas/v4}/chat/completions`, SSE. |
| `openai` | PKCE browser (opencodex port) | Responses API `POST {chatgpt.com/backend-api}/codex/responses` with Codex CLI identity headers; chat↔Responses translation in `providers/responses.py`. |
| `copilot` | GitHub device flow + `copilot_internal/v2/token` exchange (opencodex port) | OpenAI-compatible `POST {api.githubcopilot.com}/chat/completions`, SSE. |
| `xai` | OIDC discovery + PKCE browser (opencodex port) | OpenAI-compatible `POST {cli-chat-proxy.grok.com/v1}/chat/completions`, SSE. |
| `windsurf` | Browser loopback OAuth at windsurf.com → `register.windsurf.com` RegisterUser → long-lived account key; per-RPC `user_jwt` minted via GetUserJwt (opencode-windsurf-auth port) | Connect-streaming `POST {server.codeium.com}/exa.api_server_pb.ApiServerService/GetChatMessage` (gzip protobuf frames); wire helpers in `src/model_router/proto_wire.py`. Model catalog via GetCascadeModelConfigs. |
| `cursor` | PKCE browser + poll (opencodex port) | NOT PORTED — evaluated 2026-10, two candidate paths, both rejected for now: **(a) official `sdk.v1` bridge** (`cursor/sdk-bridge`, MIT): local Connect/HTTP1.1+JSON bridge running a full Cursor *agent* — architecturally mismatched (agent executes its own tools in the workspace, so it cannot honor OpenAI tool-calling for the proxy and would bypass monti's write/shell approval gates) and it authenticates with a dashboard API key, not the subscription token; **(b) reverse-engineered IDE path** (`/aiserver.v1.ChatService/StreamUnifiedChatWithTools`): needs IDE-impersonation headers, an obfuscated `x-cursor-checksum` cipher, and HTTP/2 — a fragile invented flow this router refuses. Revisit if Cursor ships a model-level chat service on the bridge (JSON+HTTP/1.1 would port trivially onto `proto_wire.py`). Auth stays working; chat fails closed. |

Default `config.yaml` points `fast` at `kimi` and `strong` at `anthropic`
so both tiers work end to end today with flat-rate subscriptions. Use
`monti models` to list each logged-in provider's live catalog, and pin any
model per tier via `config.yaml`, `--model`, or `/model`.
