import frappe
from frappe import _
from frappe.model.document import Document


class AIProvider(Document):
	def validate(self):
		if self.provider_type in ("OpenAI Compatible",) and not (self.base_url or "").strip():
			frappe.throw(_("Base URL is required for an OpenAI Compatible provider."))
		if self.is_default and not self.enabled:
			frappe.throw(_("The default provider must be enabled."))
		defaults = [row for row in (self.models or []) if row.is_default]
		if len(defaults) > 1:
			frappe.throw(_("Only one model can be marked as default."))
		if not self.default_model and defaults:
			self.default_model = defaults[0].model_name

	def on_update(self):
		if self.is_default:
			frappe.db.sql(
				"update `tabAI Provider` set is_default = 0 where name != %s and is_default = 1",
				self.name,
			)
			frappe.clear_document_cache("AI Provider", self.name)

	def api_key_preview(self):
		try:
			key = self.get_password("api_key") or ""
		except Exception:
			key = ""
		return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else ("set" if key else "")
