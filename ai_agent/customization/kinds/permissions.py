"""Role permissions on a DocType, the way the Role Permission Manager changes them (docs/customization/01 §3.1 v3).

Frappe's Role Permission Manager (``frappe/core/page/permission_manager/permission_manager.py``) never edits
the standard ``DocPerm`` rows: the first change copies them into ``Custom DocPerm`` (``permissions.setup_custom_perms``
→ ``copy_perms``), and from then on ``Meta.set_custom_permissions`` (``model/meta.py:634``) serves the Custom DocPerm
rows instead of the standard ones. ``reset`` deletes every Custom DocPerm of the DocType (``permissions.reset_perms``),
which brings the standard rows back.

Operations: ``grant`` (set rights on one rule; the rule is added when missing, like ``add`` + ``update``), ``revoke``
(clear rights, or remove the rule, like ``update`` / ``remove``), ``reset`` (back to standard, like ``reset``). The undo
of each is ``restore``: the exact Custom DocPerm rows that existed before (none = standard).

Every resulting rule set goes through Frappe's own ``validate_permissions`` (``core/doctype/doctype/doctype.py:1840``)
before approval, run as the requester. Changing permissions always needs a second person (``four_eyes_required``).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.permissions import ADMIN_ROLE, ALL_USER_ROLE, AUTOMATIC_ROLES, GUEST_ROLE, SYSTEM_USER_ROLE, std_rights
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

RIGHTS = tuple(std_rights)  # frappe/permissions.py:13
ROW_KEYS = ("role", "permlevel", "if_owner") + RIGHTS
TOP_KEYS = ("operation", "doctype", "name", "role", "permlevel", "if_owner", "rights", "rows", "allow_automatic_role",
	"reason", "purpose")
OPERATIONS = ("grant", "revoke", "reset", "restore")
BASIC_RIGHTS = ("select", "read", "write", "submit", "cancel", "create")  # doctype.py:1854 check_atleast_one_set
WRITE_RIGHTS = ("write", "create", "delete", "submit", "cancel", "amend", "import")
RISKY_RIGHTS = ("delete", "cancel", "submit")
# The Role Permission Manager never lists these (permission_manager.py:25).
NOT_IN_PERMISSION_MANAGER = ("DocType", "Patch Log", "Module Def")
# Never touched, whatever the role: who-can-do-what itself, code, the agent, books.
PROTECTED = frozenset({
	"User", "Role", "Has Role", "Role Profile", "Module Profile", "User Permission", "User Type", "User Group",
	"DocPerm", "Custom DocPerm", "DocType", "DocField", "Custom Field", "Property Setter", "Customize Form",
	"Server Script", "Client Script", "Company", "Fiscal Year", "Accounting Period", "Permission Type",
})
MAX_IMPACT_USERS = 500
MAX_IMPACT_LINES = 15


def _row(values):
	"""A comparable rule: role, permlevel, if_owner and every right as 0/1."""
	values = values or {}
	out = {"role": cstr(values.get("role")), "permlevel": cint(values.get("permlevel")), "if_owner": cint(values.get("if_owner"))}
	for right in RIGHTS:
		out[right] = cint(values.get(right))
	return out


def _key(row):
	return (row["permlevel"], row["role"], row["if_owner"])


def _sorted(rows):
	return sorted((_row(r) for r in rows or []), key=_key)


def custom_rows(doctype):
	return _sorted(frappe.get_all("Custom DocPerm", filters={"parent": doctype}, fields=list(ROW_KEYS)))


def standard_rows(doctype):
	return _sorted(frappe.get_all("DocPerm", filters={"parent": doctype}, fields=list(ROW_KEYS)))


def effective_rows(doctype):
	"""What ``Meta.set_custom_permissions`` serves: Custom DocPerm when there is any, else DocPerm."""
	return custom_rows(doctype) or standard_rows(doctype)


def protected_reason(doctype):
	"""Why the agent may never change the permissions of ``doctype``, or None."""
	from frappe.model import core_doctypes_list

	from ai_agent.runtime.policy import CODE_AND_ACCESS_DOCTYPES, is_protected_doctype

	if doctype in PROTECTED or doctype in core_doctypes_list or doctype in NOT_IN_PERMISSION_MANAGER:
		return _("{0} controls access, code or system structure; its permissions cannot be changed by the assistant").format(doctype)
	if doctype.startswith("AI ") or doctype in CODE_AND_ACCESS_DOCTYPES:
		return _("{0} controls code, access or the assistant itself; its permissions cannot be changed by the assistant").format(doctype)
	if is_protected_doctype(doctype):
		return _("{0} is system configuration (settings, company, fiscal year…); its permissions cannot be changed by the assistant").format(doctype)
	return None


def _rights_label(row):
	return [r for r in RIGHTS if row.get(r)]


class PermissionsKind:
	name = "permissions"
	setting = "allow_customize_permissions"
	required_roles = ("System Manager",)
	# Design §3.1: changing permissions needs a second person, whatever customization_require_four_eyes says.
	four_eyes_required = True

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		where = "customize.request_permissions"
		_base.reject_unknown(args, TOP_KEYS, where)
		_base.require(args, ("operation", "doctype"), where)
		operation = cstr(args["operation"]).strip()
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: grant, revoke, reset"))
		doctype = cstr(args["doctype"]).strip()
		if not frappe.db.exists("DocType", doctype):
			raise CustomizationError(_("There is no DocType {0}").format(doctype),
				hints=[_("Find the exact name with schema__search_doctypes")])
		if cint(frappe.db.get_value("DocType", doctype, "istable")):
			raise CustomizationError(_("{0} is a child table: it follows the permissions of its parent DocType").format(doctype),
				hints=[_("Change the permissions of the parent DocType instead")])
		spec = {"operation": operation, "doctype": doctype, "name": doctype}
		purpose = args.get("purpose") or args.get("reason")
		if purpose:
			spec["purpose"] = " ".join(cstr(purpose).split())[:300]

		if operation == "reset":
			for key in ("role", "rights", "rows"):
				if args.get(key) not in (None, "", []):
					raise CustomizationError(_("reset takes only doctype: it brings back the standard permissions"))
			return spec
		if operation == "restore":
			rows = args.get("rows")
			if not isinstance(rows, list):
				raise CustomizationError(_("restore needs rows: the Custom DocPerm rows to bring back ([] = standard)"))
			clean = []
			for i, raw in enumerate(rows, 1):
				if not isinstance(raw, dict):
					raise CustomizationError(_("rows[{0}] must be an object").format(i))
				_base.reject_unknown(raw, ROW_KEYS, f"rows[{i}]")
				clean.append(_row(raw))
			spec["rows"] = _sorted(clean)
			return spec

		_base.require(args, ("role",), where)
		role = cstr(args["role"]).strip()
		if role == ADMIN_ROLE:
			raise CustomizationError(_("Administrator already has every right; choose another role"))
		if not frappe.db.exists("Role", role):
			raise CustomizationError(_("There is no role {0}").format(role),
				hints=[_("Roles are matched exactly, e.g. 'Sales User'; list them with data__query on Role")])
		if role in AUTOMATIC_ROLES and not cint(args.get("allow_automatic_role")):
			raise CustomizationError(
				_("{0} is given automatically to every user (or every visitor); name a specific role").format(role),
				hints=[_("Only if the user explicitly asked for {0}, send allow_automatic_role: 1").format(role)])
		permlevel = cint(args.get("permlevel"))
		if permlevel < 0 or permlevel > 9:
			raise CustomizationError(_("permlevel must be between 0 and 9"))
		if_owner = cint(args.get("if_owner"))
		if if_owner not in (0, 1):
			raise CustomizationError(_("if_owner must be 0 or 1"))
		rights = args.get("rights")
		if isinstance(rights, str):
			rights = [r.strip() for r in rights.replace(",", " ").split() if r.strip()]
		if rights in (None, []):
			if operation == "grant":
				raise CustomizationError(_("grant needs rights, e.g. [\"read\", \"write\"]"), hints=[", ".join(RIGHTS)])
			rights = []
		if not isinstance(rights, list):
			raise CustomizationError(_("rights must be a list of: {0}").format(", ".join(RIGHTS)))
		unknown = [r for r in rights if r not in RIGHTS]
		if unknown:
			raise CustomizationError(_("Unknown right(s): {0}").format(", ".join(map(cstr, unknown))), hints=[", ".join(RIGHTS)])
		spec.update({"role": role, "permlevel": permlevel, "if_owner": if_owner,
			"rights": [r for r in RIGHTS if r in rights]})
		if role in AUTOMATIC_ROLES:
			spec["allow_automatic_role"] = 1
		return spec

	# -- state ------------------------------------------------------------------
	def snapshot(self, spec):
		doctype = spec["doctype"]
		return {"doctype": doctype, "custom_rows": custom_rows(doctype)}

	def after_rows(self, spec, before=None):
		"""The effective rules once the change is applied (in memory)."""
		doctype, operation = spec["doctype"], spec["operation"]
		rows = [dict(r) for r in (before if before is not None else effective_rows(doctype))]
		if operation == "reset":
			return standard_rows(doctype)
		if operation == "restore":
			return _sorted(spec["rows"]) or standard_rows(doctype)
		key = (spec["permlevel"], spec["role"], spec["if_owner"])
		row = next((r for r in rows if _key(r) == key), None)
		if operation == "grant":
			if row is None:
				row = _row({"role": spec["role"], "permlevel": spec["permlevel"], "if_owner": spec["if_owner"]})
				rows.append(row)
			for right in spec["rights"]:
				row[right] = 1
		elif row is not None:
			if spec["rights"]:
				for right in spec["rights"]:
					row[right] = 0
			if not spec["rights"] or not any(row[r] for r in BASIC_RIGHTS):
				rows.remove(row)
		return _sorted(rows)

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		doctype, operation = spec["doctype"], spec["operation"]
		checks = _base.role_checks(user, self.required_roles, _("Frappe's Role Permission Manager is for System Managers only"))
		reason = protected_reason(doctype)
		if reason:
			checks.append(Check("perm.doctype_protected", "critical", reason))
			return _base.build_report(checks, risk="high")
		meta = frappe.get_meta(doctype)
		before = effective_rows(doctype)
		after = self.after_rows(spec, before)
		key = (spec.get("permlevel"), spec.get("role"), spec.get("if_owner"))

		if operation in ("grant", "revoke"):
			checks += self._role_checks(spec)
		if operation == "revoke" and not any(_key(r) == key for r in before):
			checks.append(Check("perm.no_rule", "critical",
				_("{0} has no rule for {1} at level {2}{3}; there is nothing to revoke").format(
					doctype, spec["role"], spec["permlevel"], _(" (only if creator)") if spec["if_owner"] else "")))
		if operation == "revoke" and spec["rights"]:
			row = next((r for r in before if _key(r) == key), None)
			if row is not None and not any(row[r] for r in BASIC_RIGHTS if r not in spec["rights"]):
				checks.append(Check("perm.rule_removed", "info",
					_("No basic right is left on the rule of {0}, so the whole rule is removed").format(spec["role"])))
		current_custom = custom_rows(doctype)
		if operation == "reset" and not current_custom:
			checks.append(Check("perm.no_change", "critical", _("{0} already uses its standard permissions").format(doctype)))
		elif operation == "restore" and current_custom == _sorted(spec["rows"]):
			checks.append(Check("perm.no_change", "critical", _("The permissions of {0} are already these").format(doctype)))
		elif operation in ("reset", "restore") and _base_rows(after) == _base_rows(before):
			checks.append(Check("perm.same_effect", "info", _("Who can do what on {0} stays the same; only the stored rules change").format(doctype)))
		elif _base_rows(after) == _base_rows(before) and not any(c.code == "perm.no_rule" for c in checks):
			checks.append(Check("perm.no_change", "critical", _("The permissions of {0} would not change").format(doctype)))

		if operation == "grant":
			if "report" in spec["rights"] and spec["if_owner"]:
				# permission_manager.py update(): "Cannot set 'Report' permission if 'Only If Creator' permission is set"
				checks.append(Check("perm.report_if_owner", "critical",
					_("Report cannot be given on an 'only if creator' rule (Frappe refuses it)")))
			risky = [r for r in spec["rights"] if r in RISKY_RIGHTS]
			if risky:
				checks.append(Check("perm.risky_right", "warning",
					_("{0} will be able to {1} {2} records").format(spec["role"], ", ".join(risky), doctype)))
			if spec["permlevel"] > 0 and not any(cint(df.permlevel) == spec["permlevel"] for df in meta.fields):
				checks.append(Check("perm.permlevel_unused", "warning",
					_("No field of {0} uses level {1}; this rule changes nothing visible yet").format(doctype, spec["permlevel"])))

		checks += self._coverage_checks(doctype, before, after)
		if operation in ("grant", "revoke"):
			checks += self._frappe_validate(doctype, after, user)

		users = self.user_changes(doctype, before, after)
		impact = self._impact(doctype, users)
		if operation in ("grant", "revoke") and spec["role"] not in AUTOMATIC_ROLES and not self._holders(spec["role"]):
			checks.append(Check("perm.role_unused", "info", _("No enabled user holds {0} yet").format(spec["role"])))
		checks.append(Check("perm.four_eyes", "info", _("A second System Manager must approve a permission change; the requester cannot approve it")))
		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("perm.valid", "ok", _("Frappe accepts the resulting permission rules")))

		preview = {
			"doctype": doctype, "operation": operation, "columns": list(ROW_KEYS),
			"before": before, "after": after, "changes": _row_changes(before, after),
			"users": users[:MAX_IMPACT_LINES * 4], "users_total": len(users),
			"standard": not custom_rows(doctype),
		}
		risk = "high" if operation == "grant" else "medium"
		return _base.build_report(checks, impact=impact, preview=preview, risk=risk)

	def _role_checks(self, spec):
		role, rights, operation = spec["role"], spec["rights"], spec["operation"]
		out = []
		if operation == "grant":
			if role in (GUEST_ROLE, ALL_USER_ROLE):
				out.append(Check("perm.role_automatic", "critical",
					_("Giving rights to {0} opens {1} to every user and website visitor").format(role, spec["doctype"])))
			elif role == SYSTEM_USER_ROLE:
				wide = [r for r in rights if r in WRITE_RIGHTS]
				if wide:
					out.append(Check("perm.role_desk_user", "critical",
						_("Giving {0} to {1} lets every desk user change {2}; give it to a specific role").format(", ".join(wide), role, spec["doctype"])))
				else:
					out.append(Check("perm.role_desk_user", "warning",
						_("Every desk user will be able to {0} {1}").format(", ".join(rights), spec["doctype"])))
		if cint(frappe.db.get_value("Role", role, "disabled")):
			out.append(Check("perm.role_disabled", "warning", _("The role {0} is disabled: the rule has no effect until it is enabled").format(role)))
		return out

	def _coverage_checks(self, doctype, before, after):
		out = []
		if not after:
			# permission_manager.py remove(): "There must be atleast one permission rule."
			out.append(Check("perm.empty", "critical", _("{0} would have no permission rule at all").format(doctype)))
			return out
		for right in ("read", "write"):
			had = any(r[right] and not r["permlevel"] and r["role"] != ADMIN_ROLE for r in before)
			has = any(r[right] and not r["permlevel"] and r["role"] != ADMIN_ROLE for r in after)
			if had and not has:
				out.append(Check(f"perm.last_{right}", "critical",
					_("No role would be left that can {0} {1} (only Administrator)").format(right, doctype)))
		return out

	def _frappe_validate(self, doctype, rows, user):
		"""Frappe's ``validate_permissions`` on the resulting rules, as ``user`` (non-Administrators may not put
		automatic or custom roles on a custom DocType: doctype.py:1925). Run on an unsaved copy: nothing is written."""
		from frappe.core.doctype.doctype.doctype import validate_permissions

		dt = frappe.get_doc("DocType", doctype)
		dt.set("permissions", [])
		docs = []
		for i, row in enumerate(rows, 1):
			child = dt.append("permissions", {**row, "parent": doctype, "parenttype": "DocType", "parentfield": "permissions"})
			child.idx = i
			docs.append((row, child))
		out = []
		collect = _base.capture_messages()
		try:
			with _base.as_user(user):
				validate_permissions(dt)
		except Exception as e:
			messages = collect()
			text = frappe.utils.strip_html(cstr(e)) or "; ".join(messages)
			out.append(Check("perm.frappe", "critical", _("Frappe rejects the rules: {0}").format(text)))
			return out
		for message in collect():
			out.append(Check("perm.frappe", "warning", message))
		for row, child in docs:
			dropped = [r for r in RIGHTS if row[r] and not cint(child.get(r))]
			if dropped:
				# check_level_zero_is_set: create/submit/cancel/amend are cleared above level 0
				out.append(Check("perm.frappe_adjusted", "warning",
					_("Frappe drops {0} from the level {1} rule of {2}").format(", ".join(dropped), row["permlevel"], row["role"])))
		return out

	# -- impact -------------------------------------------------------------------
	@staticmethod
	def _holders(role):
		return frappe.db.sql(
			"""select distinct hr.parent from `tabHas Role` hr join `tabUser` u on u.name = hr.parent
			where hr.parenttype = 'User' and hr.role = %s and u.enabled = 1 and u.name != 'Administrator'
			limit %s""", (role, MAX_IMPACT_USERS), pluck=True)

	def _users_for(self, roles):
		roles = set(roles)
		if roles & {ALL_USER_ROLE, GUEST_ROLE}:
			return frappe.get_all("User", filters={"enabled": 1, "name": ("not in", ("Administrator", "Guest"))},
				pluck="name", limit=MAX_IMPACT_USERS)
		users = []
		if SYSTEM_USER_ROLE in roles:
			users += frappe.get_all("User", filters={"enabled": 1, "user_type": "System User", "name": ("!=", "Administrator")},
				pluck="name", limit=MAX_IMPACT_USERS)
		named = roles - set(AUTOMATIC_ROLES)
		if named:
			users += frappe.db.sql(
				"""select distinct hr.parent from `tabHas Role` hr join `tabUser` u on u.name = hr.parent
				where hr.parenttype = 'User' and hr.role in %(roles)s and u.enabled = 1 and u.name != 'Administrator'
				limit %(limit)s""", {"roles": tuple(named), "limit": MAX_IMPACT_USERS}, pluck=True)
		return list(dict.fromkeys(users))[:MAX_IMPACT_USERS]

	@staticmethod
	def effective_rights(rows, roles):
		"""Rights a user with ``roles`` gets, as ``frappe.permissions.get_role_permissions`` evaluates them:
		level 0 decides the document rights; a right that only comes from an 'only if creator' rule applies to the
		user's own documents (create excepted); higher levels give field read/write as ``read@1`` / ``write@1``."""
		roles = set(roles)
		applicable = [r for r in rows if r["role"] in roles]
		out = set()
		for right in RIGHTS:
			level0 = [r for r in applicable if not r["permlevel"] and r[right]]
			if not level0:
				continue
			if right != "create" and all(r["if_owner"] for r in level0):
				out.add(f"{right} (own)")
			else:
				out.add(right)
		for r in applicable:
			if r["permlevel"]:
				for right in ("read", "write"):
					if r[right]:
						out.add(f"{right}@{r['permlevel']}")
		return out

	def user_changes(self, doctype, before, after):
		roles = {c["role"] for c in _row_changes(before, after)}
		out = []
		for user in self._users_for(roles):
			held = frappe.get_roles(user)
			was, now = self.effective_rights(before, held), self.effective_rights(after, held)
			if was != now:
				out.append({"user": user, "gains": sorted(now - was), "loses": sorted(was - now)})
		return sorted(out, key=lambda u: u["user"])

	@staticmethod
	def _impact(doctype, users):
		lines = []
		for u in users[:MAX_IMPACT_LINES]:
			parts = []
			if u["gains"]:
				parts.append(_("gains {0}").format(", ".join(u["gains"])))
			if u["loses"]:
				parts.append(_("loses {0}").format(", ".join(u["loses"])))
			lines.append(_("{0} {1} on {2}").format(u["user"], _(" and ").join(parts), doctype))
		if len(users) > MAX_IMPACT_LINES:
			lines.append(_("… and {0} more user(s)").format(len(users) - MAX_IMPACT_LINES))
		if not users:
			lines.append(_("No enabled user's rights on {0} change today (the rule applies to whoever gets the role later)").format(doctype))
		return lines

	# -- execute / verify -----------------------------------------------------------
	def execute(self, spec, user, approval=None):
		from frappe.core.doctype.doctype.doctype import clear_permissions_cache
		from frappe.permissions import reset_perms, setup_custom_perms

		doctype, operation = spec["doctype"], spec["operation"]
		before = custom_rows(doctype)
		with _base.as_user(user):
			frappe.only_for("System Manager")  # permission_manager.py: every endpoint starts with it
			if operation == "reset":
				reset_perms(doctype)
			elif operation == "restore":
				reset_perms(doctype)
				for row in spec["rows"]:
					self._insert(doctype, row)
			else:
				setup_custom_perms(doctype)  # first change: copy DocPerm into Custom DocPerm (permissions.py:682)
				key = {"parent": doctype, "role": spec["role"], "permlevel": spec["permlevel"], "if_owner": spec["if_owner"]}
				name = frappe.db.get_value("Custom DocPerm", key)
				if operation == "grant":
					if name:
						doc = frappe.get_doc("Custom DocPerm", name)
						doc.update({r: 1 for r in spec["rights"]})
						doc.save(ignore_permissions=True)
					else:
						self._insert(doctype, {**key, **{r: 1 for r in spec["rights"]}})
				elif name:
					doc = frappe.get_doc("Custom DocPerm", name)
					doc.update({r: 0 for r in spec["rights"]})
					if not spec["rights"] or not any(cint(doc.get(r)) for r in BASIC_RIGHTS):
						frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)
					else:
						doc.save(ignore_permissions=True)
				if not frappe.db.exists("Custom DocPerm", {"parent": doctype}):
					frappe.throw(_("There must be atleast one permission rule."), title=_("Cannot Remove"))
			clear_permissions_cache(doctype)
		return {"targets": [{"doctype": "DocType", "name": doctype}],
			"undo": {"operation": "restore", "doctype": doctype, "name": doctype, "rows": before}}

	@staticmethod
	def _insert(doctype, row):
		doc = frappe.get_doc({"doctype": "Custom DocPerm", "parent": doctype, "parenttype": "DocType",
			"parentfield": "permissions", **_row(row)})
		doc.insert(ignore_permissions=True)
		return doc.name

	def verify(self, spec, result):
		doctype, operation = spec["doctype"], spec["operation"]
		frappe.clear_cache(doctype=doctype)
		actual = _base_rows(effective_rows(doctype))
		served = _base_rows(_sorted([p.as_dict() for p in frappe.get_meta(doctype, cached=False).permissions]))
		checks = []
		if operation == "reset" and custom_rows(doctype):
			checks.append(Check("verify.perm", "critical", _("{0} still has custom permissions").format(doctype)))
		elif operation == "restore" and custom_rows(doctype) != _sorted(spec["rows"]):
			checks.append(Check("verify.perm", "critical", _("The permissions of {0} are not the restored ones").format(doctype)))
		elif operation in ("grant", "revoke"):
			row = next((r for r in custom_rows(doctype) if _key(r) == (spec["permlevel"], spec["role"], spec["if_owner"])), None)
			if operation == "grant" and (row is None or not all(row[r] for r in spec["rights"])):
				checks.append(Check("verify.perm", "critical", _("{0} does not have {1} on {2}").format(spec["role"], ", ".join(spec["rights"]), doctype)))
			if operation == "revoke" and row is not None and (not spec["rights"] or any(row[r] for r in spec["rights"])):
				checks.append(Check("verify.perm", "critical", _("{0} still has the revoked right(s) on {1}").format(spec["role"], doctype)))
		if served != actual:
			checks.append(Check("verify.meta", "warning", _("The cached permissions of {0} differ from the saved ones; clear the cache").format(doctype)))
		if not checks:
			checks.append(Check("verify.perm", "ok", _("The permissions of {0} are in effect").format(doctype)))
		return checks

	def summary(self, spec):
		doctype, operation = spec["doctype"], spec["operation"]
		if operation == "reset":
			return _("Reset permissions of {0} to standard").format(doctype)
		if operation == "restore":
			return _("Restore previous permissions of {0}").format(doctype)
		owner = _(" (only if creator)") if spec.get("if_owner") else ""
		level = _(" at level {0}").format(spec["permlevel"]) if spec.get("permlevel") else ""
		if operation == "grant":
			return _("Give {0} {1} on {2}{3}{4}").format(spec["role"], ", ".join(spec["rights"]), doctype, level, owner)
		what = ", ".join(spec["rights"]) if spec["rights"] else _("every right")
		return _("Take {0} from {1} on {2}{3}{4}").format(what, spec["role"], doctype, level, owner)

	def get_source(self, name, user=None):
		"""customize.get: the rules of DocType ``name`` as the matrix the Role Permission Manager shows."""
		return {"kind": self.name, "doctype": name, "standard": not custom_rows(name), "rows": effective_rows(name)}

	inspect = get_source


def _base_rows(rows):
	return [_row(r) for r in rows]


def _row_changes(before, after):
	"""One line per rule that is added, removed or changed."""
	old = {_key(r): r for r in before}
	new = {_key(r): r for r in after}
	out = []
	for key in sorted(set(old) | set(new)):
		a, b = old.get(key), new.get(key)
		if a == b:
			continue
		was = set(_rights_label(a)) if a else set()
		now = set(_rights_label(b)) if b else set()
		out.append({"role": key[1], "permlevel": key[0], "if_owner": key[2],
			"change": "added" if a is None else ("removed" if b is None else "changed"),
			"gained": [r for r in RIGHTS if r in now - was], "lost": [r for r in RIGHTS if r in was - now]})
	return out


KIND = PermissionsKind()
