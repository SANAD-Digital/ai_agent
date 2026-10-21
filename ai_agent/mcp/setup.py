"""One-click MCP setup actions for the AI Settings page (System Manager only).

Each check reports whether one prerequisite of a working MCP connection is in place,
and each fix changes exactly that prerequisite on this site.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint

MCP_ROLE = "AI MCP User"
CHATGPT_ORIGIN = "https://chatgpt.com"
OAUTH_FLAGS = ("show_auth_server_metadata", "show_protected_resource_metadata", "enable_dynamic_client_registration")
DEFAULT_ROLES = ("AI MCP User", "System Manager")


def allowed_roles(settings=None):
	"""Roles picked in AI Settings > Allowed Roles (Has Role rows); none picked means the defaults."""
	settings = settings or frappe.get_cached_doc("AI Settings")
	value = settings.get("mcp_allowed_roles") or []
	if isinstance(value, str):  # a site not migrated yet still has the old text field
		roles = [r.strip() for r in re.split(r"[,;\n]+", value) if r.strip()]
	else:
		roles = [row.role for row in value if row.get("role")]
	return roles or list(DEFAULT_ROLES)


def move_allowed_roles_to_table():
	"""Allowed Roles was a text field (one role per line); it is now a multi-select of Has Role rows.

	On migrate, a value still stored the old way is turned into rows once and then removed, so the
	setting carries over without a patch (unknown role names are dropped). A site with no rows gets
	the defaults as rows, so the form shows what actually applies."""
	if not frappe.db.exists("DocType", "AI Settings"):
		return
	old = frappe.db.sql("select value from `tabSingles` where doctype='AI Settings' and field='mcp_allowed_roles'")
	has_rows = frappe.get_all("Has Role", filters={"parenttype": "AI Settings", "parentfield": "mcp_allowed_roles"}, limit=1)
	if not has_rows:
		names = re.split(r"[,;\n]+", str(old[0][0] or "")) if old else []
		roles = [r.strip() for r in names if r.strip() and frappe.db.exists("Role", r.strip())] or list(DEFAULT_ROLES)
		doc = frappe.get_single("AI Settings")
		for role in dict.fromkeys(roles):
			if frappe.db.exists("Role", role):
				doc.append("mcp_allowed_roles", {"role": role})
		doc.flags.ignore_validate = True
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
	if old:
		frappe.db.sql("delete from `tabSingles` where doctype='AI Settings' and field='mcp_allowed_roles'")


def _origins(oauth):
	return [o.strip().rstrip("/") for o in (oauth.allowed_public_client_origins or "").splitlines() if o.strip()]


@frappe.whitelist()
def get_status():
	frappe.only_for("System Manager")
	settings = frappe.get_single("AI Settings")
	oauth = frappe.get_single("OAuth Settings")
	allowed = allowed_roles(settings)
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


@frappe.whitelist(methods=["POST"])
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
	if MCP_ROLE not in allowed_roles(frappe.get_single("AI Settings")):
		frappe.throw(_("Add {0} to Allowed Roles first.").format(MCP_ROLE))
	if MCP_ROLE not in frappe.get_roles(user):
		frappe.get_doc("User", user).add_roles(MCP_ROLE)


@frappe.whitelist(methods=["POST"])
def grant_role(user):
	"""Give one user the MCP access role (their ERP permissions are unchanged)."""
	frappe.only_for("System Manager")
	if not frappe.db.exists("User", {"name": user, "enabled": 1}):
		frappe.throw(_("User {0} not found.").format(user))
	_ensure_role(user)
	return get_status()


@frappe.whitelist(methods=["POST"])
def revoke_client(client):
	"""Disconnect an app: revoke all of its active tokens; users must sign in again."""
	frappe.only_for("System Manager")
	frappe.db.set_value("OAuth Bearer Token", {"client": client, "status": "Active"}, "status", "Revoked")
	return get_status()
