"""OpenAI Responses API transport for the Codex subscription gateway.

Translates ChatRequest (OpenAI chat format) to the Responses API spoken by
POST https://chatgpt.com/backend-api/codex/responses, mirroring what the
Codex CLI sends on that gateway (originator/User-Agent headers plus the
chatgpt-account-id from login). Wire shape follows the public Responses
API: system prompts map to `instructions`, chat history to typed `input`
items (message / function_call / function_call_output), tools to flat
function declarations. Streaming always uses SSE; non-stream requests
buffer the same events upstream of the normalized ChatChunk interface.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator

from ..errors import AuthFlowError
from ..http import iter_sse_lines
from .base import ChatChunk, ChatRequest, ChatTransport

RESPONSES_PATH = "/codex/responses"
ORIGINATOR = "codex_cli_rs"
USER_AGENT = "codex_cli_rs/0.52.0"


def to_responses_payload(request: ChatRequest) -> dict:
    """Translate chat messages + tools to the Responses API shape."""
    instructions: list[str] = []
    items: list[dict] = []
    for msg in request.messages:
        role = msg.get("role")
        content = msg.get("content")
        text = content if isinstance(content, str) else _blocks_to_text(content)
        if role == "system":
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": msg.get("tool_call_id", ""),
                          "output": text or ""})
            continue
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                function = call["function"]
                items.append({"type": "function_call", "call_id": call.get("id", ""),
                              "name": function.get("name", ""),
                              "arguments": function.get("arguments") or "{}"})
            if text:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
        else:
            items.append({"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": text or ""}]})
    payload: dict = {"model": request.model, "input": items, "stream": True,
                     "store": False, "include": ["usage"]}
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if request.tools:
        payload["tools"] = [{"type": "function", "name": t["function"]["name"],
                             "description": t["function"].get("description", ""),
                             "parameters": t["function"].get("parameters")
                             or {"type": "object", "properties": {}},
                             "strict": False} for t in request.tools]
    if request.tool_choice is not None:
        choice = request.tool_choice
        if isinstance(choice, str):
            payload["tool_choice"] = choice
        elif isinstance(choice, dict):
            payload["tool_choice"] = {"type": "function",
                                      "name": choice["function"]["name"]}
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    return payload


def _blocks_to_text(content) -> str:
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "".join(b.get("text", "") for b in content
                   if isinstance(b, dict) and b.get("type") == "text")


class ResponsesTransport(ChatTransport):
    def __init__(self, access_token: str, request: ChatRequest,
                 account_id: str = "", base_url: str = "https://chatgpt.com/backend-api",
                 timeout: float = 300.0):
        self.url = base_url.rstrip("/") + RESPONSES_PATH
        self.access_token = access_token
        self.request = request
        self.account_id = account_id
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "originator": ORIGINATOR,
            "User-Agent": USER_AGENT,
        }
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        return headers

    def run(self) -> Iterator[ChatChunk]:
        import urllib.error

        body = json.dumps(to_responses_payload(self.request)).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers=self._headers())
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            from ..http import HttpStatusError

            raise HttpStatusError("POST", self.url, exc.code) from None
        call_index: dict[str, int] = {}
        args_streamed: set[str] = set()
        usage: dict = {}
        finish = "stop"
        finished = False
        with resp:
            for event, raw in iter_sse_lines(resp):
                if raw.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type") or event
                if kind == "response.output_text.delta":
                    text = data.get("delta")
                    if isinstance(text, str) and text:
                        yield ChatChunk(kind="delta", text=text)
                elif kind == "response.output_item.added":
                    item = data.get("item") or {}
                    if item.get("type") == "function_call":
                        item_id = item.get("id") or ""
                        call_index[item_id] = len(call_index)
                        finish = "tool_calls"
                        yield ChatChunk(kind="tool_call", tool_call={
                            "index": call_index[item_id],
                            "id": item.get("call_id") or item_id,
                            "type": "function",
                            "function": {"name": item.get("name") or "",
                                         "arguments": ""}})
                elif kind == "response.function_call_arguments.delta":
                    item_id = data.get("item_id") or ""
                    index = call_index.get(item_id)
                    if index is not None:
                        args_streamed.add(item_id)
                        yield ChatChunk(kind="tool_call", tool_call={
                            "index": index,
                            "function": {"arguments": data.get("delta") or ""}})
                elif kind == "response.output_item.done":
                    item = data.get("item") or {}
                    # Safety net: some gateway responses carry the arguments
                    # only on the completed item.
                    if item.get("type") == "function_call" and item.get("id") not in args_streamed:
                        item_id = item.get("id") or ""
                        arguments = item.get("arguments") or "{}"
                        if item_id in call_index:
                            yield ChatChunk(kind="tool_call", tool_call={
                                "index": call_index[item_id],
                                "function": {"arguments": arguments}})
                        else:
                            call_index[item_id] = len(call_index)
                            finish = "tool_calls"
                            yield ChatChunk(kind="tool_call", tool_call={
                                "index": call_index[item_id],
                                "id": item.get("call_id") or item_id,
                                "type": "function",
                                "function": {"name": item.get("name") or "",
                                             "arguments": arguments}})
                elif kind == "response.completed":
                    response = data.get("response") or {}
                    raw_usage = response.get("usage") or {}
                    if isinstance(raw_usage, dict):
                        usage = {"prompt_tokens": raw_usage.get("input_tokens", 0),
                                 "completion_tokens": raw_usage.get("output_tokens", 0),
                                 "total_tokens": raw_usage.get("total_tokens", 0)}
                    finished = True
                elif kind in ("response.failed", "error", "response.error"):
                    raise AuthFlowError(f"Codex gateway reported failure ({kind})")
        if usage:
            yield ChatChunk(kind="usage", usage=usage)
        if not finished:
            finish = "stop"
        yield ChatChunk(kind="finish", finish_reason=finish)
