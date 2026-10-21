"""Conversations: the boot payload, the session list and search, create / rename / archive / pin / delete,
export and the approval mode.

Whitelisted through ai_agent.chat.api (the public path the client calls).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from ai_agent.ai_agent.doctype.ai_provider.ai_provider import chat_capable
from ai_agent.engine import runner
from ai_agent.permissions import owner_filters
from ai_agent.runtime import policy
from ai_agent.chat.common import (
    SURFACE,
    USAGE_KEYS,
    session_dict,
    _chat_page,
    _get_session,
    _guess_thinking,
    _default_agent,
    _assert_agent,
    _resolve_provider_model,
    _approval_mode,
)



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
    return {"session": session_dict(doc)}


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
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def archive_session(session, archived=1):
    doc = _get_session(session, "write")
    doc.db_set("status", "Archived" if cint(archived) else "Active")
    if cint(archived):
        # The conversation is over: learn from it now instead of waiting for the hourly job.
        frappe.enqueue("ai_agent.engine.memory.extract_session", queue="short", session_name=doc.name, enqueue_after_commit=True)
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def pin_session(session, pinned=1):
    doc = _get_session(session, "write")
    doc.db_set("pinned", 1 if cint(pinned) else 0)
    return {"session": session_dict(doc)}


@frappe.whitelist(methods=["POST"])
def delete_session(session):
    doc = _get_session(session, "write")
    for m in frappe.get_all("AI Chat Message", filters={"session": doc.name, "status": ("in", ("pending", "streaming"))}, pluck="name"):
        runner.request_cancel(m)
    frappe.delete_doc("AI Chat Session", doc.name, ignore_permissions=True, force=True)
    return {"session": doc.name, "deleted": True}


@frappe.whitelist(methods=["POST"])
def set_approval_mode(session, mode):
    """Ask (every action waits) or Auto (drafts and edits run at once) for one conversation."""
    sdoc = _get_session(session, "write")
    sdoc.db_set("approval_mode", _approval_mode(mode), update_modified=False)
    return {"session": session_dict(sdoc)}
