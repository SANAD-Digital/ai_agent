from __future__ import annotations

import re
import json
from typing import Any

import frappe
from frappe.utils import escape_html, now_datetime, strip_html

from ai_agent.runtime.orchestrator import execute_run
from ai_agent.whatsapp_support import settings
from ai_agent.whatsapp_support.setup import ensure_support_skill_grants, support_instructions


RECEIVED_LOG_DOCTYPE = "WhatsApp Webhook Message Received Log"
WHATSAPP_LOG_DOCTYPE = "WhatsApp Log"
REQUEST_DOCTYPE = "AI WhatsApp Request"
SUPPORTED_MESSAGE_TYPES = {"", "text", "conversation", "extendedtextmessage", "extended_text"}
TERMINAL_RUN_STATUSES = {"Completed", "Failed", "Cancelled", "Timed Out"}


def should_process_received_log(doc) -> bool:
    if not doc or getattr(doc, "doctype", None) != RECEIVED_LOG_DOCTYPE:
        return False

    chat_jid = _clean(getattr(doc, "chat_jid", ""))
    if _is_group_chat(chat_jid) and not settings.allow_group_chats():
        return False

    if not _sender_is_allowed(doc):
        return False

    if _already_has_reply(doc.name):
        return False

    body = _clean(getattr(doc, "message_body", ""))
    media_url = _clean(getattr(doc, "media_url", ""))
    if not body and not media_url:
        return False

    message_type = _clean(getattr(doc, "message_type", "")).lower()
    return not message_type or message_type in SUPPORTED_MESSAGE_TYPES or bool(body)


def process_received_message(received_log: str):
    if not settings.is_enabled():
        return {"ok": True, "skipped": True, "reason": "disabled"}

    if not frappe.db.exists(RECEIVED_LOG_DOCTYPE, received_log):
        return {"ok": False, "reason": "received log not found"}

    doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, received_log)
    if not _sender_is_allowed(doc):
        return {"ok": True, "skipped": True, "reason": "sender not allowed"}
    if not should_process_received_log(doc):
        return {"ok": True, "skipped": True, "reason": "not processable"}

    if _already_has_reply(doc.name):
        return {"ok": True, "skipped": True, "reason": "reply already exists"}

    idempotency_key = _idempotency_key(doc)
    existing_run = frappe.db.exists("AI Run", {"idempotency_key": idempotency_key})
    ticket = _get_or_create_support_issue(doc) if settings.create_issue_enabled() else None
    request = _get_or_create_whatsapp_request(doc, ticket)

    try:
        run_name = existing_run or _create_run(doc, ticket, idempotency_key)
        _link_whatsapp_request_run(request, run_name)
        answer = _execute_and_get_answer(run_name, doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WhatsApp AI Support Failed")
        answer = settings.reply_on_error()
        run_name = existing_run or None

    reply_log = _send_reply(doc, answer, run_name=run_name, ticket=ticket)
    _update_whatsapp_request_reply(request, run_name, reply_log, answer)
    return {"ok": True, "run": run_name, "reply_log": reply_log, "ticket": _ticket_name(ticket)}


def retry_failed_reply(received_log: str):
    if not frappe.db.exists(RECEIVED_LOG_DOCTYPE, received_log):
        return {"ok": False, "reason": "received log not found"}

    doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, received_log)
    if not _sender_is_allowed(doc):
        return {"ok": True, "skipped": True, "reason": "sender not allowed"}
    logs = frappe.get_all(
        WHATSAPP_LOG_DOCTYPE,
        filters={"reference_type": RECEIVED_LOG_DOCTYPE, "reference_name": doc.name},
        fields=["name", "message", "client_id", "status", "error_message"],
        order_by="creation desc",
        limit=1,
    )
    if not logs:
        return process_received_message(received_log)

    log = logs[0]
    if log.status == "Send":
        return {"ok": True, "reply_log": log.name, "status": log.status}

    sender_number = _sender_number_for_reply(doc)
    if sender_number:
        frappe.db.set_value(
            WHATSAPP_LOG_DOCTYPE,
            log.name,
            {
                "mobile_no": sender_number,
                "status": "Not Send",
                "error_message": "",
                "priority": 0,
                "send_now": 1,
            },
            update_modified=False,
        )
        frappe.db.commit()

        from snd_whatsapp.snd_whatsapp.doctype.whatsapp_log.whatsapp_log import send

        reply_doc = frappe.get_doc(WHATSAPP_LOG_DOCTYPE, log.name)
        send([reply_doc.as_dict()])
        status = frappe.db.get_value(
            WHATSAPP_LOG_DOCTYPE,
            log.name,
            ["status", "error_message", "mobile_no"],
            as_dict=True,
        )
        return {
            "ok": status.status == "Send",
            "reply_log": log.name,
            "status": status.status,
            "mobile_no": status.mobile_no,
            "error": status.error_message,
        }

    lid_chat_jid = _lid_chat_jid(doc) if not _sender_number_for_reply(doc) else ""
    if not lid_chat_jid:
        return {
            "ok": False,
            "reply_log": log.name,
            "status": log.status,
            "reason": "latest failed reply is not a LID chat",
            "error": log.error_message,
        }

    device = log.client_id or _reply_device(doc)
    result = _send_lid_reply_via_platform(log.name, doc, log.message, device, lid_chat_jid)
    result.update({"reply_log": log.name})
    return result


def send_followup_reply(received_log: str, message: str):
    """Send an additional WhatsApp reply for corrections or follow-up support notes."""
    if not frappe.db.exists(RECEIVED_LOG_DOCTYPE, received_log):
        return {"ok": False, "reason": "received log not found"}

    doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, received_log)
    if not _sender_is_allowed(doc):
        return {"ok": True, "skipped": True, "reason": "sender not allowed"}
    log_name = _send_reply_without_idempotency(doc, message)
    status = frappe.db.get_value(
        WHATSAPP_LOG_DOCTYPE,
        log_name,
        ["status", "send_now", "mobile_no", "error_message"],
        as_dict=True,
    )
    return {
        "ok": bool(status and status.status == "Send"),
        "reply_log": log_name,
        "status": status.status if status else None,
        "send_now": status.send_now if status else None,
        "mobile_no": status.mobile_no if status else None,
        "error": status.error_message if status else None,
    }


def backfill_recent_requests(limit: int = 50):
    """Create AI WhatsApp Request rows for recent received logs without sending replies."""
    if not frappe.db.exists("DocType", REQUEST_DOCTYPE):
        return {"ok": False, "reason": f"{REQUEST_DOCTYPE} is not installed"}

    rows = frappe.get_all(
        RECEIVED_LOG_DOCTYPE,
        fields=["name"],
        order_by="creation desc",
        limit=max(1, min(int(limit or 50), 500)),
    )
    created = []
    for row in rows:
        doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, row.name)
        log = frappe.db.get_value(
            WHATSAPP_LOG_DOCTYPE,
            {"reference_type": RECEIVED_LOG_DOCTYPE, "reference_name": doc.name},
            ["name", "message"],
            as_dict=True,
        )
        request_name = _get_or_create_whatsapp_request(doc, None)
        if request_name and log:
            _update_whatsapp_request_reply(request_name, None, log.name, log.message)
        if request_name:
            created.append(request_name)
    return {"ok": True, "count": len(created), "requests": created[:20]}


def regenerate_reply(received_log: str):
    """Run the agent again for a received message and send an additional corrected reply."""
    if not frappe.db.exists(RECEIVED_LOG_DOCTYPE, received_log):
        return {"ok": False, "reason": "received log not found"}

    doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, received_log)
    if not _sender_is_allowed(doc):
        return {"ok": True, "skipped": True, "reason": "sender not allowed"}
    ticket = _get_or_create_support_issue(doc) if settings.create_issue_enabled() else None
    request = _get_or_create_whatsapp_request(doc, ticket)
    run_name = _create_run(doc, ticket, f"{_idempotency_key(doc)}:retry:{now_datetime()}")
    _link_whatsapp_request_run(request, run_name)
    answer = _execute_and_get_answer(run_name, doc)
    reply_log = _send_reply_without_idempotency(doc, answer)
    _update_whatsapp_request_reply(request, run_name, reply_log, answer)
    return {"ok": True, "run": run_name, "reply_log": reply_log, "answer": answer}


def diagnose_lid_reply(received_log: str):
    if not frappe.db.exists(RECEIVED_LOG_DOCTYPE, received_log):
        return {"ok": False, "reason": "received log not found"}

    doc = frappe.get_doc(RECEIVED_LOG_DOCTYPE, received_log)
    device = _reply_device(doc)
    platform_device = _platform_device_for_local_device(device, doc)
    out = {
        "ok": True,
        "received_log": doc.name,
        "chat_jid": _lid_chat_jid(doc),
        "sender_number": _sender_number_for_reply(doc),
        "recipient": _recipient_for_reply(doc),
        "customer": _find_customer(doc),
        "previous_run": _previous_run_for_chat(doc),
        "local_device": device,
        "platform_device": platform_device,
    }

    try:
        from snd_whatsapp.snd_whatsapp.page.whatsapp_chat.whatsapp_chat import (
            _get_integration_doc,
            _get_platform_api_client,
            _get_selected_device_id,
        )

        integration = _get_integration_doc()
        client = _get_platform_api_client(device)
        out.update(
            {
                "integration_found": bool(integration),
                "integration_base_url": integration.get("base_url") if integration else None,
                "integration_api_prefix": integration.get("api_prefix") if integration else None,
                "integration_access_token_set": bool(integration and integration.get("access_token")),
                "selected_device_id": _get_selected_device_id(device),
                "platform_client_ready": bool(client),
            }
        )
    except Exception as exc:
        out["diagnostic_error"] = str(exc)

    return out


def _create_run(doc, ticket, idempotency_key: str) -> str:
    agent = _ensure_support_agent()
    previous_user = frappe.session.user
    frappe.set_user(settings.support_user())
    try:
        run = frappe.get_doc(
            {
                "doctype": "AI Run",
                "agent": agent,
                "user": frappe.session.user,
                "company": settings.company(),
                "prompt": _build_prompt(doc, ticket),
                "status": "Queued",
                "parent_run": _previous_run_for_chat(doc),
                "idempotency_key": idempotency_key,
                "started_at": now_datetime(),
            }
        )
        run.insert(ignore_permissions=True)
        frappe.db.commit()
        return run.name
    finally:
        frappe.set_user(previous_user)


def _execute_and_get_answer(run_name: str, source_doc=None) -> str:
    run = frappe.get_doc("AI Run", run_name)
    if run.status not in TERMINAL_RUN_STATUSES:
        execute_run(run_name)
        run.reload()

    if run.status == "Completed" and run.final_answer:
        answer = _clean_reply(run.final_answer)
        if _is_unsafe_customer_reply(answer):
            frappe.log_error(
                f"AI Run {run_name} produced an internal/debug answer that was blocked from WhatsApp.",
                "WhatsApp AI Support Unsafe Reply Blocked",
            )
            return _safe_business_fallback_reply(run)
        return answer

    error = run.error_summary or "لم يكتمل تشغيل محرك الذكاء الاصطناعي."
    frappe.log_error(f"AI Run {run_name} ended with status {run.status}: {error}", "WhatsApp AI Support Run")
    return settings.reply_on_error()


def _ensure_support_agent() -> str:
    name = settings.agent_name()
    existing = frappe.db.exists("AI Agent", {"name": name}) or frappe.db.exists("AI Agent", {"agent_name": name})
    if existing:
        if not frappe.db.get_value("AI Agent", existing, "enabled"):
            frappe.db.set_value("AI Agent", existing, "enabled", 1)
        _harden_support_agent(existing)
        return existing

    model = None
    if frappe.db.exists("DocType", "AI Settings"):
        model = frappe.db.get_single_value("AI Settings", "default_model")

    agent = frappe.get_doc(
        {
            "doctype": "AI Agent",
            "agent_name": name,
            "description": "Technical support agent for incoming WhatsApp conversations.",
            "enabled": 1,
            "read_only": 1,
            "allow_actions": 0,
            "require_approval_for_all_writes": 1,
            "reasoning_effort": "high",
            "maximum_steps": 30,
            "maximum_tool_calls": 60,
            "model": model,
            "system_instructions": _support_instructions(),
        }
    )
    agent.insert(ignore_permissions=True)
    ensure_support_skill_grants(agent.name)
    frappe.db.commit()
    return agent.name


def _harden_support_agent(agent_name: str):
    values = {
        "read_only": 1,
        "allow_actions": 0,
        "require_approval_for_all_writes": 1,
        "reasoning_effort": "high",
        "maximum_steps": 30,
        "maximum_tool_calls": 60,
    }
    if not frappe.db.get_value("AI Agent", agent_name, "system_instructions"):
        values["system_instructions"] = _support_instructions()
    frappe.db.set_value("AI Agent", agent_name, values, update_modified=False)
    ensure_support_skill_grants(agent_name)


def _support_instructions() -> str:
    return support_instructions()


def _build_prompt(doc, ticket) -> str:
    message = _clean(doc.message_body)
    if not message and _clean(doc.media_url):
        message = "أرسل العميل مرفقاً أو وسائط بدون نص واضح."
    return message


def _previous_run_for_chat(doc) -> str | None:
    if not frappe.db.exists("DocType", REQUEST_DOCTYPE):
        return None

    filters = {
        "received_log": ["!=", doc.name],
        "ai_run": ["is", "set"],
    }
    chat_jid = _clean(getattr(doc, "chat_jid", ""))
    whatsapp_number = _recipient_for_reply(doc)
    if chat_jid:
        filters["chat_jid"] = chat_jid
    elif whatsapp_number:
        filters["whatsapp_number"] = whatsapp_number
    else:
        return None

    rows = frappe.get_all(
        REQUEST_DOCTYPE,
        filters=filters,
        fields=["ai_run"],
        order_by="creation desc",
        limit=1,
    )
    if rows and rows[0].get("ai_run"):
        return rows[0].ai_run
    return None


def _find_customer(doc) -> dict | None:
    if not frappe.db.exists("DocType", "Customer"):
        return None

    digits = _digits_only(_recipient_for_reply(doc))
    if digits:
        customer = _find_customer_by_direct_phone(digits)
        if customer:
            return customer

        customer = _find_customer_by_contact(digits)
        if customer:
            return customer

    customer = _find_customer_by_conversation_name(doc)
    if customer:
        return customer

    return None

def _find_customer_by_direct_phone(digits: str) -> dict | None:
    meta = frappe.get_meta("Customer")
    phone_fields = [
        field
        for field in ("mobile_no", "phone", "phone_no")
        if meta.has_field(field)
    ]
    for field in phone_fields:
        rows = frappe.get_all(
            "Customer",
            filters={field: ["like", f"%{digits[-9:]}"]},
            fields=["name", "customer_name", "mobile_no"],
            limit=2,
        )
        if len(rows) == 1:
            row = dict(rows[0])
            row["source"] = f"Customer.{field}"
            return row
    return None


def _find_customer_by_contact(digits: str) -> dict | None:
    if not frappe.db.exists("DocType", "Contact"):
        return None

    contacts = []
    for field in ("mobile_no", "phone"):
        if not frappe.get_meta("Contact").has_field(field):
            continue
        contacts.extend(
            frappe.get_all(
                "Contact",
                filters={field: ["like", f"%{digits[-9:]}"]},
                fields=["name", field],
                limit=3,
            )
        )

    contact_names = list(dict.fromkeys([contact.name for contact in contacts if contact.get("name")]))
    if len(contact_names) != 1 or not frappe.db.exists("DocType", "Dynamic Link"):
        return None

    links = frappe.get_all(
        "Dynamic Link",
        filters={
            "parenttype": "Contact",
            "parent": contact_names[0],
            "link_doctype": "Customer",
        },
        fields=["link_name"],
        limit=2,
    )
    if len(links) != 1:
        return None

    customer_name = links[0].link_name
    customer = frappe.db.get_value(
        "Customer",
        customer_name,
        ["name", "customer_name", "mobile_no"],
        as_dict=True,
    )
    if customer:
        customer = dict(customer)
        customer["source"] = f"Contact {contact_names[0]}"
    return customer


def _find_customer_by_conversation_name(doc) -> dict | None:
    if not frappe.db.exists("DocType", "Customer"):
        return None

    messages = _conversation_messages(doc, include_current=True)
    if not messages:
        return None

    customers = frappe.get_all(
        "Customer",
        fields=["name", "customer_name", "mobile_no"],
        limit=500,
    )
    matches = []
    normalized_messages = [_normalize_match_text(message) for message in messages]

    for customer in customers:
        names = [customer.get("name"), customer.get("customer_name")]
        for name in names:
            normalized_name = _normalize_match_text(name)
            if not normalized_name or len(normalized_name) < 2:
                continue
            if any(_contains_customer_name(message, normalized_name) for message in normalized_messages):
                row = dict(customer)
                row["source"] = f"WhatsApp conversation name: {name}"
                row["score"] = len(normalized_name)
                matches.append(row)
                break

    if not matches:
        return None

    matches.sort(key=lambda row: row.get("score") or 0, reverse=True)
    return matches[0]


def _conversation_messages(doc, include_current: bool = False) -> list[str]:
    messages = []
    if include_current:
        current = _clean(getattr(doc, "message_body", ""))
        if current:
            messages.append(current)

    chat_jid = _clean(getattr(doc, "chat_jid", ""))
    if not chat_jid:
        return messages

    rows = frappe.db.get_all(
        RECEIVED_LOG_DOCTYPE,
        filters={"chat_jid": chat_jid, "name": ["!=", doc.name]},
        fields=["message_body"],
        order_by="received_at desc",
        limit=max(settings.max_history_messages(), 80),
    )
    for row in rows:
        body = _clean(row.message_body)
        if body:
            messages.append(body)
    return messages


def _normalize_match_text(value: Any) -> str:
    text = _clean(value).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contains_customer_name(message: str, customer_name: str) -> bool:
    if not message or not customer_name:
        return False
    pattern = rf"(?<![\w]){re.escape(customer_name)}(?![\w])"
    return bool(re.search(pattern, message, flags=re.IGNORECASE))


def _get_or_create_whatsapp_request(doc, ticket=None):
    if not frappe.db.exists("DocType", REQUEST_DOCTYPE):
        return None

    raw = _raw_payload(doc)
    message_id = _clean(raw.get("message_id") or raw.get("event_id"))
    existing = None
    if message_id:
        existing = frappe.db.exists(REQUEST_DOCTYPE, {"message_id": message_id})
    if not existing:
        existing = frappe.db.exists(REQUEST_DOCTYPE, {"received_log": doc.name})

    customer = _find_customer(doc)
    message = _clean(getattr(doc, "message_body", ""))
    values = {
        "status": "Open",
        "request_type": "Other",
        "priority": "Medium",
        "whatsapp_number": _recipient_for_reply(doc),
        "chat_jid": _clean(getattr(doc, "chat_jid", "")),
        "message_id": message_id,
        "received_log": doc.name,
        "customer": customer.get("name") if customer else None,
        "customer_name": customer.get("customer_name") if customer else None,
        "company": settings.company(),
        "issue": _ticket_name(ticket) if ticket and getattr(ticket, "doctype", None) == "Issue" else None,
        "assigned_department": "Support",
        "meeting_requested": 0,
        "subject": _request_subject(message),
        "customer_message": message,
        "requested_item_service": "",
        "quantity": None,
        "first_seen_at": getattr(doc, "received_at", None) or getattr(doc, "creation", None),
        "last_message_at": getattr(doc, "received_at", None) or getattr(doc, "creation", None),
        "source_payload": json.dumps(raw, ensure_ascii=False, indent=2) if raw else "",
    }

    if existing:
        request = frappe.get_doc(REQUEST_DOCTYPE, existing)
        for field, value in values.items():
            if value not in (None, ""):
                request.set(field, value)
        request.save(ignore_permissions=True)
    else:
        request = frappe.get_doc({"doctype": REQUEST_DOCTYPE, **values})
        request.insert(ignore_permissions=True)
    frappe.db.commit()
    return request.name


def _update_whatsapp_request_reply(request_name: str | None, run_name: str | None, reply_log: str | None, answer: str):
    if not request_name or not frappe.db.exists(REQUEST_DOCTYPE, request_name):
        return
    frappe.db.set_value(
        REQUEST_DOCTYPE,
        request_name,
        {
            "ai_run": run_name,
            "whatsapp_log": reply_log,
            "agent_reply": _clean_reply(answer),
            "status": "In Progress",
        },
        update_modified=False,
    )
    frappe.db.commit()


def _link_whatsapp_request_run(request_name: str | None, run_name: str | None):
    if not request_name or not run_name or not frappe.db.exists(REQUEST_DOCTYPE, request_name):
        return
    frappe.db.set_value(REQUEST_DOCTYPE, request_name, "ai_run", run_name, update_modified=False)
    frappe.db.commit()


def _request_subject(text: str) -> str:
    message = _clean(text) or "WhatsApp Request"
    return message[:120]


def _raw_payload(doc) -> dict:
    raw = getattr(doc, "raw_data", None) or getattr(doc, "payload", None) or ""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _get_or_create_support_issue(doc):
    doctype = settings.issue_doctype()
    if not frappe.db.exists("DocType", doctype):
        return None

    try:
        if doctype == "Issue":
            return _get_or_create_standard_issue(doc)
        if doctype == "Support Issue":
            return _get_or_create_snd_support_issue(doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WhatsApp Support Issue Creation Failed")
    return None


def _get_or_create_standard_issue(doc):
    raised_by = _synthetic_email(doc)
    existing = frappe.db.get_all(
        "Issue",
        filters={"raised_by": raised_by, "status": ["not in", ["Resolved", "Closed"]]},
        fields=["name"],
        order_by="modified desc",
        limit=1,
    )
    if existing:
        issue = frappe.get_doc("Issue", existing[0].name)
        _add_issue_comment(issue.doctype, issue.name, doc)
        return issue

    issue = frappe.get_doc(
        {
            "doctype": "Issue",
            "subject": _issue_subject(doc),
            "raised_by": raised_by,
            "status": "Open",
            "description": _issue_description(doc),
        }
    )
    issue.insert(ignore_permissions=True)
    frappe.db.commit()
    return issue


def _get_or_create_snd_support_issue(doc):
    client = _recipient_for_reply(doc) or _clean(doc.chat_jid) or "WhatsApp"
    existing = frappe.db.get_all(
        "Support Issue",
        filters={"client": client, "status": ["not in", ["Closed", "Cancelled"]]},
        fields=["name"],
        order_by="modified desc",
        limit=1,
    )
    if existing:
        issue = frappe.get_doc("Support Issue", existing[0].name)
        _add_issue_comment(issue.doctype, issue.name, doc)
        return issue

    issue = frappe.get_doc(
        {
            "doctype": "Support Issue",
            "subject": _issue_subject(doc),
            "status": "Open",
            "priority": "Medium",
            "issue_type": "Other",
            "description": _issue_description(doc),
            "client": client,
            "origin": "WhatsApp",
            "raised_by": client,
            "reference_issue": doc.name,
        }
    )
    issue.insert(ignore_permissions=True)
    frappe.db.commit()
    return issue


def _add_issue_comment(doctype: str, name: str, doc):
    content = (
        f"WhatsApp message received from {_recipient_for_reply(doc) or _clean(doc.chat_jid)}"
        f"<br><br>{escape_html(_clean(doc.message_body) or _clean(doc.media_url))}"
        f"<br><br>Received Log: {doc.name}"
    )
    frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": doctype,
            "reference_name": name,
            "content": content,
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()


def _send_reply(doc, answer: str, run_name: str | None = None, ticket=None) -> str | None:
    if _already_has_reply(doc.name):
        return frappe.db.get_value(
            WHATSAPP_LOG_DOCTYPE,
            {"reference_type": RECEIVED_LOG_DOCTYPE, "reference_name": doc.name},
            "name",
        )

    return _send_reply_without_idempotency(doc, answer)


def _send_reply_without_idempotency(doc, answer: str) -> str | None:
    if not _sender_is_allowed(doc):
        return None

    recipient = _recipient_for_reply(doc)
    if not recipient:
        frappe.log_error(f"Unable to resolve WhatsApp recipient for {doc.name}", "WhatsApp AI Support")
        return None

    device = _reply_device(doc)
    if not device:
        frappe.log_error("No default WhatsApp Device found for AI support reply.", "WhatsApp AI Support")
        return None

    from snd_whatsapp.snd_whatsapp.doctype.whatsapp_log.whatsapp_log import add_notification_log

    reply_text = _clean_reply(answer)
    sender_number = _sender_number_for_reply(doc)
    lid_chat_jid = "" if sender_number else _lid_chat_jid(doc)
    log_name = add_notification_log(
        reference_type=RECEIVED_LOG_DOCTYPE,
        reference_name=doc.name,
        mobile_no=recipient,
        message=reply_text,
        client_id=device,
        priority=0,
        send_now=True,
        whatsapp_notification=None,
        whatsapp_message_type="Text",
    )
    if lid_chat_jid:
        _send_lid_reply_via_platform(log_name, doc, reply_text, device, lid_chat_jid)
    return log_name


def _send_whatsapp_log_now(log_name: str):
    from snd_whatsapp.snd_whatsapp.doctype.whatsapp_log.whatsapp_log import send

    reply_doc = frappe.get_doc(WHATSAPP_LOG_DOCTYPE, log_name)
    send([reply_doc.as_dict()])
    frappe.db.commit()


def _send_lid_reply_via_platform(log_name: str, doc, message: str, device: str, chat_jid: str):
    """Send LID conversations without changing snd_whatsapp's recipient normalization."""
    try:
        platform_device = _platform_device_for_local_device(device, doc)
        if not platform_device:
            raise ValueError("Unable to resolve WhatsApp platform device for LID reply.")

        result = _send_lid_reply_via_chat_jid_api(device, platform_device, chat_jid, message)
        if isinstance(result, dict) and result.get("ok") is False:
            raise ValueError(result.get("error") or "WhatsApp Platform rejected LID reply.")
        _mark_reply_log_sent(log_name, result)
        return {"ok": True, "status": "Send", "result": result}
    except Exception as first_error:
        try:
            platform_device = _platform_device_for_local_device(device, doc)
            if not platform_device:
                raise ValueError("Unable to resolve WhatsApp platform device for LID reply.")

            result = _send_lid_reply_via_method_api(doc, message, platform_device, chat_jid)
            if isinstance(result, dict) and not result.get("ok"):
                raise ValueError(result.get("error") or "WhatsApp Platform rejected LID reply.")
            _mark_reply_log_sent(log_name, result)
            return {"ok": True, "status": "Send", "result": result}
        except Exception as exc:
            frappe.log_error(frappe.get_traceback(), "WhatsApp AI Support LID Send Failed")
            error = f"{first_error} | Text fallback failed: {exc}"
            frappe.db.set_value(
                WHATSAPP_LOG_DOCTYPE,
                log_name,
                {
                    "status": "Not Send",
                    "error_message": error[:2000],
                    "priority": 2,
                },
                update_modified=False,
            )
            frappe.db.commit()
            return {"ok": False, "status": "Not Send", "error": error}


def _send_lid_reply_via_chat_jid_api(device: str, platform_device: str, chat_jid: str, message: str):
    from snd_whatsapp.snd_whatsapp.page.whatsapp_chat.whatsapp_chat import (
        _get_platform_api_client,
        _get_selected_device_id,
    )

    client = _get_platform_api_client(device)
    device_id = platform_device or _get_selected_device_id(device)
    if not client or not device_id:
        raise ValueError("Unable to initialize WhatsApp chat-jid platform client.")

    return client.send_text_by_chat_jid(device_id, chat_jid, message)


def _send_lid_reply_via_method_api(doc, message: str, platform_device: str, chat_jid: str):
    from snd_whatsapp.snd_whatsapp.page.whatsapp_platform_integration.whatsapp_platform_integration import (
        _call_platform_method,
    )

    return _call_platform_method(
        "snd_whatsapp_platform.snd_whatsapp_platform.api.send_message_api",
        method="POST",
        body={
            "device": platform_device,
            "recipient_type": "Individual",
            "recipient_no": "",
            "group_id": chat_jid,
            "message_type": "Text",
            "message_body": message,
            "source_type": "SANAD ERPNext",
            "source_site": frappe.local.site,
            "source_doctype": RECEIVED_LOG_DOCTYPE,
            "source_docname": doc.name,
        },
    )


def _mark_reply_log_sent(log_name: str, result):
    external_message_id = None
    if isinstance(result, dict):
        external_message_id = (
            result.get("provider_message_id")
            or result.get("message_id")
            or result.get("id")
            or result.get("name")
        )

    frappe.db.set_value(
        WHATSAPP_LOG_DOCTYPE,
        log_name,
        {
            "status": "Send",
            "error_message": "",
            "priority": 0,
            "external_message_id": external_message_id,
        },
        update_modified=False,
    )
    frappe.db.commit()


def _platform_device_for_local_device(device: str, doc) -> str | None:
    if device and frappe.db.exists("WhatsApp Device", device):
        platform_device = frappe.db.get_value("WhatsApp Device", device, "sanad_selected_device_id")
        if platform_device:
            return platform_device
    return _clean(getattr(doc, "platform_device", "")) or None


def _reply_device(doc) -> str | None:
    configured_device = settings.whatsapp_device()
    if configured_device and frappe.db.exists("WhatsApp Device", configured_device):
        return configured_device

    device = _clean(getattr(doc, "device", ""))
    if device and frappe.db.exists("WhatsApp Device", device):
        return device
    return frappe.db.get_value("WhatsApp Device", {"default": 1}, "name")


def _lid_chat_jid(doc) -> str:
    for value in (
        _clean(getattr(doc, "chat_jid", "")),
        _clean(getattr(doc, "from_jid", "")),
        _clean(getattr(doc, "sender_jid", "")),
    ):
        if value.endswith("@lid") or "@lid:" in value:
            return value
    return ""


def _sender_number_for_reply(doc) -> str:
    data = _raw_data(doc)
    for key in ("sender_number", "senderNumber", "sender_phone", "senderPhone", "resolved_phone", "resolvedPhone"):
        digits = _digits_only(data.get(key))
        if 10 <= len(digits) <= 15:
            return digits
    return ""


def _sender_is_allowed(doc) -> bool:
    allowed_numbers = settings.allowed_phone_numbers()
    if not allowed_numbers:
        return True

    sender_number = _sender_number_for_reply(doc)
    if not sender_number:
        for value in (
            _clean(getattr(doc, "chat_jid", "")),
            _clean(getattr(doc, "from_jid", "")),
            _clean(getattr(doc, "sender_jid", "")),
        ):
            if any(marker in value.lower() for marker in ("@lid", "@g.us", "@broadcast")):
                continue
            digits = _digits_only(value)
            if 7 <= len(digits) <= 15:
                sender_number = digits
                break

    return sender_number in allowed_numbers


def _already_has_reply(received_log: str) -> bool:
    return bool(
        frappe.db.exists(
            WHATSAPP_LOG_DOCTYPE,
            {"reference_type": RECEIVED_LOG_DOCTYPE, "reference_name": received_log},
        )
    )


def _issue_subject(doc) -> str:
    body = strip_html(_clean(doc.message_body)).replace("\n", " ")
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        body = "طلب دعم عبر واتساب"
    return f"WhatsApp Support - {body[:80]}"


def _issue_description(doc) -> str:
    body = escape_html(_clean(doc.message_body) or _clean(doc.media_url))
    return (
        f"<p><b>WhatsApp received log:</b> {doc.name}</p>"
        f"<p><b>From:</b> {_recipient_for_reply(doc) or _clean(doc.chat_jid)}</p>"
        f"<p><b>Message:</b></p><p>{body}</p>"
    )


def _synthetic_email(doc) -> str:
    recipient = _recipient_for_reply(doc) or _digits_only(_clean(doc.chat_jid)) or "unknown"
    return f"whatsapp-{recipient}@example.invalid"


def _idempotency_key(doc) -> str:
    message_id = _clean(getattr(doc, "message_id", ""))
    return f"whatsapp-support:{message_id or doc.name}"


def _recipient_for_reply(doc) -> str:
    chat_jid = _clean(getattr(doc, "chat_jid", ""))
    if _is_group_chat(chat_jid):
        return chat_jid

    sender_number = _sender_number_for_reply(doc)
    if sender_number:
        return sender_number

    for value in (chat_jid, _clean(getattr(doc, "from_jid", "")), _clean(getattr(doc, "sender_jid", ""))):
        digits = _digits_only(value)
        if 10 <= len(digits) <= 15:
            return digits
    return ""


def _is_group_chat(value: str) -> bool:
    return _clean(value).endswith("@g.us")


def _digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _raw_data(doc) -> dict:
    try:
        data = frappe.parse_json(_clean(getattr(doc, "raw_data", "")) or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_reply(answer: str) -> str:
    text = strip_html(str(answer or "")).strip()
    text = _format_for_whatsapp(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 900:
        text = text[:900].rstrip() + "\n\nسيتابع فريق الدعم بقية التفاصيل معك."
    return text or settings.reply_on_error()


def _format_for_whatsapp(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "- ", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _is_unsafe_customer_reply(text: str) -> bool:
    lowered = _clean(text).lower()
    if not lowered:
        return True

    unsafe_markers = (
        "reached step limit",
        "step limit",
        "produced a final answer",
        "technical reason",
        "السبب التقني",
        "الأدوات المنفذة",
        "data.query",
        "schema.describe",
        "reports.search",
        "tool calls",
        "traceback",
        "exception",
        "error_summary",
        "final_answer",
    )
    if any(marker in lowered for marker in unsafe_markers):
        return True

    json_like_lines = sum(1 for line in text.splitlines() if "{" in line and "}" in line and ":" in line)
    if json_like_lines >= 2:
        return True

    return False


def _safe_business_fallback_reply(run) -> str:
    return settings.reply_on_error()


def _ticket_name(ticket) -> str | None:
    return getattr(ticket, "name", None) if ticket else None
