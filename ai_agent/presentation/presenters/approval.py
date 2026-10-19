"""actions.request_* and customize.request_*: the approval card instead of {"approval_required": true}."""

import frappe

from ai_agent.presentation.registry import register


@register("actions.request_create_draft", "actions.request_bulk_create", "actions.request_mapped_draft",
          "actions.request_update", "actions.request_submit", "actions.request_cancel",
          "actions.request_workflow_action",
          "customize.request_field", "customize.request_properties", "customize.request_client_script",
          "customize.request_server_script", "customize.request_report", "customize.request_undo")
def present_request(tool, arguments, result, ctx):
    from ai_agent.mcp import approvals

    approval_id = result.get("approval_id")
    if not approval_id:
        return None
    approval = frappe.get_doc("AI Approval Request", approval_id)
    envelope = approvals.card_envelope(approval, ctx.user if ctx else frappe.session.user, tool=tool)
    envelope["data"]["approval_required"] = True
    return envelope
