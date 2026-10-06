"""config.yaml loading. No API keys are ever read from config or env."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Tiers in ascending quality/cost order. The classifier maps
# trivial|easy|medium|hard verdicts onto this ladder; failover walks it
# upward (same-tier wallets first, then the next rung up).
TIERS = ("everyday", "moderate", "high", "very_high")

MODES = ("auto", "everyday-only", "moderate-only", "high-only",
         "very-high-only", "weak-first-escalate")

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


def _default_pools() -> dict[str, list[Tier]]:
    return {
        "everyday": [Tier("kimi", "kimi-for-coding-highspeed")],
        "moderate": [Tier("kimi", "kimi-for-coding")],
        "high": [Tier("muse", "muse-spark-1.3")],
        "very_high": [Tier("anthropic", "claude-opus-4-5")],
    }


@dataclass
class RouterConfig:
    # Each tier is an ordered pool of interchangeable candidates:
    # same quality band, different subscription wallets. List order is
    # wallet preference — the first available candidate serves.
    tiers: dict[str, list[Tier]] = field(default_factory=_default_pools)
    default_mode: str = "auto"
    # Strict startup: refuse unless every tier has at least one logged-in,
    # transport-ready candidate. A router with missing pieces is not a router.
    require_all_tiers: bool = True
    # How long a 429/quota-drained provider stays out of rotation (seconds).
    cooldown_seconds: int = 300
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
        tiers = raw.get("tiers")
        if tiers is None:
            tiers = {}
        if not isinstance(tiers, dict):
            raise ValueError(f"invalid config in {path}: 'tiers' must be a mapping")
        if any(name in tiers for name in ("fast", "strong")):
            raise ValueError(
                f"invalid tiers in {path}: 'fast'/'strong' were replaced by "
                f"everyday/moderate/high/very_high pools in v0.2 — "
                f"rewrite each tier as a list, e.g. "
                f"everyday: [{{provider: kimi, model: kimi-for-coding-highspeed}}]")
        pools: dict[str, list[Tier]] = {}
        for name in TIERS:
            if name not in tiers:
                pools[name] = cfg.tiers[name]  # unspecified tiers keep defaults
                continue
            node = tiers[name]
            if isinstance(node, dict):
                # Single-candidate shorthand: high: {provider: x, model: y}.
                node = [node]
            if not isinstance(node, list) or not node:
                raise ValueError(
                    f"invalid tiers in {path}: tier '{name}' must be a "
                    f"non-empty list of {{provider, model}} entries")
            entries = []
            for item in node:
                if not isinstance(item, dict) or not item.get("provider") or not item.get("model"):
                    raise ValueError(
                        f"invalid tiers in {path}: tier '{name}' entries need "
                        f"provider + model")
                entries.append(Tier(provider=str(item["provider"]),
                                    model=str(item["model"])))
            pools[name] = entries
        cfg.tiers = pools
        policy = raw.get("policy") or {}
        mode = str(policy.get("default_mode", cfg.default_mode))
        if mode not in MODES:
            raise ValueError(f"invalid default_mode '{mode}' (expected one of {MODES})")
        cfg.default_mode = mode
        cfg.require_all_tiers = bool(policy.get("require_all_tiers", True))
        cfg.cooldown_seconds = int(policy.get("cooldown_seconds", 300))
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
