"""Engine events. The loop yields these; the runner persists and publishes
them; the client renders them. Every event serialises to
{"type": ..., ...fields} — the same shape goes over realtime.

Two layers:
- ProviderEvent: what a provider stream yields (block-level deltas).
- Engine events: ProviderEvents plus lifecycle events the loop adds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field



@dataclass
class Event:
    type: str = ""

    def to_dict(self):
        out = {}
        for key, value in asdict(self).items():
            if value is None or value == "" or value == {} or value == []:
                continue
            out[key] = value
        out["type"] = self.type
        return out


# ---- provider stream events -------------------------------------------------

@dataclass
class BlockStart(Event):
    """A new content block opened. kind is thinking/text/tool_use."""
    type: str = "block.start"
    block_seq: int = 0
    kind: str = "text"
    id: str = ""            # tool_use id
    name: str = ""          # tool name


@dataclass
class BlockDelta(Event):
    """Text/thinking delta, or a partial JSON delta of a tool_use input."""
    type: str = "block.delta"
    block_seq: int = 0
    delta: str = ""


@dataclass
class BlockDone(Event):
    """The block is complete. For tool_use the block carries the parsed input."""
    type: str = "block.done"
    block_seq: int = 0
    block: dict = field(default_factory=dict)


@dataclass
class UsageEvent(Event):
    type: str = "usage"
    usage: dict = field(default_factory=dict)
    context_tokens: int = 0
    model: str = ""


@dataclass
class MessageStop(Event):
    """The provider finished one response: end_turn | tool_use | max_tokens."""
    type: str = "provider.stop"
    stop_reason: str = ""


@dataclass
class ProviderError(Event):
    type: str = "provider.error"
    message: str = ""
    kind: str = "error"     # rate_limit | context_overflow | auth | timeout | error
    retryable: bool = False


# ---- engine lifecycle events -----------------------------------------------

@dataclass
class MessageStarted(Event):
    type: str = "message.started"
    model: str = ""
    provider: str = ""
    resumed: bool = False


@dataclass
class ToolResultEvent(Event):
    """Result of one tool call. block is a serialised tool_result Block."""
    type: str = "tool.result"
    block_seq: int = 0
    block: dict = field(default_factory=dict)
    duration_ms: int = 0
    tool_call: str = ""      # AI Tool Call audit row when the executor recorded one


@dataclass
class SkillLoaded(Event):
    type: str = "skill.loaded"
    block_seq: int = 0
    block: dict = field(default_factory=dict)


@dataclass
class MemoryNote(Event):
    type: str = "memory.note"
    block_seq: int = 0
    block: dict = field(default_factory=dict)


@dataclass
class ArtifactCreated(Event):
    type: str = "artifact.created"
    block_seq: int = 0
    block: dict = field(default_factory=dict)


@dataclass
class ApprovalRequested(Event):
    """A write tool needs a human decision. The loop stops here; the runner
    marks the message awaiting_approval and keeps the state for resume."""
    type: str = "approval.requested"
    block_seq: int = 0
    block: dict = field(default_factory=dict)
    approval: str = ""


@dataclass
class Compaction(Event):
    type: str = "compaction"
    before_tokens: int = 0
    after_tokens: int = 0
    summary: str = ""
    dropped_messages: int = 0


@dataclass
class MessageDone(Event):
    type: str = "message.done"
    status: str = "completed"     # completed | awaiting_approval
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)
    steps: int = 0
    tool_calls: int = 0


@dataclass
class MessageFailed(Event):
    type: str = "message.failed"
    error: str = ""
    kind: str = "error"
    usage: dict = field(default_factory=dict)


@dataclass
class MessageCancelled(Event):
    type: str = "message.cancelled"
    usage: dict = field(default_factory=dict)


