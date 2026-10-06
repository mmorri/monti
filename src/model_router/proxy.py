"""OpenAI-compatible proxy: GET /v1/models, POST /v1/chat/completions (SSE)."""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import VIRTUAL_MODELS
from .classifier import Classifier
from .config import RouterConfig
from .errors import NoSubscriptionAuth, ProviderDisabled, ReloginRequired, RouterError
from .gateway import Gateway
from .http import HttpStatusError
from .log import ClassificationLog, RequestLog
from .providers.base import ChatRequest
from .router import Router
from .store import TokenStore


class ProxyApp:
    def __init__(self, config: RouterConfig, store: TokenStore | None = None,
                 log_dir: Path | None = None):
        self.config = config
        self.store = store or TokenStore()
        self.gateway = Gateway(config, self.store)
        directory = log_dir or self._default_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.requests = RequestLog(directory / "requests.jsonl")
        self.classifier = Classifier(
            model_fn=self._classifier_model_fn,
            log_fn=ClassificationLog(directory / "classifications.jsonl"),
        )
        self.router = Router(config, self.classifier)

    def _default_log_dir(self) -> Path:
        if self.config.log_dir:
            return Path(self.config.log_dir).expanduser()
        return Path.home() / ".config" / "model-router" / "logs"

    # -- startup ------------------------------------------------------
    def check_auth(self) -> dict[str, dict]:
        status = self.gateway.tier_status()
        if not self.store.providers():
            raise NoSubscriptionAuth(
                "no subscription auth found; run `model-router login <provider>` first "
                "(providers: anthropic, copilot, cursor, kimi, muse, openai, "
                "windsurf, xai, zai)")
        for tier, entry in status.items():
            if not entry["ok"]:
                print(f"[{tier}] {entry['provider']}: disabled ({entry['transport']})")
            else:
                print(f"[{tier}] {entry['provider']}/{entry['model']}: ready")
        if self.config.require_all_tiers:
            missing = {t: e for t, e in status.items() if not e["ok"]}
            if missing:
                details = "; ".join(
                    f"{tier} tier (provider '{e['provider']}'): {e['transport']}"
                    for tier, e in missing.items())
                logins = ", ".join(
                    f"`model-router login {e['provider']}`" for e in missing.values())
                raise NoSubscriptionAuth(
                    f"not all tiers are ready ({details}); log in with {logins} "
                    "(or set policy.require_all_tiers: false to allow degraded starts)")
        return status

    # -- classifier model ---------------------------------------------
    def _classifier_model_fn(self, excerpt: str):
        from .classifier import PROMPT

        tier = self.config.fast
        request = ChatRequest(
            messages=[{"role": "user", "content": f"{PROMPT}\n\nTask:\n{excerpt}"}],
            model=tier.model, stream=False, max_tokens=16, temperature=0,
        )
        transport, reopen = self.gateway.chat(tier.provider, request)
        try:
            chunks = list(transport.run())
        except HttpStatusError as exc:
            if exc.status != 401:
                raise
            chunks = list(reopen().run())
        return "".join(c.text for c in chunks if c.kind == "delta")

    # -- chat -----------------------------------------------------------
    def chat_completion(self, body: dict, headers: dict[str, str]) -> dict:
        """Non-streaming completion; raises RouterError/HttpStatusError."""
        started = time.time()
        model = body.get("model", "router-auto")
        header_mode = _header(headers, "x-router-mode")
        session = _header(headers, "x-router-session")
        route, verdict = self.router.route(
            body.get("messages", []), model, header_mode, session)
        tier = getattr(self.config, route.tier)
        record = {"model": model, "mode": route.mode, "verdict": verdict.verdict,
                  "tier": route.tier, "provider": tier.provider,
                  "provider_model": tier.model, "escalated": route.escalated,
                  "stream": False}
        try:
            request = ChatRequest(
                messages=body.get("messages", []), model=tier.model, stream=False,
                temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
                tools=body.get("tools"), tool_choice=body.get("tool_choice"),
            )
            transport, reopen = self.gateway.chat(tier.provider, request)
            try:
                chunks = list(transport.run())
            except HttpStatusError as exc:
                if exc.status != 401:
                    raise
                chunks = list(reopen().run())
        except (RouterError, HttpStatusError) as exc:
            record.update({"ok": False, "error": str(exc),
                           "latency_ms": int((time.time() - started) * 1000)})
            self.requests.append(record)
            raise
        text = "".join(c.text for c in chunks if c.kind == "delta")
        calls = {}
        for event in chunks:
            if event.kind == "tool_call":
                delta = event.tool_call
                call = calls.setdefault(delta.get("index", 0), {"id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if delta.get("id"):
                    call["id"] = delta["id"]
                for key in ("name", "arguments"):
                    call["function"][key] += (delta.get("function") or {}).get(key) or ""
        usage = {}
        for c in chunks:
            if c.kind == "usage":
                usage = c.usage
        finish = next((c.finish_reason for c in reversed(chunks) if c.kind == "finish"), "stop")
        record.update({"ok": True, "latency_ms": int((time.time() - started) * 1000),
                       "usage": usage})
        self.requests.append(record)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        response = {
            "id": completion_id, "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": finish}],
        }
        if calls:
            response["choices"][0]["message"]["tool_calls"] = [calls[i] for i in sorted(calls)]
        if usage:
            response["usage"] = {"prompt_tokens": usage.get("prompt_tokens", 0),
                                 "completion_tokens": usage.get("completion_tokens", 0),
                                 "total_tokens": usage.get("total_tokens", 0)}
        return response

    def chat_completion_stream(self, body: dict, headers: dict[str, str], emit):
        """Streaming completion; emit(bytes) sends SSE payload; raises on setup errors."""
        started = time.time()
        model = body.get("model", "router-auto")
        header_mode = _header(headers, "x-router-mode")
        session = _header(headers, "x-router-session")
        route, verdict = self.router.route(
            body.get("messages", []), model, header_mode, session)
        tier = getattr(self.config, route.tier)
        record = {"model": model, "mode": route.mode, "verdict": verdict.verdict,
                  "tier": route.tier, "provider": tier.provider,
                  "provider_model": tier.model, "escalated": route.escalated,
                  "stream": True}
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        def chunk(delta: dict | None, finish: str | None = None) -> bytes:
            payload = {"id": completion_id, "object": "chat.completion.chunk",
                       "created": created, "model": model,
                       "choices": [{"index": 0, "delta": delta or {},
                                    "finish_reason": finish}]}
            return f"data: {json.dumps(payload)}\n\n".encode()

        try:
            request = ChatRequest(
                messages=body.get("messages", []), model=tier.model, stream=True,
                temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
                tools=body.get("tools"), tool_choice=body.get("tool_choice"),
            )
            transport, reopen = self.gateway.chat(tier.provider, request)
            emit(chunk({"role": "assistant"}))
            try:
                stream = transport.run()
                while True:
                    try:
                        event = next(stream)
                    except StopIteration:
                        break
                    if event.kind == "delta" and event.text:
                        emit(chunk({"content": event.text}))
                    elif event.kind == "tool_call":
                        emit(chunk({"tool_calls": [event.tool_call]}))
                    elif event.kind == "finish":
                        emit(chunk(None, event.finish_reason))
                    elif event.kind == "usage":
                        record["usage"] = event.usage
            except HttpStatusError as exc:
                if exc.status != 401:
                    raise
                for event in reopen().run():
                    if event.kind == "delta" and event.text:
                        emit(chunk({"content": event.text}))
                    elif event.kind == "tool_call":
                        emit(chunk({"tool_calls": [event.tool_call]}))
                    elif event.kind == "finish":
                        emit(chunk(None, event.finish_reason))
                    elif event.kind == "usage":
                        record["usage"] = event.usage
            emit(b"data: [DONE]\n\n")
        except (RouterError, HttpStatusError) as exc:
            record.update({"ok": False, "error": str(exc),
                           "latency_ms": int((time.time() - started) * 1000)})
            self.requests.append(record)
            raise
        record.update({"ok": True, "latency_ms": int((time.time() - started) * 1000)})
        self.requests.append(record)

    def models(self) -> dict:
        return {"object": "list", "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "model-router"}
            for name in VIRTUAL_MODELS
        ]}


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def error_payload(exc: Exception) -> tuple[int, dict]:
    if isinstance(exc, ReloginRequired):
        return 401, {"error": {"message": str(exc), "type": "auth",
                               "code": "relogin_required"}}
    if isinstance(exc, ProviderDisabled):
        return 503, {"error": {"message": str(exc), "type": "provider",
                               "code": "provider_unavailable"}}
    if isinstance(exc, HttpStatusError):
        return 502, {"error": {"message": str(exc), "type": "upstream",
                               "code": "upstream_error"}}
    if isinstance(exc, RouterError):
        return 400, {"error": {"message": str(exc), "type": "router", "code": "router"}}
    if isinstance(exc, ValueError):
        return 400, {"error": {"message": str(exc), "type": "request", "code": "bad_request"}}
    return 500, {"error": {"message": f"internal error: {exc}", "type": "internal",
                            "code": "internal"}}


def make_handler(app: ProxyApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "model-router/0.1"

        def log_message(self, *args):  # noqa: ANN002, ANN003
            pass

        def _send_json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] == "/v1/models":
                self._send_json(200, app.models())
            else:
                self._send_json(404, {"error": {"message": "not found"}})

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": "not found"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception as exc:
                self._send_json(400, {"error": {"message": f"invalid JSON: {exc}"}})
                return
            if not isinstance(body.get("model"), str) or not isinstance(
                    body.get("messages"), list):
                self._send_json(400, {"error": {
                    "message": "request needs model (string) + messages (array)"}})
                return
            headers = {k: v for k, v in self.headers.items()}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    app.chat_completion_stream(body, headers, self.wfile.write)
                except Exception as exc:  # noqa: BLE001
                    status, payload = error_payload(exc)
                    raw = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            try:
                self._send_json(200, app.chat_completion(body, headers))
            except Exception as exc:  # noqa: BLE001
                status, payload = error_payload(exc)
                self._send_json(status, payload)

    return Handler


def serve(app: ProxyApp, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    app.check_auth()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"model-router listening on http://{host}:{port}")
    return server
