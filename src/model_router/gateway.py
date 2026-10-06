"""Authenticated provider execution: refresh-before-expiry, 401 handling.

Per SPEC: on HTTP 401 refresh once, retry once, then fail with
RELOGIN_REQUIRED:<provider> — never silently degrade.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .config import RouterConfig
from .errors import ReloginRequired
from .http import HttpStatusError
from .providers import create as create_provider
from .providers.base import ChatRequest, ChatTransport, Provider
from .store import Credentials, TokenStore


class Gateway:
    def __init__(self, config: RouterConfig, store: TokenStore | None = None):
        self.config = config
        self.store = store or TokenStore()
        self._providers: dict[str, Provider] = {}

    def provider_for(self, provider_id: str) -> Provider:
        if provider_id not in self._providers:
            options = dict(self.config.provider_options.get(provider_id, {}))
            self._providers[provider_id] = create_provider(provider_id, **options)
        return self._providers[provider_id]

    def credentials(self, provider_id: str) -> Credentials:
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
            fresh = provider.refresh(self.store.load(provider_id) or creds)
            self.store.save(provider_id, fresh)
            return provider.open_chat(fresh, request)

        return transport, reopen_after_401

    def tier_status(self) -> dict[str, dict]:
        """Startup probe: which tiers can serve (auth present + transport ports)."""
        from .errors import ProviderTransportUnavailable

        status: dict[str, dict] = {}
        for tier in ("fast", "strong"):
            node = getattr(self.config, tier)
            creds = self.store.load(node.provider)
            entry = {"provider": node.provider, "model": node.model,
                     "auth": bool(creds), "transport": "ok", "ok": False}
            if not creds:
                entry["transport"] = "no-auth"
            else:
                try:
                    self.provider_for(node.provider).open_chat(
                        creds, ChatRequest(messages=[], model=node.model))
                except ProviderTransportUnavailable as exc:
                    entry["transport"] = f"unavailable: {exc}"
                except Exception as exc:  # noqa: BLE001
                    entry["transport"] = f"error: {exc}"
                else:
                    entry["ok"] = True
            status[tier] = entry
        return status
