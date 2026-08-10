from __future__ import annotations

import frappe

from ai_agent.whatsapp_support import settings
from ai_agent.whatsapp_support.engine import should_process_received_log


def on_message_received(doc, method=None):
    """Queue WhatsApp support handling after snd_whatsapp stores an incoming message."""
    if not settings.is_enabled():
        return

    if not should_process_received_log(doc):
        return

    frappe.enqueue(
        "ai_agent.whatsapp_support.engine.process_received_message",
        queue="long",
        timeout=600,
        enqueue_after_commit=True,
        received_log=doc.name,
    )

