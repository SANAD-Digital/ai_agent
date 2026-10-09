"""Anthropic Messages API, streamed natively: thinking blocks (with their
signature, replayed verbatim), tool_use blocks, tool_result blocks, PDF and
image documents, prompt caching breakpoints on system / tools / last user
turn, and typed stop reasons.
"""

from __future__ import annotations

from ai_agent.engine.events import MessageStop, UsageEvent
from ai_agent.engine.providers.base import (
    BlockAssembler,
    CancelToken,
    Provider,
    provider_error,
)
from ai_agent.engine.types import (
    CANCELLED,
    DOCUMENT,
    END_TURN,
    IMAGE,
    MAX_TOKENS,
    SKILL,
    TEXT,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    TOOL_USE_STOP,
    ModelRequest,
    Usage,
)

CACHE = {"type": "ephemeral"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, config):
        super().__init__(config)
        self._client = None

    def client(self):
        if self._client is None:
            import anthropic

            kwargs = {"api_key": self.config.get("api_key") or "", "timeout": self.timeout, "max_retries": 1}
            if self.config.get("base_url"):
                kwargs["base_url"] = self.config["base_url"]
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # -- request --------------------------------------------------------------

    def build_request(self, request: ModelRequest):
        model = request.model or self.model
        system = []
        if request.system:
            system.append({"type": "text", "text": request.system, "cache_control": CACHE})
        if request.system_dynamic:
            system.append({"type": "text", "text": request.system_dynamic})
        messages = convert_messages(request.messages)
        mark_last_user(messages)
        payload = {"model": model, "max_tokens": int(request.max_tokens or 8192), "messages": messages}
        if system:
            payload["system"] = system
        if request.tools:
            tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters or {"type": "object", "properties": {}}} for t in request.tools]
            tools[-1]["cache_control"] = CACHE
            payload["tools"] = tools
        if request.thinking == "adaptive":
            payload["thinking"] = {"type": "adaptive"}
        elif request.thinking == "on":
            budget = max(1024, int(request.thinking_budget or 4096))
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(payload["max_tokens"], budget + 2048)
        elif request.temperature is not None:
            payload["temperature"] = request.temperature
        effort = (request.effort or "").lower()
        if effort in EFFORTS:
            payload["output_config"] = {"effort": effort}
        extra = dict(self.config.get("extra_request") or {})
        extra.update(request.extra or {})
        if extra:
            payload["extra_body"] = extra
        return payload

    # -- stream ---------------------------------------------------------------

    def stream(self, request: ModelRequest, cancel: CancelToken):
        try:
            payload = self.build_request(request)
            manager = self.client().messages.stream(**payload)
        except Exception as exc:
            yield provider_error(exc, redact=(self.config.get("api_key"),))
            return

        asm = BlockAssembler()
        index_seq = {}
        usage = Usage()
        stop_reason = None
        try:
            with manager as stream:
                for event in stream:
                    if cancel.cancelled:
                        for ev in asm.close_all():
                            yield ev
                        yield MessageStop(stop_reason=CANCELLED)
                        return
                    et = event.type
                    if et == "message_start":
                        u = getattr(event.message, "usage", None)
                        if u is not None:
                            usage.input_tokens = int(getattr(u, "input_tokens", 0) or 0)
                            usage.cache_read_input_tokens = int(getattr(u, "cache_read_input_tokens", 0) or 0)
                            usage.cache_creation_input_tokens = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
                    elif et == "content_block_start":
                        cb = event.content_block
                        kind = cb.type
                        if kind == "redacted_thinking":
                            started = asm.start(THINKING)
                            asm.blocks[started.block_seq].data = {"redacted": getattr(cb, "data", "")}
                        elif kind == "tool_use":
                            started = asm.start(TOOL_USE, id=cb.id, name=cb.name)
                        elif kind == "thinking":
                            started = asm.start(THINKING)
                        else:
                            started = asm.start(TEXT)
                        index_seq[event.index] = started.block_seq
                        yield started
                    elif et == "content_block_delta":
                        seq = index_seq.get(event.index)
                        if seq is None:
                            continue
                        d = event.delta
                        dt = d.type
                        if dt == "text_delta":
                            yield asm.delta(seq, d.text)
                        elif dt == "thinking_delta":
                            yield asm.delta(seq, d.thinking)
                        elif dt == "input_json_delta":
                            yield asm.delta(seq, d.partial_json)
                        elif dt == "signature_delta":
                            asm.signature(seq, d.signature)
                    elif et == "content_block_stop":
                        seq = index_seq.get(event.index)
                        if seq is not None:
                            yield asm.stop(seq)
                    elif et == "message_delta":
                        stop_reason = getattr(event.delta, "stop_reason", None) or stop_reason
                        u = getattr(event, "usage", None)
                        if u is not None and getattr(u, "output_tokens", None) is not None:
                            usage.output_tokens = int(u.output_tokens or 0)
        except Exception as exc:
            for ev in asm.close_all():
                yield ev
            yield provider_error(exc, redact=(self.config.get("api_key"),))
            return

        for ev in asm.close_all():
            yield ev
        yield UsageEvent(usage=usage.to_dict(), context_tokens=usage.context_size(), model=payload["model"])
        stop = {"tool_use": TOOL_USE_STOP, "max_tokens": MAX_TOKENS}.get(stop_reason or "end_turn", END_TURN)
        yield MessageStop(stop_reason=stop)


# -- helpers ------------------------------------------------------------------

def convert_messages(messages):
    out = []
    for m in messages:
        content = []
        if m.role == "user":
            for b in m.blocks:
                if b.kind == TOOL_RESULT:
                    item = {"type": "tool_result", "tool_use_id": b.id, "content": b.text or (json_dump(b.data) if b.data else "(empty)")}
                    if b.is_error:
                        item["is_error"] = True
                    content.append(item)
                elif b.kind in (TEXT, SKILL) and b.text:
                    content.append({"type": "text", "text": b.text})
                elif b.kind == IMAGE and b.data.get("data"):
                    content.append({"type": "image", "source": {"type": "base64", "media_type": b.data.get("media_type", "image/png"), "data": b.data["data"]}})
                elif b.kind == DOCUMENT:
                    if b.data.get("data"):
                        content.append({"type": "document", "source": {"type": "base64", "media_type": b.data.get("media_type", "application/pdf"), "data": b.data["data"]}, "title": b.name or "document"})
                    elif b.text:
                        content.append({"type": "text", "text": b.text})
            # tool_result blocks must come first in a user turn
            content.sort(key=lambda c: 0 if c["type"] == "tool_result" else 1)
        else:
            for b in m.blocks:
                if b.kind == THINKING:
                    if b.data.get("redacted"):
                        content.append({"type": "redacted_thinking", "data": b.data["redacted"]})
                    elif b.signature and b.text:
                        content.append({"type": "thinking", "thinking": b.text, "signature": b.signature})
                elif b.kind == TEXT and b.text.strip():
                    content.append({"type": "text", "text": b.text})
                elif b.kind == TOOL_USE:
                    content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.data or {}})
        if not content:
            continue
        if out and out[-1]["role"] == m.role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": m.role, "content": content})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    # An assistant turn must not end with whitespace-only text; the API rejects it.
    for m in out:
        if m["role"] == "assistant":
            m["content"] = [c for c in m["content"] if c["type"] != "text" or c["text"].strip()]
    return out


def mark_last_user(messages):
    for m in reversed(messages):
        if m["role"] == "user" and m["content"]:
            last = m["content"][-1]
            if last.get("type") in ("text", "tool_result", "image", "document"):
                last["cache_control"] = CACHE
            return


def json_dump(value):
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
