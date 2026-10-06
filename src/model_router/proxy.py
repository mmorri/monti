"""OpenAI-compatible proxy: GET /v1/models, POST /v1/chat/completions (SSE)."""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import VIRTUAL_MODELS
from .classifier import Classifier
from .config import TIERS, RouterConfig
from .errors import (NoSubscriptionAuth, ProviderDisabled, ProviderTransportUnavailable,
                     ReloginRequired, RouterError)
from .gateway import Gateway, quota_exhausted, retryable_failure
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
        from .providers import REGISTRY

        status = self.gateway.tier_status()
        if not self.store.providers():
            raise NoSubscriptionAuth(
                "no subscription auth found; run `monti login <provider>` first "
                f"(providers: {', '.join(sorted(REGISTRY))})")
        for tier, entry in status.items():
            ready = [c for c in entry["candidates"] if c["ok"]]
            if ready:
                extra = f" (+{len(ready) - 1} more wallet(s))" if len(ready) > 1 else ""
                print(f"[{tier}] {ready[0]['provider']}/{ready[0]['model']}: ready{extra}")
            else:
                reasons = "; ".join(
                    f"{c['provider']}: {c['transport']}" for c in entry["candidates"])
                print(f"[{tier}] disabled ({reasons})")
        if self.config.require_all_tiers:
            missing = {t: e for t, e in status.items() if not e["ok"]}
            if missing:
                logins = sorted({c["provider"] for e in missing.values()
                                 for c in e["candidates"] if c["transport"] == "no-auth"})
                details = "; ".join(
                    f"{tier} tier (" + ", ".join(
                        f"{c['provider']}: {c['transport']}" for c in e["candidates"]) + ")"
                    for tier, e in missing.items())
                hint = ("; log in with " + ", ".join(f"`monti login {p}`" for p in logins)
                        if logins else "")
                raise NoSubscriptionAuth(
                    f"not all tiers are ready ({details}){hint} "
                    "(or set policy.require_all_tiers: false to allow degraded starts)")
        return status

    def available(self, provider_id: str) -> bool:
        """A wallet is usable when logged in, not cooling down, and its chat
        transport is ported. Constructors perform no network, so this is cheap."""
        if self.gateway.cooling(provider_id):
            return False
        creds = self.store.load(provider_id)
        if not creds:
            return False
        try:
            self.gateway.provider_for(provider_id).open_chat(
                creds, ChatRequest(messages=[], model=""))
        except ProviderTransportUnavailable:
            return False
        except Exception:  # noqa: BLE001 — constructors don't validate; fail at runtime
            return True
        return True

    # -- classifier model ---------------------------------------------
    def _classifier_wallets(self) -> list:
        """Candidate wallets for classification, free-first.

        The configured pool leads (Muse contributor tier = near-free quota,
        then high-quota flash wallets); everyday-tier wallets back it up so
        a cooled-down classifier never blocks routing.
        """
        seen: set[tuple[str, str]] = set()
        wallets = []
        backups = self.config.tiers.get("everyday", [])
        for candidate in list(self.config.classifier) + backups:
            key = (candidate.provider, candidate.model)
            if key not in seen:
                wallets.append(candidate)
                seen.add(key)
        return wallets

    def _classifier_model_fn(self, excerpt: str):
        from .classifier import PROMPT

        candidates = [w for w in self._classifier_wallets()
                      if self.available(w.provider)]
        last_error: Exception | None = None
        for candidate in candidates:
            request = ChatRequest(
                messages=[{"role": "user", "content": f"{PROMPT}\n\nTask:\n{excerpt}"}],
                model=candidate.model, stream=False, max_tokens=16, temperature=0,
            )
            try:
                transport, reopen = self.gateway.chat(candidate.provider, request)
            except (RouterError, OSError):
                continue
            try:
                chunks = list(transport.run())
            except HttpStatusError as exc:
                if exc.status != 401:
                    last_error = exc
                    continue
                try:
                    chunks = list(reopen().run())
                except (HttpStatusError, RouterError, OSError) as retry_exc:
                    last_error = retry_exc
                    continue
            except (RouterError, OSError) as exc:
                last_error = exc
                continue
            return "".join(c.text for c in chunks if c.kind == "delta")
        # Classifier unreachable: heuristic fallback covers the request, but
        # surface the failure when nothing was even available.
        if not candidates:
            raise NoSubscriptionAuth(
                "no logged-in provider available for classification; "
                "run `monti login <provider>` first")
        if last_error is not None:
            raise last_error
        raise NoSubscriptionAuth("no usable wallet for classification")

    # -- chat -----------------------------------------------------------
    def _collect(self, request: ChatRequest, provider_id: str) -> list:
        """One provider call with the single 401 refresh+retry."""
        transport, reopen = self.gateway.chat(provider_id, request)
        try:
            return list(transport.run())
        except HttpStatusError as exc:
            if exc.status != 401:
                raise
            return list(reopen().run())

    def _attempt_plan(self, route) -> list:
        """Same-tier wallets first, then higher rungs (re-checked live)."""
        return self.router.ladder_from(route.tier, route.candidate_index,
                                       self.available)

    def _note_failure(self, candidate, exc: Exception) -> tuple[bool, str | None]:
        """Common failure handling: returns (continue_ladder, skip_provider).

        skip_provider names a dead wallet whose remaining entries to skip;
        None means only this candidate failed."""
        if isinstance(exc, ReloginRequired):
            return True, candidate.provider  # dead grant: skip the whole wallet
        if quota_exhausted(exc):
            self.gateway.mark_cooldown(candidate.provider)
        if retryable_failure(exc) or quota_exhausted(exc):
            return True, None
        return False, None

    def _request_for(self, body: dict, model: str, stream: bool) -> ChatRequest:
        return ChatRequest(
            messages=body.get("messages", []), model=model, stream=stream,
            temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
            tools=body.get("tools"), tool_choice=body.get("tool_choice"),
        )

    def chat_completion(self, body: dict, headers: dict[str, str]) -> dict:
        """Non-streaming completion; raises RouterError/HttpStatusError."""
        started = time.time()
        model = body.get("model", "router-auto")
        header_mode = _header(headers, "x-router-mode")
        session = _header(headers, "x-router-session")
        route, verdict = self.router.route(
            body.get("messages", []), model, header_mode, session,
            available=self.available)
        record = {"model": model, "mode": route.mode, "verdict": verdict.verdict,
                  "tier": route.tier, "provider": route.candidate.provider,
                  "provider_model": route.candidate.model,
                  "escalated": route.escalated, "stream": False}
        attempts_log: list[dict] = []
        dead: set[str] = set()
        chunks = used = None
        last_error: Exception | None = None
        for tier_name, candidate, _index in self._attempt_plan(route):
            if candidate.provider in dead or not self.available(candidate.provider):
                continue
            try:
                chunks = self._collect(
                    self._request_for(body, candidate.model, False), candidate.provider)
            except (RouterError, HttpStatusError, OSError) as exc:
                last_error = exc
                attempts_log.append({"tier": tier_name, "provider": candidate.provider,
                                     "model": candidate.model, "error": str(exc)})
                proceed, skip = self._note_failure(candidate, exc)
                if skip:
                    dead.add(skip)
                if not proceed:
                    break
                continue
            used = (tier_name, candidate)
            break
        if used is None:
            record.update({"ok": False, "attempts": attempts_log,
                           "error": str(last_error) if last_error else "no available candidate",
                           "latency_ms": int((time.time() - started) * 1000)})
            self.requests.append(record)
            if last_error is not None:
                raise last_error
            raise NoSubscriptionAuth("no tier has a usable candidate")
        tier_name, candidate = used
        if attempts_log:
            record.update({"tier": tier_name, "provider": candidate.provider,
                           "provider_model": candidate.model, "attempts": attempts_log})
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
            body.get("messages", []), model, header_mode, session,
            available=self.available)
        record = {"model": model, "mode": route.mode, "verdict": verdict.verdict,
                  "tier": route.tier, "provider": route.candidate.provider,
                  "provider_model": route.candidate.model,
                  "escalated": route.escalated, "stream": True}
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        emitted = False

        def chunk(delta: dict | None, finish: str | None = None) -> bytes:
            payload = {"id": completion_id, "object": "chat.completion.chunk",
                       "created": created, "model": model,
                       "choices": [{"index": 0, "delta": delta or {},
                                    "finish_reason": finish}]}
            return f"data: {json.dumps(payload)}\n\n".encode()

        def relay(transport, reopen) -> None:
            nonlocal emitted

            def send(event) -> None:
                nonlocal emitted
                if event.kind == "delta" and event.text:
                    emitted = True
                    emit(chunk({"content": event.text}))
                elif event.kind == "tool_call":
                    emitted = True
                    emit(chunk({"tool_calls": [event.tool_call]}))
                elif event.kind == "finish":
                    emit(chunk(None, event.finish_reason))
                elif event.kind == "usage":
                    record["usage"] = event.usage

            stream = transport.run()
            try:
                while True:
                    try:
                        event = next(stream)
                    except StopIteration:
                        break
                    send(event)
            except HttpStatusError as exc:
                if exc.status != 401 or emitted:
                    raise
                for event in reopen().run():
                    send(event)

        attempts_log: list[dict] = []
        dead: set[str] = set()
        used = None
        last_error: Exception | None = None
        try:
            emit(chunk({"role": "assistant"}))
            for tier_name, candidate, _index in self._attempt_plan(route):
                if candidate.provider in dead or not self.available(candidate.provider):
                    continue
                try:
                    transport, reopen = self.gateway.chat(
                        candidate.provider,
                        self._request_for(body, candidate.model, True))
                    relay(transport, reopen)
                except (RouterError, HttpStatusError, OSError) as exc:
                    last_error = exc
                    # Only switch wallets while nothing has reached the client.
                    if emitted:
                        raise
                    attempts_log.append({"tier": tier_name, "provider": candidate.provider,
                                         "model": candidate.model, "error": str(exc)})
                    proceed, skip = self._note_failure(candidate, exc)
                    if skip:
                        dead.add(skip)
                    if not proceed:
                        raise
                    continue
                used = (tier_name, candidate)
                break
            if used is None:
                if last_error is not None:
                    raise last_error
                raise NoSubscriptionAuth("no tier has a usable candidate")
            emit(b"data: [DONE]\n\n")
        except (RouterError, HttpStatusError, OSError) as exc:
            record.update({"ok": False, "attempts": attempts_log,
                           "error": str(exc),
                           "latency_ms": int((time.time() - started) * 1000)})
            self.requests.append(record)
            raise
        if attempts_log:
            tier_name, candidate = used
            record.update({"tier": tier_name, "provider": candidate.provider,
                           "provider_model": candidate.model,
                           "attempts": attempts_log})
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
