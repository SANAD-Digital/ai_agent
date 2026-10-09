import frappe
from frappe.model.document import Document


class AIChatMessage(Document):
	# Access: owner of the session only (ai_agent.permissions, registered in hooks.py).
	def on_trash(self):
		for name in frappe.get_all("AI Message Block", filters={"message": self.name}, pluck="name"):
			frappe.delete_doc("AI Message Block", name, ignore_permissions=True, force=True)
		for name in frappe.get_all("File", filters={"attached_to_doctype": "AI Chat Message", "attached_to_name": self.name}, pluck="name"):
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
