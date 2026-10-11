import frappe
from frappe import _
from frappe.model.document import Document


class AISettings(Document):
    def validate(self):
        if self.default_provider and not frappe.db.get_value("AI Provider", self.default_provider, "enabled"):
            frappe.throw(_("The default provider must be enabled."))
        seen = set()
        for row in self.get("access_rules") or []:
            if row.role and row.user:
                frappe.throw(_("Row {0}: choose a role or a user, not both.").format(row.idx))
            key = (row.reference_doctype, row.reference_name, row.role or "", row.user or "")
            if key in seen:
                frappe.throw(_("Row {0} is a duplicate.").format(row.idx))
            seen.add(key)

    def on_update(self):
        from ai_agent.runtime import access

        access.clear_cache()
        # the link and AI Provider.is_default (what the engine reads) always name the same provider
        if self.default_provider and not frappe.db.get_value("AI Provider", self.default_provider, "is_default"):
            frappe.db.sql("update `tabAI Provider` set is_default = (name = %s)", self.default_provider)
            frappe.clear_document_cache("AI Provider", self.default_provider)
