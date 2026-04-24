"""Anthropic Claude adapter. Native API with explicit cache_control breakpoints.

Cache strategy (per TOKEN_BUDGET.md):
  breakpoint 1: tools schema
  breakpoint 2: system prompt
  breakpoint 3: conversation prefix (all but last 2 user messages)
  breakpoint 4: second-to-last user message
Max 4 cache_control markers per request per Anthropic docs.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.types import (
    DynamicTail,
    FrozenPrefix,
    Response,
    StreamEvent,
    ToolCall,
    Usage,
)

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


def _messages_to_claude(tail: DynamicTail) -> list[dict]:
    out: list[dict] = []
    for m in tail.messages:
        if m.role == "system":
            continue  # handled separately
        if m.role == "tool":
            blocks = [{
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
                **({"is_error": True} if tr.is_error else {}),
            } for tr in m.tool_results]
            out.append({"role": "user", "content": blocks})
            continue
        content_blocks: list[dict] = []
        if m.content:
            content_blocks.append({"type": "text", "text": m.content})
        for tc in m.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.args,
            })
        out.append({
            "role": m.role,
            "content": content_blocks if content_blocks else m.content or "",
        })
    return out


def _apply_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Mark the last user turn pre-last as a cache breakpoint. Max 2 here
    (the other 2 are used by system + tools)."""
    # Find the last two 'user' messages
    user_idxs = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_idxs) < 2:
        return messages
    target_idx = user_idxs[-2]
    msg = messages[target_idx]
    content = msg["content"]
    if isinstance(content, str):
        msg["content"] = [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        # Attach to the last block.
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    return messages


@dataclass
class ClaudeAdapter:
    name: str
    model: str
    api_key: str
    base_url: str = "https://api.anthropic.com"
    kind: str = "claude"
    timeout: float = 120.0
    max_tokens: int = 4096
    temperature: float | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Strip noise that users paste from provider docs so our
        # composed URL (`{base}/v1/messages`) doesn't double up.
        u = (self.base_url or "").strip().rstrip("/")
        for sfx in ("/v1/messages", "/messages"):
            if u.endswith(sfx):
                u = u[:-len(sfx)].rstrip("/")
                break
        if u.endswith("/v1"):
            u = u[:-3].rstrip("/")
        self.base_url = u or "https://api.anthropic.com"

    def _headers(self) -> dict[str, str]:
        from ._http import sanitize_header_value
        h = {
            "x-api-key": sanitize_header_value("api_key", self.api_key),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        for k, v in self.extra_headers.items():
            h[k] = sanitize_header_value(k, v) if isinstance(v, str) else v
        return h

    def _build_body(self, prefix: FrozenPrefix, tail: DynamicTail, *,
                    stream: bool, **opts: Any) -> dict:
        sys_text = prefix.render_system()
        system_blocks = [{
            "type": "text",
            "text": sys_text,
            "cache_control": {"type": "ephemeral"},
        }]
        tools = [{
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        } for t in prefix.tools]
        if tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

        messages = _apply_cache_breakpoints(_messages_to_claude(tail))
        body: dict[str, Any] = {
            "model": self.model,
            "system": system_blocks,
            "messages": messages,
            "max_tokens": opts.get("max_tokens", self.max_tokens),
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        t = opts.get("temperature", self.temperature)
        if t is not None:
            body["temperature"] = t
        return body

    async def chat(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any) -> Response:
        body = self._build_body(prefix, tail, stream=False, **opts)
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(
                f"{self.base_url.rstrip('/')}/v1/messages",
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()
        return self._parse(data)

    async def stream(self, prefix: FrozenPrefix, tail: DynamicTail,
                     **opts: Any) -> AsyncIterator[StreamEvent]:
        body = self._build_body(prefix, tail, stream=True, **opts)
        url = f"{self.base_url.rstrip('/')}/v1/messages"
        log.debug("claude stream → %s model=%s msgs=%d tools=%d timeout=%.1fs",
                  url, self.model, len(body["messages"]),
                  len(body.get("tools") or []), self.timeout)
        chunk_count = 0
        text_bytes = 0
        saw_message_stop = False
        async with httpx.AsyncClient(timeout=self.timeout) as cli, cli.stream(
            "POST", url, json=body, headers=self._headers(),
        ) as r:
            log.debug("claude stream ← HTTP %d", r.status_code)
            r.raise_for_status()
            partial_calls: dict[int, dict] = {}
            async for line in r.aiter_lines():
                chunk_count += 1
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                et = evt.get("type")
                if et == "content_block_start":
                    block = evt.get("content_block", {})
                    if block.get("type") == "tool_use":
                        idx = evt.get("index", 0)
                        partial_calls[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "args": "",
                        }
                elif et == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        t = delta.get("text", "")
                        text_bytes += len(t)
                        yield StreamEvent(kind="text", data=t)
                    elif delta.get("type") == "input_json_delta":
                        idx = evt.get("index", 0)
                        if idx in partial_calls:
                            partial_calls[idx]["args"] += delta.get("partial_json", "")
                elif et == "message_delta":
                    usage = evt.get("usage") or {}
                    if usage:
                        yield StreamEvent(kind="usage", data=Usage(
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                        ))
                elif et == "message_stop":
                    saw_message_stop = True
                    break
            if not saw_message_stop:
                # Stream ended without `message_stop` — server half-closed or
                # network cut. This is the silent-disconnect fingerprint.
                log.warning("claude stream ended without message_stop "
                            "(chunks=%d text_bytes=%d partial_tools=%d) — "
                            "likely upstream disconnect",
                            chunk_count, text_bytes, len(partial_calls))
            else:
                log.debug("claude stream done chunks=%d text_bytes=%d tools=%d",
                          chunk_count, text_bytes, len(partial_calls))
            for idx in sorted(partial_calls):
                e = partial_calls[idx]
                try:
                    args = json.loads(e["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield StreamEvent(kind="tool_call", data=ToolCall(
                    id=e["id"] or f"call_{uuid.uuid4().hex[:8]}",
                    name=e["name"],
                    args=args,
                ))
        yield StreamEvent(kind="done")

    def _parse(self, data: dict) -> Response:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            bt = block.get("type")
            if bt == "text":
                text_parts.append(block.get("text", ""))
            elif bt == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    name=block.get("name", ""),
                    args=block.get("input") or {},
                ))
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("input_tokens", 0),
            output_tokens=usage_raw.get("output_tokens", 0),
            cache_read_tokens=usage_raw.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage_raw.get("cache_creation_input_tokens", 0),
        )
        return Response(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=data.get("stop_reason", "stop"),
            raw=data,
            provider=self.name,
            model=self.model,
        )
