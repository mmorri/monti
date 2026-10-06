"""Committed tests for model-router (run: python -m pytest tests/ -q)."""

import base64
import hashlib
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from model_router import VIRTUAL_MODELS
from model_router.classifier import Classifier, heuristic_classify, parse_verdict
from model_router.config import RouterConfig
from model_router.errors import ProviderTransportUnavailable, ReloginRequired, RouterError
from model_router.gateway import Gateway
from model_router.http import HttpStatusError
from model_router.pkce import challenge_for, generate_pkce
from model_router.providers import REGISTRY, create
from model_router.providers.anthropic import build_auth_url as anthropic_url, to_messages_payload
from model_router.providers.base import ChatRequest, ChatTransport
from model_router.providers.cursor import (
    build_login_url,
    credentials_from_tokens,
)
from model_router.providers.copilot import device_verify_url
from model_router.proxy import ProxyApp, make_handler
from model_router.router import EscalationTracker, Router
from model_router.store import Credentials, TokenStore
from model_router.tools import TOOLS


# -- pkce -----------------------------------------------------------------
def test_pkce_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_generate_shape():
    pkce = generate_pkce()
    assert len(pkce.verifier) >= 128  # 96 bytes base64url
    assert challenge_for(pkce.verifier) == pkce.challenge


# -- cursor auth params -----------------------------------------------------
def test_cursor_login_url_carries_challenge_only():
    url = build_login_url("CHALLENGE", "UUID")
    assert url.startswith("https://cursor.com/loginDeepControl?")
    assert "challenge=CHALLENGE" in url and "uuid=UUID" in url
    assert "verifier" not in url


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


def test_cursor_credentials_identity_and_skew():
    access = _jwt({"sub": "user-1", "email": "A@X.io", "exp": 1_700_000_000})
    creds = credentials_from_tokens(access, "refresh")
    assert creds.account_id == "user-1"
    assert creds.email == "a@x.io"
    assert creds.expires == 1_700_000_000 * 1000 - 5 * 60 * 1000


# -- anthropic / copilot url shapes ------------------------------------------
def test_anthropic_auth_url_params():
    url = anthropic_url("CH", "ST", "http://127.0.0.1:54545/callback")
    assert url.startswith("https://claude.ai/oauth/authorize?")
    for needle in ("code_challenge=CH", "code_challenge_method=S256", "state=ST",
                   "client_id=9d1c250a", "response_type=code"):
        assert needle in url


def test_anthropic_messages_translation():
    body = to_messages_payload(ChatRequest(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ], model="m", stream=False))
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 4096


def test_copilot_verify_url_rejects_junk():
    assert device_verify_url("ABCD-1234").startswith("https://github.com/login/device?")
    with pytest.raises(Exception):
        device_verify_url("https://evil.example/x")


# -- store -------------------------------------------------------------------
def test_store_roundtrip_mode_0600(tmp_path: Path):
    store = TokenStore(tmp_path)
    creds = Credentials(access="a", refresh="r", expires=1, account_id="u")
    path = store.save("cursor", creds)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.load("cursor") == creds
    assert store.providers() == ["cursor"]
    assert store.clear("cursor") is True
    assert store.load("cursor") is None


# -- classifier ----------------------------------------------------------------
def test_spec_routing_examples():
    assert heuristic_classify("user: Fix this typo in README.md") == "trivial"
    assert heuristic_classify(
        "user: Design a plugin architecture for a multi-tenant billing system") == "hard"


def test_parse_verdict():
    assert parse_verdict("Hard.") == "hard"
    assert parse_verdict("  EASY\n") == "easy"
    assert parse_verdict("nope") is None


def test_classifier_falls_back_and_logs():
    records = []
    clf = Classifier(model_fn=lambda _t: (_ for _ in ()).throw(RuntimeError("down")),
                     log_fn=records.append)
    result = clf.classify("rename this variable")
    assert (result.verdict, result.source) == ("trivial", "heuristic")
    assert records and records[0]["verdict"] == "trivial"


def test_classifier_uses_model():
    clf = Classifier(model_fn=lambda _t: "hard", log_fn=lambda _r: None)
    assert clf.classify("anything").verdict == "hard"


# -- routing -------------------------------------------------------------------
def _router(**overrides):
    cfg = RouterConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))


def test_auto_routes_by_verdict():
    router = _router()
    route, _ = router.route([{"role": "user", "content": "fix typo"}], "router-auto")
    assert route.tier == "fast"
    route, _ = router.route([{"role": "user", "content": "design architecture"}], "router-auto")
    assert route.tier == "strong"


def test_header_and_model_overrides():
    router = _router()
    msgs = [{"role": "user", "content": "design architecture"}]
    route, _ = router.route(msgs, "router-fast")
    assert route.tier == "fast"
    route, _ = router.route(msgs, "router-auto", header_mode="strong-only")
    assert (route.tier, route.mode) == ("strong", "strong-only")
    with pytest.raises(ValueError):
        router.route(msgs, "router-auto", header_mode="bogus")


def _tool_error(n: int):
    return [{"role": "user", "content": "do it"},
            *[{"role": "tool", "content": f"Error: boom {i}"} for i in range(n)]]


def test_weak_first_escalate_on_tool_errors():
    cfg = RouterConfig()
    router = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    route, _ = router.route(_tool_error(3), "router-auto",
                            header_mode="weak-first-escalate", session="s1")
    assert route.tier == "strong" and route.escalated
    # sticky within the session
    route, _ = router.route([{"role": "user", "content": "fix typo"}], "router-auto",
                            header_mode="weak-first-escalate", session="s1")
    assert route.tier == "strong"


def test_weak_first_escalate_signal_and_no_session():
    cfg = RouterConfig()
    tracker = EscalationTracker(cfg)
    assert tracker.observe_request("s9", [{"role": "user", "content": "this isn't working"}])
    assert tracker.observe_request(None, _tool_error(9)) is False


# -- providers ------------------------------------------------------------------
def test_registry_has_nine():
    assert sorted(REGISTRY) == ["anthropic", "copilot", "cursor", "kimi",
                                "muse", "openai", "windsurf", "xai", "zai"]
    for provider_id in REGISTRY:
        assert create(provider_id).id == provider_id


def test_unported_transports_fail_closed():
    creds = Credentials(access="a", refresh="r")
    req = ChatRequest(messages=[], model="m")
    with pytest.raises(ProviderTransportUnavailable):
        create("cursor").open_chat(creds, req)
    # openai now speaks the Responses API transport
    assert create("openai").open_chat(creds, req).url.endswith("/codex/responses")


def test_muse_device_flow_mints_subscription_key(monkeypatch):
    from model_router.providers import meta as meta_provider

    calls = []

    def fake_post_form(url, fields, headers=None, timeout=0.0):
        from types import SimpleNamespace

        calls.append(url)
        payloads = {
            "https://auth.meta.com/oidc/device/authorization/":
                {"device_code": "dc", "user_code": "ABCD-EFGH",
                 "verification_uri": "https://auth.meta.com/device", "interval": 0},
            "https://auth.meta.com/oidc/device/token/":
                {"access_token": "dca-token", "expires_in": 3600},
        }
        return SimpleNamespace(json=lambda: payloads[url])

    def fake_mint(dca_token):
        assert dca_token == "dca-token"
        return {"api_key": "muse-minted-key", "base_url": "https://api.meta.ai/v1",
                "user_email": "A@B.com", "subs_tier_name": "Standard",
                "is_subs_active": True}

    monkeypatch.setattr(meta_provider, "post_form", fake_post_form)
    monkeypatch.setattr(meta_provider, "mint_subscription_key", fake_mint)
    creds = meta_provider.MuseProvider().login()
    assert creds.access == "muse-minted-key" and creds.email == "a@b.com"
    assert creds.extra["dca_token"] == "dca-token"
    assert creds.extra["tier"] == "Standard"
    # refresh re-mints from the stored device token; without one it fails closed
    renewed = meta_provider.MuseProvider().refresh(creds)
    assert renewed.access == "muse-minted-key"
    with pytest.raises(ReloginRequired):
        meta_provider.MuseProvider().refresh(Credentials(access="x"))


def test_zai_plan_key_login_is_interactive_only(monkeypatch):
    import getpass

    from model_router.providers.zai import ZaiProvider

    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_k: "  plan-secret-1 ")
    creds = ZaiProvider().login()
    assert creds.access == "plan-secret-1" and creds.extra == {"plan": "glm_coding"}
    with pytest.raises(ReloginRequired):
        ZaiProvider().refresh(creds)
    with pytest.raises(ValueError):
        ZaiProvider().login(plan_key="has whitespace")


def test_gateway_401_refresh_once_then_relogin(tmp_path: Path):
    store = TokenStore(tmp_path)
    store.save("xai", Credentials(access="old", refresh="dead", expires=0))

    class FlakyProvider(create("xai").__class__):
        def refresh(self, creds):
            raise ReloginRequired(self.id)

    cfg = RouterConfig()
    gateway = Gateway(cfg, store)
    gateway._providers["xai"] = FlakyProvider()
    _transport, reopen = gateway.chat("xai", ChatRequest(messages=[], model="m"))
    with pytest.raises(ReloginRequired, match="RELOGIN_REQUIRED:xai"):
        reopen()


def test_gateway_serializes_concurrent_refreshes(tmp_path: Path):
    import threading

    store = TokenStore(tmp_path)
    store.save("kimi", Credentials(access="expired", refresh="r", expires=1))
    refreshes = []

    class CountingProvider(create("kimi").__class__):
        def refresh(self, creds):
            refreshes.append(1)
            from model_router.http import now_ms

            import time as _time

            _time.sleep(0.05)  # widen the race window
            # far-future expiry so later loads skip refresh entirely
            return Credentials(access=f"fresh-{len(refreshes)}", refresh="r",
                               expires=now_ms() + 3600_000)

    gateway = Gateway(RouterConfig(), store)
    gateway._providers["kimi"] = CountingProvider()
    threads = [threading.Thread(target=lambda: gateway.credentials("kimi"))
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refreshes) == 1  # one refresh under the lock, not four
    assert store.load("kimi").access == "fresh-1"


def test_escalation_tracker_evicts_oldest_sessions():
    tracker = EscalationTracker(RouterConfig())
    tracker.observe_request("s1", [{"role": "user", "content": "this isn't working"}])
    assert tracker.state_for("s1")["escalated"] is True
    for i in range(EscalationTracker.MAX_SESSIONS + 8):
        tracker.observe_request(f"bulk-{i}", [{"role": "user", "content": "hi"}])
    assert len(tracker._sessions) <= EscalationTracker.MAX_SESSIONS
    assert "s1" not in tracker._sessions  # oldest evicted
    state = tracker.state_for("s1")  # evicted state restarts clean
    assert state["escalated"] is False


def test_no_api_key_reads_in_source():
    """No env reads; key-shaped strings only where allowlisted (OAuth scope,
    Cursor endpoint path, config reject-guard, Meta's OAuth-derived mint)."""
    root = Path(__file__).resolve().parents[1] / "src" / "model_router"
    allowed_fragments = ("create_api_key", "exchange_user_api_key", "_reject_api_keys",
                         "api_key\", \"apikey", "forbidden field", "API keys",
                         "minted subscription key", "OAuth-issued")
    hits = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "os.environ" in line or "getenv" in line or "API_KEY" in line:
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
            elif ("api_key" in line or "apikey" in line or "api-key" in line.lower()):
                if not any(frag in line for frag in allowed_fragments):
                    hits.append(f"{path.name}:{lineno}: {line.strip()}")
    assert hits == [], f"forbidden key/env reads: {hits}"

# -- proxy -----------------------------------------------------------------------
class StubTransport(ChatTransport):
    def __init__(self, text="hello", usage=None):
        self.text, self.usage = text, usage or {}

    def run(self):
        from model_router.providers.base import ChatChunk
        yield ChatChunk(kind="delta", text=self.text)
        if self.usage:
            yield ChatChunk(kind="usage", usage=self.usage)
        yield ChatChunk(kind="finish", finish_reason="stop")


def _app(tmp_path: Path, text="hello") -> ProxyApp:
    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    store = TokenStore(tmp_path / "store")
    for tier in (cfg.fast, cfg.strong):  # stub whichever providers config names
        store.save(tier.provider, Credentials(access="a", refresh="r", expires=0))

    class StubProvider:
        def __init__(self, provider):
            self.provider = provider

        def needs_refresh(self, creds):
            return False

        def open_chat(self, creds, request):
            return StubTransport(text)

        def refresh(self, creds):
            return creds

    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    app.classifier.model_fn = None  # deterministic heuristic in tests
    for tier in (cfg.fast, cfg.strong):
        app.gateway._providers[tier.provider] = StubProvider(tier.provider)
    return app


def _http(app: ProxyApp, head: str, body: bytes = b"") -> tuple[int, dict, bytes]:
    """Drive the real HTTP handler over a socketpair (no TCP bind needed)."""
    import socket

    client, handler_sock = socket.socketpair()
    errors: list = []

    def run():
        try:
            make_handler(app)(handler_sock, ("127.0.0.1", 0), None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            try:
                handler_sock.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    client.sendall(head.encode() + body)
    client.shutdown(socket.SHUT_WR)
    chunks = []
    while True:
        piece = client.recv(65536)
        if not piece:
            break
        chunks.append(piece)
    thread.join(timeout=10)
    client.close()
    assert not errors, errors
    raw = b"".join(chunks)
    header_raw, _, payload = raw.partition(b"\r\n\r\n")
    lines = header_raw.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, payload


def _get(app: ProxyApp, path: str) -> tuple[int, dict, bytes]:
    return _http(app, f"GET {path} HTTP/1.0\r\nHost: x\r\nConnection: close\r\n\r\n")


def _post(app: ProxyApp, body: dict, headers: dict | None = None) -> tuple[int, dict, bytes]:
    raw = json.dumps(body).encode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    head = (f"POST /v1/chat/completions HTTP/1.0\r\nHost: x\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
            f"{extra}Connection: close\r\n\r\n")
    return _http(app, head, raw)


def test_models_endpoint_lists_virtual_models(tmp_path: Path):
    status, _headers, raw = _get(_app(tmp_path), "/v1/models")
    assert status == 200
    assert [m["id"] for m in json.loads(raw)["data"]] == list(VIRTUAL_MODELS)


class _FlakyTransport(ChatTransport):
    def __init__(self, text_before="", status=503):
        self.text_before = text_before
        self.status = status

    def run(self):
        from model_router.providers.base import ChatChunk

        if self.text_before:
            yield ChatChunk(kind="delta", text=self.text_before)
        raise HttpStatusError("POST", "https://provider.invalid", self.status)


def _flaky_fast(tmp_path: Path, text_before="", status=503) -> ProxyApp:
    app = _app(tmp_path)
    fast = app.gateway._providers[app.config.fast.provider]
    fast.open_chat = lambda creds, request: _FlakyTransport(text_before, status)
    return app


def test_fast_tier_5xx_falls_back_to_strong(tmp_path: Path):
    app = _flaky_fast(tmp_path)
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "Fix this typo in README.md"}],
    })
    assert status == 200
    assert json.loads(raw)["choices"][0]["message"]["content"] == "hello"
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["ok"] is True and record["tier"] == "strong"
    assert record["provider"] == app.config.strong.provider
    assert record["fallback"]["from"] == app.config.fast.provider \
        and "503" in record["fallback"]["error"]


def test_fast_tier_5xx_stream_falls_back_before_output(tmp_path: Path):
    app = _flaky_fast(tmp_path)
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
        "stream": True,
    })
    assert status == 200
    assert '"content":"hello"' in raw.decode().replace(" ", "")
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["fallback"]["from"] == app.config.fast.provider
    assert record["provider"] == app.config.strong.provider


def test_no_fallback_after_partial_stream_output(tmp_path: Path):
    app = _flaky_fast(tmp_path, text_before="partial answer")
    status, _headers, _raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
        "stream": True,
    })
    assert status == 200  # headers already sent; error arrives as an SSE event
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["ok"] is False and "fallback" not in record


def test_no_fallback_when_auth_is_dead(tmp_path: Path):
    from model_router.errors import ReloginRequired

    app = _flaky_fast(tmp_path, status=401)
    fast = app.gateway._providers[app.config.fast.provider]
    fast.refresh = lambda creds: (_ for _ in ()).throw(
        ReloginRequired(app.config.fast.provider))
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
    })
    assert status == 401  # relogin_required, not a silent strong-tier switch
    assert b"relogin_required" in raw
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert "fallback" not in record


def test_chat_non_stream_and_request_log(tmp_path: Path):
    app = _app(tmp_path)
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "Fix this typo in README.md"}],
    })
    assert status == 200
    data = json.loads(raw)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hello"
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["verdict"] == "trivial" and record["tier"] == "fast"
    assert record["provider"] == "kimi" and record["ok"] is True


def test_chat_stream_sse_shape(tmp_path: Path):
    status, headers, raw = _post(_app(tmp_path, text="hi"), {
        "model": "router-strong",
        "messages": [{"role": "user", "content": "anything"}],
        "stream": True,
    })
    assert status == 200
    assert headers.get("content-type") == "text/event-stream"
    text = raw.decode()
    assert "chat.completion.chunk" in text and text.rstrip().endswith("data: [DONE]")


def test_chat_hard_routes_strong(tmp_path: Path):
    app = _app(tmp_path)
    status, _headers, _raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user",
                      "content": "Design a plugin architecture for billing"}],
    })
    assert status == 200
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["verdict"] == "hard" and record["tier"] == "strong"


def test_proxy_refuses_start_with_zero_auth(tmp_path: Path):
    from model_router.errors import NoSubscriptionAuth

    app = ProxyApp(RouterConfig(), store=TokenStore(tmp_path / "empty"),
                   log_dir=tmp_path / "logs")
    with pytest.raises(NoSubscriptionAuth):
        app.check_auth()


def test_strict_startup_refuses_partial_auth(tmp_path: Path):
    from model_router.errors import NoSubscriptionAuth

    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    store = TokenStore(tmp_path / "store")
    store.save(cfg.fast.provider, Credentials(access="a", refresh="r", expires=0))
    assert cfg.require_all_tiers is True
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    with pytest.raises(NoSubscriptionAuth, match=cfg.strong.provider):
        app.check_auth()


def test_strict_startup_refuses_unported_transport(tmp_path: Path):
    from model_router.errors import NoSubscriptionAuth

    store = TokenStore(tmp_path / "store")
    store.save("cursor", Credentials(access="a", refresh="r", expires=0))
    store.save("anthropic", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig()
    cfg.fast.provider = "cursor"  # transport unported
    assert cfg.require_all_tiers is True
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    with pytest.raises(NoSubscriptionAuth, match="cursor"):
        app.check_auth()


def test_kimi_defaults_to_global_and_refresh_follows_stored_domain(monkeypatch):
    from model_router.providers import kimi as kimi_provider

    # Global default: kimi.ai, not the CN kimi.com.
    provider = kimi_provider.KimiProvider()
    assert provider.oauth_host == "https://auth.kimi.ai"
    assert provider.gateway_base_url == "https://api.kimi.ai/coding/v1"
    cn = kimi_provider.KimiProvider(domain="kimi.com")
    assert cn.oauth_host == "https://auth.kimi.com"

    # A token issued on kimi.com keeps refreshing there even when the
    # provider default is the global domain.
    seen = []

    def fake_post_form(url, fields, headers=None, timeout=0.0):
        from types import SimpleNamespace

        seen.append(url)
        return SimpleNamespace(json=lambda: {"access_token": "new", "refresh_token": "r",
                                             "expires_in": 3600})

    monkeypatch.setattr(kimi_provider, "post_form", fake_post_form)
    creds = kimi_provider.Credentials(access="old", refresh="r",
                                      extra={"domain": "kimi.com"})
    refreshed = provider.refresh(creds)
    assert seen == ["https://auth.kimi.com/api/oauth/token"]
    assert refreshed.extra["domain"] == "kimi.com"


def test_strict_startup_all_ready(tmp_path: Path):
    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    store = TokenStore(tmp_path / "store")
    for tier in (cfg.fast, cfg.strong):
        store.save(tier.provider, Credentials(access="a", refresh="r", expires=0))
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    status = app.check_auth()
    assert status["fast"]["ok"] and status["strong"]["ok"]


def test_relaxed_startup_allows_partial(tmp_path: Path):
    store = TokenStore(tmp_path / "store")
    store.save("kimi", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg.require_all_tiers = False
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    status = app.check_auth()  # must not raise
    assert status["fast"]["ok"] and not status["strong"]["ok"]


def test_tier_status_marks_unported_transport(tmp_path: Path):
    store = TokenStore(tmp_path / "store")
    store.save("cursor", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig()
    gateway = Gateway(cfg, store)
    # point fast at cursor (unported transport) -> disabled with reason
    cfg.fast.provider = "cursor"
    status = gateway.tier_status()
    assert status["fast"]["auth"] is True and status["fast"]["ok"] is False
    assert "unavailable" in status["fast"]["transport"]


def test_responses_payload_translation():
    from model_router.providers.responses import to_responses_payload

    request = ChatRequest(model="gpt-5.1-codex", messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "data"},
    ], tools=TOOLS)
    payload = to_responses_payload(request)
    assert payload["instructions"] == "sys"
    assert [item["type"] for item in payload["input"]] == [
        "message", "function_call", "function_call_output"]
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": "hi"}
    assert payload["input"][1]["call_id"] == "c1"
    assert payload["input"][2] == {"type": "function_call_output", "call_id": "c1",
                                   "output": "data"}
    assert payload["tools"][0]["name"] == "list_files" and payload["tools"][0]["strict"] is False
    assert payload["store"] is False and payload["stream"] is True


def test_responses_stream_tools_and_usage(monkeypatch):
    import io

    from model_router.providers.responses import ResponsesTransport

    events = [
        ("response.created", {"response": {}}),
        ("response.output_text.delta", {"delta": "Hel"}),
        ("response.output_text.delta", {"delta": "lo"}),
        ("response.output_item.added", {"item": {"type": "function_call",
            "id": "item-1", "call_id": "c1", "name": "read_file"}}),
        ("response.function_call_arguments.delta",
         {"item_id": "item-1", "delta": '{"path":'}),
        ("response.function_call_arguments.delta",
         {"item_id": "item-1", "delta": '"x"}'}),
        ("response.completed", {"response": {"usage": {
            "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}}),
    ]
    raw = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                  for name, data in events).encode()
    captured = {}

    def fake_urlopen(req, timeout=0.0):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return io.BytesIO(raw)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    chunks = list(ResponsesTransport(
        "tok", ChatRequest(model="gpt-5.1-codex", messages=[], tools=TOOLS),
        account_id="acct-1").run())
    assert captured["url"].endswith("/codex/responses")
    assert captured["headers"].get("originator") == "codex_cli_rs"
    assert captured["headers"].get("chatgpt-account-id") == "acct-1"
    assert "".join(c.text for c in chunks if c.kind == "delta") == "Hello"
    calls = [c.tool_call for c in chunks if c.kind == "tool_call"]
    assert calls[0]["id"] == "c1" and calls[0]["function"]["name"] == "read_file"
    assert "".join(c["function"]["arguments"] for c in calls) == '{"path":"x"}'
    assert chunks[-2].usage == {"prompt_tokens": 7, "completion_tokens": 3,
                                "total_tokens": 10}
    assert chunks[-1].finish_reason == "tool_calls"


def test_responses_complete_item_fallback(monkeypatch):
    import io

    from model_router.providers.responses import ResponsesTransport

    # Some gateway responses carry arguments only on the completed item.
    events = [
        ("response.output_item.added", {"item": {"type": "function_call",
            "id": "item-9", "call_id": "c9", "name": "run_shell"}}),
        ("response.output_item.done", {"item": {"type": "function_call",
            "id": "item-9", "call_id": "c9", "name": "run_shell",
            "arguments": '{"command":"ls"}'}}),
        ("response.completed", {"response": {"usage": {}}}),
    ]
    raw = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                  for name, data in events).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(raw))
    chunks = list(ResponsesTransport(
        "tok", ChatRequest(model="m", messages=[])).run())
    # Assemble the call the way the agent/proxy does (by index).
    assembled = {}
    for chunk in chunks:
        if chunk.kind == "tool_call":
            call = assembled.setdefault(chunk.tool_call["index"],
                                        {"id": "", "function": {"name": "", "arguments": ""}})
            if chunk.tool_call.get("id"):
                call["id"] = chunk.tool_call["id"]
            for key in ("name", "arguments"):
                call["function"][key] += (chunk.tool_call.get("function") or {}).get(key) or ""
    assert assembled == {0: {"id": "c9", "function": {"name": "run_shell",
                                                      "arguments": '{"command":"ls"}'}}}
    assert chunks[-1].finish_reason == "tool_calls"


def test_windsurf_login_registers_account(monkeypatch):
    from model_router.oauth_common import CallbackResult
    from model_router.providers import windsurf as ws

    seen = {}

    def fake_flow(*, port, path, build_auth_url, timeout=0.0, open_browser=True,
                  token_params=("code",)):
        seen["token_params"] = token_params
        seen["url"] = build_auth_url("state-1", "http://127.0.0.1:48010/auth")[0]
        return CallbackResult(code="fb-token", state="state-1"), "http://127.0.0.1:48010/auth"

    def fake_register(token):
        assert token == "fb-token"
        return {"api_key": "devin-session-token$jwt", "name": "Dev User",
                "api_server_url": ""}

    monkeypatch.setattr(ws, "run_callback_flow", fake_flow)
    monkeypatch.setattr(ws, "register_user", fake_register)
    creds = ws.WindsurfProvider().login()
    assert creds.access == "devin-session-token$jwt"
    assert creds.account_id == "Dev User"
    assert creds.extra["api_server_url"] == ws.DEFAULT_API_HOST
    assert seen["token_params"][0] == "firebase_id_token"
    assert seen["url"].startswith("https://windsurf.com/windsurf/signin?")
    assert "response_type=token" in seen["url"]
    with pytest.raises(ReloginRequired):
        ws.WindsurfProvider().refresh(creds)


def test_windsurf_wire_roundtrip_and_request_build():
    from model_router.proto_wire import encode_varint, encode_varint_field, iter_fields
    from model_router.providers import windsurf as ws

    # Multi-byte tag sanity: field 35's tag must varint-encode to 2 bytes —
    # a single-byte tag encoder silently corrupts it (reference §7).
    assert encode_varint((35 << 3) | 2) == b"\x9a\x02"

    # varint roundtrip incl. values needing multiple bytes
    for value in (0, 1, 127, 128, 300, 2 ** 31, 2 ** 45):
        decoded = list(iter_fields(encode_varint_field(9, value)))
        assert decoded[0][0] == 9 and decoded[0][2] == value

    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "data"},
    ]
    collapsed = ws.collapse_system_into_user(messages)
    assert [m["role"] for m in collapsed] == ["user", "assistant", "tool"]
    assert collapsed[0]["content"].startswith("<system>\nbe terse\n</system>\nhi")

    body = ws.build_chat_request("key", "jwt", "swe-1.6", messages, TOOLS[:1],
                                 "cascade-1", "prompt-1", "session-1")
    # decode top-level: metadata=1, prompts=3 (x3), request_type=7,
    # completion=8, tools=10, cascade=16, model=21, prompt=22
    tops = [(num, wire) for num, wire, _ in iter_fields(body)]
    assert tops.count((3, 2)) == 3
    assert (16, 2) in tops and (21, 2) in tops and (22, 2) in tops
    model_field = [v for num, wire, v in iter_fields(body) if num == 21]
    assert model_field[0] == b"swe-1.6"


def test_windsurf_frame_decode_tools_usage_finish():
    import gzip
    import io

    from model_router.proto_wire import encode_message, encode_string, \
        encode_varint_field, frame_connect, iter_connect_frames
    from model_router.providers import windsurf as ws

    # Visible delta, tool call start + args, usage, finish, EOS trailer.
    chat = b"".join([
        encode_string(3, "Hello"),
        encode_message(6, encode_string(1, "call-7") + encode_string(2, "run_shell")),
        encode_message(6, encode_string(3, '{"command":"ls"}')),
        encode_message(28, encode_message(2,
            encode_message(4, encode_string(1, "Input tokens")
                           + b"\x15" + b"\x00\x00\x80A"  # fixed32 float 16.0
                           ) + encode_string(5, "input_tokens"))),
        encode_varint_field(5, 10),
    ])
    trailer = b"{}"
    stream = io.BytesIO(frame_connect(chat, compress=False)
                        + bytes([0x02]) + len(trailer).to_bytes(4, "big") + trailer)
    frames = list(iter_connect_frames(stream))
    assert [f[0] for f in frames] == [0x00, 0x02]
    events = list(ws.decode_chat_frame(frames[0][1]))
    assert events[0] == ("delta", "Hello")
    assert events[1] == ("tool", ("call-7", "run_shell", ""))
    assert events[2] == ("tool", ("", "", '{"command":"ls"}'))
    usage = dict(e for e in events if e[0] == "usage")
    assert usage["usage"] == {"prompt_tokens": 16, "total_tokens": 16}
    assert events[-1] == ("finish", "tool_calls")

    # gzip-compressed frames arrive decompressed from the streaming reader
    gz = io.BytesIO(frame_connect(chat, compress=True))
    flags, payload = next(iter_connect_frames(gz))
    assert flags & 0x01 and payload == chat


def test_windsurf_transport_assembles_chunks(monkeypatch):
    import io

    from model_router.proto_wire import encode_message, encode_string, \
        encode_varint_field, frame_connect
    from model_router.providers import windsurf as ws

    chat = b"".join([
        encode_string(3, "Hi"),
        encode_message(6, encode_string(1, "call-1") + encode_string(2, "read_file")),
        encode_message(6, encode_string(3, '{"path":')),
        encode_message(6, encode_string(3, '"a"}')),
        encode_varint_field(5, 2),  # STOP_PATTERN -> stop
    ])
    trailer = b"{}"
    body = frame_connect(chat, compress=False) \
        + bytes([0x02]) + len(trailer).to_bytes(4, "big") + trailer

    captured = {}

    def fake_urlopen(req, timeout=0.0):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ws, "mint_user_jwt", lambda *_a, **_k: "eyJfake.jwt.token")
    request = ChatRequest(model="swe-1.6", messages=[{"role": "user", "content": "hi"}])
    chunks = list(ws.WindsurfTransport("acct-key", request).run())
    assert captured["url"].endswith("ApiServerService/GetChatMessage")
    assert captured["headers"]["content-type"] == "application/connect+proto"
    assert "".join(c.text for c in chunks if c.kind == "delta") == "Hi"
    assembled = {}
    for chunk in chunks:
        if chunk.kind == "tool_call":
            call = assembled.setdefault(chunk.tool_call["index"],
                                        {"id": "", "function": {"name": "", "arguments": ""}})
            if chunk.tool_call.get("id"):
                call["id"] = chunk.tool_call["id"]
            for key in ("name", "arguments"):
                call["function"][key] += (chunk.tool_call.get("function") or {}).get(key) or ""
    assert assembled == {0: {"id": "call-1", "function": {
        "name": "read_file", "arguments": '{"path":"a"}'}}}
    # wire finish=stop upgrades to tool_calls because a call was announced
    assert chunks[-1].finish_reason == "tool_calls"


def test_windsurf_trailer_error_names_the_model(monkeypatch):
    import io

    from model_router.proto_wire import frame_connect
    from model_router.providers import windsurf as ws

    trailer = json.dumps({"error": {"code": "permission_denied",
                                    "message": "an internal error occurred"}}).encode()
    body = bytes([0x02]) + len(trailer).to_bytes(4, "big") + trailer
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *_a, **_k: io.BytesIO(body))
    monkeypatch.setattr(ws, "mint_user_jwt", lambda *_a, **_k: "eyJfake.jwt.token")
    with pytest.raises(RouterError, match="swe-1.6"):
        list(ws.WindsurfTransport(
            "k", ChatRequest(model="swe-1.6", messages=[])).run())


def test_proxy_preserves_tool_calls(tmp_path: Path):
    from model_router.providers.base import ChatChunk

    class ToolTransport(ChatTransport):
        def run(self):
            yield ChatChunk(kind="tool_call", tool_call={"index": 0, "id": "c1",
                "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}})
            yield ChatChunk(kind="tool_call", tool_call={"index": 0,
                "function": {"arguments": '"x"}'}})
            yield ChatChunk(kind="finish", finish_reason="tool_calls")

    app = _app(tmp_path)
    provider = app.gateway._providers["kimi"]
    provider.open_chat = lambda *_args: ToolTransport()
    body = {"model": "router-fast", "messages": [{"role": "user", "content": "read a file"}]}
    status, _, raw = _post(app, body)
    assert status == 200
    choice = json.loads(raw)["choices"][0]
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"path":"x"}'
    assert choice["finish_reason"] == "tool_calls"
    status, _, raw = _post(app, {**body, "stream": True})
    assert status == 200
    assert b'"tool_calls"' in raw and b'"c1"' in raw
