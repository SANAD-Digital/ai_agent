"""OpenAI-compatible chat completions with streaming (OpenAI, DeepSeek,
Ollama, any /v1/chat/completions server).

Native features used: streamed tool-call deltas, `stream_options.include_usage`,
`reasoning_content` deltas (DeepSeek thinking) surfaced as thinking blocks,
`reasoning_effort` where the server supports it, arbitrary `extra_body`
from the provider record (for example DeepSeek's `thinking` switch).
"""

from __future__ import annotations

import json

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


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, config):
        super().__init__(config)
        self._client = None

    # -- client ---------------------------------------------------------------

    def client(self):
        if self._client is None:
            from openai import OpenAI

            kwargs = {"api_key": self.config.get("api_key") or "sk-none", "timeout": self.timeout, "max_retries": 1}
            if self.config.get("base_url"):
                kwargs["base_url"] = self.config["base_url"]
            self._client = OpenAI(**kwargs)
        return self._client

    # -- request --------------------------------------------------------------

    def build_request(self, request: ModelRequest, include_usage=True):
        model = request.model or self.model
        messages = [{"role": "system", "content": join_system(request.system, request.system_dynamic)}]
        messages.extend(convert_messages(request.messages, keep_reasoning=self._keeps_reasoning(model), gemini=self._is_gemini(model)))
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": int(request.max_tokens or 4096),
            "stream": True,
        }
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                        **({"strict": True} if self.config.get("strict_tools") else {}),
                    },
                }
                for t in request.tools
            ]
            payload["tool_choice"] = "auto"
        effort = (request.effort or "").lower()
        if effort and self._supports_effort(model):
            payload["reasoning_effort"] = effort
        extra = dict(self.config.get("extra_request") or {})
        extra.update(request.extra or {})
        if request.thinking in ("adaptive", "on") and self._is_deepseek(model) and "thinking" not in extra:
            extra["thinking"] = {"type": "enabled"}
        if request.thinking == "off" and self._is_deepseek(model) and "thinking" not in extra:
            extra["thinking"] = {"type": "disabled"}
        if extra:
            payload["extra_body"] = extra
        return payload

    def _is_deepseek(self, model):
        return "deepseek" in (model or "").lower() or "deepseek" in (self.config.get("base_url") or "").lower()

    def _is_gemini(self, model):
        return "gemini" in (model or "").lower() or "generativelanguage.googleapis.com" in (self.config.get("base_url") or "")

    def _keeps_reasoning(self, model):
        # DeepSeek requires reasoning_content on the current turn's assistant
        # messages that carry tool calls; OpenAI rejects unknown keys.
        return self._is_deepseek(model)

    def _supports_effort(self, model):
        m = (model or "").lower()
        if self._is_deepseek(model):
            return False
        return m.startswith("o") or "gpt-5" in m or bool(self.config.get("supports_reasoning_effort"))

    # -- stream ---------------------------------------------------------------

    def stream(self, request: ModelRequest, cancel: CancelToken):
        try:
            payload = self.build_request(request)
            try:
                raw = self.client().chat.completions.create(**payload)
            except Exception as exc:  # some servers reject stream_options
                if "stream_options" in str(exc):
                    raw = self.client().chat.completions.create(**self.build_request(request, include_usage=False))
                else:
                    raise
        except Exception as exc:
            yield provider_error(exc, redact=(self.config.get("api_key"),))
            return

        asm = BlockAssembler()
        tool_seq = {}           # tool_call index -> block seq
        finish = None
        usage = None
        try:
            for chunk in raw:
                if cancel.cancelled:
                    try:
                        raw.close()
                    except Exception:
                        pass
                    for ev in asm.close_all():
                        yield ev
                    yield MessageStop(stop_reason=CANCELLED)
                    return
                if getattr(chunk, "usage", None):
                    usage = usage_from(chunk.usage)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning is None and hasattr(delta, "model_extra"):
                        reasoning = (delta.model_extra or {}).get("reasoning_content") or (delta.model_extra or {}).get("reasoning")
                    if reasoning:
                        for ev in asm.close_kind_except(THINKING):
                            yield ev
                        seq, started = asm.ensure(THINKING)
                        if started:
                            yield started
                        yield asm.delta(seq, reasoning)
                    content = getattr(delta, "content", None)
                    if content:
                        for ev in asm.close_kind_except(TEXT):
                            yield ev
                        seq, started = asm.ensure(TEXT)
                        if started:
                            yield started
                        yield asm.delta(seq, content)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        index = getattr(tc, "index", 0) or 0
                        fn = getattr(tc, "function", None)
                        if index not in tool_seq:
                            for ev in asm.close_all():
                                yield ev
                            started = asm.start(TOOL_USE, id=getattr(tc, "id", "") or f"call_{index}", name=(getattr(fn, "name", "") or ""))
                            tool_seq[index] = started.block_seq
                            yield started
                        seq = tool_seq[index]
                        if fn is not None:
                            if getattr(fn, "name", None) and not asm.blocks[seq].name:
                                asm.blocks[seq].name = fn.name
                            if getattr(tc, "id", None) and not asm.blocks[seq].id:
                                asm.blocks[seq].id = tc.id
                            args = getattr(fn, "arguments", None)
                            if args:
                                yield asm.delta(seq, args)
                        sig = gemini_signature(tc)
                        if sig:
                            asm.signature(seq, sig)
                if getattr(choice, "finish_reason", None):
                    finish = choice.finish_reason
        except Exception as exc:
            for ev in asm.close_all():
                yield ev
            yield provider_error(exc, redact=(self.config.get("api_key"),))
            return

        for ev in asm.close_all():
            yield ev
        if usage is None:
            usage = Usage()
        yield UsageEvent(usage=usage.to_dict(), context_tokens=usage.context_size(), model=payload["model"])
        stop = {"tool_calls": TOOL_USE_STOP, "function_call": TOOL_USE_STOP, "length": MAX_TOKENS}.get(finish or "stop", END_TURN)
        if stop == END_TURN and any(b.kind == TOOL_USE for b in asm.blocks):
            stop = TOOL_USE_STOP
        yield MessageStop(stop_reason=stop)


# -- helpers ------------------------------------------------------------------

def join_system(static, dynamic):
    parts = [p for p in (static, dynamic) if p]
    return "\n\n".join(parts)


def usage_from(u):
    details = getattr(u, "prompt_tokens_details", None)
    cached = 0
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)   # DeepSeek naming
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


# Gemini (OpenAI-compatible endpoint) returns a thought signature on the first
# tool call of each step and rejects the next request unless it is sent back
# with that call. Calls recorded without one (e.g. made by another model earlier
# in the conversation) carry Google's documented placeholder instead.
GEMINI_SIGNATURE_PLACEHOLDER = "skip_thought_signature_validator"


def gemini_signature(tool_call):
    extra = getattr(tool_call, "extra_content", None)
    if extra is None and hasattr(tool_call, "model_extra"):
        extra = (tool_call.model_extra or {}).get("extra_content")
    if not isinstance(extra, dict):
        return ""
    return ((extra.get("google") or {}).get("thought_signature")) or ""


def convert_messages(messages, keep_reasoning=False, gemini=False):
    """Native blocks -> chat-completions messages. Tool results become
    role=tool messages; thinking is replayed as reasoning_content on the
    current turn only (DeepSeek), otherwise dropped. For Gemini, tool calls
    carry their thought signature back (extra_content.google)."""
    out = []
    last_user_index = -1
    for i, m in enumerate(messages):
        if m.role == "user" and any(b.kind not in (TOOL_RESULT,) for b in m.blocks):
            last_user_index = i
    for i, m in enumerate(messages):
        if m.role == "user":
            tool_results = [b for b in m.blocks if b.kind == TOOL_RESULT]
            for b in tool_results:
                out.append({"role": "tool", "tool_call_id": b.id, "content": tool_result_text(b)})
            parts = []
            text_parts = []
            for b in m.blocks:
                if b.kind in (TEXT, SKILL):
                    text_parts.append(b.text)
                elif b.kind == IMAGE and b.data.get("data"):
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{b.data.get('media_type', 'image/png')};base64,{b.data['data']}"}})
                elif b.kind == DOCUMENT:
                    text_parts.append(b.text or f"[attached document: {b.name}]")
            if text_parts or parts:
                text = "\n\n".join(t for t in text_parts if t)
                if parts:
                    content = ([{"type": "text", "text": text}] if text else []) + parts
                else:
                    content = text
                out.append({"role": "user", "content": content})
        elif m.role == "assistant":
            text = "".join(b.text for b in m.blocks if b.kind == TEXT)
            tool_calls = []
            for b in m.blocks:
                if b.kind != TOOL_USE:
                    continue
                call = {"id": b.id or f"call_{b.seq}", "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.data, ensure_ascii=False)}}
                if gemini:
                    sig = b.signature or (GEMINI_SIGNATURE_PLACEHOLDER if not tool_calls else "")
                    if sig:
                        call["extra_content"] = {"google": {"thought_signature": sig}}
                tool_calls.append(call)
            msg = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if keep_reasoning and i > last_user_index:
                reasoning = "".join(b.text for b in m.blocks if b.kind == THINKING)
                if reasoning:
                    msg["reasoning_content"] = reasoning
            if msg["content"] is None and not tool_calls:
                continue
            out.append(msg)
    return out


def tool_result_text(block):
    if block.text:
        return block.text
    if block.data:
        return json.dumps(block.data, ensure_ascii=False, default=str)
    return "(empty)"
