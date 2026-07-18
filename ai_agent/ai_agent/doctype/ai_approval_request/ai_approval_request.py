import frappe
from frappe.model.document import Document


class AIApprovalRequest(Document):
    def has_permission(self, perm):
        if self.flags.ignore_permissions:
            return True
        if perm == "read" and self.requested_by == frappe.session.user:
            return True
        if perm in ("write", "create") and frappe.has_role("System Manager"):
            return True
        return super().has_permission(perm)
