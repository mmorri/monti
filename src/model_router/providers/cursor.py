"""Cursor subscription auth: standalone PKCE browser login + poll.

Port of opencodex src/oauth/cursor.ts (MIT, see THIRD_PARTY.md):
login https://cursor.com/loginDeepControl (challenge only, never verifier);
poll GET https://api2.cursor.sh/auth/poll?uuid&verifier (404 = pending);
refresh POST https://api2.cursor.sh/auth/exchange_user_api_key.

NOTE on claude-code-proxy drift: its cursor auth posts refresh to
{base}/auth/refresh instead. This port follows opencodex's
/auth/exchange_user_api_key endpoint.
"""

from __future__ import annotations

import time
import urllib.parse
import uuid as uuid_mod
import webbrowser

from ..errors import AuthFlowError, ReloginRequired
from ..http import HttpStatusError, get, now_ms, post_json
from ..jwt_util import decode_jwt_payload
from ..pkce import generate_pkce
from ..store import Credentials
from .base import Provider

LOGIN_URL = "https://cursor.com/loginDeepControl"
POLL_URL = "https://api2.cursor.sh/auth/poll"
REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"

POLL_MAX_ATTEMPTS = 150
POLL_BASE_DELAY_S = 1.0
POLL_MAX_DELAY_S = 10.0
POLL_BACKOFF = 1.2
EXPIRY_SKEW_MS = 5 * 60 * 1000
FALLBACK_TTL_MS = 60 * 60 * 1000
REFRESH_TIMEOUT_S = 15.0
REFRESH_ATTEMPTS = 3
REFRESH_RETRY_BASE_S = 0.3

TERMINAL_POLL_STATUSES = frozenset({400, 401, 403, 410})
RETRYABLE_REFRESH_STATUSES = frozenset({429, 500, 502, 503, 504})


def build_login_url(challenge: str, uuid: str) -> str:
    params = urllib.parse.urlencode(
        {"challenge": challenge, "uuid": uuid, "mode": "login", "redirectTarget": "cli"}
    )
    return f"{LOGIN_URL}?{params}"


def token_expiry_ms(access_token: str) -> int:
    payload = decode_jwt_payload(access_token) or {}
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp * 1000) - EXPIRY_SKEW_MS
    return now_ms() + FALLBACK_TTL_MS


def credentials_from_tokens(access_token: str, refresh_token: str) -> Credentials:
    payload = decode_jwt_payload(access_token) or decode_jwt_payload(refresh_token) or {}
    sub = payload.get("sub")
    account_id = sub if isinstance(sub, str) and sub else (str(sub) if isinstance(sub, int) else "")
    email = payload.get("email")
    return Credentials(
        access=access_token,
        refresh=refresh_token,
        expires=token_expiry_ms(access_token),
        account_id=account_id,
        email=email.lower() if isinstance(email, str) and email else "",
    )


def poll_once(uuid: str, verifier: str) -> tuple[str, str] | None:
    """One poll round. Returns (access, refresh), None while pending."""
    url = f"{POLL_URL}?{urllib.parse.urlencode({'uuid': uuid, 'verifier': verifier})}"
    try:
        resp = get(url, timeout=30.0)
    except HttpStatusError as exc:
        if exc.status == 404:
            return None
        if exc.status in TERMINAL_POLL_STATUSES:
            raise AuthFlowError(
                f"Cursor login rejected by the auth server (HTTP {exc.status}); "
                "start a new login"
            ) from None
        raise
    data = resp.json()
    access, refresh = data.get("accessToken"), data.get("refreshToken")
    if not access or not refresh:
        raise AuthFlowError("Cursor auth response missing tokens")
    return access, refresh


class CursorProvider(Provider):
    id = "cursor"
    display = "Cursor"

    def login(self, **kwargs) -> Credentials:
        pkce = generate_pkce()
        uuid = uuid_mod.uuid4().hex if kwargs.get("compact_uuid") else str(uuid_mod.uuid4())
        url = build_login_url(pkce.challenge, uuid)
        print("\nApprove the Cursor login in your browser, then return here.")
        print(f"Open this URL:\n  {url}\n")
        if kwargs.get("open_browser", True):
            try:
                webbrowser.open(url, new=1)
            except Exception:
                pass
        delay = kwargs.get("poll_base_delay_s", POLL_BASE_DELAY_S)
        errors = 0
        attempts = kwargs.get("poll_max_attempts", POLL_MAX_ATTEMPTS)
        for _ in range(attempts):
            time.sleep(delay)
            try:
                tokens = poll_once(uuid, pkce.verifier)
                errors = 0
            except AuthFlowError:
                raise
            except Exception:
                errors += 1
                if errors >= 3:
                    raise AuthFlowError("Too many consecutive errors during Cursor auth polling")
                delay = min(delay * POLL_BACKOFF, POLL_MAX_DELAY_S)
                continue
            if tokens is None:
                delay = min(delay * POLL_BACKOFF, POLL_MAX_DELAY_S)
                continue
            return credentials_from_tokens(*tokens)
        raise AuthFlowError("Cursor authentication polling timeout")

    def refresh(self, creds: Credentials) -> Credentials:
        import random

        last: Exception | None = None
        for attempt in range(REFRESH_ATTEMPTS):
            try:
                resp = post_json(
                    REFRESH_URL, {},
                    headers={"Authorization": f"Bearer {creds.refresh}"},
                    timeout=REFRESH_TIMEOUT_S,
                )
            except HttpStatusError as exc:
                if exc.status in RETRYABLE_REFRESH_STATUSES:
                    last = exc
                else:
                    raise ReloginRequired(self.id) from None
            except Exception as exc:  # transient network error
                last = exc
            else:
                data = resp.json()
                access = data.get("accessToken") or data.get("access_token") or ""
                refresh = data.get("refreshToken") or data.get("refresh_token") or creds.refresh
                if not access:
                    raise ReloginRequired(self.id)
                return credentials_from_tokens(access, refresh)
            time.sleep(REFRESH_RETRY_BASE_S * (2 ** attempt) * (0.8 + random.random() * 0.4))
        raise ReloginRequired(self.id) from last
