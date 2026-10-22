"""Messages: read, send, stop, retry, edit, attachments, approval decisions from the card, feedback.

Whitelisted through ai_agent.chat.api (the public path the client calls).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from ai_agent.engine import approvals as approvals_mod
from ai_agent.engine import runner
from ai_agent.runtime import policy
from ai_agent.chat.common import (
    MAX_TEXT,
    MAX_FILES,
    USAGE_KEYS,
    MESSAGE_FIELDS,
    session_dict,
    message_dict,
    _blocks_for,
    _attachments_for,
    _chat_page,
    _get_session,
    _get_message,
    _assert_agent,
    MODEL_CALLS_PER_MINUTE,
    MODEL_CALLS_PER_DAY,
    _throttle,
    _resolve_provider_model,
    _run_options,
    _parse_list,
    _approval_mode,
)
from ai_agent.chat.sessions import create_session, set_approval_mode



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
        tail = frappe.cache.hgetall(runner.TAIL_KEY.format(row.name)) or {}
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
        "chat_session": sdoc.name, "provider": sdoc.provider, "model": sdoc.model,
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
    return {"ok": True, "feedback": value}
