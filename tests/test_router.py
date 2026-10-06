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
from model_router.config import RouterConfig, Tier
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
    url = anthropic_url("CH", "ST", "http://localhost:54545/callback")
    assert url.startswith("https://claude.ai/oauth/authorize?")
    for needle in ("code_challenge=CH", "code_challenge_method=S256", "state=ST",
                   "client_id=9d1c250a", "response_type=code",
                   "redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback",
                   "user%3Asessions%3Aclaude_code"):
        assert needle in url


def test_anthropic_login_uses_localhost_callback_and_platform_token_url(monkeypatch):
    from model_router.providers import anthropic as ap

    assert ap.TOKEN_URL == "https://platform.claude.com/v1/oauth/token"
    seen = {}

    def fake_flow(*, port, path, build_auth_url, open_browser=True, **kwargs):
        url, _instructions = build_auth_url("ST", f"http://localhost:{port}{path}")
        seen.update(url=url, extra=kwargs)
        return None, f"http://localhost:{port}{path}"

    monkeypatch.setattr(ap, "run_callback_flow", fake_flow)
    monkeypatch.setattr(ap, "prompt_manual_code", lambda _provider: "CODE")
    monkeypatch.setattr(ap, "_post_token", lambda _fields: {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "account": {"uuid": "u", "email_address": "A@B.c"}})
    creds = ap.AnthropicProvider().login()
    assert seen["extra"].get("redirect_host") == "localhost"
    assert creds.access == "a" and creds.email == "a@b.c"


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
    assert route.tier == "everyday"
    assert (route.candidate.provider, route.candidate_index) == (
        RouterConfig().tiers["everyday"][0].provider, 0)
    route, _ = router.route([{"role": "user", "content": "design architecture"}], "router-auto")
    assert route.tier == "very_high"
    route, _ = router.route([{"role": "user", "content": "debug this failing test"}], "router-auto")
    assert route.tier == "high"


def test_header_and_model_overrides():
    router = _router()
    msgs = [{"role": "user", "content": "design architecture"}]
    route, _ = router.route(msgs, "router-everyday")
    assert route.tier == "everyday"
    route, _ = router.route(msgs, "router-auto", header_mode="very-high-only")
    assert (route.tier, route.mode) == ("very_high", "very-high-only")
    route, _ = router.route(msgs, "router-auto", header_mode="very_high-only")
    assert route.tier == "very_high"
    with pytest.raises(ValueError):
        router.route(msgs, "router-auto", header_mode="bogus")


def test_pool_prefers_first_available_wallet():
    cfg = RouterConfig()
    cfg.tiers["high"] = [Tier("kimi", "k3"), Tier("muse", "muse-spark-1.3")]
    router = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    # No filter: first wallet serves.
    route, _ = router.route([{"role": "user", "content": "debug this bug"}], "router-auto")
    assert (route.tier, route.candidate_index) == ("high", 0)
    assert route.candidate.provider == "kimi"
    # Only the second wallet is available: route picks it, not the first.
    route, _ = router.route([{"role": "user", "content": "debug this bug"}],
                            "router-auto", available=lambda p: p == "muse")
    assert route.tier == "high"
    assert route.candidate_index == 1
    # Nothing available anywhere: fail closed with a login hint.
    from model_router.errors import NoSubscriptionAuth
    with pytest.raises(NoSubscriptionAuth):
        router.route([{"role": "user", "content": "debug this bug"}],
                     "router-auto", available=lambda _p: False)


def test_ladder_walks_same_tier_then_up():
    cfg = RouterConfig()
    router = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    plan = router.ladder_from("moderate", 0)
    names = [(tier, cand.provider) for tier, cand, _idx in plan]
    assert names[0][0] == "moderate"
    tiers_seen = [tier for tier, _p in names]
    assert tiers_seen == sorted(tiers_seen, key=("everyday", "moderate", "high", "very_high").index)
    # Starting past the end of a tier's pool skips straight to higher rungs.
    plan = router.ladder_from("moderate", 99)
    assert plan and plan[0][0] == "high"
    # very_high is the ceiling: the ladder never steps down or wraps.
    plan = router.ladder_from("very_high", 0)
    assert [tier for tier, _c, _i in plan] == ["very_high"]


def _tool_error(n: int):
    return [{"role": "user", "content": "do it"},
            *[{"role": "tool", "content": f"Error: boom {i}"} for i in range(n)]]


def test_weak_first_escalate_on_tool_errors():
    cfg = RouterConfig()
    router = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    route, _ = router.route(_tool_error(3), "router-auto",
                            header_mode="weak-first-escalate", session="s1")
    # "do it" is easy -> moderate; escalation climbs exactly one rung.
    assert route.tier == "high" and route.escalated
    # The session stays escalated: a trivial task still climbs one rung
    # (everyday -> moderate) instead of serving the bottom tier.
    route, _ = router.route([{"role": "user", "content": "fix typo"}], "router-auto",
                            header_mode="weak-first-escalate", session="s1")
    assert route.tier == "moderate" and route.escalated
    # escalation never climbs past the top rung
    router2 = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    router2.tracker.observe_request("top", _tool_error(9))
    route, _ = router2.route([{"role": "user", "content": "design architecture"}],
                             "router-auto", header_mode="weak-first-escalate",
                             session="top")
    assert route.tier == "very_high" and route.escalated


def test_empty_tier_degrades_to_lower_rung():
    cfg = RouterConfig()
    router = Router(cfg, Classifier(model_fn=None, log_fn=lambda _r: None))
    # high tier fully unavailable -> nearest lower servable rung (moderate).
    available = lambda provider: provider not in {c.provider for c in cfg.tiers["high"]}
    route, _ = router.route([{"role": "user", "content": "debug this bug"}],
                            "router-auto", available=available)
    assert route.tier == "moderate"


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
    from model_router.config import TIERS

    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    store = TokenStore(tmp_path / "store")
    providers = {c.provider for tier in TIERS for c in cfg.tiers.get(tier, [])}
    for provider_id in providers:  # stub every wallet the pools name
        store.save(provider_id, Credentials(access="a", refresh="r", expires=0))

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
    for provider_id in providers:
        app.gateway._providers[provider_id] = StubProvider(provider_id)
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


def _flaky_provider(app: ProxyApp, provider_id: str, text_before="",
                      status=503) -> ProxyApp:
    stub = app.gateway._providers[provider_id]
    stub.open_chat = lambda creds, request: _FlakyTransport(text_before, status)
    return app


def _first_two_wallets(app: ProxyApp, tier: str) -> tuple[str, str]:
    pool = app.config.tiers[tier]
    assert len(pool) >= 2, "test needs a two-wallet tier pool"
    return pool[0].provider, pool[1].provider


def test_same_tier_wallet_rotation_on_503(tmp_path: Path):
    # First everyday wallet 503s; the second wallet in the SAME tier serves.
    app = _app(tmp_path)
    first, second = _first_two_wallets(app, "everyday")
    _flaky_provider(app, first)
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "Fix this typo in README.md"}],
    })
    assert status == 200
    assert json.loads(raw)["choices"][0]["message"]["content"] == "hello"
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["ok"] is True and record["tier"] == "everyday"
    assert record["provider"] == second
    assert [a["provider"] for a in record["attempts"]] == [first]
    assert "503" in record["attempts"][0]["error"]


def test_climb_to_next_rung_when_tier_is_down(tmp_path: Path):
    # Whole everyday pool 503s; moderate serves via its surviving wallet
    # (the shared kimi stub is flaky in every tier, so windsurf answers).
    app = _app(tmp_path)
    for provider_id in {c.provider for c in app.config.tiers["everyday"]}:
        _flaky_provider(app, provider_id)
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
        "stream": True,
    })
    assert status == 200
    assert '"content":"hello"' in raw.decode().replace(" ", "")
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["tier"] == "moderate"
    assert record["provider"] == app.config.tiers["moderate"][1].provider
    assert {a["tier"] for a in record["attempts"]} == {"everyday", "moderate"}


def test_429_cools_down_and_rotation_skips_it(tmp_path: Path):
    app = _app(tmp_path)
    first, second = _first_two_wallets(app, "everyday")
    _flaky_provider(app, first, status=429)
    status, _headers, _raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
    })
    assert status == 200
    assert app.gateway.cooling(first)  # quota-drained wallet leaves rotation
    assert not app.gateway.cooling(second)
    # Next request routes straight to the surviving wallet.
    route, _ = app.router.route([{"role": "user", "content": "fix typo"}],
                                "router-auto", available=app.available)
    assert route.candidate.provider == second and route.candidate_index == 1


def test_dead_wallet_is_skipped_but_tier_survives(tmp_path: Path):
    # 401 + dead refresh on the first wallet rotates within the tier (200),
    # because a dead grant is a wallet problem, not a request problem.
    from model_router.errors import ReloginRequired

    app = _app(tmp_path)
    first, second = _first_two_wallets(app, "everyday")
    _flaky_provider(app, first, status=401)
    app.gateway._providers[first].refresh = lambda creds: (_ for _ in ()).throw(
        ReloginRequired(first))
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
    })
    assert status == 200
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["provider"] == second
    assert "RELOGIN_REQUIRED" in record["attempts"][0]["error"]


def test_all_wallets_dead_surfaces_401(tmp_path: Path):
    from model_router.errors import ReloginRequired

    app = _app(tmp_path)
    for provider_id in list(app.gateway._providers):
        _flaky_provider(app, provider_id, status=401)
        app.gateway._providers[provider_id].refresh = (
            lambda creds, p=provider_id: (_ for _ in ()).throw(ReloginRequired(p)))
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
    })
    assert status == 401  # every wallet dead: relogin, not a silent switch
    assert b"relogin_required" in raw


def test_no_fallback_after_partial_stream_output(tmp_path: Path):
    app = _app(tmp_path)
    first, _second = _first_two_wallets(app, "everyday")
    _flaky_provider(app, first, text_before="partial answer")
    status, _headers, _raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "fix typo"}],
        "stream": True,
    })
    assert status == 200  # headers already sent; error arrives as an SSE event
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["ok"] is False and record["attempts"] == []


def test_chat_non_stream_and_request_log(tmp_path: Path):
    app = _app(tmp_path)
    first = app.config.tiers["everyday"][0]
    status, _headers, raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user", "content": "Fix this typo in README.md"}],
    })
    assert status == 200
    data = json.loads(raw)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hello"
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["verdict"] == "trivial" and record["tier"] == "everyday"
    assert record["provider"] == first.provider and record["ok"] is True


def test_chat_stream_sse_shape(tmp_path: Path):
    status, headers, raw = _post(_app(tmp_path, text="hi"), {
        "model": "router-high",
        "messages": [{"role": "user", "content": "anything"}],
        "stream": True,
    })
    assert status == 200
    assert headers.get("content-type") == "text/event-stream"
    text = raw.decode()
    assert "chat.completion.chunk" in text and text.rstrip().endswith("data: [DONE]")


def test_chat_hard_routes_very_high(tmp_path: Path):
    app = _app(tmp_path)
    first = app.config.tiers["very_high"][0]
    status, _headers, _raw = _post(app, {
        "model": "router-auto",
        "messages": [{"role": "user",
                      "content": "Design a plugin architecture for billing"}],
    })
    assert status == 200
    record = json.loads((tmp_path / "logs" / "requests.jsonl").read_text().strip())
    assert record["verdict"] == "hard" and record["tier"] == "very_high"
    assert record["provider"] == first.provider


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
    store.save(cfg.tiers["everyday"][0].provider,
               Credentials(access="a", refresh="r", expires=0))
    assert cfg.require_all_tiers is True
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    with pytest.raises(NoSubscriptionAuth, match="moderate"):
        app.check_auth()


def test_strict_startup_refuses_unported_transport(tmp_path: Path):
    from model_router.errors import NoSubscriptionAuth

    store = TokenStore(tmp_path / "store")
    store.save("cursor", Credentials(access="a", refresh="r", expires=0))
    store.save("anthropic", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig()
    cfg.tiers["everyday"] = [Tier("cursor", "m")]  # transport unported
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
    from model_router.config import TIERS

    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    store = TokenStore(tmp_path / "store")
    for tier in TIERS:
        for candidate in cfg.tiers[tier]:
            store.save(candidate.provider, Credentials(access="a", refresh="r", expires=0))
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    status = app.check_auth()
    assert all(status[tier]["ok"] for tier in TIERS)


def test_relaxed_startup_allows_partial(tmp_path: Path):
    store = TokenStore(tmp_path / "store")
    store.save("kimi", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    cfg.require_all_tiers = False
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    status = app.check_auth()  # must not raise
    assert status["everyday"]["ok"] and status["moderate"]["ok"]
    assert not status["very_high"]["ok"]  # needs anthropic/windsurf/openai/muse


def test_tier_status_marks_unported_transport(tmp_path: Path):
    store = TokenStore(tmp_path / "store")
    store.save("cursor", Credentials(access="a", refresh="r", expires=0))
    cfg = RouterConfig()
    gateway = Gateway(cfg, store)
    # point everyday at cursor (unported transport) -> disabled with reason
    cfg.tiers["everyday"] = [Tier("cursor", "m")]
    status = gateway.tier_status()
    candidate = status["everyday"]["candidates"][0]
    assert candidate["auth"] is True and candidate["ok"] is False
    assert status["everyday"]["ok"] is False
    assert "unavailable" in candidate["transport"]


def test_legacy_fast_strong_config_rejected(tmp_path: Path):
    legacy = tmp_path / "config.yaml"
    legacy.write_text("tiers:\n  fast: {provider: kimi, model: m}\n"
                      "  strong: {provider: xai, model: m}\n")
    with pytest.raises(ValueError, match="everyday/moderate/high/very_high"):
        RouterConfig.load(legacy)


def test_classifier_parsing_and_wallet_order(tmp_path: Path):
    from model_router.config import RouterConfig, Tier
    from model_router.proxy import ProxyApp
    from model_router.store import TokenStore, Credentials

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "tiers:\n"
        "  everyday: [{provider: kimi, model: kimi-for-coding-highspeed}]\n"
        "  moderate: [{provider: kimi, model: kimi-for-coding}]\n"
        "  high: [{provider: kimi, model: k3}]\n"
        "  very_high: [{provider: kimi, model: k3}]\n"
        "classifier:\n"
        "  - {provider: muse, model: muse-spark-1.3-contributor}\n"
        "  - {provider: zai, model: glm-5.3-flash}\n")
    cfg = RouterConfig.load(cfg_file)
    assert cfg.classifier == [Tier("muse", "muse-spark-1.3-contributor"),
                              Tier("zai", "glm-5.3-flash")]
    # Single-dict shorthand still parses.
    single = tmp_path / "single.yaml"
    single.write_text(cfg_file.read_text().replace(
        "  - {provider: muse, model: muse-spark-1.3-contributor}\n"
        "  - {provider: zai, model: glm-5.3-flash}\n",
        "  {provider: muse, model: m}\n"))
    assert RouterConfig.load(single).classifier == [Tier("muse", "m")]

    store = TokenStore(tmp_path / "store")
    for provider in ("kimi", "muse", "zai"):
        store.save(provider, Credentials(access="a", refresh="r", expires=0))
    app = ProxyApp(cfg, store=store, log_dir=tmp_path / "logs")
    # Configured free-first pool leads; everyday wallets back it up, deduped.
    order = [(w.provider, w.model) for w in app._classifier_wallets()]
    assert order[:2] == [("muse", "muse-spark-1.3-contributor"),
                         ("zai", "glm-5.3-flash")]
    assert ("kimi", "kimi-for-coding-highspeed") in order
    assert order.count(("kimi", "kimi-for-coding")) == 0  # non-everyday tiers excluded
    # Cooled-down pool wallets are skipped at call time, everyday survives.
    app.gateway.mark_cooldown("muse", seconds=60)
    app.gateway.mark_cooldown("zai", seconds=60)
    usable = [(w.provider, w.model) for w in app._classifier_wallets()
              if app.available(w.provider)]
    assert usable == [("kimi", "kimi-for-coding-highspeed")]


def test_setup_builds_pools_from_logins_and_catalogs():
    import yaml

    from model_router.config import RouterConfig
    from model_router.onboarding import build_config_dict

    logged_in = {"anthropic", "kimi", "muse", "zai"}
    catalogs = {
        "anthropic": ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5",
                      "claude-haiku-4-5-20251001"],
        "kimi": ["k3", "kimi-for-coding", "kimi-for-coding-highspeed"],
        "muse": ["muse-spark-1.3", "muse-spark-1.3-contributor"],
        "zai": ["glm-5.3", "glm-5.3-flash"],
    }
    config, notes = build_config_dict(logged_in, catalogs)
    # Every pool entry belongs to a logged-in provider and its catalog
    # (dated snapshot prefixes count: claude-haiku-4-5).
    assert [e["model"] for e in config["tiers"]["everyday"]] == [
        "glm-5.3-flash", "kimi-for-coding-highspeed"]
    assert config["tiers"]["moderate"][-1] == {"provider": "anthropic",
                                               "model": "claude-haiku-4-5"}
    assert [e["provider"] for e in config["tiers"]["very_high"]] == ["anthropic",
                                                                     "anthropic", "muse"]
    assert [e["model"] for e in config["classifier"]] == [
        "muse-spark-1.3-contributor", "glm-5.3-flash", "kimi-for-coding-highspeed"]
    assert config["policy"]["require_all_tiers"] is True
    # Round-trips through RouterConfig.load.
    written = tmp_yaml(config)
    loaded = RouterConfig.load(written)
    assert [(t.provider, t.model) for t in loaded.tiers["high"]] == [
        ("zai", "glm-5.3"), ("anthropic", "claude-sonnet-5-5"),
        ("kimi", "k3"), ("muse", "muse-spark-1.3")]

    # A subscription missing (windsurf, openai logged out) drops its entries;
    # missing catalog entries are skipped with a note.
    logged_in2 = {"kimi", "windsurf", "openai"}
    catalogs2 = {"kimi": ["kimi-for-coding"], "windsurf": ["gpt-5-5-medium"],
                 "openai": None}
    config2, notes2 = build_config_dict(logged_in2, catalogs2)
    assert config2["tiers"]["moderate"] == [
        {"provider": "kimi", "model": "kimi-for-coding"},
        {"provider": "windsurf", "model": "gpt-5-5-medium"}]
    assert any("kimi-for-coding-highspeed" in n for n in notes2)
    # Empty tiers force relaxed startup.
    assert config2["policy"]["require_all_tiers"] is False
    assert any("require_all_tiers" in n for n in notes2)


def tmp_yaml(config: dict):
    import tempfile

    import yaml

    path = Path(tempfile.mkstemp(suffix=".yaml")[1])
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def test_setup_command_end_to_end(tmp_path, monkeypatch):
    from model_router.onboarding import run_setup
    from model_router.store import TokenStore, Credentials

    store = TokenStore(tmp_path / "tokens")
    store.save("kimi", Credentials(access="a"))
    store.save("muse", Credentials(access="a"))
    store.save("zai", Credentials(access="a"))
    # Decline every new login, decline "more", then it probes + writes.
    answers = iter(["n"] * 12)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    def fake_probe(_store, logged_in):
        return {"kimi": ["k3", "kimi-for-coding", "kimi-for-coding-highspeed"],
                "muse": ["muse-spark-1.3", "muse-spark-1.3-contributor"],
                "zai": ["glm-5.3", "glm-5.3-flash"]}

    import model_router.onboarding as ob
    monkeypatch.setattr(ob, "probe_catalogs", fake_probe)
    out = tmp_path / "config.yaml"
    assert run_setup(store, output=out, print_fn=lambda *_a: None) == 0
    text = out.read_text()
    assert "glm-5.3-flash" in text and "muse-spark-1.3-contributor" in text
    # Second run backs up the previous config instead of clobbering.
    assert run_setup(store, output=out, print_fn=lambda *_a: None) == 0
    assert list(out.parent.glob("config.yaml.bak-*")), "backup not written"


def test_repo_config_pools_reference_known_providers():
    from model_router.providers import REGISTRY

    cfg = RouterConfig.load(Path(__file__).resolve().parents[1] / "config.yaml")
    for tier in ("everyday", "moderate", "high", "very_high"):
        assert cfg.tiers[tier], f"tier '{tier}' must not be empty"
        for candidate in cfg.tiers[tier]:
            assert candidate.provider in REGISTRY, candidate
            assert candidate.model and not any(ch.isspace() for ch in candidate.model)


def test_cooldown_configurable(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "tiers:\n"
        "  everyday: [{provider: kimi, model: m}]\n"
        "  moderate: [{provider: kimi, model: m}]\n"
        "  high: [{provider: kimi, model: m}]\n"
        "  very_high: [{provider: kimi, model: m}]\n"
        "policy:\n  cooldown_seconds: 60\n")
    assert RouterConfig.load(cfg_file).cooldown_seconds == 60
    assert RouterConfig().cooldown_seconds == 300


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
    body = {"model": "router-moderate", "messages": [{"role": "user", "content": "read a file"}]}
    status, _, raw = _post(app, body)
    assert status == 200
    choice = json.loads(raw)["choices"][0]
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == '{"path":"x"}'
    assert choice["finish_reason"] == "tool_calls"
    status, _, raw = _post(app, {**body, "stream": True})
    assert status == 200
    assert b'"tool_calls"' in raw and b'"c1"' in raw
