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
    return ProviderCaps(
        context_window=int(row.get("context_window_tokens") or 0) or guess_context_window(model),
        max_output_tokens=int(row.get("max_output_tokens") or 0) or guess_max_output(model),
        supports_tools=row.get("supports_tools", 1) in (1, True, "1", None),
        supports_thinking=bool(row.get("supports_thinking")) or guess_thinking(model),
        supports_vision=bool(row.get("supports_vision")),
        supports_pdf=bool(row.get("supports_pdf")) or "claude" in model,
        supports_cache_control="claude" in model,
        input_price_per_million=float(row.get("input_price_per_million") or 0),
        output_price_per_million=float(row.get("output_price_per_million") or 0),
    )


def guess_context_window(model):
    m = (model or "").lower()
    if "claude" in m:
        return 200_000
    if "gemini" in m:
        return 1_000_000
    if "deepseek" in m:
        return 128_000
    if "gpt-5" in m or "gpt-4.1" in m:
        return 400_000
    return 128_000


def guess_max_output(model):
    m = (model or "").lower()
    if "claude" in m:
        return 32_000
    if "deepseek" in m:
        return 8_192
    return 8_192


def guess_thinking(model):
    m = (model or "").lower()
    return "claude" in m or "deepseek" in m or "reasoner" in m or m.startswith("o")


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
