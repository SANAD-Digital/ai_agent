import frappe
from frappe.model.document import Document

# What the approval's outcome means for the tool call that asked for it.
TOOL_CALL_STATUS = {"Executed": "Executed", "Failed": "Failed", "Rejected": "Denied", "Expired": "Denied"}


class AIApprovalRequest(Document):
    # Access: ai_agent.permissions.has_approval_permission (hooks.py).

    def on_update(self):
        if self.has_value_changed("status"):
            settle_tool_call(self.name, self.run, self.status, self.creation)


def settle_tool_call(approval_name, run, status, created):
    """The request_* tool call stays "Requested" until its approval ends; then it takes the outcome."""
    target = TOOL_CALL_STATUS.get(status)
    if not target or not run:
        return
    row = frappe.db.get_value(
        "AI Tool Call",
        {"run": run, "status": "Requested", "creation": ("<=", created)},
        "name",
        order_by="creation desc",
    )
    if row:
        frappe.db.set_value("AI Tool Call", row, {"status": target, "permission_decision": f"approval {approval_name}: {status}"}, update_modified=False)
