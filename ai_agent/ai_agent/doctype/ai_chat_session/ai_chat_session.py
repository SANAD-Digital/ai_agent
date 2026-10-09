import frappe
from frappe.model.document import Document


class AIChatSession(Document):
	# Access: owner only (ai_agent.permissions, registered in hooks.py).
	def on_trash(self):
		for name in frappe.get_all("AI Chat Message", filters={"session": self.name}, pluck="name"):
			frappe.delete_doc("AI Chat Message", name, ignore_permissions=True, force=True)
