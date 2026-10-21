import frappe
from frappe import _
from frappe.model.document import Document

# Kept by the chat engine (db_set): a user editing their own session through /api/resource may not touch them.
SYSTEM_FIELDS = (
	"user", "surface", "last_seq", "message_count", "input_tokens", "output_tokens", "cache_read_input_tokens",
	"cache_creation_input_tokens", "estimated_cost", "summary", "summary_through_seq", "memory_extracted_seq",
	"root_run", "last_run", "external_key",
)


class AIChatSession(Document):
	# Access: owner only (ai_agent.permissions, registered in hooks.py).

	def validate(self):
		if not self.is_new():
			changed = [f for f in SYSTEM_FIELDS if self.meta.get_field(f) and self.has_value_changed(f)]
			if changed:
				frappe.throw(_("These conversation fields are kept by the system: {0}").format(", ".join(changed)), frappe.PermissionError)
		if self.is_new() or self.has_value_changed("provider") or self.has_value_changed("model"):
			from ai_agent.engine.providers.factory import validate_provider_model

			validate_provider_model(self.provider, self.model)
		if self.company and (self.is_new() or self.has_value_changed("company")) and not frappe.has_permission("Company", "read", self.company):
			frappe.throw(_("No permission to read company {0}.").format(self.company), frappe.PermissionError)

	def on_trash(self):
		for name in frappe.get_all("AI Chat Message", filters={"session": self.name}, pluck="name"):
			frappe.delete_doc("AI Chat Message", name, ignore_permissions=True, force=True)
