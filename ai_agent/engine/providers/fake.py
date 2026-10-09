"""Scripted provider for tests. Each call to stream() consumes one script:
a list of steps such as

    [("thinking", "…"), ("text", "Hello"), ("tool", "data__query", {"doctype": "Customer"}),
     ("usage", {"input_tokens": 10, "output_tokens": 5}), ("stop", "tool_use")]

Text is emitted in small chunks so consumers see real deltas. An ("error",
message, kind) step yields a ProviderError instead of a stop.
"""

from __future__ import annotations

import json

from ai_agent.engine.events import MessageStop, ProviderError, UsageEvent
from ai_agent.engine.providers.base import BlockAssembler, Provider
from ai_agent.engine.types import END_TURN, TEXT, THINKING, TOOL_USE, TOOL_USE_STOP, Usage


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, scripts, chunk=7, config=None):
        super().__init__(config or {"model": "fake-1"})
        self.scripts = list(scripts)
        self.chunk = chunk
        self.requests = []          # every ModelRequest seen, for assertions

    def stream(self, request, cancel):
        self.requests.append(request)
        if not self.scripts:
            yield ProviderError(message="no script left", kind="error")
            return
        script = self.scripts.pop(0)
        asm = BlockAssembler()
        usage = None
        stop = None
        n = 0
        for step in script:
            if cancel.cancelled:
                for ev in asm.close_all():
                    yield ev
                yield MessageStop(stop_reason="cancelled")
                return
            kind = step[0]
            if kind in ("text", "thinking"):
                started = asm.start(TEXT if kind == "text" else THINKING)
                yield started
                text = step[1]
                for i in range(0, len(text), self.chunk):
                    if cancel.cancelled:
                        break
                    yield asm.delta(started.block_seq, text[i:i + self.chunk])
                yield asm.stop(started.block_seq)
            elif kind == "tool":
                n += 1
                started = asm.start(TOOL_USE, id=f"toolu_{n}_{len(self.requests)}", name=step[1])
                yield started
                raw = json.dumps(step[2], ensure_ascii=False)
                for i in range(0, len(raw), self.chunk):
                    yield asm.delta(started.block_seq, raw[i:i + self.chunk])
                yield asm.stop(started.block_seq)
            elif kind == "usage":
                usage = Usage(**step[1])
            elif kind == "stop":
                stop = step[1]
            elif kind == "error":
                for ev in asm.close_all():
                    yield ev
                yield ProviderError(message=step[1], kind=step[2] if len(step) > 2 else "error", retryable=len(step) > 3 and step[3])
                return
        for ev in asm.close_all():
            yield ev
        usage = usage or Usage(input_tokens=100, output_tokens=20)
        yield UsageEvent(usage=usage.to_dict(), context_tokens=usage.context_size(), model=request.model)
        if stop is None:
            stop = TOOL_USE_STOP if any(b.kind == TOOL_USE for b in asm.blocks) else END_TURN
        yield MessageStop(stop_reason=stop)
