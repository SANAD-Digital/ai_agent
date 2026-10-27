"""AI MCP: one MCP connection of a user, with the agent and company it works with (mcp/profiles.py picks the
record for a request: the one for its connection first, then the user's general one)."""

import frappe
from frappe import _
from frappe.model.document import Document


class AIMCP(Document):
	def validate(self):
		if not frappe.db.get_value("AI Agent", self.agent, "enabled"):
			frappe.throw(_("Agent {0} is disabled.").format(self.agent))
		if self.company and not frappe.has_permission("Company", "read", self.company, user=self.user):
			frappe.throw(_("{0} cannot read company {1}.").format(self.user, self.company))
		if self.enabled:
			self._one_enabled_record_per_connection()

	def _one_enabled_record_per_connection(self):
		filters = {"user": self.user, "enabled": 1, "name": ("!=", self.name)}
		filters["connection"] = self.connection if self.connection else ("is", "not set")
		other = frappe.db.get_value("AI MCP", filters, "title")
		if other:
			where = _("for this connection") if self.connection else _("for all connections")
			frappe.throw(_("{0} already has an enabled MCP record {1}: {2}").format(self.user, where, other))

	def on_update(self):
		from ai_agent.mcp import profiles

		profiles.clear_cache()

	def on_trash(self):
		from ai_agent.mcp import profiles

		profiles.clear_cache()


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def user_connections(doctype, txt, searchfield, start, page_len, filters):
	"""The OAuth clients this user has connected with (a token issued to them), for the Connection field."""
	frappe.only_for("System Manager")
	user = (filters or {}).get("user")
	if not user:
		return []
	return frappe.db.sql(
		"""select c.name, c.app_name from `tabOAuth Client` c
		where c.name in (select t.client from `tabOAuth Bearer Token` t where t.user = %(user)s)
		and (c.name like %(txt)s or c.app_name like %(txt)s)
		order by c.modified desc limit %(start)s, %(page_len)s""",
		{"user": user, "txt": f"%{txt}%", "start": start, "page_len": page_len},
	)
