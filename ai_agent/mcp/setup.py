"""One-click MCP setup actions for the AI Settings page (System Manager only).

Each check reports whether one prerequisite of a working MCP connection is in place,
and each fix changes exactly that prerequisite on this site.
"""

import frappe
from frappe import _
from frappe.utils import cint

MCP_ROLE = "AI MCP User"
CHATGPT_ORIGIN = "https://chatgpt.com"
OAUTH_FLAGS = ("show_auth_server_metadata", "show_protected_resource_metadata", "enable_dynamic_client_registration")
DEFAULT_ROLES = "AI MCP User\nSystem Manager"


def _allowed_roles(settings=None):
	settings = settings or frappe.get_single("AI Settings")
	return [r.strip() for r in (settings.mcp_allowed_roles or DEFAULT_ROLES).splitlines() if r.strip()]


def _origins(oauth):
	return [o.strip().rstrip("/") for o in (oauth.allowed_public_client_origins or "").splitlines() if o.strip()]


@frappe.whitelist()
def get_status():
	frappe.only_for("System Manager")
	settings = frappe.get_single("AI Settings")
	oauth = frappe.get_single("OAuth Settings")
	allowed = _allowed_roles(settings)
	role_users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", allowed], "parenttype": "User", "parent": ["not in", ["Administrator", "Guest"]]},
		pluck="parent",
		distinct=True,
	)
	return {
		"checks": {
			"mcp_enabled": bool(cint(settings.enable_mcp)),
			"oauth": all(cint(oauth.get(f)) for f in OAUTH_FLAGS),
			"chatgpt_origin": CHATGPT_ORIGIN in _origins(oauth) or "*" in _origins(oauth),
			"my_role": bool(set(allowed) & set(frappe.get_roles())),
		},
		"agent": settings.mcp_agent or "ERP Analyst",
		"action_requests": bool(cint(settings.mcp_enable_action_requests)),
		"role_users": len(set(role_users)),
		"clients": _connected_clients(),
	}


def _connected_clients():
	tokens = frappe.get_all(
		"OAuth Bearer Token",
		filters={"status": "Active"},
		fields=["client", "user", "modified"],
		order_by="modified desc",
	)
	by_client = {}
	for t in tokens:
		row = by_client.setdefault(t.client, {"client": t.client, "users": set(), "last_used": t.modified})
		row["users"].add(t.user)
	names = dict(frappe.get_all("OAuth Client", filters={"name": ["in", list(by_client)]}, fields=["name", "app_name"], as_list=True)) if by_client else {}
	return [
		{"client": c, "app_name": names.get(c) or c, "users": sorted(r["users"]), "last_used": r["last_used"]}
		for c, r in by_client.items()
	]


@frappe.whitelist()
def fix(item):
	"""Apply one prerequisite: mcp_enabled | oauth | chatgpt_origin | my_role | all."""
	frappe.only_for("System Manager")
	items = ["mcp_enabled", "oauth", "chatgpt_origin", "my_role"] if item == "all" else [item]
	for name in items:
		if name == "mcp_enabled":
			frappe.db.set_single_value("AI Settings", "enable_mcp", 1)
		elif name in ("oauth", "chatgpt_origin"):
			oauth = frappe.get_single("OAuth Settings")
			if name == "oauth":
				for f in OAUTH_FLAGS:
					oauth.set(f, 1)
			elif CHATGPT_ORIGIN not in _origins(oauth):
				oauth.allowed_public_client_origins = "\n".join(_origins(oauth) + [CHATGPT_ORIGIN])
			oauth.save(ignore_permissions=True)
		elif name == "my_role":
			_ensure_role(frappe.session.user)
		else:
			frappe.throw(_("Unknown setup step: {0}").format(name))
	frappe.clear_cache(doctype="AI Settings")
	return get_status()


def _ensure_role(user):
	if MCP_ROLE not in _allowed_roles():
		frappe.throw(_("Add {0} to Allowed MCP Roles first.").format(MCP_ROLE))
	if MCP_ROLE not in frappe.get_roles(user):
		frappe.get_doc("User", user).add_roles(MCP_ROLE)


@frappe.whitelist()
def grant_role(user):
	"""Give one user the MCP access role (their ERP permissions are unchanged)."""
	frappe.only_for("System Manager")
	if not frappe.db.exists("User", {"name": user, "enabled": 1}):
		frappe.throw(_("User {0} not found.").format(user))
	_ensure_role(user)
	return get_status()


@frappe.whitelist()
def revoke_client(client):
	"""Disconnect an app: revoke all of its active tokens; users must sign in again."""
	frappe.only_for("System Manager")
	frappe.db.set_value("OAuth Bearer Token", {"client": client, "status": "Active"}, "status", "Revoked")
	return get_status()
