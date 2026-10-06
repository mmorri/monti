"""Difficulty classifier: subscription model first, heuristic fallback.

Model prompt: "Rate this coding task's difficulty as exactly one word:
trivial, easy, or hard." Task truncated to ~2k chars. Every classification is
logged (timestamp, verdict, latency, source).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

PROMPT = ("Rate this coding task's difficulty as exactly one word: "
          "trivial, easy, or hard.")
TASK_LIMIT = 2000

_TRIVIAL = ("typo", "rename", "fix comment", "format", "whitespace", "spelling")
_HARD = ("design", "refactor", "architect", "race condition", "deadlock",
         "distributed", "migration", "security", "concurrency", "performance regression")

_VERDICTS = ("trivial", "easy", "hard")


@dataclass
class Classification:
    verdict: str
    latency_ms: int
    source: str  # "model" | "heuristic"


def heuristic_classify(text: str) -> str:
    lowered = text.lower()
    if any(keyword in lowered for keyword in _HARD):
        return "hard"
    if any(keyword in lowered for keyword in _TRIVIAL):
        return "trivial"
    if len(text) > 4000:
        return "hard"
    return "easy"


def parse_verdict(raw: str) -> str | None:
    match = re.search(r"\b(trivial|easy|hard)\b", raw.strip().lower())
    return match.group(1) if match else None


class Classifier:
    def __init__(self, model_fn: Callable[[str], str] | None = None,
                 log_fn: Callable[[dict], None] | None = None):
        # model_fn(task_excerpt) -> raw verdict text; raises on failure.
        self.model_fn = model_fn
        self.log_fn = log_fn or (lambda _record: None)

    def classify(self, task: str) -> Classification:
        started = time.time()
        excerpt = task[:TASK_LIMIT]
        verdict, source = "easy", "heuristic"
        if self.model_fn is not None:
            try:
                parsed = parse_verdict(self.model_fn(excerpt))
                if parsed:
                    verdict, source = parsed, "model"
                else:
                    verdict = heuristic_classify(task)
            except Exception:
                verdict = heuristic_classify(task)
        else:
            verdict = heuristic_classify(task)
        latency_ms = int((time.time() - started) * 1000)
        self.log_fn({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "verdict": verdict,
            "latency_ms": latency_ms,
            "source": source,
        })
        return Classification(verdict=verdict, latency_ms=latency_ms, source=source)
