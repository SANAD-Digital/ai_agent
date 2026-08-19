import frappe
from frappe.model.document import Document


class AIRun(Document):
    def validate(self):
        if not self.user:
            self.user = frappe.session.user
        if not self.run_id:
            self.run_id = frappe.generate_hash(length=16)

    def before_insert(self):
        # Chat-session identity: follow-up runs inherit the chain's root so
        # sessions are queried by one indexed field instead of walking
        # parent_run links.
        if self.parent_run and not self.root_run:
            self.root_run = (
                frappe.db.get_value("AI Run", self.parent_run, "root_run")
                or self.parent_run
            )

    def after_insert(self):
        if not self.root_run:
            self.db_set("root_run", self.name, update_modified=False)

    def has_permission(self, perm):
        if self.flags.ignore_permissions:
            return True
        if perm == "read" and self.user == frappe.session.user:
            return True
        if perm == "read" and frappe.db.exists("Has Role", {"parent": frappe.session.user, "role": "System Manager"}):
            return True
        return super().has_permission(perm)
