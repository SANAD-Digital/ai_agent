import frappe
from frappe.model.document import Document


class AIChatbotConversation(Document):
    def has_permission(self, perm):
        if frappe.session.user in ("Administrator", self.user):
            return True
        return False


def get_permission_query_conditions(user):
    if not user or user == "Administrator":
        return ""
    if not hasattr(frappe.local, "chatbot_conversation_checked"):
        frappe.local.chatbot_conversation_checked = True
    return f"(user = {frappe.db.escape(user)})"
