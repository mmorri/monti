"""Provider registry."""

from __future__ import annotations

from .anthropic import AnthropicProvider
from .base import Provider
from .copilot import CopilotProvider
from .cursor import CursorProvider
from .kimi import KimiProvider
from .meta import MuseProvider
from .openai_codex import OpenAIProvider
from .windsurf import WindsurfProvider
from .xai import XaiProvider
from .zai import ZaiProvider

REGISTRY: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "copilot": CopilotProvider,
    "cursor": CursorProvider,
    "kimi": KimiProvider,
    "muse": MuseProvider,
    "openai": OpenAIProvider,
    "windsurf": WindsurfProvider,
    "xai": XaiProvider,
    "zai": ZaiProvider,
}


def create(provider_id: str, **kwargs) -> Provider:
    try:
        factory = REGISTRY[provider_id]
    except KeyError:
        raise ValueError(f"unknown provider '{provider_id}' (known: {sorted(REGISTRY)})") from None
    accepted = {"gateway_base_url", "oauth_host", "config_dir", "progress"}
    return factory(**{k: v for k, v in kwargs.items() if k in accepted})
