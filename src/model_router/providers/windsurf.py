"""Windsurf (Cognition) subscription: browser OAuth + Connect-RPC chat.

Python port of opencode-windsurf-auth (MIT, see THIRD_PARTY.md), the
cloud-direct path — no local language_server required:

  login   implicit grant at windsurf.com/windsurf/signin (client
          3GUryQ7ldAeKEuD2obYnppsnmj58eP5u; loopback /auth callback or
          show-auth-token manual paste) -> POST register.windsurf.com
          .../SeatManagementService/RegisterUser with the firebase token ->
          long-lived account api_key (OAuth-issued) + api_server_url.
  chat    POST {server.codeium.com}/exa.api_server_pb.ApiServerService/
          GetChatMessage — Connect-streaming protobuf (gzip frames).
  jwt     every RPC carries a short-lived user_jwt minted via
          exa.auth_pb.AuthService/GetUserJwt (cached ~24 min).
  catalog GetCascadeModelConfigs lists per-account model UIDs.

Wire notes that cost the reference implementation debugging time and are
preserved here: system messages must be inlined into the next user turn
(the cloud rejects source=SYSTEM); ChatMessage field #3 is the *visible*
text while #9 is reasoning; the account api_key (OAuth-issued) is the
persistent credential and the user_jwt is the per-RPC one; tool-call ids
only arrive on the first frame of each call.
"""

from __future__ import annotations

import json
import platform
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_mod
from collections.abc import Iterator

from ..errors import AuthFlowError, ReloginRequired, RouterError
from ..http import HttpStatusError, now_ms, post_json, request as http_request
from ..jwt_util import decode_jwt_payload
from ..oauth_common import prompt_manual_code, run_callback_flow
from ..proto_wire import (
    encode_double_field,
    encode_message,
    encode_string,
    encode_timestamp_body,
    encode_varint_field,
    field_text,
    frame_connect,
    iter_connect_frames,
    iter_fields,
)
from .base import ChatChunk, ChatRequest, ChatTransport, Provider
from ..store import Credentials

SIGNIN_URL = "https://windsurf.com/windsurf/signin"
OAUTH_CLIENT_ID = "3GUryQ7ldAeKEuD2obYnppsnmj58eP5u"
REGISTER_URL = ("https://register.windsurf.com/exa.seat_management_pb."
                "SeatManagementService/RegisterUser")
DEFAULT_API_HOST = "https://server.codeium.com"
GET_USER_JWT_PATH = "/exa.auth_pb.AuthService/GetUserJwt"
GET_CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
GET_MODELS_PATH = "/exa.api_server_pb.ApiServerService/GetCascadeModelConfigs"
CALLBACK_PORT = 48010
CALLBACK_PATH = "/auth"
WINDSURF_VERSION = "2.0.0"  # the cloud rejects unknown version strings
MAX_TOOL_DESC = 6998  # Codeium's per-description tool validator gate

SOURCE_USER, SOURCE_ASSISTANT, SOURCE_TOOL = 1, 2, 4
REQUEST_TYPE_CASCADE = 5

# Module-level caches keyed by (account_key, host): the ~24-min user_jwt and
# the per-conversation session/cascade ids (reused so the cloud's prompt
# cache actually hits across turns).
_jwt_cache: dict[tuple[str, str], tuple[str, int]] = {}
_session_cache: dict[tuple[str, str], dict[str, str]] = {}


def _os_string() -> str:
    return {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().lower())


def build_signin_url(state: str, redirect_uri: str) -> str:
    params = urllib.parse.urlencode({
        "response_type": "token",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "login",
        "redirect_parameters_type": "query",
    })
    return f"{SIGNIN_URL}?{params}"


def register_user(firebase_token: str) -> dict:
    """Exchange the browser token for the long-lived account key."""
    if not firebase_token:
        raise AuthFlowError("empty Windsurf sign-in token")
    try:
        resp = post_json(REGISTER_URL, {"firebase_id_token": firebase_token},
                         headers={"Connect-Protocol-Version": "1"}, timeout=30.0)
    except HttpStatusError as exc:
        raise AuthFlowError(f"Windsurf RegisterUser failed (HTTP {exc.status})") from None
    payload = resp.json()
    key = payload.get("api_key")  # Windsurf account api_key field (OAuth-issued)
    if not isinstance(key, str) or not key:
        raise AuthFlowError("Windsurf RegisterUser response lacked the "
                            "account api_key (OAuth-issued)")
    return {"api_key": key,  # Windsurf account api_key field (OAuth-issued)
            "name": payload.get("name") or "",
            "api_server_url": payload.get("api_server_url") or DEFAULT_API_HOST}


def build_metadata(account_key: str, session_id: str, request_id: int,
                   trigger_id: str, user_jwt: str = "") -> bytes:
    """exa.codeium_common_pb.Metadata — the load-bearing subset (reference §7)."""
    parts = [
        encode_string(1, "windsurf"),        # ide_name
        encode_string(2, WINDSURF_VERSION),  # extension_version
        encode_string(3, account_key),       # api_key (OAuth-issued account token)
        encode_string(4, "en"),              # locale
        encode_string(5, _os_string()),      # os
        encode_string(7, WINDSURF_VERSION),  # ide_version
        encode_varint_field(9, request_id),  # request_id (uint64, monotonic)
        encode_string(10, session_id),       # session_id
        encode_string(12, "windsurf"),       # extension_name
        encode_message(16, encode_timestamp_body()),  # ls_timestamp
        encode_string(25, trigger_id),       # trigger_id
        encode_string(26, "Unset"),          # plan_name
        encode_string(28, "windsurf"),       # ide_type
    ]
    if user_jwt:
        parts.append(encode_string(21, user_jwt))
    return b"".join(parts)


def mint_user_jwt(account_key: str, host: str) -> str:
    """GetUserJwt: unary proto RPC returning the ~24-min per-RPC token."""
    cache_key = (account_key, host)
    cached = _jwt_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    body = encode_message(1, build_metadata(
        account_key, str(uuid_mod.uuid4()), now_ms(), str(uuid_mod.uuid4())))
    try:
        resp = http_request("POST", host.rstrip("/") + GET_USER_JWT_PATH,
                            headers={"Content-Type": "application/proto",
                                     "Connect-Protocol-Version": "1"},
                            body=body, timeout=30.0)
    except HttpStatusError as exc:
        raise AuthFlowError(f"Windsurf GetUserJwt failed (HTTP {exc.status})") from None
    jwt_token = field_text(resp.body, 1)
    if not jwt_token.startswith("eyJ"):
        raise AuthFlowError("Windsurf GetUserJwt returned an unexpected body")
    payload = decode_jwt_payload(jwt_token) or {}
    expires = payload.get("exp")
    expires_at = expires if isinstance(expires, (int, float)) else time.time() + 600
    _jwt_cache[cache_key] = (jwt_token, expires_at)
    return jwt_token


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def collapse_system_into_user(messages: list[dict]) -> list[dict]:
    """Inline system messages into the next user turn (cloud rejects source=3)."""
    out: list[dict] = []
    pending: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            text = _text_of(msg.get("content"))
            if text:
                pending.append(text)
        elif role == "user" and pending:
            wrapped = "<system>\n" + "\n\n".join(pending) + "\n</system>\n" \
                + _text_of(msg.get("content"))
            out.append(dict(msg, content=wrapped))
            pending = []
        else:
            out.append(msg)
    if pending:
        out.append({"role": "user",
                    "content": "<system>\n" + "\n\n".join(pending) + "\n</system>"})
    return out


def _encode_tool_call(call: dict) -> bytes:
    function = call.get("function") or {}
    return b"".join([
        encode_string(1, call.get("id") or ""),
        encode_string(2, function.get("name") or ""),
        encode_string(3, function.get("arguments") or "{}"),
    ])


def _encode_prompt_message(msg: dict) -> bytes:
    role = msg.get("role")
    text = _text_of(msg.get("content"))
    source = {"user": SOURCE_USER, "assistant": SOURCE_ASSISTANT,
              "tool": SOURCE_TOOL}.get(role, SOURCE_USER)
    parts = [
        encode_varint_field(2, source),
        encode_string(3, text),
        encode_varint_field(4, max(1, len(text) // 4)),  # rough token estimate
        encode_varint_field(5, 1),                        # safe_for_code_telemetry
    ]
    if role == "tool":
        parts.append(encode_string(7, msg.get("tool_call_id") or ""))
    if role == "assistant":
        for call in msg.get("tool_calls") or []:
            parts.append(encode_message(6, _encode_tool_call(call)))
    return b"".join(parts)


def _encode_completion_config(temperature: float | None,
                              max_tokens: int | None) -> bytes:
    return b"".join([
        encode_varint_field(1, 1),
        encode_varint_field(2, 64000),                      # max_input_tokens
        encode_varint_field(3, max_tokens or 128000),       # max_output_tokens
        encode_double_field(5, temperature if temperature is not None else 0.7),
        encode_double_field(6, 0.95),                       # top_p
        encode_varint_field(7, 50),                         # top_k
        encode_double_field(8, 1.0),
        encode_double_field(11, 1.0),
    ])


def _encode_tool_def(tool: dict) -> bytes:
    function = tool.get("function") or {}
    description = function.get("description") or ""
    if len(description) > MAX_TOOL_DESC:
        description = description[:MAX_TOOL_DESC - 24] + "\n…(truncated for cloud)"
    return b"".join([
        encode_string(1, function.get("name") or ""),
        encode_string(2, description),
        encode_string(3, json.dumps(function.get("parameters") or {})),
    ])


def build_chat_request(account_key: str, user_jwt: str, model_uid: str,
                       messages: list[dict], tools: list[dict] | None,
                       cascade_id: str, prompt_id: str, session_id: str,
                       temperature: float | None = None,
                       max_tokens: int | None = None) -> bytes:
    """GetChatMessageRequest (field layout from captured LS traffic)."""
    parts = [encode_message(1, build_metadata(
        account_key, session_id, now_ms(), str(uuid_mod.uuid4()), user_jwt))]
    for msg in collapse_system_into_user(messages):
        parts.append(encode_message(3, _encode_prompt_message(msg)))
    parts.append(encode_varint_field(7, REQUEST_TYPE_CASCADE))
    parts.append(encode_message(8, _encode_completion_config(temperature, max_tokens)))
    for tool in tools or []:
        parts.append(encode_message(10, _encode_tool_def(tool)))
    parts.append(encode_string(16, cascade_id))
    parts.append(encode_string(21, model_uid))
    parts.append(encode_string(22, prompt_id))
    return b"".join(parts)


_FINISH_REASONS = {10: "tool_calls", 11: "content_filter", 1: "length", 3: "length"}


def decode_usage_block(payload: bytes) -> dict[str, int]:
    """UsageStats (#28): repeated entries with metric_id #5 and float value."""
    usage: dict[str, int] = {}
    for num, wire, value in iter_fields(payload):
        if num != 2 or wire != 2 or not isinstance(value, bytes):
            continue
        metric, amount = "", None
        for snum, swire, svalue in iter_fields(value):
            if snum == 5 and swire == 2 and isinstance(svalue, bytes):
                metric = svalue.decode("utf-8", "replace")
            elif snum == 4 and swire == 2 and isinstance(svalue, bytes):
                for dnum, dwire, dvalue in iter_fields(svalue):
                    if dnum == 2 and dwire == 5 and isinstance(dvalue, bytes):
                        amount = struct.unpack("<f", dvalue)[0]
        if metric and amount is not None:
            usage[metric] = int(round(amount))
    out: dict[str, int] = {}
    if "input_tokens" in usage:
        out["prompt_tokens"] = usage["input_tokens"]
    if "output_tokens" in usage:
        out["completion_tokens"] = usage["output_tokens"]
    if out:
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out


def decode_chat_frame(payload: bytes) -> Iterator[tuple[str, object]]:
    """One ChatMessage proto -> ('delta', str) | ('tool', id, name, args) |
    ('finish', reason) | ('usage', dict) tuples (visible text is field #3)."""
    for num, wire, value in iter_fields(payload):
        if num == 3 and wire == 2 and isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
            if text:
                yield "delta", text
        elif num == 5 and wire == 0:
            yield "finish", _FINISH_REASONS.get(value, "stop")
        elif num == 6 and wire == 2 and isinstance(value, bytes):
            call_id = name = args_delta = ""
            for snum, _swire, svalue in iter_fields(value):
                if snum in (1, 2, 3) and isinstance(svalue, bytes):
                    decoded = svalue.decode("utf-8", "replace")
                    if snum == 1:
                        call_id = decoded
                    elif snum == 2:
                        name = decoded
                    else:
                        args_delta = decoded
            if call_id or name or args_delta:
                yield "tool", (call_id, name, args_delta)
        elif num == 28 and wire == 2 and isinstance(value, bytes):
            usage = decode_usage_block(value)
            if usage:
                yield "usage", usage


class WindsurfTransport(ChatTransport):
    """Streams GetChatMessage Connect frames as normalized ChatChunks."""

    def __init__(self, account_key: str, request: ChatRequest,
                 api_host: str = DEFAULT_API_HOST, timeout: float = 300.0):
        self.account_key = account_key
        self.request = request
        self.api_host = api_host.rstrip("/")
        self.timeout = timeout

    def run(self) -> Iterator[ChatChunk]:
        user_jwt = mint_user_jwt(self.account_key, self.api_host)
        session = _session_cache.setdefault((self.account_key, self.api_host), {
            "session_id": str(uuid_mod.uuid4()),
            "cascade_id": str(uuid_mod.uuid4())})
        body = frame_connect(build_chat_request(
            self.account_key, user_jwt, self.request.model,
            self.request.messages, self.request.tools,
            session["cascade_id"], str(uuid_mod.uuid4()), session["session_id"],
            temperature=self.request.temperature, max_tokens=self.request.max_tokens))
        url = self.api_host + GET_CHAT_PATH
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/connect+proto",
            "Connect-Protocol-Version": "1",
            "Connect-Content-Encoding": "gzip",
            "Connect-Accept-Encoding": "gzip",
        })
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise HttpStatusError("POST", url, exc.code) from None

        call_index: list[str] = []  # announced call ids, in order
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        saw_eos = False
        with resp:
            for flags, payload in iter_connect_frames(resp):
                if flags & 0x02:
                    saw_eos = True
                    error = _trailer_error(payload)
                    if error:
                        raise _trailer_exc(error, self.request.model)
                    continue
                for kind, value in decode_chat_frame(payload):
                    if kind == "tool":
                        yield _tool_call_chunk(value, call_index)
                    elif kind == "finish":
                        finish_reason = value
                    elif kind == "usage":
                        usage = value
                    else:
                        yield ChatChunk(kind="delta", text=value)
        if not saw_eos:
            raise RouterError("windsurf stream ended without an end-of-stream frame")
        if usage:
            yield ChatChunk(kind="usage", usage=usage)
        if call_index and finish_reason in (None, "stop"):
            finish_reason = "tool_calls"
        yield ChatChunk(kind="finish", finish_reason=finish_reason or "stop")


def _tool_call_chunk(raw: tuple[str, str, str], call_index: list[str]) -> ChatChunk:
    """Wire tool-call deltas -> announce (id+name) / arguments chunk pairs."""
    call_id, name, arguments = raw
    if call_id:
        index = len(call_index)
        call_index.append(call_id)
        return ChatChunk(kind="tool_call", tool_call={
            "index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}})
    index = max(len(call_index) - 1, 0)
    return ChatChunk(kind="tool_call", tool_call={
        "index": index, "function": {"arguments": arguments}})


def _trailer_error(payload: bytes) -> dict | None:
    text = payload.decode("utf-8", "replace")
    if not text or '"error"' not in text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"code": "", "message": text[:300]}
    error = parsed.get("error") or {}
    return {"code": error.get("code", ""), "message": error.get("message", text[:300])}


def _trailer_exc(error: dict, model_uid: str) -> RouterError:
    message = error.get("message") or "unknown error"
    if error.get("code") == "permission_denied" and "internal error" in message:
        return RouterError(
            f'windsurf denied model "{model_uid}" — it is likely not enabled '
            f"for your account tier (check your Windsurf plan or pick another "
            f"UID); cloud said: {message}")
    return RouterError(f"windsurf chat failed ({error.get('code') or 'error'}): "
                       f"{message}")


class WindsurfProvider(Provider):
    id = "windsurf"
    display = "Windsurf (Cognition)"

    def __init__(self, progress=None, gateway_base_url: str = DEFAULT_API_HOST):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        state = str(uuid_mod.uuid4())

        def build(flow_state: str, redirect_uri: str):
            return build_signin_url(flow_state, redirect_uri), (
                "Complete Windsurf sign-in in your browser. The page will "
                "redirect back to this machine when done."
            )

        result, _redirect = run_callback_flow(
            port=CALLBACK_PORT, path=CALLBACK_PATH, build_auth_url=build,
            open_browser=kwargs.get("open_browser", True),
            token_params=("firebase_id_token", "access_token", "token"),
        )
        if result is not None:
            token = result.code
        else:
            # Manual paste: the sign-in page renders the token when the
            # redirect target is the literal "show-auth-token".
            url = build_signin_url(state, "show-auth-token")
            print(f"Open this URL and paste the token shown on the page:\n  {url}\n")
            token = prompt_manual_code(self.id)
        payload = register_user(token)
        print(f"Windsurf account: {payload['name'] or 'unknown'}")
        return Credentials(
            access=payload["api_key"],  # Windsurf account api_key (OAuth-issued)
            account_id=payload["name"],
            email="",
            extra={"api_server_url": payload["api_server_url"] or DEFAULT_API_HOST,
                   "name": payload["name"]},
        )

    def refresh(self, creds: Credentials) -> Credentials:
        # The account key is long-lived; only the per-RPC user_jwt expires,
        # and that is reminted inside the transport. A dead key means
        # logging in again.
        raise ReloginRequired(self.id)

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        host = (creds.extra or {}).get("api_server_url") or self.gateway_base_url
        return WindsurfTransport(creds.access, request, api_host=host)

    def list_models(self, creds: Credentials) -> list[str]:
        host = ((creds.extra or {}).get("api_server_url") or self.gateway_base_url).rstrip("/")
        try:
            user_jwt = mint_user_jwt(creds.access, host)
            body = encode_message(1, build_metadata(
                creds.access, str(uuid_mod.uuid4()), now_ms(),
                str(uuid_mod.uuid4()), user_jwt))
            resp = http_request("POST", host + GET_MODELS_PATH,
                                headers={"Content-Type": "application/proto",
                                         "Connect-Protocol-Version": "1"},
                                body=body, timeout=30.0)
        except (HttpStatusError, AuthFlowError, OSError):
            return []
        uids: list[str] = []
        for num, wire, value in iter_fields(resp.body):
            if num != 1 or wire != 2 or not isinstance(value, bytes):
                continue
            model_uid = field_text(value, 22)
            disabled = any(n == 4 and w == 0 for n, w, _v in iter_fields(value))
            if model_uid and not disabled:
                uids.append(model_uid)
        return sorted(uids)
