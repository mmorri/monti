"""Authenticated provider execution: refresh-before-expiry, 401 handling.

Per SPEC: on HTTP 401 refresh once, retry once, then fail with
RELOGIN_REQUIRED:<provider> — never silently degrade. Transient tier
failures (429/5xx/network) walk the ladder: remaining wallets in the same
tier first, then the next rung up. Quota-drained providers cool down for
policy.cooldown_seconds so rotation skips them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

from .config import TIERS, RouterConfig
from .errors import ReloginRequired
from .http import HttpStatusError
from .providers import create as create_provider
from .providers.base import ChatRequest, ChatTransport, Provider
from .store import Credentials, TokenStore

# Transient upstream failures worth retrying against the next wallet.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Error text that means "this wallet is spent for now" (not broken auth).
QUOTA_SIGNALS = ("quota", "rate limit", "rate_limit", "ratelimit",
                 "permission_denied", "insufficient", "overloaded",
                 "capacity", "too many requests")


def retryable_failure(exc: Exception) -> bool:
    if isinstance(exc, HttpStatusError):
        return exc.status in RETRYABLE_STATUSES
    return isinstance(exc, OSError)  # URLError / dropped connections


def quota_exhausted(exc: Exception) -> bool:
    """True when the provider refused on quota/rate grounds: cool it down."""
    if isinstance(exc, HttpStatusError) and exc.status == 429:
        return True
    return any(signal in str(exc).lower() for signal in QUOTA_SIGNALS)


class Gateway:
    def __init__(self, config: RouterConfig, store: TokenStore | None = None):
        self.config = config
        self.store = store or TokenStore()
        self._providers: dict[str, Provider] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._cooldowns: dict[str, float] = {}  # provider -> epoch when back in rotation

    def _lock_for(self, provider_id: str) -> threading.Lock:
        return self._locks.setdefault(provider_id, threading.Lock())

    def cooling(self, provider_id: str) -> bool:
        """True while a quota-drained provider stays out of rotation."""
        return self._cooldowns.get(provider_id, 0) > time.time()

    def mark_cooldown(self, provider_id: str,
                      seconds: float | None = None) -> None:
        self._cooldowns[provider_id] = (
            time.time() + (self.config.cooldown_seconds if seconds is None else seconds))

    def provider_for(self, provider_id: str) -> Provider:
        if provider_id not in self._providers:
            options = dict(self.config.provider_options.get(provider_id, {}))
            self._providers[provider_id] = create_provider(provider_id, **options)
        return self._providers[provider_id]

    def credentials(self, provider_id: str) -> Credentials:
        # Serialized per provider: concurrent 401s must not double-refresh a
        # (possibly rotating) grant.
        with self._lock_for(provider_id):
            provider = self.provider_for(provider_id)
            creds = self.store.load(provider_id)
            if creds is None:
                raise ReloginRequired(provider_id)
            if provider.needs_refresh(creds):
                creds = provider.refresh(creds)
                self.store.save(provider_id, creds)
            return creds

    def chat(self, provider_id: str, request: ChatRequest):
        """Open a chat transport, refreshing first when expired.

        Returns (transport, refresher_for_401) where the refresher performs the
        single 401 refresh+reopen and raises ReloginRequired when dead.
        """
        creds = self.credentials(provider_id)
        provider = self.provider_for(provider_id)
        transport = provider.open_chat(creds, request)

        def reopen_after_401() -> ChatTransport:
            with self._lock_for(provider_id):
                fresh = provider.refresh(self.store.load(provider_id) or creds)
                self.store.save(provider_id, fresh)
                return provider.open_chat(fresh, request)

        return transport, reopen_after_401

    def tier_status(self) -> dict[str, dict]:
        """Startup probe: per-tier pools with per-candidate auth/transport state.

        A tier is ready when at least one pool entry is logged in and
        transport-capable. Cooling providers are reported but not ready.
        """
        from .errors import ProviderTransportUnavailable

        status: dict[str, dict] = {}
        for tier in TIERS:
            candidates = []
            for entry in self.config.tiers.get(tier, []):
                creds = self.store.load(entry.provider)
                candidate = {"provider": entry.provider, "model": entry.model,
                             "auth": bool(creds), "transport": "ok", "ok": False}
                if not creds:
                    candidate["transport"] = "no-auth"
                elif self.cooling(entry.provider):
                    candidate["transport"] = "cooling-down"
                else:
                    try:
                        self.provider_for(entry.provider).open_chat(
                            creds, ChatRequest(messages=[], model=entry.model))
                    except ProviderTransportUnavailable as exc:
                        candidate["transport"] = f"unavailable: {exc}"
                    except Exception as exc:  # noqa: BLE001
                        candidate["transport"] = f"error: {exc}"
                    else:
                        candidate["ok"] = True
                candidates.append(candidate)
            status[tier] = {"ok": any(c["ok"] for c in candidates),
                            "candidates": candidates}
        return status
