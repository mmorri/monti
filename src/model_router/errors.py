"""Error types. Token values must never appear in error messages."""


class RouterError(Exception):
    """Base error; safe to surface to the local user."""


class ReloginRequired(RouterError):
    """Provider grant is dead: refresh failed, user must log in again."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"RELOGIN_REQUIRED:{provider}")


class NoSubscriptionAuth(RouterError):
    """No provider has stored subscription tokens; proxy refuses to start."""


class ProviderDisabled(RouterError):
    """Provider tier cannot serve requests (no auth or transport unavailable)."""

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        super().__init__(f"provider '{provider}' unavailable: {reason}")


class ProviderTransportUnavailable(ProviderDisabled):
    """Auth works but the chat transport was not ported (never a fake flow)."""


class AuthFlowError(RouterError):
    """Login/refresh failed for a reason other than a dead grant."""
