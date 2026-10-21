"""Chat API for the agent workspace (contract: docs/redesign/client-contract.md).

Sessions and messages are private to their owner (ai_agent.permissions).
Every assistant reply is produced by ai_agent.engine.runner in a background
job and streamed to the browser over the `ai_agent:chat` realtime event.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from ai_agent import roles
from frappe.utils import cint, now_datetime

from ai_agent.ai_agent.doctype.ai_provider.ai_provider import chat_capable
from ai_agent.engine.errors import is_user_message, kind_of, user_message
from ai_agent.engine import approvals as approvals_mod
from ai_agent.engine import runner
from ai_agent.engine.providers.base import guess_context_window
from ai_agent.permissions import assert_owned_access, owner_filters
from ai_agent.runtime import policy

SURFACE = "Desk"
MAX_TEXT = 200_000
MAX_FILES = 10
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "estimated_cost")
MESSAGE_FIELDS = [
    "name", "session", "seq", "role", "status", "text", "creation", "started_at", "completed_at", "duration_ms",
    "model", "provider", "stop_reason", "error", "context_tokens", "feedback", "event_seq", "run", "parent_message",
    "context_doctype", "context_name", "user", *USAGE_KEYS,
]
BLOCK_FIELDS = [
    "name", "message", "seq", "block_type", "status", "content", "content_json", "tool_call_id", "tool_name",
    "is_user_visible", "started_at", "completed_at", "duration_ms", "approval", "artifact",
]


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------

def session_dict(doc, preview=None, context_tokens=None):
    get = doc.get
    model = get("model") or ""
    return {
        "name": doc.name,
        "title": get("title") or _("New Conversation"),
        "agent": get("agent"),
        "company": get("company"),
        "provider": get("provider"),
        "model": model,
        "approval_mode": get("approval_mode") or "Ask",
        "status": get("status") or "Active",
        "surface": get("surface") or frappe.db.get_value("AI Chat Session", doc.name, "surface") or "Desk",
        "pinned": cint(get("pinned")),
        "last_message_on": str(get("last_message_on") or get("modified") or ""),
        "message_count": cint(get("message_count")),
        "preview": preview if preview is not None else "",
        "usage": {k: (get(k) or 0) for k in USAGE_KEYS},
        "context_tokens": cint(context_tokens) if context_tokens is not None else cint(get("context_tokens")),
        "context_window": _context_window(get("provider"), model),
        "last_seq": cint(get("last_seq")),
    }


def _context_window(provider, model):
    try:
        if provider and model:
            row = frappe.db.get_value("AI Provider Model", {"parent": provider, "model_name": model}, "context_window_tokens")
            if cint(row):
                return cint(row)
    except Exception:
        pass
    return guess_context_window(model)


def block_dict(row):
    data = row.get("content_json")
    if isinstance(data, str) and data:
        try:
            data = json.loads(data)
        except ValueError:
            data = {"_raw": data}
    return {
        "seq": cint(row.get("seq")),
        "kind": row.get("block_type"),
        "status": row.get("status") or "completed",
        "text": row.get("content") or "",
        "data": data if isinstance(data, (dict, list)) else {},
        "id": row.get("tool_call_id") or "",
        "name": row.get("tool_name") or "",
        "is_error": row.get("status") in ("failed", "denied"),
        "started_at": str(row.get("started_at") or ""),
        "completed_at": str(row.get("completed_at") or ""),
        "duration_ms": cint(row.get("duration_ms")),
    }


def _error_fields(row):
    """What a failed reply shows: one plain sentence for everyone, and the raw
    technical text (provider codes, JSON) only for System Managers. Replies that
    failed before friendly messages existed stored the raw text; they are
    worded the same way here."""
    error = row.get("error")
    if not error:
        return {"error": None, "error_detail": None}
    admin = "System Manager" in frappe.get_roles()
    if is_user_message(error):
        detail = frappe.db.get_value("AI Run", row.get("run"), "error_summary") if admin and row.get("run") else None
        return {"error": error, "error_detail": detail}
    return {"error": user_message(kind_of("error", error), error, user=row.get("user") or frappe.session.user),
            "error_detail": error if admin else None}


def message_dict(row, blocks=None, attachments=None):
    get = row.get
    return {
        "name": row.name,
        "session": get("session"),
        "seq": cint(get("seq")),
        "role": get("role"),
        "status": get("status"),
        "text": get("text") or "",
        "created_at": str(get("creation") or ""),
        "completed_at": str(get("completed_at") or "") or None,
        "duration_ms": cint(get("duration_ms")),
        "model": get("model") or "",
        "provider": get("provider") or "",
        "stop_reason": get("stop_reason") or "",
        **_error_fields(row),
        "usage": {k: (get(k) or 0) for k in USAGE_KEYS},
        "context_tokens": cint(get("context_tokens")),
        "attachments": attachments or [],
        "blocks": blocks or [],
        "feedback": cint(get("feedback")),
        "event_seq": cint(get("event_seq")),
        "run": get("run"),
        "parent_message": get("parent_message"),
        "context_doctype": get("context_doctype"),
        "context_name": get("context_name"),
    }


def _blocks_for(message_names, include_hidden=False):
    if not message_names:
        return {}
    filters = {"message": ("in", list(message_names))}
    rows = frappe.get_all("AI Message Block", filters=filters, fields=BLOCK_FIELDS, order_by="seq asc")
    out = {}
    for r in rows:
        out.setdefault(r.message, []).append(block_dict(r))
    return out


def _attachments_for(message_names):
    if not message_names:
        return {}
    rows = frappe.get_all(
        "File",
        filters={"attached_to_doctype": "AI Chat Message", "attached_to_name": ("in", list(message_names)), "is_folder": 0},
        fields=["name", "file_name", "file_url", "file_size", "is_private", "attached_to_name"],
        order_by="creation asc",
    )
    out = {}
    for r in rows:
        out.setdefault(r.attached_to_name, []).append({"name": r.name, "file_name": r.file_name, "file_url": r.file_url, "file_size": r.file_size, "is_private": r.is_private})
    return out


def _chat_page():
    """The full chat page and its own endpoints: AI Chat User (or System Manager)."""
    roles.assert_role([roles.CHAT_ROLE])


def _any_chat():
    """Endpoints the page and the support window share."""
    roles.assert_role([roles.CHAT_ROLE, roles.BUBBLE_ROLE])


def _get_session(name, ptype="read"):
    _any_chat()
    doc = frappe.get_doc("AI Chat Session", name)
    assert_owned_access(doc.user, "AI Chat Session", doc.name)
    if ptype != "read" and doc.user != frappe.session.user:
        frappe.throw(_("Not authorized to change this conversation."), frappe.PermissionError)
    return doc


def _get_message(name, ptype="read"):
    _any_chat()
    doc = frappe.get_doc("AI Chat Message", name)
    assert_owned_access(doc.user, "AI Chat Message", doc.name)
    if ptype != "read" and doc.user != frappe.session.user:
        frappe.throw(_("Not authorized to change this message."), frappe.PermissionError)
    return doc


# ---------------------------------------------------------------------------
# bootstrap / sessions
# ---------------------------------------------------------------------------

@frappe.whitelist()
def bootstrap():
    _chat_page()
    user = frappe.session.user
    info = frappe.db.get_value("User", user, ["full_name", "user_image", "language"], as_dict=True) or {}
    lang = info.get("language") or frappe.local.lang or "en"
    providers = []
    for p in available_providers():
        providers.append({
            "name": p["name"],
            "label": p.get("provider_name") or p["name"],
            "provider_type": p.get("provider_type"),
            "is_default": cint(p.get("is_default")),
            "default_model": p.get("default_model"),
            "models": [
                {"model_name": m.get("model_name"), "label": m.get("label") or m.get("model_name"), "is_default": cint(m.get("is_default")), "supports_thinking": cint(m.get("supports_thinking")) or _guess_thinking(m.get("model_name"))}
                for m in p.get("models", [])
            ],
        })
    agents = []
    for a in frappe.get_all("AI Agent", filters={"enabled": 1}, fields=["name", "agent_name", "description", "read_only", "allow_actions", "provider", "model"]):
        # Names and descriptions are data, translated in the "AI Agent" context (ar.csv for the
        # standard agents; any other agent through Desk's Translation list).
        agents.append({"name": a["name"], "agent_name": _(a.get("agent_name") or a["name"], context="AI Agent"),
            "description": _(a.get("description") or "", context="AI Agent") if a.get("description") else "", "read_only": cint(a.get("read_only")), "allow_actions": cint(a.get("allow_actions")), "is_default": cint(a.get("is_default")),
            "provider": a.get("provider") or None, "model": a.get("model") or None})
    return {
        "user": {"name": user, "full_name": info.get("full_name") or user, "image": info.get("user_image"), "language": lang, "dir": "rtl" if str(lang).startswith(("ar", "he", "fa", "ur")) else "ltr"},
        "agents": agents,
        "providers": providers,
        "settings": {"third_party_warning": policy.third_party_warning(), "thinking_options": ["off", "adaptive", "on"], "auto_approval": policy.auto_approval_available()},
        "sessions": list_sessions(limit=50)["sessions"],
    }


def available_providers():
    """Enabled AI Providers the policy allows, with their selectable models (no secrets)."""
    providers = [
        provider
        for provider in frappe.get_all(
            "AI Provider",
            filters={"enabled": 1},
            fields=["name", "provider_name", "provider_type", "default_model", "is_default", "description"],
            order_by="is_default desc, provider_name asc",
        )
        if policy.provider_allowed(provider.provider_type)
    ]
    for provider in providers:
        rows = frappe.get_all(
            "AI Provider Model",
            filters={"parent": provider.name, "parenttype": "AI Provider"},
            fields=["model_name", "label", "is_default", "supports_vision", "supports_thinking", "supports_tools", "model_type", "context_window_tokens"],
            order_by="idx asc",
        )
        rows = [row for row in rows if chat_capable(row)]   # speech, agent-only or tool-less models cannot run the chat
        provider["models"] = rows
        if not provider.default_model:
            provider["default_model"] = next((row.model_name for row in rows if row.is_default), rows[0].model_name if rows else "")
    # a provider enabled before any model was picked would only fail when chosen
    return [p for p in providers if p["models"] or p["default_model"]]


def _guess_thinking(model):
    from ai_agent.engine.providers.base import guess_thinking

    return 1 if guess_thinking(model or "") else 0


@frappe.whitelist()
def list_sessions(limit=50, offset=0, include_archived=0, search=""):
    _chat_page()
    limit, offset = min(cint(limit) or 50, 200), cint(offset)
    filters = dict(owner_filters())
    filters["surface"] = SURFACE
    # Sessions from before the message model (last_seq = 0) have nothing to
    # show until scripts/migrate_chat_history.py has converted them.
    filters["last_seq"] = (">", 0)
    if not cint(include_archived):
        filters["status"] = "Active"
    or_filters = None
    hits = {}
    if search:
        term = f"%{search.strip()}%"
        # Titles and what was said: the sessions are still filtered by owner (owner_filters) below.
        hits = _content_hits(term)
        or_filters = {"title": ("like", term), **({"name": ("in", list(hits))} if hits else {})}
    rows = frappe.get_all(
        "AI Chat Session",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "title", "user", "agent", "company", "provider", "model", "status", "pinned", "last_message_on", "message_count", "last_seq", "modified", *USAGE_KEYS],
        order_by="pinned desc, last_message_on desc, modified desc",
        limit=limit + 1,
        start=offset,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    previews = _previews([r.name for r in rows])
    previews.update({name: hits[name] for name in (r.name for r in rows) if name in hits})
    return {"sessions": [session_dict(r, preview=previews.get(r.name, "")) for r in rows], "has_more": has_more}


def _content_hits(term, limit=300):
    """session -> a short excerpt around the first message that contains the search term."""
    rows = frappe.db.sql(
        """select m.session, m.text from `tabAI Chat Message` m
        where m.text like %(term)s and m.user = %(user)s
        order by m.creation desc limit %(limit)s""",
        {"term": term, "user": frappe.session.user, "limit": limit}, as_dict=True)
    needle = term.strip("%").lower()
    out = {}
    for r in rows:
        if r.session in out:
            continue
        text = " ".join((r.text or "").split())
        at = max(text.lower().find(needle), 0)
        start = max(at - 40, 0)
        out[r.session] = ("…" if start else "") + text[start:start + 120]
    return out


def _previews(session_names):
    """The last completed answer of each session: one row per session (it read every answer of 50 sessions)."""
    if not session_names:
        return {}
    rows = frappe.db.sql(
        """select m.session, left(m.text, 400) text from `tabAI Chat Message` m
        join (select session, max(seq) seq from `tabAI Chat Message`
              where session in %(names)s and role = 'assistant' and status = 'completed' and ifnull(text, '') != ''
              group by session) last on last.session = m.session and last.seq = m.seq""",
        {"names": tuple(session_names)}, as_dict=True)
    return {r.session: " ".join((r.text or "").split())[:120] for r in rows}


@frappe.whitelist(methods=["POST"])
def create_session(agent=None, company=None, provider=None, model=None, title=None):
    _chat_page()
    agent = agent or _default_agent()
    _assert_agent(agent)
    if company and not frappe.has_permission("Company", "read", company):
        frappe.throw(_("No access to company {0}").format(company), frappe.PermissionError)
    provider, model = _resolve_provider_model(provider, model, agent)
    doc = frappe.get_doc({
        "doctype": "AI Chat Session",
        "title": (title or "").strip()[:80] or _("New Conversation"),
        "user": frappe.session.user,
        "agent": agent,
        "company": company or None,
        "provider": provider,
        "model": model,
        "status": "Active",
        "surface": SURFACE,
        "last_message_on": now_datetime(),
        "message_count": 0,
        "last_seq": 0,
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"session": session_dict(doc)}


def _default_agent():
    name = frappe.db.get_value("AI Agent", {"is_default": 1, "enabled": 1}, "name") or frappe.db.get_value("AI Agent", {"enabled": 1}, "name")
    if not name:
        frappe.throw(_("No enabled AI Agent is configured."))
    return name


def _assert_agent(agent):
    if not frappe.db.get_value("AI Agent", agent, "enabled"):
        frappe.throw(_("Agent {0} is not available.").format(agent))


# Calls that start a model run (and spend the provider budget) per user: a script or a forged page must not loop them.
MODEL_CALLS_PER_MINUTE = 20
MODEL_CALLS_PER_DAY = 600
VIEW_CALLS_PER_MINUTE = 120


def _throttle(bucket, per_minute, per_day=None):
    import time

    if frappe.flags.in_test:
        return
    user = frappe.session.user
    now = int(time.time())
    for span, limit in ((60, per_minute), (86400, per_day)):
        if not limit:
            continue
        key = frappe.cache.make_key(f"ai-agent-chat-rate:{bucket}:{user}:{span}:{now // span}")
        count = frappe.cache.incrby(key, 1)
        if count == 1:
            frappe.cache.expire(key, span * 2)
        if count > limit:
            frappe.throw(_("Too many requests. Wait a moment and try again."), frappe.RateLimitExceededError)


def _resolve_provider_model(provider, model, agent):
    from ai_agent.engine.providers.factory import resolve_provider_doc

    agent_provider, agent_model = (frappe.db.get_value("AI Agent", agent, ["provider", "model"]) if agent else None) or (None, None)
    if not provider and agent_provider and frappe.db.get_value("AI Provider", agent_provider, "enabled"):
        provider = agent_provider   # no explicit choice: the agent's own provider
    doc = resolve_provider_doc(provider or None)
    policy.assert_provider_allowed(doc.provider_type or "OpenAI Compatible")
    if model:
        known = {r.model_name for r in doc.get("models") or []}
        if known and model not in known:
            frappe.throw(_("Model {0} is not configured for provider {1}.").format(model, doc.name))
    else:
        # the agent's model only counts on the agent's provider (or when it names none)
        model = (agent_model if agent_provider in (None, "", doc.name) else None) or doc.get("default_model") \
            or next((r.model_name for r in doc.get("models") or [] if r.is_default), None) \
            or next((r.model_name for r in doc.get("models") or []), None) or ""
    return doc.name, model


@frappe.whitelist()
def get_session(session):
    doc = _get_session(session)
    last = frappe.db.get_value("AI Chat Message", {"session": doc.name, "role": "assistant", "status": "completed"}, "context_tokens", order_by="seq desc")
    return {"session": session_dict(doc, preview=_previews([doc.name]).get(doc.name, ""), context_tokens=last)}


@frappe.whitelist()
def export_session(session):
    """The conversation as Markdown: what was asked and answered, plus the tools each reply used (names only;
    tool results stay in the system)."""
    doc = _get_session(session)
    messages = frappe.get_all(
        "AI Chat Message",
        filters={"session": doc.name, "status": ("in", ("completed", "failed", "cancelled", "awaiting_approval"))},
        fields=["name", "role", "text", "model", "creation", "status", "error"],
        order_by="seq asc",
    )
    tools = {}
    if messages:
        for row in frappe.get_all("AI Message Block", filters={"message": ("in", [m.name for m in messages]), "block_type": "tool_use"}, fields=["message", "tool_name"], order_by="seq asc"):
            tools.setdefault(row.message, []).append((row.tool_name or "").replace("__", "."))
    lines = [f"# {doc.title or _('Conversation')}", "", f"*{doc.agent or ''} · {frappe.utils.format_datetime(doc.creation)}*", ""]
    for m in messages:
        who = _("You") if m.role == "user" else (_("Assistant") + (f" ({m.model})" if m.model else ""))
        lines += [f"## {who} · {frappe.utils.format_datetime(m.creation, 'yyyy-MM-dd HH:mm')}", ""]
        if tools.get(m.name):
            lines += ["> " + _("Tools used: {0}").format(", ".join(dict.fromkeys(tools[m.name]))), ""]
        body = m.text or (m.error if m.status == "failed" else "")
        lines += [body or "_" + _("(no text)") + "_", ""]
    filename = "".join(c if c.isalnum() or c in " -_" else "_" for c in (doc.title or doc.name))[:60].strip() or doc.name
    return {"filename": f"{filename}.md", "content": "\n".join(lines)}


@frappe.whitelist(methods=["POST"])
def rename_session(session, title):
    doc = _get_session(session, "write")
    title = " ".join(str(title or "").split())[:80]
    if not title:
        frappe.throw(_("Title is required."))
    doc.db_set("title", title)
    frappe.db.commit()
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def archive_session(session, archived=1):
    doc = _get_session(session, "write")
    doc.db_set("status", "Archived" if cint(archived) else "Active")
    frappe.db.commit()
    if cint(archived):
        # The conversation is over: learn from it now instead of waiting for the hourly job.
        frappe.enqueue("ai_agent.engine.memory.extract_session", queue="short", session_name=doc.name, enqueue_after_commit=True)
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def pin_session(session, pinned=1):
    doc = _get_session(session, "write")
    doc.db_set("pinned", 1 if cint(pinned) else 0)
    frappe.db.commit()
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def delete_session(session):
    doc = _get_session(session, "write")
    for m in frappe.get_all("AI Chat Message", filters={"session": doc.name, "status": ("in", ("pending", "streaming"))}, pluck="name"):
        runner.request_cancel(m)
    frappe.delete_doc("AI Chat Session", doc.name, ignore_permissions=True, force=True)
    frappe.db.commit()
    return {"session": doc.name, "deleted": True}


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_messages(session, before_seq=None, limit=40):
    doc = _get_session(session)
    limit = min(cint(limit) or 40, 200)
    filters = {"session": doc.name}
    if before_seq not in (None, "", 0, "0"):
        filters["seq"] = ("<", cint(before_seq))
    rows = frappe.get_all("AI Chat Message", filters=filters, fields=MESSAGE_FIELDS, order_by="seq desc", limit=limit + 1)
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    names = [r.name for r in rows]
    blocks = _blocks_for(names)
    attachments = _attachments_for([r.name for r in rows if r.role == "user"])
    messages = [message_dict(r, blocks=_with_tail(r, blocks.get(r.name, [])), attachments=attachments.get(r.name, [])) for r in rows]
    return {"messages": messages, "has_more": has_more}


def _with_tail(row, blocks):
    """Merge the Redis delta tail into streaming blocks so a reconnecting client sees the current text."""
    if row.get("status") != "streaming":
        return blocks
    try:
        tail = frappe.cache().hgetall(runner.TAIL_KEY.format(row.name)) or {}
    except Exception:
        return blocks
    if not tail:
        return blocks
    decoded = {}
    for k, v in tail.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        value = v.decode() if isinstance(v, bytes) else v
        if key == "_seq":
            # Live realtime sequence: everything up to it is inside this snapshot.
            row.update({"event_seq": max(cint(row.get("event_seq")), cint(value))})
            continue
        if not key.lstrip("-").isdigit():
            continue
        decoded[cint(key)] = str(value)
    for b in blocks:
        if b["status"] == "streaming" and b["seq"] in decoded and len(decoded[b["seq"]]) > len(b["text"]):
            b["text"] = decoded[b["seq"]]
    return blocks


@frappe.whitelist()
def get_message(message):
    doc = _get_message(message)
    blocks = _blocks_for([doc.name]).get(doc.name, [])
    attachments = _attachments_for([doc.name]).get(doc.name, []) if doc.role == "user" else []
    return {"message": message_dict(doc, blocks=_with_tail(doc, blocks), attachments=attachments)}


@frappe.whitelist(methods=["POST"])
def send_message(text=None, session=None, agent=None, company=None, provider=None, model=None, thinking=None, files=None, skill=None, plugin=None, context_doctype=None, context_name=None, approval_mode=None):
    _throttle("model", MODEL_CALLS_PER_MINUTE, MODEL_CALLS_PER_DAY)
    text = (text or "").strip()
    files = _parse_list(files)
    if not text and not files:
        frappe.throw(_("Type a message or attach a file."))
    if len(text) > MAX_TEXT:
        frappe.throw(_("The message is too long."))
    if len(files) > MAX_FILES:
        frappe.throw(_("You can attach up to {0} files.").format(MAX_FILES))
    user = frappe.session.user

    if session:
        sdoc = _get_session(session, "write")
        if (sdoc.surface or "Desk") == "Desk":
            _chat_page()
        else:
            # The support bubble (and any non-Desk surface) has its agent, company and model fixed by its own
            # settings: a bubble-only user must not switch them (to an agent with actions) through this API.
            agent = company = provider = model = approval_mode = None
        changes = {}
        if agent and agent != sdoc.agent:
            _assert_agent(agent)
            changes["agent"] = agent
        if company is not None and company != (sdoc.company or "") and (company == "" or frappe.has_permission("Company", "read", company)):
            changes["company"] = company or None
        if provider or model:
            p, m = _resolve_provider_model(provider or sdoc.provider, model or sdoc.model, agent or sdoc.agent)
            if p != sdoc.provider or m != sdoc.model:
                changes.update({"provider": p, "model": m})
        if sdoc.title in (None, "", _("New Conversation")) and text and cint(sdoc.message_count) == 0:
            changes["title"] = " ".join(text.split())[:80]
        if changes:
            sdoc.db_set(changes, update_modified=False)
            sdoc.reload()
    else:
        created = create_session(agent=agent, company=company, provider=provider, model=model, title=" ".join(text.split())[:80])
        sdoc = frappe.get_doc("AI Chat Session", created["session"]["name"])
    if approval_mode and approval_mode != (sdoc.approval_mode or "Ask"):
        sdoc.db_set("approval_mode", _approval_mode(approval_mode), update_modified=False)

    _assert_agent(sdoc.agent)
    if frappe.db.exists("AI Chat Message", {"session": sdoc.name, "status": ("in", ("pending", "streaming"))}):
        frappe.throw(_("Wait for the current reply to finish, or stop it first."))
    # Going on while an approval card waits: that reply stops waiting (the card can still be decided).
    from ai_agent.engine.approvals import settle_waiting

    for waiting in frappe.get_all("AI Chat Message", filters={"session": sdoc.name, "status": "awaiting_approval"}, pluck="name"):
        settle_waiting(waiting, "superseded")

    seq = cint(sdoc.last_seq)
    user_msg = frappe.get_doc({
        "doctype": "AI Chat Message",
        "session": sdoc.name,
        "seq": seq + 1,
        "role": "user",
        "status": "completed",
        "user": user,
        "context_doctype": context_doctype or None,
        "context_name": context_name or None,
        "completed_at": now_datetime(),
    })
    user_msg.insert(ignore_permissions=True)
    user_msg.db_set("text", text, update_modified=False)   # verbatim: insert() would HTML-sanitize "<"
    _attach_files(user_msg.name, files)

    run = frappe.get_doc({
        "doctype": "AI Run",
        "agent": sdoc.agent,
        "user": user,
        "company": sdoc.company,
        "prompt": text or _("(attachments)"),
        "status": "Queued",
        "session": sdoc.name,
        "chat_session": sdoc.name,
        "provider": sdoc.provider,
        "model": sdoc.model,
        "route": _run_options(skill=skill, plugin=plugin, thinking=thinking),
        "context_doctype": context_doctype or None,
        "context_name": context_name or None,
    })
    run.insert(ignore_permissions=True)

    assistant = frappe.get_doc({
        "doctype": "AI Chat Message",
        "session": sdoc.name,
        "seq": seq + 2,
        "role": "assistant",
        "status": "pending",
        "user": user,
        "run": run.name,
        "parent_message": user_msg.name,
        "provider": sdoc.provider,
        "model": sdoc.model,
    })
    assistant.insert(ignore_permissions=True)
    run.db_set("message", assistant.name, update_modified=False)
    sdoc.db_set({"last_seq": seq + 2, "message_count": cint(sdoc.message_count) + 2, "last_message_on": now_datetime()}, update_modified=False)
    frappe.db.commit()

    runner.enqueue(assistant.name, user)
    sdoc.reload()
    return {
        "session": session_dict(sdoc),
        "user_message": message_dict(user_msg, blocks=[], attachments=_attachments_for([user_msg.name]).get(user_msg.name, [])),
        "assistant_message": message_dict(assistant, blocks=[]),
    }


def _run_options(skill=None, plugin=None, thinking=None):
    """Per-message options ride on AI Run.route as `key:value;key:value`."""
    parts = []
    if skill:
        parts.append(f"skill:{skill}")
    if plugin:
        from ai_agent.runtime import access

        if not access.is_allowed(access.PLUGIN, plugin):
            frappe.throw(_("This plugin is not available to you."), frappe.PermissionError)
        parts.append(f"plugin:{plugin}")
    if thinking in ("off", "adaptive", "on"):
        parts.append(f"thinking:{thinking}")
    return ";".join(parts) or None


def _parse_list(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    return [v for v in value if v]


def _attach_files(message_name, file_names):
    for name in file_names:
        f = frappe.get_doc("File", name)
        # Only the user's own upload: re-pointing a file would take it off the document it belongs to.
        if f.owner != frappe.session.user:
            frappe.throw(_("File {0} is not yours.").format(name), frappe.PermissionError)
        if f.attached_to_name and (f.attached_to_doctype != "AI Chat Message" or f.attached_to_name != message_name):
            frappe.throw(_("File {0} is already attached to another document.").format(name))
        f.db_set({"attached_to_doctype": "AI Chat Message", "attached_to_name": message_name, "is_private": 1}, update_modified=False)


@frappe.whitelist(methods=["POST"])
def stop_message(message):
    doc = _get_message(message, "write")
    if doc.status not in ("pending", "streaming"):
        return {"status": doc.status}
    runner.request_cancel(doc.name)
    if doc.run:
        frappe.db.set_value("AI Run", doc.run, "cancel_requested", 1, update_modified=False)
    if doc.status == "pending" or not runner.job_is_alive(doc.name):
        # Not started yet, or its job is gone (worker restarted, crashed): no runner will ever read the
        # flag, so settle it here. A job that starts later sees the terminal status and returns.
        doc.db_set({"status": "cancelled", "completed_at": now_datetime()}, update_modified=False)
        if doc.run:
            frappe.db.set_value("AI Run", doc.run, {"status": "Cancelled", "completed_at": now_datetime()}, update_modified=False)
        frappe.db.set_value("AI Message Block", {"message": doc.name, "status": "streaming"}, "status", "completed", update_modified=False)
        frappe.db.commit()
        pub = runner.Publisher(doc.session, doc.name, doc.user, start_seq=cint(doc.event_seq))
        from ai_agent.engine.events import MessageCancelled

        pub.publish(MessageCancelled())
        return {"status": "cancelled"}
    frappe.db.commit()
    return {"status": "cancelling"}


@frappe.whitelist(methods=["POST"])
def retry_message(message):
    _throttle("model", MODEL_CALLS_PER_MINUTE, MODEL_CALLS_PER_DAY)
    doc = _get_message(message, "write")
    if doc.role != "assistant":
        frappe.throw(_("Only an assistant message can be regenerated."))
    if doc.status in ("pending", "streaming"):
        frappe.throw(_("The message is still running."))
    if frappe.db.exists("AI Chat Message", {"session": doc.session, "seq": (">", doc.seq)}):
        frappe.throw(_("Only the last reply can be regenerated."))
    _reset_assistant(doc)
    frappe.db.commit()
    runner.enqueue(doc.name, doc.user)
    return {"assistant_message": message_dict(frappe.get_doc("AI Chat Message", doc.name), blocks=[])}


def _reset_assistant(doc):
    for b in frappe.get_all("AI Message Block", filters={"message": doc.name}, pluck="name"):
        frappe.delete_doc("AI Message Block", b, ignore_permissions=True, force=True)
    sdoc = frappe.get_doc("AI Chat Session", doc.session)
    for key in USAGE_KEYS:
        sdoc.db_set(key, max(0, (sdoc.get(key) or 0) - (doc.get(key) or 0)), update_modified=False)
    doc.db_set({"status": "pending", "text": "", "error": None, "stop_reason": None, "completed_at": None, "started_at": None, "duration_ms": 0, "steps": 0, "tool_calls": 0, "context_tokens": 0, "event_seq": cint(doc.event_seq), **{k: 0 for k in USAGE_KEYS}}, update_modified=False)
    if doc.run:
        frappe.db.set_value("AI Run", doc.run, {"status": "Queued", "cancel_requested": 0, "final_answer": None, "error_summary": None, "completed_at": None}, update_modified=False)


@frappe.whitelist(methods=["POST"])
def edit_message(message, text):
    _throttle("model", MODEL_CALLS_PER_MINUTE, MODEL_CALLS_PER_DAY)
    doc = _get_message(message, "write")
    if doc.role != "user":
        frappe.throw(_("Only your own message can be edited."))
    text = (text or "").strip()
    if not text:
        frappe.throw(_("Type a message."))
    later = frappe.get_all("AI Chat Message", filters={"session": doc.session, "seq": (">", doc.seq)}, fields=["name", "status", "role"], order_by="seq asc")
    for m in later:
        if m.status in ("pending", "streaming"):
            runner.request_cancel(m.name)
        frappe.delete_doc("AI Chat Message", m.name, ignore_permissions=True, force=True)
    doc.db_set({"text": text}, update_modified=False)
    sdoc = frappe.get_doc("AI Chat Session", doc.session)
    run = frappe.get_doc({
        "doctype": "AI Run", "agent": sdoc.agent, "user": doc.user, "company": sdoc.company, "prompt": text, "status": "Queued",
        "session": sdoc.name, "chat_session": sdoc.name, "provider": sdoc.provider, "model": sdoc.model,
        "context_doctype": doc.context_doctype, "context_name": doc.context_name,
    })
    run.insert(ignore_permissions=True)
    assistant = frappe.get_doc({
        "doctype": "AI Chat Message", "session": sdoc.name, "seq": doc.seq + 1, "role": "assistant", "status": "pending", "user": doc.user,
        "run": run.name, "parent_message": doc.name, "provider": sdoc.provider, "model": sdoc.model,
    })
    assistant.insert(ignore_permissions=True)
    run.db_set("message", assistant.name, update_modified=False)
    sdoc.db_set({"last_seq": doc.seq + 1, "message_count": cint(frappe.db.count("AI Chat Message", {"session": sdoc.name})), "last_message_on": now_datetime()}, update_modified=False)
    frappe.db.commit()
    runner.enqueue(assistant.name, doc.user)
    return {"user_message": message_dict(frappe.get_doc("AI Chat Message", doc.name), blocks=[], attachments=_attachments_for([doc.name]).get(doc.name, [])), "assistant_message": message_dict(assistant, blocks=[])}


def _approval_mode(mode):
    """A valid approval mode the policy allows; Auto only while auto-approval is available."""
    if mode not in policy.APPROVAL_MODES:
        frappe.throw(_("Unknown approval mode: {0}").format(mode))
    if mode == "Auto" and not policy.auto_approval_available():
        frappe.throw(_("Auto-approval is turned off on this site."), frappe.PermissionError)
    return mode


@frappe.whitelist(methods=["POST"])
def set_approval_mode(session, mode):
    """Ask (every action waits) or Auto (drafts and edits run at once) for one conversation."""
    sdoc = _get_session(session, "write")
    sdoc.db_set("approval_mode", _approval_mode(mode), update_modified=False)
    return {"session": session_dict(sdoc)}


@frappe.whitelist(methods=["POST"])
def decide_approval(approval, decision, reason=None):
    """approve | reject | approve_always (approve, and switch this conversation to auto mode)."""
    if decision not in ("approve", "reject", "approve_always"):
        frappe.throw(_("decision must be approve or reject"))
    switched = None
    if decision == "approve_always":
        doc = frappe.get_doc("AI Approval Request", approval)
        if policy.requires_human(doc.action_type) or doc.action_type not in policy.AUTO_APPROVE_TYPES:
            frappe.throw(_("This kind of action always needs approval."))
        session = frappe.db.get_value("AI Chat Message", frappe.db.get_value("AI Message Block", {"approval": approval, "block_type": "approval"}, "message"), "session")
        if not session:
            frappe.throw(_("This request is not part of a conversation."))
        switched = set_approval_mode(session, "Auto")["session"]
        decision = "approve"
    try:
        snapshot, message = approvals_mod.decide(approval, decision, reason=reason, user=frappe.session.user)
    except frappe.PermissionError:
        raise
    except Exception as exc:
        # Keep the exception type visible to the client (permission vs. validation).
        from ai_agent.runtime.exceptions import PermissionDenied

        if isinstance(exc, PermissionDenied):
            frappe.throw(str(exc), frappe.PermissionError)
        frappe.throw(str(exc))
    return {"approval": snapshot, "message": message, "session": switched}


@frappe.whitelist(methods=["POST"])
def feedback(message, value=0):
    doc = _get_message(message, "write")
    value = max(-1, min(1, cint(value)))
    doc.db_set("feedback", value, update_modified=False)
    frappe.db.commit()
    return {"ok": True, "feedback": value}


# ---------------------------------------------------------------------------
# helpers for the composer
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_skills(agent=None, company=None):
    _any_chat()
    from ai_agent.runtime.skill_registry import SkillRegistry

    agent = agent or _default_agent()
    skills = SkillRegistry().list_effective_skills(frappe.session.user, agent, company or None) or []
    return {
        "skills": [
            {"name": s.get("name"), "title": _chat_text(s.get("title") or s.get("name")), "description": _chat_text(SKILL_SUMMARIES.get(s.get("name"), ""))}
            for s in skills if not s.get("allow_implicit_invocation")
        ],
        "plugins": list_plugins(),
    }


# What each app skill covers, for people choosing it in the / menu (the skill's own description
# is written for the model). English, shown translated.
SKILL_SUMMARIES = {
    "accounting": "Revenue, expenses, profit, cash and bank balances, receivables and payables",
    "documents": "Create, change, submit or cancel documents, with your approval",
    "inventory": "Stock quantities and values, item movements and slow-moving items",
    "purchasing": "Purchases, suppliers, purchase returns and payables",
    "sales": "Sales, customers, returns and receivables",
    "system": "Setup checks and any data the other skills do not cover",
}


def _chat_text(text):
    """App-shipped names and descriptions are English; shown in the user's language (context "AI Chat")."""
    return _(text, context="AI Chat") if text else text


def list_plugins():
    """The plugins this user may pick from the / menu."""
    from ai_agent.runtime import access

    return [
        {"name": p.name, "title": _chat_text(p.title or p.name), "description": _chat_text(p.description or ""), "icon": p.icon or "", "output": p.output or ""}
        for p in access.allowed_plugins()
    ]


@frappe.whitelist()
def search_documents(txt, doctype=None, limit=10):
    _any_chat()
    txt = (txt or "").strip()
    if len(txt) < 2:
        return {"results": []}
    results = []
    if doctype:
        if not frappe.has_permission(doctype, "read"):
            return {"results": []}
        meta = frappe.get_meta(doctype)
        fields = ["name"] + ([meta.title_field] if meta.title_field and meta.title_field != "name" else [])
        for r in frappe.get_list(doctype, filters=[[doctype, "name", "like", f"%{txt}%"]], fields=fields, limit=min(cint(limit) or 10, 50)):
            results.append({"doctype": doctype, "name": r.name, "title": r.get(meta.title_field) if meta.title_field else r.name})
        return {"results": results}
    from frappe.utils.global_search import search

    for r in search(txt, limit=min(cint(limit) or 10, 50)):
        results.append({"doctype": r.get("doctype"), "name": r.get("name"), "title": (r.get("content") or "")[:80]})
    return {"results": results}


@frappe.whitelist()
def get_artifact(artifact):
    _any_chat()
    doc = frappe.get_doc("AI Artifact", artifact)
    owner = frappe.db.get_value("AI Run", doc.run, "user") if doc.run else None
    assert_owned_access(owner, "AI Artifact", doc.name)
    try:
        content = json.loads(doc.content_json or "{}")
    except ValueError:
        content = {}
    rows = content.get("rows") or []
    return {
        "artifact": doc.name,
        "artifact_type": doc.artifact_type,
        "title": doc.title,
        "summary": doc.summary,
        "row_count": doc.row_count,
        "columns": content.get("columns") or (list(rows[0].keys()) if rows else []),
        "rows": rows,
        "chart": content.get("chart") or (content if doc.artifact_type == "Chart" else None),
        "links": content.get("links") or content.get("documents") or [],
    }


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_memories(agent=None):
    _any_chat()
    from ai_agent.engine import memory as memory_mod

    return {"memories": memory_mod.list_for_user(frappe.session.user, agent=agent or None), "enabled": bool(cint(frappe.db.get_single_value("AI Settings", "enable_memory")))}


@frappe.whitelist(methods=["POST"])
def delete_memory(memory):
    _any_chat()
    doc = frappe.get_doc("AI Memory", memory)
    if doc.user != frappe.session.user:
        frappe.throw(_("Not your memory."), frappe.PermissionError)
    frappe.delete_doc("AI Memory", doc.name, ignore_permissions=True, force=True)
    frappe.db.commit()
    return {"deleted": doc.name}


@frappe.whitelist(methods=["POST"])
def remember_now(session):
    _throttle("model", MODEL_CALLS_PER_MINUTE, MODEL_CALLS_PER_DAY)
    """Extract memories from a session immediately (the user asked to)."""
    doc = _get_session(session, "write")
    from ai_agent.engine import memory as memory_mod

    stored = memory_mod.extract_session(doc.name, force=True)
    return {"stored": stored}


# ---------------------------------------------------------------------------
# MCP App view inside the chat (Desk acts as the MCP Apps host)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def view_html():
    _any_chat()
    from ai_agent.chat.view_host import view_html as build

    html, version = build()
    return {"html": html, "version": version}


@frappe.whitelist(methods=["POST"])
def view_call(session, name, arguments=None):
    _throttle("view", VIEW_CALLS_PER_MINUTE)
    from ai_agent.chat import view_host

    doc = _get_session(session)
    if isinstance(arguments, str):
        arguments = frappe.parse_json(arguments) or {}
    agent = frappe.get_cached_doc("AI Agent", doc.agent)
    return view_host.call(agent, doc.company, name, arguments or {})
