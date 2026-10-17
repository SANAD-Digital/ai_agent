"""Conversation history as native messages.

Storage keeps one AI Chat Message per user or assistant turn with its blocks;
the assistant's tool results live inside the assistant message (they are part
of its run). For the provider they must be user-role turns, so `to_messages`
splits an assistant turn at every tool_result.

`load` is the only Frappe-aware function here; `to_messages` and
`ensure_tool_results` are pure and unit-tested.
"""

from __future__ import annotations

import json

from ai_agent.engine.types import (
    APPROVAL,
    QUESTION,
    SUGGESTIONS,
    ARTIFACT,
    DOCUMENT,
    ERROR,
    IMAGE,
    MEMORY,
    SKILL,
    STATUS,
    TEXT,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    Block,
    Message,
)

ASSISTANT_KINDS = (THINKING, TEXT, TOOL_USE)
USER_KINDS = (TEXT, IMAGE, DOCUMENT, SKILL)


def to_messages(turns):
    """turns: [(role, seq, name, [Block])] in session order -> [Message] for the provider."""
    out = []
    for role, seq, name, blocks in turns:
        if role == "user":
            keep = [b for b in blocks if b.kind in USER_KINDS and (b.text or b.data)]
            if keep:
                out.append(Message(role="user", blocks=keep, name=name, seq=seq))
            continue
        current = []
        pending_results = []

        def flush_assistant():
            nonlocal current
            if current:
                out.append(Message(role="assistant", blocks=current, name=name, seq=seq))
                current = []

        def flush_results():
            nonlocal pending_results
            if pending_results:
                out.append(Message(role="user", blocks=pending_results, name=name, seq=seq))
                pending_results = []

        for b in blocks:
            if b.kind in ASSISTANT_KINDS:
                flush_results()
                current.append(b)
            elif b.kind == TOOL_RESULT:
                flush_assistant()
                pending_results.append(b)
            elif b.kind == SKILL:
                # A loaded skill is a durable user-turn instruction block.
                flush_assistant()
                pending_results.append(b)
            elif b.kind in (APPROVAL, ARTIFACT, MEMORY, ERROR, STATUS, QUESTION, SUGGESTIONS):
                continue
        flush_assistant()
        flush_results()
    return ensure_tool_results(out)


def ensure_tool_results(messages):
    """Every tool_use must be answered by a tool_result in the next user turn,
    or providers reject the history. Synthesize a neutral result when a run
    was interrupted before the tool finished."""
    fixed = []
    i = 0
    while i < len(messages):
        m = messages[i]
        fixed.append(m)
        if m.role == "assistant" and m.tool_uses():
            ids = [b.id for b in m.tool_uses()]
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            have = {b.id for b in (nxt.blocks if nxt and nxt.role == "user" else []) if b.kind == TOOL_RESULT}
            missing = [tid for tid in ids if tid not in have]
            if missing:
                synthetic = [Block(kind=TOOL_RESULT, id=tid, text="No result: the run was interrupted before this tool finished.", is_error=True) for tid in missing]
                if nxt and nxt.role == "user":
                    nxt.blocks = synthetic + nxt.blocks
                else:
                    fixed.append(Message(role="user", blocks=synthetic))
        i += 1
    # Merge adjacent same-role messages so providers get strict alternation.
    merged = []
    for m in fixed:
        if merged and merged[-1].role == m.role:
            merged[-1].blocks = merged[-1].blocks + m.blocks
        else:
            merged.append(Message(role=m.role, blocks=list(m.blocks), name=m.name, seq=m.seq))
    return merged


def summary_messages(summary):
    if not summary:
        return []
    return [
        Message(role="user", blocks=[Block(kind=TEXT, text="Summary of the earlier part of this conversation (for context only):\n\n" + summary.strip())]),
        Message(role="assistant", blocks=[Block(kind=TEXT, text="Understood. I will continue from there.")]),
    ]


# -- Frappe-aware -------------------------------------------------------------

def block_from_row(row):
    data = row.get("content_json")
    if isinstance(data, str) and data:
        try:
            data = json.loads(data)
        except ValueError:
            data = {"_raw": data}
    return Block(
        kind=row.get("block_type") or TEXT,
        text=row.get("content") or "",
        data=data if isinstance(data, dict) else {},
        id=row.get("tool_call_id") or "",
        name=row.get("tool_name") or "",
        is_error=row.get("status") in ("failed", "denied"),
        signature=row.get("signature") or "",
        seq=int(row.get("seq") or 0),
    )


def load(session_name, exclude=(), after_seq=0):
    """Provider-ready history for a session: persisted summary first, then
    every completed turn after the summary point, as native messages.
    `exclude` names messages of the turn being produced (they are appended
    by the caller), so they never take part in the tool_result repair."""
    import frappe

    if isinstance(exclude, str):
        exclude = {exclude}
    exclude = set(exclude or ())
    session = frappe.db.get_value("AI Chat Session", session_name, ["summary", "summary_through_seq"], as_dict=True) or {}
    start_seq = max(int(after_seq or 0), int(session.get("summary_through_seq") or 0))
    filters = {"session": session_name, "seq": (">", start_seq), "status": ("in", ("completed", "awaiting_approval", "failed", "cancelled"))}
    rows = frappe.get_all("AI Chat Message", filters=filters, fields=["name", "role", "seq", "status", "text"], order_by="seq asc")
    rows = [r for r in rows if r.name not in exclude]
    if not rows:
        return summary_messages(session.get("summary"))
    names = [r.name for r in rows]
    blocks = frappe.get_all(
        "AI Message Block",
        filters={"message": ("in", names)},
        fields=["message", "seq", "block_type", "status", "content", "content_json", "tool_call_id", "tool_name", "signature"],
        order_by="message asc, seq asc",
    )
    by_message = {}
    for b in blocks:
        by_message.setdefault(b.message, []).append(block_from_row(b))
    turns = []
    for r in rows:
        turn_blocks = sorted(by_message.get(r.name, []), key=lambda b: b.seq)
        if r.role == "user":
            # The user's words live on the message; blocks hold attachments/skills only.
            if (r.text or "").strip() and not any(b.kind == TEXT for b in turn_blocks):
                turn_blocks = [Block(kind=TEXT, text=r.text)] + turn_blocks
            if not turn_blocks:
                continue
        turns.append((r.role, r.seq, r.name, turn_blocks))
    return summary_messages(session.get("summary")) + to_messages(turns)
