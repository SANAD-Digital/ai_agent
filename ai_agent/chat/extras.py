"""Around the conversation: skills and plugins, document search, artifacts, memories, the embedded views.

Whitelisted through ai_agent.chat.api (the public path the client calls).
"""

from __future__ import annotations

import json
import frappe
from frappe import _
from frappe.utils import cint
from ai_agent.permissions import assert_owned_access
from ai_agent.chat.common import (
    _any_chat,
    _get_session,
    _default_agent,
    MODEL_CALLS_PER_MINUTE,
    MODEL_CALLS_PER_DAY,
    VIEW_CALLS_PER_MINUTE,
    _throttle,
)



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
