import frappe
from frappe.model.document import Document


class AIChatSession(Document):
	def has_permission(self, perm):
		if self.flags.ignore_permissions:
			return True
		if self.user == frappe.session.user:
			return True
		if frappe.db.exists(
			"Has Role", {"parent": frappe.session.user, "role": "System Manager"}
		):
			return True
		return super().has_permission(perm)


def get_permission_query_conditions(user=None):
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""
	return f"(`tabAI Chat Session`.`user` = {frappe.db.escape(user)})"
