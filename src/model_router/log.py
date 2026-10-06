"""Structured JSONL logs: requests + classifications."""

from __future__ import annotations

import json
import time
from pathlib import Path


class JsonlLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def __call__(self, record: dict) -> None:
        self.append(record)


class RequestLog(JsonlLog):
    pass


class ClassificationLog(JsonlLog):
    pass
