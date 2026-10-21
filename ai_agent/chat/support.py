"""The floating support bubble, on the chat engine.

Same engine, storage and client components as the agent workspace; what
differs is fixed by "AI Chatbot Settings": the agent, the company, the
title, the welcome message, who may use it. Its conversations are sessions
with surface "Chatbot", so they never mix with the workspace's own list
(but any of them can be opened full-size in the workspace).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from ai_agent.chat import api
from ai_agent.chatbot import settings as bot_settings
from ai_agent.permissions import owner_filters

SURFACE = "Chatbot"
RESUME_HOURS = 12          # reopen the last conversation when it is this recent

# Labels are translated per request in bootstrap().
SUGGESTIONS = [
    {"label": "Why does this document not appear in the report?", "needs_document": True},
    {"label": "Explain this screen and what I need to fill in", "needs_document": True},
    {"label": "I got an error message, help me fix it"},
    {"label": "How do I create a sales invoice?"},
]


def _assert_access():
    if not bot_settings.is_enabled() or not bot_settings.can_access():
        frappe.throw(_("Technical support is not available for your account."), frappe.PermissionError)


def _agent():
    name = bot_settings.agent_name()
    if not frappe.db.get_value("AI Agent", name, "enabled"):
        name = frappe.db.get_value("AI Agent", {"is_default": 1, "enabled": 1}, "name") or frappe.db.get_value("AI Agent", {"enabled": 1}, "name")
    if not name:
        frappe.throw(_("No technical support agent is set up."))
    return name


def _latest_session(user):
    row = frappe.get_all(
        "AI Chat Session",
        filters={"user": user, "surface": SURFACE, "status": "Active", "last_seq": (">", 0)},
        fields=["name", "last_message_on"],
        order_by="last_message_on desc",
        limit=1,
    )
    if not row:
        return None
    age = frappe.utils.time_diff_in_hours(now_datetime(), row[0].last_message_on or now_datetime())
    return row[0].name if age <= RESUME_HOURS else None


@frappe.whitelist()
def bootstrap():
    """Everything the bubble needs to draw itself. Never returns secrets."""
    if not bot_settings.is_enabled():
        return {"enabled": False}
    user = frappe.session.user
    if not bot_settings.can_access(user):
        return {"enabled": True, "can_access": False}
    info = frappe.db.get_value("User", user, ["full_name", "user_image", "language"], as_dict=True) or {}
    lang = info.get("language") or frappe.local.lang or "ar"
    agent = _agent()
    session = _latest_session(user)
    from ai_agent.runtime import policy

    return {
        "enabled": True,
        "can_access": True,
        "title": bot_settings.widget_title(),
        "welcome_message": bot_settings.welcome_message(),
        "agent": {"name": agent, "agent_name": _(frappe.db.get_value("AI Agent", agent, "agent_name") or agent, context="AI Agent")},
        "user": {"name": user, "full_name": info.get("full_name") or user, "image": info.get("user_image"), "language": lang, "dir": "rtl" if str(lang).startswith(("ar", "he", "fa", "ur")) else "ltr"},
        "session": api.get_session(session)["session"] if session else None,
        "suggestions": [{**s, "label": _(s["label"])} for s in SUGGESTIONS],
        "third_party_warning": policy.third_party_warning(),
        "auto_approval": policy.auto_approval_available(),
    }


@frappe.whitelist(methods=["POST"])
def send_message(text=None, session=None, files=None, skill=None, plugin=None, context_doctype=None, context_name=None):
    _assert_access()
    if session:
        if frappe.db.get_value("AI Chat Session", session, "surface") != SURFACE:
            frappe.throw(_("This is not a support conversation."), frappe.PermissionError)
    else:
        agent = _agent()
        provider, model = api._resolve_provider_model(bot_settings.provider(), bot_settings.model(), agent)
        company = bot_settings.company()
        title = " ".join((text or "").split())[:80] or _("Support conversation")
        doc = frappe.get_doc({
            "doctype": "AI Chat Session",
            "title": title,
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
        session = doc.name
    if context_doctype and context_name and not frappe.has_permission(context_doctype, "read", context_name):
        context_doctype = context_name = None
    return api.send_message(text=text, session=session, files=files, skill=skill, plugin=plugin, context_doctype=context_doctype, context_name=context_name)


@frappe.whitelist()
def list_skills():
    """The / menu of the bubble: skills of the support agent this user may use, and plugins."""
    _assert_access()
    return api.list_skills(agent=_agent(), company=bot_settings.company() or None)


@frappe.whitelist()
def list_sessions(limit=20):
    _assert_access()
    filters = dict(owner_filters())
    filters.update({"surface": SURFACE, "status": "Active", "last_seq": (">", 0)})
    rows = frappe.get_all(
        "AI Chat Session",
        filters=filters,
        fields=["name", "title", "user", "agent", "company", "provider", "model", "status", "pinned", "last_message_on", "message_count", "last_seq", "modified", *api.USAGE_KEYS],
        order_by="last_message_on desc",
        limit=min(cint(limit) or 20, 50),
    )
    previews = api._previews([r.name for r in rows])
    return {"sessions": [api.session_dict(r, preview=previews.get(r.name, "")) for r in rows]}
