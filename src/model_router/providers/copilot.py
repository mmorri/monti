"""Muse subscription auth: GitHub device flow + copilot_internal exchange.

Port of opencodex src/oauth/github-copilot.ts (MIT, see THIRD_PARTY.md):
device code via the public VS Code GitHub OAuth app
(Iv1.b507a08c87ecfe98, scope read:user), poll
https://github.com/login/oauth/access_token with the device_code grant, then
GET https://api.github.com/copilot_internal/v2/token for the short-lived
Copilot token. Refresh renews the durable GitHub grant and re-exchanges.

Chat transport: OpenAI-compatible POST
https://api.githubcopilot.com/chat/completions.
"""

from __future__ import annotations

import time
import urllib.parse

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, get, now_ms, post_form
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .openai_compat import OpenAICompatTransport, list_openai_models

OAUTH_CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
OAUTH_SCOPE = "read:user"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_FLOW_TTL_S = 15 * 60.0
EXPIRY_SKEW_MS = 2 * 60 * 1000
TERMINAL_ERRORS = frozenset({"invalid_grant", "access_denied", "expired_token"})
GATEWAY_BASE_URL = "https://api.githubcopilot.com"

EDITOR_HEADERS = {
    "Editor-Version": "model-router/0.1.0",
    "Editor-Plugin-Version": "model-router/0.1.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "model-router",
    "Accept": "application/json",
}


def device_verify_url(user_code: str) -> str:
    code = user_code.strip()
    if not code:
        raise AuthFlowError("GitHub device flow returned an invalid user code")
    allowed = all(ch.isalnum() or ch == "-" for ch in code)
    if not allowed:
        raise AuthFlowError("GitHub device flow returned an invalid user code")
    return f"https://github.com/login/device?user_code={urllib.parse.quote(code)}"


class CopilotProvider(Provider):
    id = "copilot"
    display = "GitHub Copilot"

    def __init__(self, progress=None, gateway_base_url: str = GATEWAY_BASE_URL):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        try:
            data = post_form(DEVICE_CODE_URL, {
                "client_id": OAUTH_CLIENT_ID, "scope": OAUTH_SCOPE,
            }, headers={"Accept": "application/json"}, timeout=30.0).json()
        except HttpStatusError as exc:
            raise AuthFlowError(f"GitHub device code request failed (HTTP {exc.status})") from None
        user_code, device_code = data.get("user_code"), data.get("device_code")
        if not user_code or not device_code:
            raise AuthFlowError("GitHub device flow missing codes")
        print(f"\nOpen {device_verify_url(user_code)} and enter code: {user_code}\n")
        interval = data.get("interval")
        poll_s = max(float(interval) if isinstance(interval, (int, float)) else
                     DEFAULT_POLL_INTERVAL_S, 1.0)
        ttl = data.get("expires_in")
        deadline = time.time() + (float(ttl) if isinstance(ttl, (int, float)) else DEFAULT_FLOW_TTL_S)
        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                payload = post_form(ACCESS_TOKEN_URL, {
                    "client_id": OAUTH_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }, headers={"Accept": "application/json"}, timeout=30.0).json()
            except HttpStatusError:
                continue
            error = payload.get("error")
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    poll_s += 5.0
                continue
            if error:
                raise AuthFlowError("GitHub device authorization denied or expired")
            github_access = payload.get("access_token")
            github_refresh = payload.get("refresh_token") or ""
            if not github_access:
                raise AuthFlowError("GitHub device flow missing access token")
            return self._exchange(github_access, github_refresh)
        raise AuthFlowError("GitHub device authorization timed out")

    def _exchange(self, github_access: str, github_refresh: str) -> Credentials:
        headers = dict(EDITOR_HEADERS)
        headers["Authorization"] = f"token {github_access}"
        try:
            payload = get(COPILOT_TOKEN_URL, headers=headers, timeout=30.0).json()
        except HttpStatusError as exc:
            raise AuthFlowError(f"Copilot token exchange failed (HTTP {exc.status})") from None
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise AuthFlowError("Copilot token exchange missing token")
        expires_at = payload.get("expires_at")
        refresh_in = payload.get("refresh_in")
        if isinstance(expires_at, (int, float)):
            expires = int(expires_at * 1000) - EXPIRY_SKEW_MS
        elif isinstance(refresh_in, (int, float)):
            expires = now_ms() + int(refresh_in * 1000) - EXPIRY_SKEW_MS
        else:
            expires = now_ms() + 30 * 60 * 1000 - EXPIRY_SKEW_MS
        endpoints = payload.get("endpoints") or {}
        extra = {"api_base": endpoints["api"]} if isinstance(endpoints, dict) and endpoints.get("api") else None
        # refresh holds the durable GitHub grant (gho_ access or refresh token).
        return Credentials(access=token, refresh=github_refresh or github_access,
                           expires=expires, extra=extra)

    def refresh(self, creds: Credentials) -> Credentials:
        if not creds.refresh:
            raise ReloginRequired(self.id)
        grant = creds.refresh
        if not grant.startswith("gho_"):
            try:
                payload = post_form(ACCESS_TOKEN_URL, {
                    "client_id": OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": grant,
                }, headers={"Accept": "application/json"}, timeout=30.0).json()
            except HttpStatusError as exc:
                raise ReloginRequired(self.id) from exc
            if payload.get("error") in TERMINAL_ERRORS or not payload.get("access_token"):
                raise ReloginRequired(self.id)
            grant = payload["access_token"]
            following = payload.get("refresh_token") or creds.refresh
        else:
            following = grant
        try:
            fresh = self._exchange(grant, "")
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc
        fresh.refresh = following
        return fresh

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        base = (creds.extra or {}).get("api_base") or self.gateway_base_url
        headers = dict(EDITOR_HEADERS)
        headers["Copilot-Vision-Request"] = "true"
        return OpenAICompatTransport(
            base_url=base, access_token=creds.access, request=request,
            extra_headers=headers)

    def list_models(self, creds: Credentials) -> list[str]:
        base = (creds.extra or {}).get("api_base") or self.gateway_base_url
        return list_openai_models(base, creds.access, extra_headers=EDITOR_HEADERS)
