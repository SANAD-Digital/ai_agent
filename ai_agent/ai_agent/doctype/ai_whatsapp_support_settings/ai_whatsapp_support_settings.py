import frappe
from frappe.model.document import Document


class AIWhatsAppSupportSettings(Document):
    def validate(self):
        from ai_agent.whatsapp_support.settings import support_user_problem

        if self.enabled or self.support_user:
            # Enabled needs a safe user; a set user must be safe even while disabled.
            problem = support_user_problem(self.support_user)
            if problem:
                frappe.throw(problem, title="Unsafe WhatsApp support user")
