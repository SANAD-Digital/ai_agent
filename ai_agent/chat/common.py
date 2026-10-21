"""Shared pieces of the Desk chat API: limits, record shapes (session/message/block dicts), access checks,
rate limits, provider and agent resolution.

Whitelisted through ai_agent.chat.api (the public path the client calls).
"""

from __future__ import annotations

import json
import frappe
from frappe import _
from ai_agent import roles
from frappe.utils import cint
from ai_agent.engine.errors import is_user_message, kind_of, user_message
from ai_agent.engine.providers.base import guess_context_window
from ai_agent.permissions import assert_owned_access
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


def _guess_thinking(model):
    from ai_agent.engine.providers.base import guess_thinking

    return 1 if guess_thinking(model or "") else 0


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


def _approval_mode(mode):
    """A valid approval mode the policy allows; Auto only while auto-approval is available."""
    if mode not in policy.APPROVAL_MODES:
        frappe.throw(_("Unknown approval mode: {0}").format(mode))
    if mode == "Auto" and not policy.auto_approval_available():
        frappe.throw(_("Auto-approval is turned off on this site."), frappe.PermissionError)
    return mode
