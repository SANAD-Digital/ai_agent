"""The agent loop (pure).

run() yields engine events. It never touches a database: the provider,
the tool executor, the compactor and the cancel token are injected, so the
loop is unit-tested with a scripted provider and a fake executor.

One assistant message = one run() call. Block sequence numbers are global
to the message (provider block numbers are re-based), so the runner can
persist and the client can render straight from the events.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from ai_agent.engine.events import (
    ApprovalRequested,
    ArtifactCreated,
    BlockDelta,
    BlockDone,
    BlockStart,
    Compaction,
    MemoryNote,
    MessageCancelled,
    MessageDone,
    MessageFailed,
    MessageStarted,
    MessageStop,
    ProviderError,
    SkillLoaded,
    ToolResultEvent,
    UsageEvent,
)
from ai_agent.engine.types import (
    ARTIFACT,
    CANCELLED,
    END_TURN,
    MAX_TOKENS,
    MEMORY,
    SKILL,
    TEXT,
    TOOL_RESULT,
    TOOL_USE,
    TOOL_USE_STOP,
    Block,
    Message,
    ModelRequest,
    Usage,
    estimate_tokens,
)

EXTRA_EVENTS = {SKILL: SkillLoaded, ARTIFACT: ArtifactCreated, MEMORY: MemoryNote}


@dataclass
class LoopConfig:
    model: str = ""
    provider_name: str = ""
    max_steps: int = 20
    max_tool_calls: int = 40
    max_tokens: int = 8192
    thinking: str = "off"
    thinking_budget: int = 0
    effort: str = ""
    temperature: float | None = None
    retries: int = 2
    backoff_seconds: float = 2.0


class Loop:
    def __init__(self, provider, executor, compactor, cancel, config: LoopConfig, system_static, system_dynamic, sleep=time.sleep):
        self.provider = provider
        self.executor = executor
        self.compactor = compactor
        self.cancel = cancel
        self.config = config
        self.system_static = system_static
        self.system_dynamic = system_dynamic
        self.sleep = sleep
        self.seq = 0
        self.usage = Usage()
        self.steps = 0
        self.tool_calls = 0

    def base_tokens(self, tools):
        """What every request carries besides the history: the system prompt and the
        full tool definitions (name, description and the parameter schema, which is
        usually the larger part)."""
        return (estimate_tokens(self.system_static) + estimate_tokens(self.system_dynamic)
                + sum(estimate_tokens(json.dumps({"name": t.name, "description": t.description, "parameters": t.parameters}, ensure_ascii=False)) for t in tools))

    # -- public -----------------------------------------------------------------

    def run(self, messages, start_seq=0, resumed=False):
        """messages: provider-ready history ending with the current user turn
        (or, on resume, with the tool_result of the decided approval)."""
        self.seq = start_seq
        yield MessageStarted(model=self.config.model, provider=self.config.provider_name, resumed=resumed)
        tools = self.executor.specs()
        base_tokens = self.base_tokens(tools)
        continued = 0
        nudged = False
        attempts = 0
        overflowed = False

        while True:
            if self.cancel.cancelled:
                yield MessageCancelled(usage=self.usage.to_dict())
                return
            if self.steps >= self.config.max_steps:
                yield MessageFailed(error="Reached the maximum number of steps for this agent.", kind="limit", usage=self.usage.to_dict())
                return

            result = self.compactor.ensure_fits(messages, base_tokens=base_tokens)
            if result.changed:
                messages = result.messages
                yield Compaction(before_tokens=result.before_tokens, after_tokens=result.after_tokens, summary=result.summary, dropped_messages=result.dropped_messages)

            last_step = self.steps + 1 >= self.config.max_steps
            request = ModelRequest(
                model=self.config.model,
                system=self.system_static,
                system_dynamic=self.system_dynamic,
                messages=messages,
                tools=[] if last_step else tools,
                max_tokens=self.config.max_tokens,
                thinking=self.config.thinking,
                thinking_budget=self.config.thinking_budget,
                effort=self.config.effort,
                temperature=self.config.temperature,
            )

            response, stop, error = [], None, None
            local_to_global = {}
            for ev in self.provider.stream(request, self.cancel):
                # Provider events are re-based onto message-level sequence numbers
                # on copies: the provider keeps using its own objects.
                if isinstance(ev, BlockStart):
                    g = self._next_seq()
                    local_to_global[ev.block_seq] = g
                    yield replace(ev, block_seq=g)
                elif isinstance(ev, BlockDelta):
                    yield replace(ev, block_seq=local_to_global.get(ev.block_seq, ev.block_seq))
                elif isinstance(ev, BlockDone):
                    g = local_to_global.get(ev.block_seq, ev.block_seq)
                    payload = dict(ev.block, seq=g)
                    block = Block.from_dict(payload)
                    block.seq = g
                    response.append(block)
                    yield replace(ev, block_seq=g, block=payload)
                elif isinstance(ev, UsageEvent):
                    self.usage.add(Usage(**ev.usage))
                    yield ev
                elif isinstance(ev, MessageStop):
                    stop = ev.stop_reason
                elif isinstance(ev, ProviderError):
                    error = ev
                    yield ev

            if error is not None:
                if error.kind == "context_overflow" and not overflowed and len(messages) > 1:
                    # The estimate was optimistic: compact hard against what was really sent and retry once.
                    overflowed = True
                    result = self.compactor.force(messages, base_tokens=base_tokens)
                    if result.changed:
                        messages = result.messages
                        yield Compaction(before_tokens=result.before_tokens, after_tokens=result.after_tokens, summary=result.summary, dropped_messages=result.dropped_messages)
                        continue
                if error.retryable and attempts < self.config.retries and not self.cancel.cancelled:
                    attempts += 1
                    self.sleep(self.config.backoff_seconds * attempts)
                    continue
                yield MessageFailed(error=error.message, kind=error.kind, usage=self.usage.to_dict())
                return
            attempts = 0
            self.steps += 1

            if stop == CANCELLED or self.cancel.cancelled:
                yield MessageCancelled(usage=self.usage.to_dict())
                return

            if not response:
                if not nudged:
                    nudged = True
                    messages = messages + [Message(role="user", blocks=[Block(kind=TEXT, text="Please answer the request above.")])]
                    continue
                yield MessageFailed(error="The model returned an empty response.", kind="empty", usage=self.usage.to_dict())
                return

            messages = messages + [Message(role="assistant", blocks=response)]
            tool_uses = [b for b in response if b.kind == TOOL_USE]

            if stop == TOOL_USE_STOP and tool_uses:
                results = []
                for call in tool_uses:
                    if self.cancel.cancelled:
                        yield MessageCancelled(usage=self.usage.to_dict())
                        return
                    if self.tool_calls >= self.config.max_tool_calls:
                        block = Block(kind=TOOL_RESULT, id=call.id, name=call.name, text="Tool call budget for this message is exhausted; answer with what you have.", is_error=True)
                        block.seq = self._next_seq()
                        results.append(block)
                        yield ToolResultEvent(block_seq=block.seq, block=block.to_dict())
                        continue
                    self.tool_calls += 1
                    outcome = self.executor.execute(call)
                    for extra in outcome.extras:
                        extra.seq = self._next_seq()
                        yield EXTRA_EVENTS[extra.kind](block_seq=extra.seq, block=extra.to_dict())
                    if outcome.approval:
                        block = outcome.approval_block
                        block.seq = self._next_seq()
                        yield ApprovalRequested(block_seq=block.seq, block=block.to_dict(), approval=outcome.approval)
                        yield MessageDone(status="awaiting_approval", stop_reason=TOOL_USE_STOP, usage=self.usage.to_dict(), steps=self.steps, tool_calls=self.tool_calls)
                        return
                    block = outcome.result
                    block.seq = self._next_seq()
                    results.append(block)
                    yield ToolResultEvent(block_seq=block.seq, block=block.to_dict(), duration_ms=outcome.duration_ms, tool_call=outcome.tool_call)
                    for extra in outcome.extras:
                        if extra.kind == SKILL:
                            results.append(extra)
                messages = messages + [Message(role="user", blocks=results)]
                if getattr(self.executor, "specs_changed", False):
                    tools = self.executor.specs()
                    base_tokens = self.base_tokens(tools)
                    self.executor.specs_changed = False
                continue

            if stop == MAX_TOKENS and continued < 1:
                continued += 1
                messages = messages + [Message(role="user", blocks=[Block(kind=TEXT, text="You stopped because of the output limit. Continue exactly where you stopped, without repeating.")])]
                continue

            yield MessageDone(status="completed", stop_reason=stop or END_TURN, usage=self.usage.to_dict(), steps=self.steps, tool_calls=self.tool_calls)
            return

    # -- internals ---------------------------------------------------------------

    def _next_seq(self):
        value = self.seq
        self.seq += 1
        return value


def summarizer(provider, model, cancel):
    """A compactor summarize() callable that asks the same provider for a
    summary without tools, collecting the streamed text."""

    def summarize(messages, prompt):
        request = ModelRequest(model=model, system="You write faithful, compact summaries of conversations for later reuse.", system_dynamic="", messages=messages + [Message(role="user", blocks=[Block(kind=TEXT, text=prompt)])], tools=[], max_tokens=1500, thinking="off")
        text = []
        for ev in provider.stream(request, cancel):
            if isinstance(ev, BlockDone) and ev.block.get("kind") == TEXT:
                text.append(ev.block.get("text") or "")
            elif isinstance(ev, ProviderError):
                return ""
        return "".join(text)

    return summarize
