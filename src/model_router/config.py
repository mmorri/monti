"""config.yaml loading. No API keys are ever read from config or env."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODES = ("auto", "fast-only", "strong-only", "weak-first-escalate")

_DEFAULT_ESCALATION_SIGNALS = (
    "this isn't working",
    "this is not working",
    "try a different approach",
    "that didn't work",
    "still failing",
    "still broken",
)


@dataclass
class Tier:
    provider: str
    model: str


@dataclass
class Escalation:
    tool_error_threshold: int = 3
    signals: tuple[str, ...] = _DEFAULT_ESCALATION_SIGNALS


@dataclass
class RouterConfig:
    fast: Tier = field(default_factory=lambda: Tier("kimi", "kimi-latest"))
    strong: Tier = field(default_factory=lambda: Tier("anthropic", "claude-sonnet-4-5"))
    default_mode: str = "auto"
    # Strict startup: refuse unless every configured tier is logged in AND
    # transport-ready. A router with missing pieces is not a router.
    require_all_tiers: bool = True
    escalation: Escalation = field(default_factory=Escalation)
    provider_options: dict = field(default_factory=dict)
    log_dir: str = ""
    port: int = 8787

    @classmethod
    def load(cls, path: Path | None) -> "RouterConfig":
        cfg = cls()
        if path is None:
            return cfg
        raw = yaml.safe_load(Path(path).read_text("utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"invalid config in {path}: top level must be a mapping")
        tiers = raw.get("tiers") or {}
        for name in ("fast", "strong"):
            node = tiers.get(name) or {}
            setattr(cfg, name, Tier(
                provider=str(node.get("provider", getattr(cfg, name).provider)),
                model=str(node.get("model", getattr(cfg, name).model)),
            ))
        policy = raw.get("policy") or {}
        mode = str(policy.get("default_mode", cfg.default_mode))
        if mode not in MODES:
            raise ValueError(f"invalid default_mode '{mode}' (expected one of {MODES})")
        cfg.default_mode = mode
        cfg.require_all_tiers = bool(policy.get("require_all_tiers", True))
        esc = policy.get("escalation") or {}
        cfg.escalation = Escalation(
            tool_error_threshold=int(esc.get("tool_error_threshold", 3)),
            signals=tuple(str(s) for s in esc.get("signals", _DEFAULT_ESCALATION_SIGNALS)),
        )
        cfg.provider_options = dict(raw.get("providers") or {})
        cfg.log_dir = str(raw.get("log_dir", cfg.log_dir))
        cfg.port = int(raw.get("port", cfg.port))
        _reject_api_keys(raw, str(path))
        return cfg


def _reject_api_keys(raw: object, where: str) -> None:
    """Fail closed if config contains anything shaped like key material."""
    text = repr(raw).lower()
    for needle in ("api_key", "apikey", "api-key", "secret_key", "client_secret"):
        if needle in text:
            raise ValueError(f"refusing {where}: contains forbidden field '{needle}' "
                             "(subscription-only router accepts no API keys)")
