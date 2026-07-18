import frappe
from frappe.model.document import Document


class AIAgent(Document):
    def validate(self):
        if self.is_default:
            existing = frappe.get_all(
                "AI Agent",
                filters={"is_default": 1, "name": ("!=", self.name), "enabled": 1},
                limit=1,
            )
            if existing:
                frappe.throw("Only one default agent can be enabled at a time.")