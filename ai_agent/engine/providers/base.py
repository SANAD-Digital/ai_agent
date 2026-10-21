"""Provider contract.

stream(request, cancel) yields provider events in arrival order:
  BlockStart / BlockDelta / BlockDone  for thinking, text and tool_use blocks
  UsageEvent                           once per response (may arrive before MessageStop)
  MessageStop(stop_reason)             exactly once on success
  ProviderError                        instead of MessageStop on failure

Block sequence numbers are local to one response (0, 1, 2 …); the loop
re-bases them onto the message.
"""

from __future__ import annotations

import json
import re
import time

from ai_agent.engine.events import BlockDelta, BlockDone, BlockStart, ProviderError
from ai_agent.engine.types import TEXT, THINKING, TOOL_USE, Block, ProviderCaps


class CancelToken:
    """Cooperative cancellation shared by the runner, the loop and the streams."""

    def __init__(self, check=None):
        self._flag = False
        self._check = check
        self._last = 0.0

    def cancel(self):
        self._flag = True

    @property
    def cancelled(self):
        if self._flag:
            return True
        if self._check and time.monotonic() - self._last > 1.0:
            self._last = time.monotonic()
            try:
                self._flag = bool(self._check())
            except Exception:
                pass
        return self._flag


class Provider:
    name = "base"

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model") or ""
        self.timeout = float(config.get("timeout") or 120)

    def caps(self, model=None) -> ProviderCaps:
        return caps_from_config(self.config, model or self.model)

    def stream(self, request, cancel: CancelToken):
        raise NotImplementedError

    def count_tokens(self, request):
        """Optional exact count; None means 'estimate'."""
        return None


def caps_from_config(config, model):
    rows = config.get("models") or {}
    row = rows.get(model) or {}
    local = is_local(config)
    return ProviderCaps(
        context_window=int(row.get("context_window_tokens") or 0) or guess_context_window(model, local=local),
        max_output_tokens=int(row.get("max_output_tokens") or 0) or guess_max_output(model, local=local),
        supports_tools=row.get("supports_tools", 1) in (1, True, "1", None),
        supports_thinking=bool(row.get("supports_thinking")) or guess_thinking(model),
        supports_vision=bool(row.get("supports_vision")),
        supports_pdf=bool(row.get("supports_pdf")) or "claude" in model,
        supports_cache_control="claude" in model,
        input_price_per_million=float(row.get("input_price_per_million") or 0) or guess_prices(model)[0],
        output_price_per_million=float(row.get("output_price_per_million") or 0) or guess_prices(model)[1],
    )


# Anthropic list prices in USD per million tokens (input, output), used when the model row has no price so the
# chat can show an estimated cost. First match wins: specific ids before their family. Other providers' prices
# change too often to ship; enter them on the model row (AI Provider > Models).
CLAUDE_PRICES = (
    ("claude-fable-5", 10.0, 50.0), ("claude-mythos-5", 10.0, 50.0),
    ("claude-opus-5-5", 4.0, 20.0), ("claude-opus-5", 5.0, 25.0), ("claude-opus-4", 5.0, 25.0),
    ("claude-sonnet-5", 2.0, 10.0), ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-5-5", 0.10, 0.50), ("claude-haiku-4", 1.0, 5.0),
)


def guess_prices(model):
    m = (model or "").lower()
    return next(((i, o) for needle, i, o in CLAUDE_PRICES if needle in m), (0.0, 0.0))


# Fallback limits by model family, used only when the provider's model row has no
# value (the provider's own model list fills those rows: Anthropic, Gemini, Groq,
# OpenRouter). First match wins, so specific names come before their family.
# (substring, context window, max output, thinking)
MODEL_LIMITS = (
    # Anthropic
    ("claude", 200_000, 32_000, True),
    # Google: speech/live variants are small; Gemini 2.5+ and 3.x think
    ("-tts", 8_192, 16_384, False),
    ("-live", 131_072, 65_536, False),
    ("gemini-1.", 1_048_576, 8_192, False),
    ("gemini-2.0", 1_048_576, 8_192, False),
    ("gemini", 1_048_576, 65_536, True),
    ("antigravity", 1_048_576, 65_536, True),
    ("gemma-4", 262_144, 32_768, True),
    ("gemma-3", 131_072, 8_192, False),
    ("gemma", 8_192, 4_096, False),
    # DeepSeek: V4 (Flash/Pro) 1M context, 384K output; the older chat/reasoner 128K
    ("deepseek-v4", 1_048_576, 393_216, True),
    ("deepseek-flash", 1_048_576, 393_216, True),
    ("deepseek-pro", 1_048_576, 393_216, True),
    ("deepseek-reasoner", 128_000, 32_768, True),
    ("deepseek-r1", 128_000, 32_768, True),
    ("deepseek", 128_000, 8_192, False),
    # OpenAI
    ("gpt-oss", 131_072, 32_768, True),
    ("gpt-6", 1_050_000, 128_000, True),
    ("gpt-5.", 1_050_000, 128_000, True),
    ("gpt-5", 400_000, 128_000, True),
    ("gpt-4.1", 1_047_576, 32_768, False),
    ("gpt-4o", 128_000, 16_384, False),
    ("gpt-4-turbo", 128_000, 4_096, False),
    ("gpt-4", 8_192, 4_096, False),
    ("gpt-3.5", 16_385, 4_096, False),
    # Open models commonly served by Groq, OpenRouter, Together, Ollama...
    ("kimi", 131_072, 16_384, True),
    ("qwq", 131_072, 32_768, True),
    ("qwen", 131_072, 8_192, False),
    ("llama-guard", 8_192, 1_024, False),
    ("llama", 131_072, 8_192, False),
    ("mixtral", 32_768, 4_096, False),
    ("codestral", 256_000, 8_192, False),
    ("mistral", 128_000, 8_192, False),
    ("command", 128_000, 4_096, False),
    ("nemotron", 128_000, 8_192, False),
)
DEFAULT_LIMITS = (128_000, 8_192, False)
# A local Ollama server runs with its own num_ctx (a few thousand tokens by
# default), whatever the model could do; stay small unless the row says otherwise.
LOCAL_CONTEXT = 8_192


def is_local(config):
    base = str(config.get("base_url") or "")
    return config.get("type") == "Ollama" or ":11434" in base


def model_limits(model):
    m = (model or "").lower()
    for needle, context, output, thinking in MODEL_LIMITS:
        if needle in m:
            return context, output, thinking
    if re.match(r"^(?:[\w.-]+/)?o\d", m):   # o1, o3, o4-mini, openai/o3...
        return 200_000, 100_000, True
    return DEFAULT_LIMITS


def guess_context_window(model, local=False):
    context = model_limits(model)[0]
    return min(context, LOCAL_CONTEXT) if local else context


def guess_max_output(model, local=False):
    output = model_limits(model)[1]
    return min(output, 4_096) if local else output


def guess_thinking(model):
    m = (model or "").lower()
    return model_limits(model)[2] or "reasoner" in m or "thinking" in m


class BlockAssembler:
    """Turns raw deltas into BlockStart/BlockDelta/BlockDone events and keeps
    the assembled blocks so the caller can append the assistant message."""

    def __init__(self):
        self.blocks = []          # [Block]
        self._open = {}           # seq -> {"json": str}
        self._next = 0

    def start(self, kind, id="", name=""):
        seq = self._next
        self._next += 1
        block = Block(kind=kind, id=id, name=name, seq=seq)
        self.blocks.append(block)
        self._open[seq] = {"json": ""}
        return BlockStart(block_seq=seq, kind=kind, id=id, name=name)

    def current(self, kind):
        """Seq of the last open block of this kind, or None."""
        for block in reversed(self.blocks):
            if block.kind == kind and block.seq in self._open:
                return block.seq
        return None

    def ensure(self, kind, id="", name=""):
        seq = self.current(kind)
        if seq is not None:
            return seq, None
        ev = self.start(kind, id=id, name=name)
        return ev.block_seq, ev

    def delta(self, seq, text):
        block = self.blocks[seq]
        if block.kind == TOOL_USE:
            self._open[seq]["json"] += text
        else:
            block.text += text
        return BlockDelta(block_seq=seq, delta=text)

    def signature(self, seq, sig):
        self.blocks[seq].signature += sig

    def stop(self, seq):
        block = self.blocks[seq]
        if block.kind == TOOL_USE and not block.data:
            raw = self._open[seq]["json"].strip()
            try:
                block.data = json.loads(raw) if raw else {}
            except ValueError:
                block.data = {"_raw": raw, "_error": "invalid JSON from the model"}
            if not isinstance(block.data, dict):
                block.data = {"value": block.data}
        self._open.pop(seq, None)
        return BlockDone(block_seq=seq, block=block.to_dict())

    def close_all(self):
        for seq in sorted(self._open):
            yield self.stop(seq)

    def close_kind_except(self, kind):
        """Close open blocks that are not of `kind` (text after thinking, etc.)."""
        for block in list(self.blocks):
            if block.seq in self._open and block.kind != kind:
                yield self.stop(block.seq)


def classify_error(exc):
    """Map SDK/HTTP failures to a small vocabulary the loop can react to."""
    text = str(exc)
    low = text.lower()
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429 or "rate limit" in low or "rate_limit" in low:
        return "rate_limit", True
    if status in (401, 403) or "api key" in low or "authentication" in low or "unauthorized" in low:
        return "auth", False
    if "context length" in low or "context_length" in low or "too many tokens" in low or "prompt is too long" in low or "maximum context" in low:
        return "context_overflow", False
    if "timed out" in low or "timeout" in low or status == 408:
        return "timeout", True
    if status and int(status) >= 500:
        return "server", True
    return "error", False


def provider_error(exc, redact=()):
    kind, retryable = classify_error(exc)
    message = str(exc) or exc.__class__.__name__
    for secret in redact:
        if secret:
            message = message.replace(secret, "***")
    return ProviderError(message=message[:600], kind=kind, retryable=retryable)


__all__ = [
    "Provider", "CancelToken", "BlockAssembler", "provider_error", "classify_error", "caps_from_config",
    "TEXT", "THINKING", "TOOL_USE",
]
