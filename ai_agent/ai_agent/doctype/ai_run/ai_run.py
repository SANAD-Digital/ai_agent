import frappe
from frappe.model.document import Document


class AIRun(Document):
    def validate(self):
        if not self.user:
            self.user = frappe.session.user
        if not self.run_id:
            self.run_id = frappe.generate_hash(length=16)

    def has_permission(self, perm):
        if self.flags.ignore_permissions:
            return True
        if perm == "read" and self.user == frappe.session.user:
            return True
        if perm == "read" and frappe.db.exists("Has Role", {"parent": frappe.session.user, "role": "System Manager"}):
            return True
        return super().has_permission(perm)
