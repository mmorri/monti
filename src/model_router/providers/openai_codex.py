"""OpenAI/ChatGPT (Codex) subscription auth: PKCE browser login.

Port of opencodex src/oauth/chatgpt.ts (MIT, see THIRD_PARTY.md):
public client app_EMoamEEZ73f0CkXaXp7hrann,
authorize https://auth.openai.com/oauth/authorize,
token https://auth.openai.com/oauth/token,
callback http://localhost:1455/auth/callback,
scope "openid profile email offline_access api.connectors.read api.connectors.invoke".

Chat transport: Responses API at
https://chatgpt.com/backend-api/codex/responses (see providers/responses.py);
translated to/from the normalized ChatChunk interface, with the Codex CLI
identity headers the gateway expects.
"""

from __future__ import annotations

import urllib.parse

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, now_ms, post_form
from ..jwt_util import decode_jwt_payload
from ..oauth_common import prompt_manual_code, run_callback_flow
from ..pkce import generate_pkce
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .responses import ResponsesTransport

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
CALLBACK_HOST = "localhost"  # docstring flow uses localhost, not 127.0.0.1
ORIGINATOR = "model-router"
EXPIRY_SKEW_MS = 5 * 60 * 1000
GATEWAY_BASE_URL = "https://chatgpt.com/backend-api"


def build_auth_url(verifier_challenge: str, state: str, redirect_uri: str,
                   force_login: bool = False) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": verifier_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
        "id_token_add_organizations": "true",
    }
    if force_login:
        params["prompt"] = "login"
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def extract_account_id(id_token: str | None, access_token: str | None) -> str:
    for token in (id_token, access_token):
        if not token:
            continue
        payload = decode_jwt_payload(token)
        if not payload:
            continue
        top = payload.get("chatgpt_account_id")
        if isinstance(top, str) and top:
            return top
        ns = payload.get("https://api.openai.com/auth")
        if isinstance(ns, dict) and isinstance(ns.get("chatgpt_account_id"), str):
            return ns["chatgpt_account_id"]
        orgs = payload.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict) \
                and isinstance(orgs[0].get("id"), str):
            return orgs[0]["id"]
    return ""


def credentials_from_payload(payload: dict, refresh_fallback: str = "") -> Credentials:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or refresh_fallback
    if not isinstance(access, str) or not access:
        raise AuthFlowError("ChatGPT token response missing access token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthFlowError("ChatGPT token response did not include a refresh token")
    expires_in = payload.get("expires_in")
    expires = now_ms() + 3600 * 1000 - EXPIRY_SKEW_MS
    if isinstance(expires_in, (int, float)) and expires_in >= 0:
        expires = now_ms() + int(expires_in * 1000) - EXPIRY_SKEW_MS
    id_token = payload.get("id_token")
    account_id = extract_account_id(
        id_token if isinstance(id_token, str) else None, access)
    return Credentials(access=access, refresh=refresh, expires=expires, account_id=account_id)


class OpenAIProvider(Provider):
    id = "openai"
    display = "OpenAI/ChatGPT"

    def __init__(self, progress=None, gateway_base_url: str = GATEWAY_BASE_URL):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        pkce = generate_pkce()
        force = bool(kwargs.get("force_login", False))

        def build(state: str, redirect_uri: str):
            return build_auth_url(pkce.challenge, state, redirect_uri, force), \
                "Complete ChatGPT login in your browser."

        result, redirect_uri = run_callback_flow(
            port=CALLBACK_PORT, path=CALLBACK_PATH, build_auth_url=build,
            open_browser=kwargs.get("open_browser", True),
            redirect_host=CALLBACK_HOST,
        )
        code = result.code if result else prompt_manual_code(self.id)
        try:
            payload = post_form(TOKEN_URL, {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": pkce.verifier,
            }, timeout=30.0).json()
        except HttpStatusError as exc:
            raise AuthFlowError(f"ChatGPT token exchange failed (HTTP {exc.status})") from None
        return credentials_from_payload(payload)

    def refresh(self, creds: Credentials) -> Credentials:
        if not creds.refresh:
            raise ReloginRequired(self.id)
        try:
            payload = post_form(TOKEN_URL, {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": creds.refresh,
            }, timeout=30.0).json()
        except HttpStatusError as exc:
            raise ReloginRequired(self.id) from exc
        try:
            return credentials_from_payload(payload, creds.refresh)
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        return ResponsesTransport(
            access_token=creds.access, request=request,
            account_id=creds.account_id, base_url=self.gateway_base_url)
