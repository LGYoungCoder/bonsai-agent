"""Shared base for OpenAI-compatible providers (OpenAI / GLM / Qwen / MiniMax / DeepSeek / Kimi).

Each concrete adapter is a thin subclass adjusting: base_url default, model list,
temperature clamp, cache hint placement. The wire shape (messages, tools, SSE)
is identical within tolerance — and the tolerance is documented inline.
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


def _role_to_openai(role: str) -> str:
    # OpenAI-compat uses "tool" for tool_result messages.
    return role


def _messages_to_openai(prefix: FrozenPrefix, tail: DynamicTail) -> list[dict]:
    out: list[dict] = []
    out.append({"role": "system", "content": prefix.render_system()})
    for m in tail.messages:
        if m.role == "tool":
            # one OpenAI message per tool_result
            for tr in m.tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "content": tr.content,
                })
            continue
        msg: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            msg["content"] = m.content
        if m.tool_calls:
            msg["tool_calls"] = [{
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)},
            } for tc in m.tool_calls]
            if not m.content:
                msg["content"] = None
        out.append(msg)
    return out


def _tools_to_openai(prefix: FrozenPrefix) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.input_schema,
        },
    } for t in prefix.tools]


_URL_NOISE = ("/chat/completions", "/embeddings")


def _normalize(u: str) -> str:
    # Be permissive about what users paste into base_url — strip the
    # common endpoint suffixes from provider docs before we compose the
    # real URL (adapter composes `.../chat/completions`, so a pasted
    # endpoint would otherwise double-up).
    u = (u or "").strip().rstrip("/")
    for sfx in _URL_NOISE:
        if u.endswith(sfx):
            return u[:-len(sfx)].rstrip("/")
    return u


@dataclass
class OpenAICompatAdapter:
    name: str
    model: str
    api_key: str
    base_url: str
    kind: str = "openai"
    timeout: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        self.base_url = _normalize(self.base_url)

    def _clamp_temperature(self, t: float | None) -> float | None:
        # Override in subclasses for model-specific clamps (Qwen/MiniMax).
        return t

    def _build_body(self, prefix: FrozenPrefix, tail: DynamicTail, *,
                    stream: bool, **opts: Any) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _messages_to_openai(prefix, tail),
            "max_tokens": opts.get("max_tokens", self.max_tokens),
            "stream": stream,
        }
        if prefix.tools:
            body["tools"] = _tools_to_openai(prefix)
            body["tool_choice"] = "auto"
            # parallel tool calls — OpenAI default true, most others ignore the flag.
            body["parallel_tool_calls"] = True
        t = self._clamp_temperature(opts.get("temperature", self.temperature))
        if t is not None:
            body["temperature"] = t
        body.update(self.extra_body)
        return body

    def _headers(self) -> dict[str, str]:
        from ._http import sanitize_header_value
        key = sanitize_header_value("api_key", self.api_key)
        h = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        for k, v in self.extra_headers.items():
            h[k] = sanitize_header_value(k, v) if isinstance(v, str) else v
        return h

    async def chat(self, prefix: FrozenPrefix, tail: DynamicTail, **opts: Any) -> Response:
        body = self._build_body(prefix, tail, stream=False, **opts)
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()
        return self._parse_response(data)

    async def stream(self, prefix: FrozenPrefix, tail: DynamicTail,
                     **opts: Any) -> AsyncIterator[StreamEvent]:
        body = self._build_body(prefix, tail, stream=True, **opts)
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        log.debug("%s stream → %s model=%s msgs=%d tools=%d timeout=%.1fs",
                  self.kind, url, self.model, len(body["messages"]),
                  len(body.get("tools") or []), self.timeout)
        chunk_count = 0
        text_bytes = 0
        saw_done = False
        async with httpx.AsyncClient(timeout=self.timeout) as cli, cli.stream(
            "POST", url, json=body, headers=self._headers(),
        ) as r:
            log.debug("%s stream ← HTTP %d", self.kind, r.status_code)
            r.raise_for_status()
            partial_calls: dict[int, dict] = {}
            async for line in r.aiter_lines():
                chunk_count += 1
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                if delta.get("content"):
                    text_bytes += len(delta["content"])
                    yield StreamEvent(kind="text", data=delta["content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = partial_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["args"] += fn["arguments"]
                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield StreamEvent(kind="usage", data=Usage(
                        input_tokens=u.get("prompt_tokens", 0),
                        output_tokens=u.get("completion_tokens", 0),
                        cache_read_tokens=(u.get("prompt_tokens_details") or {})
                            .get("cached_tokens", 0),
                        cache_creation_tokens=0,
                    ))
            if not saw_done:
                # Stream ended without `[DONE]` marker — server half-closed or
                # network cut. This is the silent-disconnect fingerprint.
                log.warning("%s stream ended without [DONE] "
                            "(chunks=%d text_bytes=%d partial_tools=%d) — "
                            "likely upstream disconnect",
                            self.kind, chunk_count, text_bytes, len(partial_calls))
            else:
                log.debug("%s stream done chunks=%d text_bytes=%d tools=%d",
                          self.kind, chunk_count, text_bytes, len(partial_calls))
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

    def _parse_response(self, data: dict) -> Response:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=fn.get("name", ""),
                args=args,
            ))
        usage_raw = data.get("usage") or {}
        cached = (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
            cache_read_tokens=cached,
            cache_creation_tokens=0,
        )
        return Response(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason", "stop"),
            raw=data,
            provider=self.name,
            model=self.model,
        )
