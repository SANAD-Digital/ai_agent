from __future__ import annotations

import frappe

from ai_agent.chatbot import engine, settings


@frappe.whitelist()
def get_config():
    """Widget boot config. Never returns secrets."""
    return engine.get_config()


@frappe.whitelist()
def get_messages(conversation: str):
    if not settings.is_enabled() or not engine.can_access():
        return []
    return engine.get_messages(conversation)


@frappe.whitelist()
def send_message(message: str, conversation: str | None = None,
                 context_doctype: str | None = None, context_name: str | None = None):
    return engine.send_message(message, conversation, context=(context_doctype, context_name))


@frappe.whitelist()
def start_message(message: str, conversation: str | None = None,
                  context_doctype: str | None = None, context_name: str | None = None):
    """Begin streaming an assistant reply in the background. Returns a stream_id.

    context_doctype/context_name: the form the user has open (optional).
    """
    if not settings.is_enabled() or not engine.can_access():
        frappe.throw(frappe._("الدردشة غير متاحة."))
    return engine.begin_stream_message(message, conversation, context=(context_doctype, context_name))


@frappe.whitelist()
def get_stream_content(stream_id: str):
    """Return the current accumulated content for a streaming reply."""
    return engine.get_stream_content(stream_id)


@frappe.whitelist()
def start_conversation():
    if not settings.is_enabled() or not engine.can_access():
        frappe.throw(frappe._("الدردشة غير متاحة."))
    return engine.get_or_create_conversation()
