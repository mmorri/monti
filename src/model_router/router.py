"""Routing policy: modes, verdict mapping, weak-first-escalate sessions."""

from __future__ import annotations

from dataclasses import dataclass

from . import VIRTUAL_MODELS
from .classifier import Classification, Classifier
from .config import MODES, RouterConfig

MODEL_TO_MODE = {
    "router-auto": "auto",
    "router-fast": "fast-only",
    "router-strong": "strong-only",
}


@dataclass
class Route:
    tier: str  # "fast" | "strong"
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
    """Per-session failure counting for weak-first-escalate (Switchyard auto-esc)."""

    def __init__(self, config: RouterConfig):
        self.config = config
        self._sessions: dict[str, dict] = {}

    def state_for(self, session: str) -> dict:
        return self._sessions.setdefault(session, {"errors": 0, "escalated": False})

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


class Router:
    def __init__(self, config: RouterConfig, classifier: Classifier,
                 tracker: EscalationTracker | None = None):
        self.config = config
        self.classifier = classifier
        self.tracker = tracker or EscalationTracker(config)

    def resolve_mode(self, model: str, header_mode: str | None) -> str:
        if header_mode:
            normalized = header_mode.strip().lower()
            if normalized not in MODES:
                raise ValueError(f"invalid X-Router-Mode '{header_mode}' (expected one of {MODES})")
            return normalized
        if model in MODEL_TO_MODE:
            return MODEL_TO_MODE[model]
        if model in VIRTUAL_MODELS:
            return self.config.default_mode
        return self.config.default_mode

    def route(self, messages: list[dict], model: str,
              header_mode: str | None = None,
              session: str | None = None) -> tuple[Route, Classification]:
        mode = self.resolve_mode(model, header_mode)
        latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        text = task_text([latest_user])
        if mode == "fast-only":
            verdict = Classification("easy", 0, "manual")
            return Route("fast", mode, verdict.verdict), verdict
        if mode == "strong-only":
            verdict = Classification("hard", 0, "manual")
            return Route("strong", mode, verdict.verdict), verdict
        if mode == "weak-first-escalate":
            verdict = self.classifier.classify(text)
            escalated = self.tracker.observe_request(session, messages)
            tier = "strong" if escalated else "fast"
            return Route(tier, mode, verdict.verdict, escalated=escalated), verdict
        verdict = self.classifier.classify(text)
        tier = "strong" if verdict.verdict == "hard" else "fast"
        return Route(tier, mode, verdict.verdict), verdict
