"""Shared OpenAI-compatible chat transport (SSE + non-stream).

Used by subscription gateways that speak the OpenAI chat API over Bearer
subscription tokens: xAI cli-chat-proxy, Kimi coding, Muse.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator

from ..errors import ProviderDisabled
from ..http import HttpStatusError, iter_sse_lines
from .base import ChatChunk, ChatRequest, ChatTransport


class OpenAICompatTransport(ChatTransport):
    def __init__(
        self,
        base_url: str,
        access_token: str,
        request: ChatRequest,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 300.0,
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.access_token = access_token
        self.request = request
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

    def _payload(self) -> dict:
        req = self.request
        payload: dict = {
            "model": req.model,
            "messages": req.messages,
            "stream": req.stream,
        }
        if req.stream:
            payload["stream_options"] = {"include_usage": True}
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.tools is not None:
            payload["tools"] = req.tools
        if req.tool_choice is not None:
            payload["tool_choice"] = req.tool_choice
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.request.stream else "application/json",
            **self.extra_headers,
        }

    def run(self) -> Iterator[ChatChunk]:
        import urllib.error

        body = json.dumps(self._payload()).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise HttpStatusError("POST", self.url, exc.code) from None
        if not self.request.stream:
            with resp:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    raise ProviderDisabled("upstream", "invalid JSON from provider") from exc
            yield from _non_stream_chunks(data)
            return
        finished = False
        with resp:
            for _event, data in iter_sse_lines(resp):
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for event in _stream_chunks(chunk):
                    finished = finished or event.kind == "finish"
                    yield event
        if not finished:
            yield ChatChunk(kind="finish", finish_reason="stop")


def _stream_chunks(chunk: dict) -> Iterator[ChatChunk]:
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            yield ChatChunk(kind="delta", text=text)
        for call in delta.get("tool_calls") or []:
            yield ChatChunk(kind="tool_call", tool_call=call)
        if choice.get("finish_reason"):
            yield ChatChunk(kind="finish", finish_reason=choice["finish_reason"])
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        yield ChatChunk(kind="usage", usage=_usage(usage))


def _non_stream_chunks(data: dict) -> Iterator[ChatChunk]:
    choices = data.get("choices") or []
    text = ""
    reason = "stop"
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        reason = choices[0].get("finish_reason") or "stop"
        for index, call in enumerate(message.get("tool_calls") or []):
            yield ChatChunk(kind="tool_call", tool_call={"index": index, **call})
    if text:
        yield ChatChunk(kind="delta", text=text)
    if isinstance(data.get("usage"), dict):
        yield ChatChunk(kind="usage", usage=_usage(data["usage"]))
    yield ChatChunk(kind="finish", finish_reason=reason)


def _usage(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = raw.get(key)
        if isinstance(value, int):
            out[key] = value
    return out


def list_openai_models(base_url: str, access_token: str,
                       extra_headers: dict[str, str] | None = None) -> list[str]:
    """GET {base}/models for logged-in subscriptions; empty when unavailable."""
    from ..http import get

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json",
               **(extra_headers or {})}
    try:
        data = get(url, headers=headers, timeout=30.0).json()
    except Exception:  # noqa: BLE001 — catalog is best-effort
        return []
    models = [item.get("id") for item in data.get("data") or []
              if isinstance(item, dict) and isinstance(item.get("id"), str)]
    return sorted(models)
