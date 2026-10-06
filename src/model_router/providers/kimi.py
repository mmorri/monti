"""Kimi (Kimi Code) subscription auth: device authorization grant.

Port of opencodex src/oauth/kimi.ts (MIT, see THIRD_PARTY.md), cross-checked
against CLIProxyAPI internal/auth/kimi/kimi.go for the two Kimi regions:
global kimi.ai (auth.kimi.ai + api.kimi.ai/coding/v1, the default) and CN
kimi.com (auth.kimi.com + api.kimi.com/coding/v1 via the domain option).
Public client 17e5f671-d194-4dfb-9706-5516cb48c098, POST
/api/oauth/device_authorization then POST /api/oauth/token with the
device_code grant; refresh via the refresh_token grant. Kimi CLI headers
(User-Agent KimiCLI/0.14.0, X-Msh-*) identify the client; device id
persists in the config dir, mode 0600.
"""

from __future__ import annotations

import os
import platform
import time
import uuid as uuid_mod
from pathlib import Path

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, now_ms, post_form
from ..jwt_util import jwt_claim
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .openai_compat import OpenAICompatTransport, list_openai_models

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CLI_VERSION = "0.14.0"
DEVICE_ID_FILENAME = "kimi-device-id"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_FLOW_TTL_S = 15 * 60.0
EXPIRY_SKEW_MS = 5 * 60 * 1000

# Global first: kimi.ai is the international subscription; kimi.com serves CN.
DOMAINS: dict[str, tuple[str, str]] = {
    "kimi.ai": ("https://auth.kimi.ai", "https://api.kimi.ai/coding/v1"),
    "kimi.com": ("https://auth.kimi.com", "https://api.kimi.com/coding/v1"),
}
DEFAULT_DOMAIN = "kimi.ai"


def hosts_for(domain: str) -> tuple[str, str]:
    """(oauth_host, gateway_base_url) for a Kimi domain."""
    return DOMAINS.get(domain.strip().lower(), DOMAINS[DEFAULT_DOMAIN])


# Live catalog observed on the kimi.ai coding gateway (chat serves these;
# /models rejects subscription tokens, so this is the documented fallback).
KNOWN_CODING_MODELS = ("k3", "k3-256k", "kimi-for-coding",
                       "kimi-for-coding-highspeed")


def _device_id(config_dir: Path) -> str:
    path = config_dir / DEVICE_ID_FILENAME
    try:
        existing = path.read_text("utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    device_id = uuid_mod.uuid4().hex
    config_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(device_id + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return device_id


def common_headers(config_dir: Path) -> dict[str, str]:
    system = platform.system()
    label = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(system, system)
    model = " ".join(p for p in (label, platform.release(), platform.machine()) if p)
    return {
        "User-Agent": f"KimiCLI/{KIMI_CLI_VERSION}",
        "X-Msh-Platform": "kimi_code_cli",
        "X-Msh-Version": KIMI_CLI_VERSION,
        "X-Msh-Device-Name": platform.node(),
        "X-Msh-Device-Model": model,
        "X-Msh-Os-Version": platform.version(),
        "X-Msh-Device-Id": _device_id(config_dir),
    }


def credentials_from_payload(payload: dict, refresh_fallback: str = "",
                             domain: str = "") -> Credentials:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token") or refresh_fallback
    if not isinstance(access, str) or not access:
        raise AuthFlowError("Kimi token response missing access token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthFlowError("Kimi token response did not include a refresh token")
    expires_in = payload.get("expires_in")
    expires = now_ms() + 3600 * 1000 - EXPIRY_SKEW_MS
    if isinstance(expires_in, (int, float)) and expires_in >= 0:
        expires = now_ms() + int(expires_in * 1000) - EXPIRY_SKEW_MS
    account_id = jwt_claim(access, "user_id") or jwt_claim(refresh, "user_id") \
        or jwt_claim(access, "sub") or jwt_claim(refresh, "sub") or ""
    email = ((jwt_claim(access, "email") or jwt_claim(refresh, "email")) or "").lower()
    return Credentials(access=access, refresh=refresh, expires=expires,
                       account_id=account_id, email=email,
                       extra={"domain": domain} if domain else None)


class KimiProvider(Provider):
    id = "kimi"
    display = "Kimi"

    def __init__(self, progress=None, domain: str = DEFAULT_DOMAIN,
                 oauth_host: str | None = None,
                 config_dir: Path | None = None,
                 gateway_base_url: str | None = None):
        super().__init__(progress)
        self.domain = domain if domain in DOMAINS else DEFAULT_DOMAIN
        default_oauth, default_gateway = hosts_for(self.domain)
        self.oauth_host = (oauth_host or default_oauth).rstrip("/")
        self.config_dir = config_dir or (Path.home() / ".config" / "model-router")
        self.gateway_base_url = gateway_base_url or default_gateway

    def _headers(self) -> dict[str, str]:
        return common_headers(self.config_dir)

    def _host_for(self, creds: Credentials) -> str:
        """Refresh against the region the token was issued on, not the
        current config default — an account on kimi.ai stays on kimi.ai."""
        stored = (creds.extra or {}).get("domain")
        if stored in DOMAINS and stored != self.domain and not self._overridden():
            return hosts_for(stored)[0]
        return self.oauth_host

    def _overridden(self) -> bool:
        default_oauth, _default_gateway = hosts_for(self.domain)
        return self.oauth_host.rstrip("/") != default_oauth.rstrip("/")

    def _gateway_for(self, creds: Credentials) -> str:
        stored = (creds.extra or {}).get("domain")
        if stored in DOMAINS and stored != self.domain and not self._overridden():
            return hosts_for(stored)[1]
        return self.gateway_base_url

    def _headers(self) -> dict[str, str]:
        return common_headers(self.config_dir)

    def login(self, **kwargs) -> Credentials:
        try:
            resp = post_form(
                f"{self.oauth_host}/api/oauth/device_authorization",
                {"client_id": CLIENT_ID}, headers=self._headers(), timeout=30.0)
        except HttpStatusError as exc:
            raise AuthFlowError(f"Kimi device authorization failed (HTTP {exc.status})") from None
        data = resp.json()
        user_code = data.get("user_code")
        device_code = data.get("device_code")
        verify_uri = data.get("verification_uri_complete") or data.get("verification_uri")
        if not user_code or not device_code:
            raise AuthFlowError("Kimi device authorization missing codes")
        print(f"\nOpen {verify_uri or self.oauth_host} and enter code: {user_code}\n")
        interval = data.get("interval")
        poll_s = float(interval) if isinstance(interval, (int, float)) and interval > 0 \
            else DEFAULT_POLL_INTERVAL_S
        ttl = data.get("expires_in")
        deadline = time.time() + (float(ttl) if isinstance(ttl, (int, float)) else DEFAULT_FLOW_TTL_S)
        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                payload = post_form(
                    f"{self.oauth_host}/api/oauth/token",
                    {"client_id": CLIENT_ID, "device_code": device_code,
                     "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                    headers=self._headers(), timeout=30.0).json()
            except HttpStatusError as exc:
                if exc.status in (400, 428):
                    continue  # authorization_pending / slow_down style
                raise AuthFlowError(f"Kimi token poll failed (HTTP {exc.status})") from None
            if payload.get("error") in ("authorization_pending", "slow_down"):
                if payload.get("error") == "slow_down":
                    poll_s += 5.0
                continue
            if payload.get("error"):
                raise AuthFlowError("Kimi device authorization denied or expired")
            return credentials_from_payload(payload, domain=self.domain)
        raise AuthFlowError("Kimi device authorization timed out")

    def refresh(self, creds: Credentials) -> Credentials:
        if not creds.refresh:
            raise ReloginRequired(self.id)
        host = self._host_for(creds)
        try:
            payload = post_form(
                f"{host}/api/oauth/token",
                {"grant_type": "refresh_token", "refresh_token": creds.refresh,
                 "client_id": CLIENT_ID},
                headers=self._headers(), timeout=30.0).json()
        except HttpStatusError as exc:
            raise ReloginRequired(self.id) from exc
        if payload.get("error"):
            raise ReloginRequired(self.id)
        try:
            domain = (creds.extra or {}).get("domain") or self.domain
            return credentials_from_payload(payload, creds.refresh, domain=domain)
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        # The coding gateway sits behind a bot-signature firewall (Cloudflare
        # 1010) and rejects non-CLI clients — the Kimi CLI identity headers
        # are mandatory on chat calls, not just during OAuth.
        return OpenAICompatTransport(
            base_url=self._gateway_for(creds), access_token=creds.access,
            request=request, extra_headers=self._headers())

    def list_models(self, creds: Credentials) -> list[str]:
        live = list_openai_models(self._gateway_for(creds), creds.access,
                                  extra_headers=self._headers())
        if live:
            return live
        # The coding gateway rejects subscription tokens on /models (401)
        # while chat works fine — fall back to the catalog observed live
        # from this gateway (verified 2026-10-05).
        return list(KNOWN_CODING_MODELS)
