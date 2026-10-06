"""Interactive onboarding: connect subscriptions, auto-pick models, save config.

`monti setup` walks the user through every provider login (skippable), then
probes each logged-in subscription's live catalog and builds the tier pools
from a benchmark-informed knowledge table filtered by what actually exists
on the account. The result is written to the user config
(~/.config/model-router/config.yaml) — everything stays hand-editable
afterwards; `monti models` verifies.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import yaml

from .config import TIERS
from .providers import REGISTRY

# Login order = value per wallet (frontier first), cursor excluded (chat
# transport unported), xai last (least common).
LOGIN_ORDER = ["anthropic", "zai", "kimi", "muse", "openai", "windsurf",
               "copilot", "xai"]

LOGIN_HINTS = {
    "zai": "pastes your GLM Coding Plan key (hidden input)",
    "copilot": "GitHub device code",
    "windsurf": "browser, loops back automatically",
}

# Benchmark-informed knowledge table (docs/MODELS.md is the reference).
# Ordered per tier: earlier = preferred burn order.
KNOWN_TIERS: dict[str, list[tuple[str, str]]] = {
    "everyday": [
        ("zai", "glm-5.3-flash"),
        ("kimi", "kimi-for-coding-highspeed"),
    ],
    "moderate": [
        ("kimi", "kimi-for-coding"),
        ("windsurf", "gpt-5-5-medium"),
        ("anthropic", "claude-haiku-4-5"),
    ],
    "high": [
        ("zai", "glm-5.3"),
        ("anthropic", "claude-sonnet-5-5"),
        ("kimi", "k3"),
        ("muse", "muse-spark-1.3"),
        ("openai", "gpt-6-sol"),
    ],
    "very_high": [
        ("anthropic", "claude-fable-5-1"),
        ("anthropic", "claude-opus-5-5"),
        ("openai", "gpt-6-astra"),
        ("muse", "muse-spark-1.3"),
        ("windsurf", "claude-fable-5-1-medium"),
    ],
}

# Classification pool, free-first (contributor tier trains on the ~2k
# excerpt in exchange for near-free quota).
KNOWN_CLASSIFIER: list[tuple[str, str]] = [
    ("muse", "muse-spark-1.3-contributor"),
    ("zai", "glm-5.3-flash"),
    ("kimi", "kimi-for-coding-highspeed"),
]


def model_matches(model: str, catalog: list[str] | None) -> bool:
    """A known ID is usable when the catalog confirms it exactly or as a
    dated snapshot prefix (claude-haiku-4-5 -> claude-haiku-4-5-20251001).
    No catalog (openai) = trust the knowledge table."""
    if catalog is None:
        return True
    return model in catalog or any(entry.startswith(model) for entry in catalog)


def _filter_known(known: list[tuple[str, str]], catalogs: dict[str, list[str] | None],
                   logged_in: set[str], notes: list[str]) -> list[dict]:
    picked = []
    for provider, model in known:
        if provider not in logged_in:
            continue
        if not model_matches(model, catalogs.get(provider)):
            notes.append(f"skipped {provider}/{model}: not in live catalog")
            continue
        picked.append({"provider": provider, "model": model})
    return picked


def build_config_dict(logged_in: set[str],
                      catalogs: dict[str, list[str] | None]) -> tuple[dict, list[str]]:
    """Pick pools from the knowledge table, filtered by logins + catalogs."""
    notes: list[str] = []
    tiers: dict[str, list[dict]] = {}
    for tier in TIERS:
        tiers[tier] = _filter_known(KNOWN_TIERS[tier], catalogs, logged_in, notes)
    classifier = _filter_known(KNOWN_CLASSIFIER, catalogs, logged_in, notes)
    all_ready = all(tiers[tier] for tier in TIERS) and bool(classifier)
    config = {
        "tiers": tiers,
        "classifier": classifier,
        "policy": {
            "default_mode": "auto",
            # Strict only when onboarding produced a servable pool everywhere.
            "require_all_tiers": all_ready,
            "cooldown_seconds": 300,
        },
        "port": 8787,
        "log_dir": "~/.config/model-router/logs",
    }
    if not all_ready:
        empty = [t for t in TIERS if not tiers[t]]
        notes.append(f"relaxed require_all_tiers (no wallet for: {', '.join(empty)})"
                     if empty else "relaxed require_all_tiers (no classifier wallet)")
    return config, notes


def probe_catalogs(store, logged_in: set[str]) -> dict[str, list[str] | None]:
    """Live catalog per provider; None when the provider has no listing
    endpoint (openai) — the knowledge table is trusted there."""
    from .gateway import Gateway
    from .config import RouterConfig

    gateway = Gateway(RouterConfig(), store)
    catalogs: dict[str, list[str] | None] = {}
    for provider in sorted(logged_in):
        creds = store.load(provider)
        if creds is None:
            continue
        try:
            models = gateway.provider_for(provider).list_models(creds)
        except Exception:  # noqa: BLE001 — catalog is best-effort
            models = []
        catalogs[provider] = models or None
    return catalogs


def write_config(config: dict, path: Path) -> Path | None:
    """Write the config, backing up any previous file. Returns backup path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
        shutil.copy2(path, backup)
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return backup


def run_setup(store, output: Path | None = None,
              input_fn=None, print_fn=None) -> int:
    from .cli import _user_config
    from .errors import RouterError
    from .http import HttpStatusError

    input_fn = input_fn or input  # resolved lazily so tests can patch builtins
    print_fn = print_fn or print
    target = output or _user_config()
    print_fn("Monti setup — connect subscriptions, auto-pick models.\n")

    already = set(store.providers())
    if already:
        print_fn(f"Already logged in: {', '.join(sorted(already))}")
    for provider in LOGIN_ORDER:
        if provider in already:
            continue
        if provider not in REGISTRY:
            continue
        try:
            answer = input_fn(f"Log in to {provider}? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            continue
        hint = LOGIN_HINTS.get(provider)
        print_fn(f"  ({hint})" if hint else "")
        try:
            creds = REGISTRY[provider]().login(open_browser=True)
        except (RouterError, HttpStatusError, OSError, ValueError) as exc:
            print_fn(f"  login failed: {exc} (skipping — rerun setup anytime)")
            continue
        store.save(provider, creds)
        identity = creds.email or creds.account_id or "ok"
        print_fn(f"  logged in ({identity})")

    logged_in = set(store.providers())
    if not logged_in:
        print_fn("\nNo subscriptions connected. Run `monti setup` again when "
                 "ready — nothing was written.")
        return 1

    try:
        more = input_fn("\nLog in to more subscriptions? [y/N] ").strip().lower()
    except EOFError:
        more = ""
    if more in ("y", "yes"):
        print_fn("Rerun `monti setup` to add more providers; existing logins "
                 "are kept.")
        return 0

    print_fn("\nProbing live catalogs...")
    catalogs = probe_catalogs(store, logged_in)
    for provider in sorted(catalogs):
        if catalogs[provider]:
            print_fn(f"  {provider}: {len(catalogs[provider])} models")
        else:
            print_fn(f"  {provider}: no model listing (using known IDs)")

    config, notes = build_config_dict(logged_in, catalogs)
    print_fn("\nChosen pools (wallet burn order):")
    for tier in TIERS:
        entries = config["tiers"][tier]
        shown = ", ".join(f"{e['provider']}/{e['model']}" for e in entries)
        print_fn(f"  {tier:10s}: {shown or '(empty)'}")
    classifier = config["classifier"]
    print_fn("  classifier : " + (" -> ".join(
        f"{e['provider']}/{e['model']}" for e in classifier) or "(heuristic)"))
    for note in notes:
        print_fn(f"  note: {note}")

    backup = write_config(config, target)
    print_fn(f"\nSaved to {target}"
             + (f" (previous backed up to {backup.name})" if backup else ""))
    print_fn("Edit it anytime; `monti models` shows live state, "
             "`monti serve` starts the proxy.")
    return 0
