import frappe
from frappe.model.document import Document


class AIMessageBlock(Document):
	# Access follows the parent message (ai_agent.permissions).
	pass


def on_doctype_update():
	# Approval decisions find their card block by approval (engine/approvals.py, chat/api.py).
	frappe.db.add_index("AI Message Block", ["approval"])
