"""Minimal stdlib HTTP client. Errors are status-only; bodies may hold tokens."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass


class HttpStatusError(Exception):
    def __init__(self, method: str, url: str, status: int | None, body_snippet: str = ""):
        self.method = method
        self.url = _redact_url(url)
        self.status = status
        # Status-only by default; caller may attach a caller-scrubbed snippet.
        super().__init__(f"{method} {self.url} -> HTTP {status}")


def _redact_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.query:
            return f"{parts.scheme}://{parts.hostname}{parts.path}"
        return f"{parts.scheme}://{parts.hostname}{parts.path}?<query-redacted>"
    except Exception:
        return "<url>"


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> dict:
        data = json.loads(self.body.decode("utf-8"))
        return data if isinstance(data, dict) else {}


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> HttpResponse:
    req = urllib.request.Request(url, data=body, method=method.upper())
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_headers = {k.lower(): v for k, v in resp.getheaders()}
            return HttpResponse(resp.status, resp.read(), raw_headers)
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(method, url, exc.code) from None


def get(url: str, **kwargs) -> HttpResponse:
    return request("GET", url, **kwargs)


def post_json(url: str, payload: dict, **kwargs) -> HttpResponse:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    return request("POST", url, headers=headers, body=json.dumps(payload).encode(), **kwargs)


def post_form(url: str, fields: dict[str, str], **kwargs) -> HttpResponse:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    return request(
        "POST", url, headers=headers,
        body=urllib.parse.urlencode(fields).encode(), **kwargs,
    )


def iter_sse_lines(resp) -> Iterator[tuple[str, str]]:
    """Yield (field, value) pairs from an SSE byte stream. Caller owns resp."""
    event = ""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            if data_lines:
                yield event or "message", "\n".join(data_lines)
            event, data_lines = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        field = field.strip()
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event or "message", "\n".join(data_lines)


def now_ms() -> int:
    return int(time.time() * 1000)
