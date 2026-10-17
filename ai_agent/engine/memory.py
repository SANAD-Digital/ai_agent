"""Long-term memory for the chat engine.

Three parts:
- recall(): what the model sees on every call — the user's preferences
  (always, they are few and short) plus the context/mapping facts most
  relevant to the current request, capped to a few hundred tokens.
- extract_session(): after a conversation goes idle, one cheap model call
  reads the transcript since the last extraction and writes durable facts
  (preferences, entity aliases, decisions, confirmed mappings) as AI Memory
  rows. Duplicates are folded by MemoryService.store.
- The model can still store explicitly (memory.store_context) and mappings
  are still learned from approved documents (memory_learning).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint

from ai_agent.runtime.memory_service import MemoryService, normalize_entity_key

MAX_PREFERENCES = 12
MAX_RELEVANT = 6
MAX_LINE_CHARS = 1200   # a whole memory (the store limit); the total stays under MAX_RECALL_CHARS
MAX_RECALL_CHARS = 5000
IDLE_MINUTES = 20
MIN_NEW_MESSAGES = 2
TRANSCRIPT_CHARS = 24_000

EXTRACT_PROMPT = """You maintain long-term memory for an ERP assistant. Read the conversation and extract only facts worth remembering in FUTURE conversations with this user. Return a JSON array; each item:
{"type": "preference" | "context" | "mapping", "content": "<one sentence, in the user's language>", "entity_type": "<Customer|Supplier|Item|Account|Company|Warehouse|Report|...>" or null, "entity_key": "<the entity name>" or null}

Keep:
- preferences: how the user wants answers (language, level of detail, table vs prose, currency, default company/period, tone).
- context: durable facts about their work (which reports they check, what "our main customer" means, projects, responsibilities, names they use for things).
- mapping: confirmed pairings (customer -> account, alias -> document, item -> warehouse) the user confirmed or that came from an executed approval.
Drop: greetings, one-off numbers or results, anything the assistant merely guessed, secrets, salaries, personal data of third parties, and anything already listed under "Already remembered".
Return [] when nothing qualifies. JSON only, no prose."""


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------

def enabled(agent_doc, settings=None):
    settings = settings or frappe.get_cached_doc("AI Settings")
    return bool(settings.get("enable_memory")) and not agent_doc.get("disable_memory")


def recall(ctx, agent_doc, query_text, limit=MAX_RELEVANT):
    """Prompt block: preferences first, then the most relevant facts."""
    if not enabled(agent_doc):
        return ""
    try:
        service = MemoryService()
        rows = []
        for scope in ("User", "Agent", "Company"):
            rows.extend(service.retrieve(user=ctx.user, scope=scope, company=ctx.company, agent=ctx.agent_name))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "AI memory recall")
        return ""
    if not rows:
        return ""
    seen = set()
    preferences, others = [], []
    for r in rows:
        key = normalize_entity_key(r.content or "")[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        (preferences if r.memory_type == "Preference" else others).append(r)
    preferences.sort(key=lambda r: str(r.modified or ""), reverse=True)
    normalized_query = normalize_entity_key(query_text or "")
    others.sort(key=lambda r: MemoryService()._relevance_key(r, normalized_query), reverse=True)

    lines, budget = [], MAX_RECALL_CHARS

    def add(rows):
        nonlocal budget
        for r in rows:
            line = f"- {_line(r)}"
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line)

    if preferences:
        lines.append("Preferences:")
        add(preferences[:MAX_PREFERENCES])
    relevant = others[:limit]
    if relevant:
        lines.append("Facts that may apply:")
        add(relevant)
    return "\n".join(lines)


def _line(row):
    text = " ".join(str(row.content or "").split())
    text = re.sub(r"^(agent=[^;]+;\s*)?(company=[^;]+;\s*)?", "", text)
    if row.get("mapping_json"):
        text += f" (mapping: {str(row.mapping_json)[:120]})"
    return text[:MAX_LINE_CHARS]


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def extract_idle_sessions(limit=20):
    """Scheduler entry: sessions idle for IDLE_MINUTES with unseen messages."""
    if not cint(frappe.db.get_single_value("AI Settings", "enable_memory")):
        return 0
    if not frappe.db.has_column("AI Chat Session", "memory_extracted_seq"):
        return 0   # schema not migrated yet
    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-IDLE_MINUTES)
    rows = frappe.get_all(
        "AI Chat Session",
        filters={"surface": ("in", ("Desk", "Chatbot")), "last_message_on": ("<", cutoff)},
        fields=["name", "last_seq", "memory_extracted_seq"],
        order_by="last_message_on desc",
        limit=limit * 3,
    )
    done = 0
    for r in rows:
        if cint(r.last_seq) - cint(r.memory_extracted_seq) < MIN_NEW_MESSAGES:
            continue
        try:
            extract_session(r.name)
            done += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI memory extraction {r.name}")
        if done >= limit:
            break
    return done


def extract_session(session_name, force=False):
    """One model call over the unseen part of the transcript -> AI Memory rows.
    Returns the number of new memories."""
    session = frappe.get_doc("AI Chat Session", session_name)
    agent = frappe.get_cached_doc("AI Agent", session.agent)
    if not enabled(agent):
        return 0
    since = cint(session.get("memory_extracted_seq"))
    if not force and cint(session.last_seq) - since < MIN_NEW_MESSAGES:
        return 0
    messages = frappe.get_all(
        "AI Chat Message",
        filters={"session": session.name, "seq": (">", since), "status": "completed"},
        fields=["seq", "role", "text"],
        order_by="seq asc",
    )
    transcript = "\n\n".join(f"{'User' if m.role == 'user' else 'Assistant'}: {(m.text or '').strip()}" for m in messages if (m.text or "").strip())
    if not transcript.strip():
        _mark(session)
        return 0
    transcript = transcript[-TRANSCRIPT_CHARS:]

    from ai_agent.runtime.permission_context import PermissionContext

    ctx = PermissionContext(session.user, session.agent, session.company)
    already = recall(ctx, agent, transcript[:2000], limit=20)
    facts = _ask_model(session, transcript, already)

    service = MemoryService()
    known = [normalize_entity_key(r.content or "") for r in frappe.get_all("AI Memory", filters={"user": session.user}, fields=["content"])]
    stored = 0
    for fact in facts:
        kind = str(fact.get("type") or "").lower()
        content = " ".join(str(fact.get("content") or "").split())
        if kind not in ("preference", "context", "mapping") or len(content) < 8:
            continue
        if _similar_known(content, known):
            continue
        known.append(normalize_entity_key(content))
        memory_type = {"preference": "Preference", "context": "Context", "mapping": "Mapping"}[kind]
        scope = "User" if kind == "preference" else "Agent"
        try:
            doc = service.store(
                user=session.user,
                scope=scope,
                memory_type=memory_type,
                content=content,
                company=session.company,
                agent=session.agent if scope == "Agent" else None,
                sensitivity="Low",
                entity_type=fact.get("entity_type") or None,
                entity_key=fact.get("entity_key") or None,
            )
            if doc:
                stored += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), "AI memory store")
    _mark(session)
    frappe.db.commit()
    return stored


def _similar_known(content, known, threshold=0.8):
    """Near-duplicate guard: same normalised text, or ≥80% token overlap."""
    norm = normalize_entity_key(content)
    tokens = {t for t in norm.split() if len(t) >= 3}
    for other in known:
        if other == norm:
            return True
        other_tokens = {t for t in other.split() if len(t) >= 3}
        if tokens and other_tokens and len(tokens & other_tokens) / len(tokens | other_tokens) >= threshold:
            return True
    return False


def _mark(session):
    if frappe.db.has_column("AI Chat Session", "memory_extracted_seq"):
        session.db_set("memory_extracted_seq", cint(session.last_seq), update_modified=False)


def _ask_model(session, transcript, already):
    from ai_agent.engine.events import BlockDone, ProviderError
    from ai_agent.engine.providers.base import CancelToken
    from ai_agent.engine.providers.factory import provider_for
    from ai_agent.engine.types import TEXT, Block, Message, ModelRequest

    provider, _name, model = provider_for(session.provider, session.model)
    user_text = "Already remembered:\n" + (already or "(nothing)") + "\n\nConversation:\n" + transcript
    request = ModelRequest(model=model, system=EXTRACT_PROMPT, system_dynamic="", messages=[Message(role="user", blocks=[Block(kind=TEXT, text=user_text)])], tools=[], max_tokens=1200, thinking="off", temperature=0)
    text = ""
    for ev in provider.stream(request, CancelToken()):
        if isinstance(ev, BlockDone) and ev.block.get("kind") == TEXT:
            text += ev.block.get("text") or ""
        elif isinstance(ev, ProviderError):
            frappe.log_error(ev.message, "AI memory extraction provider error")
            return []
    return parse_facts(text)


def parse_facts(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [f for f in data if isinstance(f, dict)]


# ---------------------------------------------------------------------------
# user-facing listing
# ---------------------------------------------------------------------------

def list_for_user(user, agent=None, limit=200):
    rows = frappe.get_all(
        "AI Memory",
        filters={"user": user},
        fields=["name", "scope", "agent", "company", "memory_type", "content", "entity_type", "entity_key", "mapping_json", "modified", "source_run"],
        order_by="modified desc",
        limit=limit,
    )
    out = []
    for r in rows:
        if agent and r.agent and r.agent != agent:
            continue
        out.append({
            "name": r.name,
            "type": r.memory_type,
            "scope": r.scope,
            "agent": r.agent,
            "company": r.company,
            "content": _line(r),
            "entity": f"{r.entity_type}: {r.entity_key}" if r.entity_type and r.entity_key else "",
            "modified": str(r.modified or ""),
        })
    return out
