"""Since 2026-11-07 what an MCP connection may do is on its AI MCP record (mcp/profiles.py), not in AI Settings. An
updated site keeps what it allowed:

- every AI MCP record takes the site's former values;
- a user who could connect without a record (the site did not require one, they hold an allowed role) and could
  prepare changes gets a record with those values: without one they would only read. A record is a grant, so
  nobody without an allowed role gets one.

A fresh install has no former values and nothing to do.
"""

import frappe
from frappe.utils import cint

FORMER = {
	"mcp_enable_action_requests": "allow_action_requests",
	"mcp_require_approval": "require_approval",
	"mcp_enable_app_approval": "approval_buttons",
	"customization_allow_mcp": "allow_customization",
	"mcp_max_requests_per_minute": "max_requests_per_minute",
}


def execute():
	# the fields left AI Settings; their values stay in tabSingles
	former = dict(frappe.db.sql(
		"select field, value from `tabSingles` where doctype = 'AI Settings' and field in %(fields)s",
		{"fields": tuple(FORMER)}))
	if not former:
		return
	values = {new: cint(former[old]) for old, new in FORMER.items() if former.get(old) not in (None, "")}
	if values.get("max_requests_per_minute", 1) < 1:
		values.pop("max_requests_per_minute")  # 0 meant the default

	for name in frappe.get_all("AI MCP", pluck="name"):
		frappe.db.set_value("AI MCP", name, values, update_modified=False)

	settings = frappe.get_single("AI Settings")
	if not (cint(settings.enable_mcp) and not cint(settings.get("mcp_require_profile")) and values.get("allow_action_requests")):
		return  # users without a record could not connect, or could only read: nothing to keep
	agent = settings.get("mcp_agent") or "ERP Analyst"
	if not frappe.db.get_value("AI Agent", agent, "enabled"):
		return

	from ai_agent.mcp.setup import allowed_roles

	roles = allowed_roles(settings)
	users = set(frappe.get_all("OAuth Bearer Token", filters={"status": "Active"}, pluck="user", distinct=True))
	users |= set(frappe.get_all("Has Role", filters={"role": "AI MCP User", "parenttype": "User"}, pluck="parent", distinct=True))
	with_record = set(frappe.get_all("AI MCP", pluck="user", distinct=True))
	for user in sorted(users - with_record - {"Guest"}):
		if not frappe.db.get_value("User", user, "enabled") or not set(roles) & set(frappe.get_roles(user)):
			continue
		try:
			frappe.get_doc({"doctype": "AI MCP", "title": "MCP", "user": user, "agent": agent, "enabled": 1, **values}).insert(
				ignore_permissions=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"MCP record for {user} (patch mcp_switches_to_ai_mcp)")
