"""Native message model shared by the loop, the providers and the storage.

A message is a role plus an ordered list of blocks. The same blocks are
persisted (AI Message Block), rendered by the chat client and replayed to
the provider as real conversation turns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Block kinds. "skill" and "memory" and "status" are engine-only blocks that
# the provider never sees as such: history.py turns a skill block into a user
# turn and drops the rest.
THINKING = "thinking"
TEXT = "text"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
APPROVAL = "approval"
ARTIFACT = "artifact"
SKILL = "skill"
MEMORY = "memory"
ERROR = "error"
STATUS = "status"
IMAGE = "image"
DOCUMENT = "document"
QUESTION = "question"        # ui.ask: options the user picks from (UI only, like approval)
SUGGESTIONS = "suggestions"  # ui.suggest: follow-up chips under the answer (UI only)

BLOCK_KINDS = (THINKING, TEXT, TOOL_USE, TOOL_RESULT, APPROVAL, ARTIFACT, SKILL, MEMORY, ERROR, STATUS, IMAGE, DOCUMENT, QUESTION, SUGGESTIONS)

# Stop reasons, normalised across providers.
END_TURN = "end_turn"
TOOL_USE_STOP = "tool_use"
MAX_TOKENS = "max_tokens"
CANCELLED = "cancelled"
ERROR_STOP = "error"


@dataclass
class Block:
    kind: str
    text: str = ""                      # text / thinking content, or the rendered tool result text
    data: dict = field(default_factory=dict)   # tool_use input, structured tool result, artifact spec, approval snapshot
    id: str = ""                        # provider tool_use id (tool_use and tool_result share it)
    name: str = ""                      # tool name / skill name
    is_error: bool = False
    signature: str = ""                 # provider thinking signature (replayed verbatim)
    seq: int = 0

    def to_dict(self):
        out = {"kind": self.kind, "seq": self.seq}
        if self.text:
            out["text"] = self.text
        if self.data:
            out["data"] = self.data
        if self.id:
            out["id"] = self.id
        if self.name:
            out["name"] = self.name
        if self.is_error:
            out["is_error"] = True
        if self.signature:
            out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, raw):
        return cls(
            kind=raw.get("kind") or raw.get("block_type") or TEXT,
            text=raw.get("text") or "",
            data=raw.get("data") or {},
            id=raw.get("id") or "",
            name=raw.get("name") or "",
            is_error=bool(raw.get("is_error")),
            signature=raw.get("signature") or "",
            seq=int(raw.get("seq") or 0),
        )


@dataclass
class Message:
    role: str                            # "user" | "assistant"
    blocks: list = field(default_factory=list)
    name: str = ""                       # storage name when persisted
    seq: int = 0                         # position in the session when persisted

    def text(self):
        return "".join(b.text for b in self.blocks if b.kind == TEXT)

    def tool_uses(self):
        return [b for b in self.blocks if b.kind == TOOL_USE]

    def to_dict(self):
        return {"role": self.role, "name": self.name, "seq": self.seq, "blocks": [b.to_dict() for b in self.blocks]}


@dataclass
class ToolSpec:
    name: str                            # wire name ("data__query"); registry name is with dots
    description: str
    parameters: dict
    read_only: bool = True
    cache: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other):
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        return self

    def context_size(self):
        """Tokens the last call actually held: everything read plus what was written."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens + self.output_tokens

    def to_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class ProviderCaps:
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    supports_tools: bool = True
    supports_thinking: bool = False
    supports_vision: bool = False
    supports_pdf: bool = False
    supports_cache_control: bool = False
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0

    def cost(self, usage: Usage):
        if not (self.input_price_per_million or self.output_price_per_million):
            return 0.0
        inputs = usage.input_tokens + usage.cache_read_input_tokens * 0.1 + usage.cache_creation_input_tokens * 1.25
        return (inputs * self.input_price_per_million + usage.output_tokens * self.output_price_per_million) / 1_000_000


@dataclass
class ModelRequest:
    model: str
    system: str                          # static part (cacheable prefix)
    system_dynamic: str                  # per-turn part (user, date, memory…)
    messages: list                       # [Message]
    tools: list                          # [ToolSpec]
    max_tokens: int = 8192
    thinking: str = "off"                # off | adaptive | on
    thinking_budget: int = 0
    effort: str = ""                     # low | medium | high (provider-specific mapping)
    temperature: float | None = None
    extra: dict = field(default_factory=dict)


def estimate_tokens(text):
    """Cheap, provider-agnostic upper-ish estimate: Arabic and JSON both run
    close to 3 characters per token; 4 is too optimistic for this workload."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def message_tokens(message: Message):
    total = 4
    for b in message.blocks:
        total += estimate_tokens(b.text)
        if b.data:
            total += estimate_tokens(json.dumps(b.data, ensure_ascii=False, default=str))
    return total


def wire_name(registry_name):
    return registry_name.replace(".", "__")


def registry_name(wire):
    return wire.replace("__", ".")


def clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [truncated {len(text) - limit} chars]"


