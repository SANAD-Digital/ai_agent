"""Token-budgeted context management (pure).

Two levels, applied before every model call:
1. Trim: old tool results (outside the last `keep_recent` turns) are clipped
   to a short preview. Cheap, lossless for the recent work.
2. Summarise: when still over budget, everything before the recent window is
   replaced by a model-written summary. The summary and the seq it covers
   are handed back so the runner can persist them on the session.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_agent.engine.types import TEXT, TOOL_RESULT, Block, Message, clip, message_tokens

TRIM_PREVIEW_CHARS = 400
SUMMARY_PROMPT = (
    "Summarise the conversation so far for your own future reference. Keep: the user's goal, every fact and "
    "number established from tools (with document names), decisions and approvals, open questions, and what "
    "remains to be done. Omit pleasantries. Write in the language the user used. Maximum 600 words."
)


@dataclass
class CompactionResult:
    messages: list
    before_tokens: int
    after_tokens: int
    summary: str = ""
    summary_through_seq: int = 0
    dropped_messages: int = 0

    @property
    def changed(self):
        return self.before_tokens != self.after_tokens or bool(self.summary)


class Compactor:
    def __init__(self, budget_tokens, keep_recent=8, summarize=None):
        self.budget = int(budget_tokens)
        self.keep_recent = keep_recent
        self.summarize = summarize          # callable(messages, prompt) -> str

    def total(self, messages, base_tokens=0):
        return base_tokens + sum(message_tokens(m) for m in messages)

    def force(self, messages, base_tokens=0):
        """The provider rejected the request as too long although the estimate
        said it fits: shrink the budget to what we actually sent minus a margin
        and compact against that, so the retry is guaranteed smaller."""
        actual = self.total(messages, base_tokens)
        self.budget = min(self.budget, int(actual * 0.6))
        return self.ensure_fits(messages, base_tokens)

    def ensure_fits(self, messages, base_tokens=0):
        before = self.total(messages, base_tokens)
        if before <= self.budget:
            return CompactionResult(messages, before, before)
        messages = self._trim_old_results(messages)
        after = self.total(messages, base_tokens)
        if after <= self.budget or not self.summarize:
            return CompactionResult(messages, before, after)
        return self._summarize(messages, before, base_tokens)

    def _recent_boundary(self, messages):
        """Index of the first message of the recent window, aligned to a user
        text turn so tool_use/tool_result pairs are never split."""
        idx = max(0, len(messages) - self.keep_recent)
        while idx > 0 and not self._is_user_text_turn(messages[idx]):
            idx -= 1
        return idx

    @staticmethod
    def _is_user_text_turn(m):
        return m.role == "user" and any(b.kind == TEXT for b in m.blocks) and not any(b.kind == TOOL_RESULT for b in m.blocks)

    def _trim_old_results(self, messages):
        boundary = self._recent_boundary(messages)
        out = []
        for i, m in enumerate(messages):
            if i >= boundary:
                out.append(m)
                continue
            blocks = []
            for b in m.blocks:
                if b.kind == TOOL_RESULT and (len(b.text) > TRIM_PREVIEW_CHARS or b.data):
                    blocks.append(Block(kind=TOOL_RESULT, id=b.id, text=clip(b.text or str(b.data), TRIM_PREVIEW_CHARS), is_error=b.is_error, seq=b.seq))
                else:
                    blocks.append(b)
            out.append(Message(role=m.role, blocks=blocks, name=m.name, seq=m.seq))
        return out

    def _summarize(self, messages, before, base_tokens):
        boundary = self._recent_boundary(messages)
        if boundary == 0:
            # Nothing old enough for the normal window: fold everything before
            # the current user turn instead (the model still needs that turn).
            last_user = max((i for i, m in enumerate(messages) if self._is_user_text_turn(m)), default=0)
            boundary = last_user
            if boundary == 0:
                return CompactionResult(messages, before, self.total(messages, base_tokens))
        old, recent = messages[:boundary], messages[boundary:]
        summary = (self.summarize(old, SUMMARY_PROMPT) or "").strip()
        if not summary:
            return CompactionResult(messages, before, self.total(messages, base_tokens))
        # One chat message can span the cut (an assistant turn and its tool results): only messages that are
        # wholly in the summary count as summarized, or the next load would skip the rest of that message.
        recent_seqs = {m.seq for m in recent if m.seq}
        through = max((m.seq for m in old if m.seq and m.seq not in recent_seqs), default=0)
        head = [
            Message(role="user", blocks=[Block(kind=TEXT, text="Summary of the earlier part of this conversation (for context only):\n\n" + summary)]),
            Message(role="assistant", blocks=[Block(kind=TEXT, text="Understood. I will continue from there.")]),
        ]
        new_messages = head + recent
        return CompactionResult(
            new_messages,
            before,
            self.total(new_messages, base_tokens),
            summary=summary,
            summary_through_seq=through,
            dropped_messages=len(old),
        )


def budget_for(context_window, max_output_tokens, reserve_ratio=0.15):
    """Tokens available for system + history: window minus the reply and a safety margin."""
    return max(8_000, int(context_window * (1 - reserve_ratio)) - int(max_output_tokens))


