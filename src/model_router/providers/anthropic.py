"""Anthropic (Claude Pro/Max) subscription auth: PKCE browser login.

Port of opencodex src/oauth/anthropic.ts (MIT, see THIRD_PARTY.md):
authorize https://claude.ai/oauth/authorize,
token https://platform.claude.com/v1/oauth/token (Claude Code 2.1.220+ posts
the code exchange there, not api.anthropic.com),
callback http://localhost:54545/callback (registration rejects 127.0.0.1),
scope "user:profile user:inference user:sessions:claude_code
user:mcp_servers user:file_upload". Tracked against CLIProxyAPI
internal/auth/claude (MIT).

Chat transport: Anthropic Messages API (public documented API) with the OAuth
beta headers the opencodex adapter applies for authMode == "oauth".
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterator

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, iter_sse_lines, now_ms, post_json, get as http_get
from ..oauth_common import prompt_manual_code, run_callback_flow
from ..pkce import generate_pkce
from ..store import Credentials
from .base import ChatChunk, ChatRequest, ChatTransport, Provider

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CALLBACK_PORT = 54545
CALLBACK_PATH = "/callback"
CALLBACK_HOST = "localhost"  # registration rejects http://127.0.0.1:...
SCOPES = ("user:profile user:inference user:sessions:claude_code "
          "user:mcp_servers user:file_upload")
EXPIRY_SKEW_MS = 5 * 60 * 1000

OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def build_auth_url(challenge: str, state: str, redirect_uri: str) -> str:
    params = urllib.parse.urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"{AUTH_URL}?{params}"


def credentials_from_payload(payload: dict, refresh_fallback: str = "") -> Credentials:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or refresh_fallback
    if not isinstance(access, str) or not access:
        raise AuthFlowError("Anthropic token response missing access token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthFlowError("Anthropic token response did not include a refresh token")
    expires_in = payload.get("expires_in")
    if not (isinstance(expires_in, (int, float)) and expires_in >= 0):
        expires_in = 3600
    expires = now_ms() + int(expires_in * 1000) - EXPIRY_SKEW_MS
    account = payload.get("account") or {}
    return Credentials(
        access=access, refresh=refresh, expires=expires,
        account_id=account.get("uuid") if isinstance(account, dict) else "" or "",
        email=(account.get("email_address") or "").lower() if isinstance(account, dict) else "",
    )


def _post_token(fields: dict) -> dict:
    # Anthropic's edge (Cloudflare) 403s non-browser signatures — mirror the
    # axios-shaped headers the working reference sends on OAuth calls.
    try:
        return post_json(TOKEN_URL, fields, headers={
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "axios/1.15.2",
        }, timeout=30.0).json()
    except HttpStatusError as exc:
        raise AuthFlowError(f"Anthropic token request failed (HTTP {exc.status})") from None


class AnthropicProvider(Provider):
    id = "anthropic"
    display = "Anthropic"

    def login(self, **kwargs) -> Credentials:
        pkce = generate_pkce()

        def build(state: str, redirect_uri: str):
            return build_auth_url(pkce.challenge, state, redirect_uri), (
                "Complete Claude login in your browser. If the browser cannot reach "
                "this machine, paste the final redirect URL or authorization code "
                "when prompted."
            )

        result, redirect_uri = run_callback_flow(
            port=CALLBACK_PORT, path=CALLBACK_PATH, build_auth_url=build,
            open_browser=kwargs.get("open_browser", True),
            redirect_host=CALLBACK_HOST,
        )
        if result:
            code, state = result.code, result.state
        else:
            pasted = prompt_manual_code(self.id)
            code, _, frag = pasted.partition("#")
            state = frag or ""
        # Field order mirrors Claude Code's wire body (grant_type, code,
        # redirect_uri, client_id, code_verifier, state).
        payload = _post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": pkce.verifier,
            "state": state,
        })
        return credentials_from_payload(payload)

    def refresh(self, creds: Credentials) -> Credentials:
        if not creds.refresh:
            raise ReloginRequired(self.id)
        try:
            payload = _post_token({
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh,
                "scope": SCOPES,
            })
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc
        try:
            return credentials_from_payload(payload, creds.refresh)
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        return AnthropicTransport(creds.access, request)

    def list_models(self, creds: Credentials) -> list[str]:
        try:
            data = http_get("https://api.anthropic.com/v1/models", headers={
                "Authorization": f"Bearer {creds.access}",
                "anthropic-version": ANTHROPIC_VERSION,
                "anthropic-beta": OAUTH_BETA,
                "Accept": "application/json",
            }, timeout=30.0).json()
        except Exception:  # noqa: BLE001 — catalog is best-effort
            return []
        return sorted(item["id"] for item in data.get("data") or []
                      if isinstance(item, dict) and isinstance(item.get("id"), str))


def to_messages_payload(request: ChatRequest) -> dict:
    """Translate text and complete tool exchanges to Anthropic Messages."""
    system_parts, messages = [], []
    for msg in request.messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            system_parts.append(content if isinstance(content, str) else _blocks_to_text(content))
            continue
        blocks = []
        if role == "tool":
            blocks.append({"type": "tool_result", "tool_use_id": msg["tool_call_id"],
                           "content": content})
        else:
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                blocks.extend(content)
            for call in msg.get("tool_calls") or []:
                function = call["function"]
                blocks.append({"type": "tool_use", "id": call["id"], "name": function["name"],
                               "input": json.loads(function.get("arguments") or "{}")})
        mapped_role = "assistant" if role == "assistant" else "user"
        if messages and messages[-1]["role"] == mapped_role:
            previous = messages[-1]["content"]
            if isinstance(previous, str):
                previous = [{"type": "text", "text": previous}]
            messages[-1]["content"] = previous + blocks
        else:
            # Preserve simple text requests as strings for compatibility.
            value = blocks[0]["text"] if len(blocks) == 1 and blocks[0]["type"] == "text" else blocks
            messages.append({"role": mapped_role, "content": value})
    body = {"model": request.model, "messages": messages, "stream": request.stream,
            "max_tokens": request.max_tokens or 4096}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.tools:
        body["tools"] = [{"name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "input_schema": t["function"]["parameters"]} for t in request.tools]
    if request.tool_choice:
        choice = request.tool_choice
        if isinstance(choice, str) and choice in ("auto", "required", "none"):
            body["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
        elif isinstance(choice, dict):
            body["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return body


def _blocks_to_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "".join(b.get("text", "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")


def _finish_reason(reason):
    return {"tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop",
            "end_turn": "stop"}.get(reason, "stop")


class AnthropicTransport(ChatTransport):
    def __init__(self, access_token: str, request: ChatRequest, timeout: float = 300.0):
        self.access_token, self.request, self.timeout = access_token, request, timeout

    def run(self) -> Iterator[ChatChunk]:
        import urllib.error

        req = urllib.request.Request(MESSAGES_URL,
            data=json.dumps(to_messages_payload(self.request)).encode(), method="POST", headers={
                "Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json",
                "Accept": "text/event-stream" if self.request.stream else "application/json",
                "anthropic-version": ANTHROPIC_VERSION, "anthropic-beta": OAUTH_BETA,
            })
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise HttpStatusError("POST", MESSAGES_URL, exc.code) from None
        with resp:
            if not self.request.stream:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    raise AuthFlowError("Anthropic returned invalid JSON") from exc
                yield from _non_stream_chunks(data)
                return
            usage, reason = {}, "stop"
            for event, raw in iter_sse_lines(resp):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event == "error":
                    raise AuthFlowError("Anthropic reported a streaming error")
                if event == "message_start":
                    usage["prompt_tokens"] = (data.get("message", {}).get("usage") or {}).get("input_tokens", 0)
                elif event == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        yield ChatChunk(kind="tool_call", tool_call={
                            "index": data["index"], "id": block["id"], "type": "function",
                            "function": {"name": block["name"], "arguments": ""}})
                elif event == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield ChatChunk(kind="delta", text=delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta":
                        yield ChatChunk(kind="tool_call", tool_call={"index": data["index"],
                            "function": {"arguments": delta.get("partial_json", "")}})
                elif event == "message_delta":
                    reason = _finish_reason((data.get("delta") or {}).get("stop_reason"))
                    usage["completion_tokens"] = (data.get("usage") or {}).get("output_tokens", 0)
            if usage:
                usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                yield ChatChunk(kind="usage", usage=usage)
            yield ChatChunk(kind="finish", finish_reason=reason)


def _non_stream_chunks(data: dict) -> Iterator[ChatChunk]:
    for index, block in enumerate(data.get("content") or []):
        if block.get("type") == "text":
            yield ChatChunk(kind="delta", text=block.get("text", ""))
        elif block.get("type") == "tool_use":
            yield ChatChunk(kind="tool_call", tool_call={"index": index, "id": block["id"],
                "type": "function", "function": {"name": block["name"],
                "arguments": json.dumps(block.get("input") or {})}})
    usage = data.get("usage") or {}
    if usage:
        prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        yield ChatChunk(kind="usage", usage={"prompt_tokens": prompt, "completion_tokens": completion,
                                             "total_tokens": prompt + completion})
    yield ChatChunk(kind="finish", finish_reason=_finish_reason(data.get("stop_reason")))
