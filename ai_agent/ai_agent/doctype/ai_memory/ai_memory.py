import frappe
from frappe import _
from frappe.model.document import Document


class AIMemory(Document):
    def validate(self):
        if self.scope != "Agent" and not self.user:
            frappe.throw(_("User is required unless Scope is Agent."))
        if self.scope == "Agent" and not self.agent:
            frappe.throw(_("Agent is required when Scope is Agent."))
