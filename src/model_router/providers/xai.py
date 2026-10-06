"""xAI/Grok subscription auth: OIDC discovery + PKCE browser login.

Port of opencodex src/oauth/xai.ts (MIT, see THIRD_PARTY.md):
issuer https://auth.x.ai, public client b1a00492-073a-47ea-816f-4c329264a828,
scopes "openid profile email offline_access grok-cli:access api:access",
callback http://127.0.0.1:56121/callback. Endpoints come from
/.well-known/openid-configuration and must stay on auth.x.ai / accounts.x.ai.

Chat transport: OpenAI-compatible POST
https://cli-chat-proxy.grok.com/v1/chat/completions (same gateway opencodex /
claude-code-proxy use for the Grok subscription).
"""

from __future__ import annotations

import urllib.parse
import uuid as uuid_mod

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, get, now_ms, post_form
from ..jwt_util import jwt_claim
from ..oauth_common import prompt_manual_code, run_callback_flow
from ..pkce import PKCE, generate_pkce
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .openai_compat import OpenAICompatTransport, list_openai_models

ISSUER = "https://auth.x.ai"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
CALLBACK_PORT = 56121
CALLBACK_PATH = "/callback"
TRUSTED_HOSTS = frozenset({"auth.x.ai", "accounts.x.ai"})
REFRESH_SKEW_MS = 2 * 60 * 1000
GATEWAY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"


def discover_endpoints() -> tuple[str, str]:
    try:
        payload = get(DISCOVERY_URL, headers={"Accept": "application/json"}, timeout=30.0).json()
    except HttpStatusError as exc:
        raise AuthFlowError(f"xAI OAuth discovery failed (HTTP {exc.status})") from None
    authz, token = payload.get("authorization_endpoint"), payload.get("token_endpoint")
    if not isinstance(authz, str) or not isinstance(token, str):
        raise AuthFlowError("xAI OAuth discovery response missing authorization/token endpoints")
    return _validate_endpoint(authz), _validate_endpoint(token)


def _validate_endpoint(raw: str) -> str:
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https:" and parts.scheme != "https":
        raise AuthFlowError("xAI OAuth discovery returned an unexpected endpoint")
    if parts.username or parts.password or parts.port or host not in TRUSTED_HOSTS:
        raise AuthFlowError(f"xAI OAuth discovery returned an unexpected endpoint (host: {host or 'none'})")
    return raw


def build_auth_url(pkce: PKCE, authorization_endpoint: str, state: str, redirect_uri: str) -> str:
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": str(uuid_mod.uuid4()),
    })
    return f"{authorization_endpoint}?{params}"


def credentials_from_payload(payload: dict, refresh_fallback: str = "") -> Credentials:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or refresh_fallback
    if not isinstance(access, str) or not access:
        raise AuthFlowError("xAI token response missing access token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthFlowError("xAI token response did not include a refresh token")
    expires_in = payload.get("expires_in")
    skew = REFRESH_SKEW_MS
    expires = now_ms() + 3600 * 1000 - skew
    if isinstance(expires_in, (int, float)) and expires_in >= 0:
        expires = now_ms() + int(expires_in * 1000) - skew
    id_token = payload.get("id_token")
    probe = id_token if isinstance(id_token, str) and id_token else access
    account_id = jwt_claim(probe, "sub") or ""
    email = (jwt_claim(probe, "email") or "").lower()
    return Credentials(access=access, refresh=refresh, expires=expires,
                       account_id=account_id, email=email)


def post_token(token_endpoint: str, fields: dict[str, str]) -> dict:
    try:
        return post_form(token_endpoint, fields, timeout=30.0).json()
    except HttpStatusError as exc:
        raise AuthFlowError(f"xAI token request failed (HTTP {exc.status})") from None


class XaiProvider(Provider):
    id = "xai"
    display = "xAI/Grok"
    expiry_skew_ms = REFRESH_SKEW_MS

    def __init__(self, progress=None, gateway_base_url: str = GATEWAY_BASE_URL):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        pkce = generate_pkce()
        authz, token_url = discover_endpoints()

        def build(state: str, redirect_uri: str):
            return build_auth_url(pkce, authz, state, redirect_uri), (
                "Complete xAI/Grok login in your browser. If the browser cannot "
                "reach this machine, paste the final redirect URL or authorization "
                "code when prompted."
            )

        result, redirect_uri = run_callback_flow(
            port=CALLBACK_PORT, path=CALLBACK_PATH, build_auth_url=build,
            open_browser=kwargs.get("open_browser", True),
        )
        code = result.code if result else prompt_manual_code(self.id)
        payload = post_token(token_url, {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": pkce.verifier,
        })
        return credentials_from_payload(payload)

    def refresh(self, creds: Credentials) -> Credentials:
        if not creds.refresh:
            raise ReloginRequired(self.id)
        _authz, token_url = discover_endpoints()
        try:
            payload = post_token(token_url, {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": creds.refresh,
            })
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc
        try:
            return credentials_from_payload(payload, creds.refresh)
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        return OpenAICompatTransport(
            base_url=self.gateway_base_url, access_token=creds.access, request=request)

    def list_models(self, creds: Credentials) -> list[str]:
        return list_openai_models(self.gateway_base_url, creds.access)
