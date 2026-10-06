# Third-party attributions

OAuth flows in `src/model_router/providers/` are Python ports of the following
MIT-licensed open-source implementations. Only the subscription-OAuth login /
refresh mechanics and endpoint constants were ported; no API-key handling was
carried over (this router accepts none).

## opencodex (OAuth ports: cursor, xai, openai, anthropic, kimi, copilot)

- Source inspected: `lidge-jun/opencodex` fork, `src/oauth/` (`pkce.ts`,
  `cursor.ts`, `xai.ts`, `chatgpt.ts`, `anthropic.ts`, `kimi.ts`,
  `github-copilot.ts`, `callback-server.ts`), `src/adapters/cursor.ts`,
  `src/adapters/cursor/live-transport.ts`.
- License: MIT, (c) 2026 opencodex contributors.
- The canonical upstream repo could not be identified (only forks visible);
  bodies were inspected on the fork carrying the Cursor PKCE commit.

## claude-code-proxy (cross-check: cursor, xai endpoints)

- Source inspected: `mulfyx/claude-code-proxy` (main), `src/providers/cursor/auth.rs`,
  `src/providers/grok/auth/`, `src/config.rs`, `src/paths.rs`.
- License: MIT, (c) 2026 Raine Virta.
- Drift note: its Cursor refresh posts to `{base}/auth/refresh`, while opencodex
  posts to `https://api2.cursor.sh/auth/exchange_user_api_key`. This router
  follows opencodex; if refresh fails with HTTP 404, the `/auth/refresh`
  variant is the documented fallback to try.

## CLIProxyAPI (OAuth port: muse/meta)

- Source ported: `router-for-me/CLIProxyAPI` (main), `internal/auth/meta/meta.go`
  (MIT, (c) router-for-me/CLIProxyAPI contributors).
- Ported: the Meta Muse Code RFC 8628 device flow (client id, auth.meta.com
  endpoints, polling semantics) and the `api.meta.ai/muse-code/key` mint
  exchange. The minted credential is OAuth-derived subscription state, not a
  user-managed platform key. The Responses translation for the `openai`
  provider follows the same gateway wire that project's codex executor
  speaks.

## opencode-windsurf-auth (OAuth + chat port: windsurf)

- Source ported: `rsvedant/opencode-windsurf-auth` (MIT, (c) 2026 contributors),
  `src/oauth/login.ts`, `src/oauth/register-user.ts`, `src/cloud-direct/wire.ts`,
  `chat.ts`, `auth.ts`, `metadata.ts`, `catalog.ts`, and the wire notes in
  `docs/CASCADE_PROTOCOL.md`.
- Ported: the implicit-grant browser login (client
  3GUryQ7ldAeKEuD2obYnppsnmj58eP5u, loopback /auth or show-auth-token paste),
  RegisterUser exchange, GetUserJwt minting with cache, the GetChatMessage
  Connect-streaming request/response wire format (system-message collapsing,
  tool-call delta assembly, usage/finish decoding, trailer-error handling
  incl. the opaque permission_denied case), and the GetCascadeModelConfigs
  catalog. Generic protobuf/Connect primitives live in
  `src/model_router/proto_wire.py` for reuse by future ports (e.g. Cursor).

## cursor/sdk-bridge (evaluated, not ported)

- `cursor/sdk-bridge` (MIT) is the official `sdk.v1` contract: a local
  Connect/HTTP-1.1+JSON bridge embedding `@cursor/sdk`, driving full Cursor
  *agents* (spawn → send → stream run) with a dashboard API key. Evaluated as
  a chat transport and rejected: agents execute their own tools (the OpenAI
  tool-calling contract the proxy speaks cannot be honored, and monti's
  approval gates would be bypassed), and the key is a dashboard credential,
  not the PKCE subscription token. Documented in docs/TRANSPORTS.md; becomes
  attractive the day a model-level chat RPC appears on the bridge.

## Z.ai plan-key exception

- No third-party code involved: the GLM Coding Plan key is supplied by the
  user at `monti login zai` and stored like any OAuth token. See the
  plan-key exception in SPEC.md.


## gajae-code (cross-check: provider coverage)

- Endpoint/token-shape cross-checks only; no code ported.
- License: MIT (Yeachan-Heo + contributors).

## MIT license text (applies to all three)

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.
