"""Routing policy: modes, verdict mapping, weak-first-escalate sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import VIRTUAL_MODELS
from .classifier import Classification, Classifier
from .config import MODES, TIERS, RouterConfig, Tier

MODEL_TO_MODE = {
    "router-auto": "auto",
    "router-everyday": "everyday-only",
    "router-moderate": "moderate-only",
    "router-high": "high-only",
    "router-very-high": "very-high-only",
}

# Classifier verdict -> entry rung on the tier ladder.
VERDICT_TO_TIER = {
    "trivial": "everyday",
    "easy": "moderate",
    "medium": "high",
    "hard": "very_high",
}

# Pinned mode -> (tier, synthetic verdict). Verdicts stay human-readable in logs.
PINNED_MODES = {
    "everyday-only": ("everyday", "trivial"),
    "moderate-only": ("moderate", "easy"),
    "high-only": ("high", "medium"),
    "very-high-only": ("very_high", "hard"),
}


@dataclass
class Route:
    tier: str  # entry rung: one of TIERS
    candidate: Tier  # chosen pool entry (provider + model)
    candidate_index: int  # position in the tier pool
    mode: str
    verdict: str
    escalated: bool = False


def task_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"{msg.get('role', '')}: {content}")
        elif isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
            parts.append(f"{msg.get('role', '')}: {text}")
    return "\n".join(parts)


class EscalationTracker:
    """Per-session failure counting for weak-first-escalate (Switchyard auto-esc).

    Sessions are capped: the oldest idle session state is evicted so a
    long-running proxy does not grow without bound.
    """

    MAX_SESSIONS = 1024

    def __init__(self, config: RouterConfig):
        self.config = config
        self._sessions: dict[str, dict] = {}

    def state_for(self, session: str) -> dict:
        state = self._sessions.pop(session, None) or {"errors": 0, "escalated": False}
        self._sessions[session] = state  # refresh recency order
        while len(self._sessions) > self.MAX_SESSIONS:
            oldest = next(iter(self._sessions))
            self._sessions.pop(oldest)
        return state

    def observe_request(self, session: str | None, messages: list[dict]) -> bool:
        """Count trailing tool errors + explicit signals. Returns escalated."""
        if not session:
            return False
        state = self.state_for(session)
        if state["escalated"]:
            return True
        errors = _trailing_tool_errors(messages)
        if errors:
            state["errors"] = errors
        else:
            state["errors"] = 0
        if state["errors"] >= self.config.escalation.tool_error_threshold:
            state["escalated"] = True
        text = task_text(messages[-3:]).lower()
        if any(sig in text for sig in self.config.escalation.signals):
            state["escalated"] = True
        return state["escalated"]


def _trailing_tool_errors(messages: list[dict]) -> int:
    count = 0
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            continue
        if msg.get("role") != "tool":
            break
        content = msg.get("content", "")
        text = content if isinstance(content, str) else repr(content)
        lowered = text.lower()
        if "error" in lowered or "failed" in lowered or "failure" in lowered:
            count += 1
            continue
        break
    return count


def _normalize_mode(raw: str) -> str:
    """Canonical mode spelling: 'very_high-only' <-> 'very-high-only'."""
    normalized = raw.strip().lower().replace("_", "-")
    if normalized.endswith("-only"):
        tier_part = normalized[: -len("-only")].replace("-", "_")
        for tier in TIERS:
            if tier_part == tier:
                return tier.replace("_", "-") + "-only"
    return normalized


class Router:
    def __init__(self, config: RouterConfig, classifier: Classifier,
                 tracker: EscalationTracker | None = None):
        self.config = config
        self.classifier = classifier
        self.tracker = tracker or EscalationTracker(config)

    def resolve_mode(self, model: str, header_mode: str | None) -> str:
        if header_mode:
            normalized = _normalize_mode(header_mode)
            for mode in MODES:
                if _normalize_mode(mode) == normalized:
                    return mode
            raise ValueError(
                f"invalid X-Router-Mode '{header_mode}' (expected one of {MODES})")
        if model in MODEL_TO_MODE:
            return MODEL_TO_MODE[model]
        if model in VIRTUAL_MODELS:
            return self.config.default_mode
        return self.config.default_mode

    def tier_for_mode(self, mode: str) -> str | None:
        """Tier pinned by a mode, or None when the mode routes dynamically."""
        normalized = _normalize_mode(mode)
        for pinned, (tier, _verdict) in PINNED_MODES.items():
            if _normalize_mode(pinned) == normalized:
                return tier
        return None

    def pinned_verdict(self, mode: str) -> str:
        """Synthetic verdict for a pinned mode (human-readable in logs)."""
        normalized = _normalize_mode(mode)
        for pinned, (_tier, verdict) in PINNED_MODES.items():
            if _normalize_mode(pinned) == normalized:
                return verdict
        raise ValueError(f"not a pinned mode: {mode}")

    def pick_candidate(self, tier: str,
                       available: Callable[[str], bool] | None = None,
                       start: int = 0) -> tuple[Tier, int] | tuple[None, None]:
        """First available pool entry at/after `start` (config order = wallet
        preference). Returns (None, None) when the tier has nothing to serve."""
        pool = self.config.tiers.get(tier, [])
        for index in range(start, len(pool)):
            if available is None or available(pool[index].provider):
                return pool[index], index
        return None, None

    def ladder_from(self, tier: str, candidate_index: int = 0,
                    available: Callable[[str], bool] | None = None,
                    ) -> list[tuple[str, Tier, int]]:
        """Attempt plan: remaining candidates in this tier's pool first (same
        quality, next wallet), then each higher rung's available candidates.
        Never steps down — a routed tier is a quality floor, not a ceiling."""
        plan: list[tuple[str, Tier, int]] = []
        rung = TIERS.index(tier)
        for name in TIERS[rung:]:
            pool = self.config.tiers.get(name, [])
            start = candidate_index if name == tier else 0
            for index in range(start, len(pool)):
                if available is None or available(pool[index].provider):
                    plan.append((name, pool[index], index))
        return plan

    def route(self, messages: list[dict], model: str,
              header_mode: str | None = None,
              session: str | None = None,
              available: Callable[[str], bool] | None = None,
              ) -> tuple[Route, Classification]:
        from .errors import NoSubscriptionAuth

        mode = self.resolve_mode(model, header_mode)
        latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        text = task_text([latest_user])
        pinned = self.tier_for_mode(mode)
        if pinned is not None:
            verdict = Classification(self.pinned_verdict(mode), 0, "manual")
            return self._to_route(pinned, 0, mode, verdict, available)
        verdict = self.classifier.classify(text)
        tier = VERDICT_TO_TIER.get(verdict.verdict, "moderate")
        if mode == "weak-first-escalate":
            if self.tracker.observe_request(session, messages):
                rung = min(TIERS.index(tier) + 1, len(TIERS) - 1)
                tier = TIERS[rung]
                return self._to_route(tier, 0, mode, verdict, available, escalated=True)
        return self._to_route(tier, 0, mode, verdict, available)

    def _to_route(self, tier: str, start: int, mode: str, verdict: Classification,
                  available: Callable[[str], bool] | None,
                  escalated: bool = False) -> tuple[Route, Classification]:
        from .errors import NoSubscriptionAuth

        candidate, index = self.pick_candidate(tier, available, start)
        if candidate is None:
            # Step down the ladder: a tier with no ready wallet degrades to
            # the nearest lower rung that can serve, rather than failing.
            rung = TIERS.index(tier)
            for name in reversed(TIERS[:rung]):
                candidate, index = self.pick_candidate(name, available)
                if candidate is not None:
                    tier = name
                    break
            else:
                raise NoSubscriptionAuth(
                    "no tier has a logged-in, transport-ready candidate; "
                    "run `monti login <provider>` for at least one provider "
                    "in the configured pools")
        return Route(tier, candidate, index, mode, verdict.verdict,
                     escalated=escalated), verdict
