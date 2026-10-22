"""Helpers shared by the customization kinds."""

from __future__ import annotations

import contextlib
import hashlib
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, nowdate

from ai_agent.customization.contract import Check, CheckReport, CustomizationError

HEADER_TAG = "ai_agent:"
HEADER_RE = re.compile(r"^\s*(#|//|--|/\*)\s*ai_agent:.*$")


@contextlib.contextmanager
def as_user(user):
	"""Run as ``user`` (the approver at execution) and restore the session user afterwards."""
	current = frappe.session.user
	if not user or user == current:
		yield
		return
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(current)


def table(doctype):
	"""``tabDocType`` quoted for raw SQL; refuses a name that has no table or could break out of the quotes."""
	if not doctype or "`" in doctype or not frappe.db.table_exists(doctype):
		raise CustomizationError(f"'{doctype}' has no table.")
	return f"`tab{doctype}`"


def column(doctype, fieldname):
	"""``fieldname`` quoted for raw SQL; refuses a column the table does not have."""
	if not fieldname or "`" in fieldname or not frappe.db.has_column(doctype, fieldname):
		raise CustomizationError(f"'{doctype}' has no column '{fieldname}'.")
	return f"`{fieldname}`"


def statement_limited(query, seconds=5):
	"""Run ``query`` under a statement time limit: per statement on MariaDB, per session elsewhere (restored after)."""
	if frappe.db.db_type == "mariadb":
		return frappe.db.sql(f"SET STATEMENT max_statement_time={cint(seconds)} FOR {query}")
	from frappe.database.database import get_query_execution_timeout

	frappe.db.set_execution_timeout(seconds)
	try:
		return frappe.db.sql(query)
	finally:
		with contextlib.suppress(Exception):
			frappe.db.set_execution_timeout(get_query_execution_timeout())


def has_roles(user, roles):
	if user == "Administrator":
		return True
	held = set(frappe.get_roles(user))
	return all(role in held for role in roles)


def role_checks(user, roles, why):
	missing = [r for r in roles if not has_roles(user, (r,))]
	if not missing:
		return []
	return [Check("roles.missing", "critical", _("{0} needs the role {1}: {2}").format(user, ", ".join(missing), why))]


def reject_unknown(args, allowed, where):
	unknown = sorted(set(args or {}) - set(allowed))
	if unknown:
		raise CustomizationError(
			_("Unknown argument(s) for {0}: {1}").format(where, ", ".join(unknown)),
			hints=[_("Allowed: {0}").format(", ".join(sorted(allowed)))],
		)


def require(args, keys, where):
	missing = [k for k in keys if args.get(k) in (None, "", [], {})]
	if missing:
		raise CustomizationError(_("{0} needs: {1}").format(where, ", ".join(missing)))


def customizable_reason(doctype, allow_custom=False):
	"""Why ``doctype`` cannot be customized by the agent, or None."""
	from frappe.model import core_doctypes_list

	from ai_agent.runtime.policy import CODE_AND_ACCESS_DOCTYPES, is_protected_doctype

	if not doctype or not frappe.db.exists("DocType", doctype):
		return _("There is no DocType {0}").format(doctype)
	if doctype in core_doctypes_list:
		return _("{0} is a core DocType and cannot be customized").format(doctype)
	meta = frappe.get_meta(doctype)
	if meta.issingle:
		return _("{0} is a Single (settings) DocType and cannot be customized").format(doctype)
	if meta.custom and not allow_custom:
		return _("{0} is a custom DocType: change it in the DocType itself, not through Customize Form").format(doctype)
	if doctype in CODE_AND_ACCESS_DOCTYPES or is_protected_doctype(doctype):
		return _("{0} controls code, access or system configuration and is protected").format(doctype)
	return None


def header(comment, purpose, requested_by, approved_by=None, approval=None, suffix=""):
	"""Provenance line written into every script the agent writes (D9, WA-FUNCTION style)."""
	purpose = re.sub(r"\s+", " ", str(purpose or "")).strip()[:140] or "-"
	parts = [f"{HEADER_TAG} {purpose}", approval or "APR-pending", requested_by or "-", approved_by or "-", nowdate()]
	return f"{comment} {' | '.join(parts)}{suffix}"


def strip_header(text):
	lines = (text or "").split("\n")
	while lines and HEADER_RE.match(lines[0]):
		lines.pop(0)
	return "\n".join(lines)


def is_ai_made(text):
	return any(HEADER_RE.match(line) for line in (text or "").split("\n")[:3]) or \
		bool(re.search(r"/\*\s*ai_agent:", text or ""))


def header_purpose(text):
	for line in (text or "").split("\n")[:3]:
		if HEADER_RE.match(line):
			body = line.split(HEADER_TAG, 1)[1]
			return body.split("|", 1)[0].strip()
	return ""


def sha256(text):
	return hashlib.sha256((text or "").encode()).hexdigest()


def line_preview(text, limit=2):
	return "\n".join(strip_header(text or "").strip().split("\n")[:limit])


def approval_name(approval=None):
	return approval or frappe.flags.get("ai_customize_approval")


def build_report(checks, impact=None, preview=None, risk=None):
	"""CheckReport with risk raised by warnings/criticals when the kind did not fix it."""
	levels = {c.level for c in checks}
	computed = "high" if "critical" in levels else ("medium" if "warning" in levels else "low")
	order = {"low": 0, "medium": 1, "high": 2}
	risk = risk or "low"
	if order[computed] > order[risk]:
		risk = computed
	return CheckReport(checks=list(checks), risk=risk, impact=list(impact or []), preview=preview or {})


def read_user_count(doctype):
	"""Enabled desk users holding a role that can read ``doctype`` (approximate audience)."""
	roles = {p.role for p in frappe.get_meta(doctype).permissions if not p.permlevel and p.read}
	if not roles:
		return 0
	rows = frappe.db.sql(
		"""select count(distinct hr.parent) from `tabHas Role` hr join `tabUser` u on u.name = hr.parent
		where hr.parenttype = 'User' and hr.role in %(roles)s and u.enabled = 1 and u.user_type = 'System User'""",
		{"roles": tuple(roles)},
	)
	return cint(rows[0][0]) if rows else 0


def capture_messages():
	"""Start collecting frappe.msgprint output; call the returned function to get the texts."""
	if getattr(frappe.local, "message_log", None) is None:
		frappe.local.message_log = []
	start = len(frappe.local.message_log)

	def collect():
		import json

		out = []
		for entry in (frappe.local.message_log or [])[start:]:
			if isinstance(entry, str):
				try:
					entry = json.loads(entry)
				except ValueError:
					entry = {"message": entry}
			message = entry.get("message") if isinstance(entry, dict) else str(entry)
			if message:
				out.append(frappe.utils.strip_html(str(message)))
		del frappe.local.message_log[start:]
		return out

	return collect


DRY_RUN_SAVEPOINT = "ai_customize_dry_run"


@contextlib.contextmanager
def dry_run():
	"""Run Frappe's own insert/save validation and roll it back: a savepoint, with ``frappe.db.commit``
	disabled meanwhile so nothing the controllers do can become permanent."""
	point = f"{DRY_RUN_SAVEPOINT}_{frappe.generate_hash(length=6)}"
	frappe.db.savepoint(point)
	commit = frappe.db.commit
	frappe.db.commit = lambda *args, **kwargs: None
	flags = frappe.flags.get("ai_customize_dry_run")
	frappe.flags.ai_customize_dry_run = True
	try:
		yield
	finally:
		frappe.db.commit = commit
		frappe.flags.ai_customize_dry_run = flags
		try:
			frappe.db.rollback(save_point=point)
		except Exception:
			frappe.db.rollback()  # the error ended the transaction (deadlock, lost connection): nothing survives


def frappe_error(exc, messages=None):
	text = frappe.utils.strip_html(cstr(getattr(exc, "message", None) or exc or ""))
	extra = [m for m in (messages or []) if m and m not in text]
	return "; ".join([t for t in [text.strip()] + extra if t])[:400] or exc.__class__.__name__
