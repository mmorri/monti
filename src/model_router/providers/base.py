"""Provider interface: OAuth login/refresh + chat transport.

Every provider MUST implement login/refresh from a proven subscription flow
(see THIRD_PARTY.md). A provider whose chat transport was not ported raises
ProviderTransportUnavailable from open_chat — never a fake or partial flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..errors import ProviderTransportUnavailable
from ..http import now_ms
from ..store import Credentials


@dataclass
class ChatChunk:
    """Normalized streaming event emitted by every chat transport."""

    kind: str  # "delta" | "tool_call" | "usage" | "finish"
    text: str = ""
    tool_call: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    model: str
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None


class ChatTransport(ABC):
    @abstractmethod
    def run(self) -> Iterator[ChatChunk]:
        ...


Progress = Callable[[str], None]


class Provider(ABC):
    """One subscription identity: login, refresh, and (optionally) chat."""

    id: str = ""
    display: str = ""
    expiry_skew_ms: int = 5 * 60 * 1000

    def __init__(self, progress: Progress | None = None):
        self.progress = progress or (lambda _msg: None)

    def say(self, message: str) -> None:
        self.progress(f"[{self.id}] {message}")

    @abstractmethod
    def login(self, **kwargs) -> Credentials:
        """Interactive subscription login; returns fresh credentials."""

    @abstractmethod
    def refresh(self, creds: Credentials) -> Credentials:
        """Exchange the refresh token; raise ReloginRequired when dead."""

    def needs_refresh(self, creds: Credentials) -> bool:
        if not creds.expires:
            return False
        return now_ms() >= creds.expires

    def open_chat(self, creds: Credentials, request: ChatRequest, **kwargs) -> ChatTransport:
        raise ProviderTransportUnavailable(
            self.id,
            "chat transport not ported yet (see docs/TRANSPORTS.md); "
            "auth works, requests fail closed instead of faking a call.",
        )

    def list_models(self, creds: Credentials) -> list[str]:
        """Model catalog for a logged-in subscription; empty when unknown."""
        return []
