"""Meta Muse Code subscription auth: RFC 8628 device flow + key mint.

Port of CLIProxyAPI internal/auth/meta/meta.go (MIT, see THIRD_PARTY.md):
device authorization at https://auth.meta.com/oidc/device/authorization/
(public client 1031625952748946, User-Agent muse-code/1.0.2), poll
https://auth.meta.com/oidc/device/token/ with the device_code grant, then
mint the subscription credential from the device token via
POST https://api.meta.ai/muse-code/key. The minted key carries the Muse
Code subscription (tier/quota) and does not expire on the device-token
timer; the device token is kept so refresh can re-mint.

Chat transport: OpenAI-compatible POST {base_url}/chat/completions where
base_url comes from the mint response (default https://api.meta.ai/v1).
"""

from __future__ import annotations

import time

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, now_ms, post_form, post_json
from ..store import Credentials
from .base import ChatRequest, ChatTransport, Provider
from .openai_compat import OpenAICompatTransport, list_openai_models

CLIENT_ID = "1031625952748946"
AUTH_HOST = "https://auth.meta.com"
DEVICE_AUTH_URL = f"{AUTH_HOST}/oidc/device/authorization/"
TOKEN_URL = f"{AUTH_HOST}/oidc/device/token/"
MINT_URL = "https://api.meta.ai/muse-code/key"
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
USER_AGENT = "muse-code/1.0.2"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_FLOW_TTL_S = 15 * 60.0
EXPIRY_SKEW_MS = 5 * 60 * 1000


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def mint_subscription_key(dca_token: str) -> dict:
    """Exchange the device token for the Muse Code subscription credential."""
    try:
        return post_json(MINT_URL, {"dca_token": dca_token}, headers={
            "Authorization": f"Bearer {dca_token}", "User-Agent": USER_AGENT,
        }, timeout=30.0).json()
    except HttpStatusError as exc:
        raise AuthFlowError(f"Muse key mint failed (HTTP {exc.status})") from None


def credentials_from_mint(dca_token: str, token_payload: dict,
                          minted: dict | None) -> Credentials:
    expires_in = token_payload.get("expires_in")
    dca_expires = now_ms() + 3600 * 1000 - EXPIRY_SKEW_MS
    if isinstance(expires_in, (int, float)) and expires_in >= 0:
        dca_expires = now_ms() + int(expires_in * 1000) - EXPIRY_SKEW_MS
    if minted and minted.get("api_key"):  # Muse minted subscription key (OAuth-derived)
        key = minted["api_key"]  # Muse minted subscription key (OAuth-derived)
        return Credentials(
            access=key,
            expires=0,  # minted key has no device-token timer
            email=(minted.get("user_email") or "").lower(),
            account_id=str(minted.get("subs_tier_id") or ""),
            extra={"dca_token": dca_token,
                   "base_url": minted.get("base_url") or DEFAULT_BASE_URL,
                   "tier": minted.get("subs_tier_name") or ""},
        )
    # Mint unavailable: fall back to the short-lived device token.
    return Credentials(access=dca_token, expires=dca_expires,
                       extra={"base_url": DEFAULT_BASE_URL})


class MuseProvider(Provider):
    id = "muse"
    display = "Muse Code (Meta)"

    def __init__(self, progress=None, gateway_base_url: str = DEFAULT_BASE_URL):
        super().__init__(progress)
        self.gateway_base_url = gateway_base_url

    def login(self, **kwargs) -> Credentials:
        try:
            data = post_form(DEVICE_AUTH_URL, {"client_id": CLIENT_ID},
                             headers=_headers(), timeout=30.0).json()
        except HttpStatusError as exc:
            raise AuthFlowError(f"Muse device flow failed (HTTP {exc.status})") from None
        device_code, user_code = data.get("device_code"), data.get("user_code")
        verify = data.get("verification_uri_complete") or data.get("verification_uri")
        if not device_code or not user_code:
            raise AuthFlowError("Muse device flow response missing codes")
        print(f"\nOpen {verify or AUTH_HOST} and enter code: {user_code}\n")
        interval = data.get("interval")
        poll_s = float(interval) if isinstance(interval, (int, float)) and interval > 0 \
            else DEFAULT_POLL_INTERVAL_S
        ttl = data.get("expires_in")
        deadline = time.time() + (float(ttl) if isinstance(ttl, (int, float)) else DEFAULT_FLOW_TTL_S)
        while time.time() < deadline:
            time.sleep(poll_s)
            try:
                payload = post_form(TOKEN_URL, {
                    "grant_type": DEVICE_CODE_GRANT,
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                }, headers=_headers(), timeout=30.0).json()
            except HttpStatusError as exc:
                if exc.status in (400, 428):
                    continue  # authorization_pending / slow_down style
                raise AuthFlowError(f"Muse token poll failed (HTTP {exc.status})") from None
            if payload.get("error") in ("authorization_pending", "slow_down"):
                if payload.get("error") == "slow_down":
                    poll_s += 5.0
                continue
            if payload.get("error"):
                raise AuthFlowError("Muse device authorization denied or expired")
            dca = payload.get("access_token")
            if not isinstance(dca, str) or not dca:
                raise AuthFlowError("Muse token response missing access token")
            try:
                minted = mint_subscription_key(dca)
            except AuthFlowError as exc:
                print(f"warning: {exc}; storing the device token instead")
                minted = None
            creds = credentials_from_mint(dca, payload, minted)
            if minted:
                print(f"Muse Code tier: {minted.get('subs_tier_name') or 'unknown'}"
                      f"{' (inactive)' if not minted.get('is_subs_active', True) else ''}")
            return creds
        raise AuthFlowError("Muse device authorization timed out")

    def refresh(self, creds: Credentials) -> Credentials:
        dca = (creds.extra or {}).get("dca_token")
        if not dca:
            raise ReloginRequired(self.id)
        try:
            minted = mint_subscription_key(dca)
        except AuthFlowError as exc:
            raise ReloginRequired(self.id) from exc
        refreshed = credentials_from_mint(dca, {}, minted)
        refreshed.email = refreshed.email or creds.email
        return refreshed

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        base = (creds.extra or {}).get("base_url") or self.gateway_base_url
        return OpenAICompatTransport(
            base_url=base, access_token=creds.access, request=request,
            extra_headers={"User-Agent": USER_AGENT})

    def list_models(self, creds: Credentials) -> list[str]:
        base = (creds.extra or {}).get("base_url") or self.gateway_base_url
        return list_openai_models(base, creds.access,
                                  extra_headers={"User-Agent": USER_AGENT})
