"""Standalone view of one chart artifact.

MCP clients cannot draw the agent's charts, so ``artifacts.create_chart``
returns a link to this page. Access follows the owning AI Run's rules.
"""

import json

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=" + frappe.utils.quote(
			frappe.request.full_path
		)
		raise frappe.Redirect

	name = frappe.form_dict.get("artifact")
	artifact = frappe.db.get_value(
		"AI Artifact", {"name": name, "artifact_type": "Chart"}, ["run", "title", "content_json"], as_dict=True
	) if name else None
	if not artifact or not frappe.has_permission("AI Run", "read", doc=artifact.run):
		frappe.throw(_("Chart not found or not accessible."), frappe.PermissionError)

	try:
		spec = json.loads(artifact.content_json or "{}")
	except ValueError:
		spec = {}

	context.no_cache = 1
	context.show_sidebar = False
	context.title = artifact.title or _("Chart view")
	context.chart = spec
