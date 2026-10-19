"""Query Report SQL checks with sqlglot (MySQL dialect) — docs/customization/01 §3.3.5.

``analyze(query, filters, user, roles) -> (list[Check], tables)``: read-only single statement, no
dangerous functions, only ``tab*`` tables of existing DocTypes the user may read, no system/salary
tables, ``%(x)s`` placeholders exactly matching declared filters, and a warning for every role that
would see rows through the report that it cannot read in the list (Query Reports skip row
permissions: ``Report.execute_query_report`` -> ``frappe.db.sql``).

``preview(query, values, doctype)`` runs the query once under a 5-second statement limit, 5 rows,
masked. ``explain(query, values)`` flags full scans of large tables.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import get_first_day, nowdate

from ai_agent.customization.contract import Check

MAX_CHARS = 20000
PREVIEW_ROWS = 5
STATEMENT_SECONDS = 5
LARGE_TABLE_ROWS = 100000

PLACEHOLDER_RE = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
BARE_PERCENT_RE = re.compile(r"%(?!\(|%)")
INTO_FILE_RE = re.compile(r"\binto\s+(outfile|dumpfile)\b|\binto\s+@", re.I)
DANGEROUS_FUNCTIONS = {"sleep", "benchmark", "load_file", "get_lock", "release_lock", "release_all_locks",
	"is_free_lock", "is_used_lock", "sys_exec", "sys_eval", "master_pos_wait", "source_pos_wait"}
DANGEROUS_FN_RE = re.compile(r"\b(" + "|".join(sorted(DANGEROUS_FUNCTIONS)) + r")\s*\(", re.I)
# System tables a report may never read.
BLOCKED_TABLES = {"__Auth", "__UserSettings", "__global_search", "tabSessions", "tabToken Cache",
	"tabUser Social Login", "tabAccess Log", "tabActivity Log", "tabOAuth Bearer Token",
	"tabOAuth Authorization Code", "tabOAuth Client", "tabOAuth Provider Settings", "tabEmail Account",
	"tabAI Settings", "tabAI Provider", "tabServer Script", "tabUser Permission"}


def placeholders(query):
	return list(dict.fromkeys(PLACEHOLDER_RE.findall(query or "")))


def _parse(query):
	import sqlglot

	text = PLACEHOLDER_RE.sub(lambda m: f":{m.group(1)}", query).replace("%%", "%")
	return sqlglot.parse(text, read="mysql")


def _filter_names(filters):
	return [f.get("fieldname") for f in filters or [] if isinstance(f, dict) and f.get("fieldname")]


def _doctype_of(table):
	return table[3:] if table.startswith("tab") else None


def parent_doctypes(child):
	return frappe.get_all("DocField", filters={"options": child, "fieldtype": ("in", ("Table", "Table MultiSelect"))},
		pluck="parent", distinct=True) + frappe.get_all("Custom Field",
		filters={"options": child, "fieldtype": ("in", ("Table", "Table MultiSelect"))}, pluck="dt", distinct=True)


def can_read(doctype, user):
	"""The user may read ``doctype``; a child table through any parent that holds it."""
	meta = frappe.get_meta(doctype)
	if meta.istable:
		return any(frappe.has_permission(p, "read", user=user) for p in parent_doctypes(doctype))
	return bool(frappe.has_permission(doctype, "read", user=user))


def read_roles(doctype):
	"""Roles with plain (not if_owner) level-0 read on ``doctype`` (a child: on any parent)."""
	meta = frappe.get_meta(doctype)
	doctypes = parent_doctypes(doctype) if meta.istable else [doctype]
	plain, owner_only = set(), set()
	for dt in doctypes:
		for perm in frappe.get_meta(dt).permissions:
			if perm.permlevel or not perm.read:
				continue
			(owner_only if perm.if_owner else plain).add(perm.role)
	return plain, owner_only - plain


def default_roles(tables):
	"""Our default (stricter than Frappe's): roles that can read every table the query reads."""
	common = None
	for doctype in tables:
		plain, _owner = read_roles(doctype)
		common = plain if common is None else common & plain
	return sorted(common or [])


def analyze(query, filters=None, user=None, roles=None):
	"""Checks for a Query Report query. Returns ``(checks, doctypes_read)``."""
	from sqlglot import exp

	user = user or frappe.session.user
	query = (query or "").strip().rstrip(";").strip()
	checks: list[Check] = []
	if not query:
		return [Check("sql.empty", "critical", _("The query is empty"))], []
	if len(query) > MAX_CHARS:
		return [Check("sql.size", "critical", _("The query is longer than {0} characters").format(MAX_CHARS))], []

	if INTO_FILE_RE.search(query):
		checks.append(Check("sql.into", "critical", _("SELECT ... INTO (file or variable) is not allowed")))
	for match in DANGEROUS_FN_RE.finditer(query):
		checks.append(Check("sql.function", "critical", _("The function {0}() is not allowed in a report").format(match.group(1).upper())))
	if BARE_PERCENT_RE.search(PLACEHOLDER_RE.sub("", query).replace("%%", "")):
		checks.append(Check("sql.percent", "critical", _("A literal % must be written %% (the query runs with filter values): LIKE '%%abc%%'")))
	if checks:
		return checks, []

	try:
		statements = [s for s in _parse(query) if s is not None]
	except Exception as e:
		return [Check("sql.syntax", "critical", _("The query could not be parsed: {0}").format(str(e).splitlines()[0][:200]))], []
	if len(statements) != 1:
		return [Check("sql.statements", "critical", _("Exactly one SELECT statement is allowed (found {0})").format(len(statements)))], []
	root = statements[0]
	if not isinstance(root, exp.Select | exp.Union | exp.Intersect | exp.Except):
		return [Check("sql.read_only", "critical", _("Only a read-only SELECT is allowed (found {0})").format(root.key.upper()))], []
	writes = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge,
		exp.TruncateTable, exp.Set)
	for node in root.walk():
		if isinstance(node, writes):
			checks.append(Check("sql.read_only", "critical", _("The query contains a write ({0})").format(node.key.upper())))
			break
	for node in root.find_all(exp.Select):
		if node.args.get("locks"):
			checks.append(Check("sql.lock", "critical", _("FOR UPDATE / LOCK IN SHARE MODE is not allowed")))
			break
	for node in root.find_all(exp.Func):
		name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
		if name in DANGEROUS_FUNCTIONS:
			checks.append(Check("sql.function", "critical", _("The function {0}() is not allowed in a report").format(name.upper())))

	cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
	tables = []
	for table in root.find_all(exp.Table):
		name = table.name
		if table.args.get("db") or table.args.get("catalog"):
			checks.append(Check("sql.table_system", "critical", _("Reading {0} is not allowed: only this site's tab… tables").format(table.sql(dialect="mysql"))))
			continue
		if name in cte_names or not name:
			continue
		if name in BLOCKED_TABLES or name.startswith(("__", "tabOAuth")):
			checks.append(Check("sql.table_blocked", "critical", _("The system table {0} may not be read by a report").format(name)))
			continue
		doctype = _doctype_of(name)
		if not doctype:
			checks.append(Check("sql.table_system", "critical", _("{0} is not a DocType table (tabName)").format(name)))
			continue
		if doctype not in tables:
			tables.append(doctype)

	from ai_agent.runtime import masking

	salary = masking.salary_doctypes()
	for doctype in tables:
		if not frappe.db.exists("DocType", doctype):
			checks.append(Check("sql.table_missing", "critical", _("There is no DocType {0} (table `tab{0}`)").format(doctype)))
			continue
		if doctype in salary:
			checks.append(Check("sql.table_blocked", "critical", _("{0} holds pay data, which reports built by the agent may not read").format(doctype)))
			continue
		from ai_agent.customization.analyzers.python_code import protected_doctype

		if protected_doctype(doctype):
			checks.append(Check("sql.table_blocked", "critical", _("{0} controls code or access and may not be read by a report").format(doctype)))
			continue
		if not can_read(doctype, user):
			checks.append(Check("sql.table_permission", "critical", _("{0} cannot read {1}, so cannot build a report on it").format(user, doctype)))

	checks.extend(_placeholder_checks(query, filters))
	if roles:
		checks.extend(_role_widening(tables, roles))
	checks.extend(_user_permission_checks(tables))

	# A single table with plain filters is a Report Builder report (row permissions apply).
	if len(tables) == 1 and not root.find(exp.Join) and not root.find(exp.Group) and not root.find(exp.AggFunc) \
		and isinstance(root, exp.Select) and not cte_names:
		checks.append(Check("sql.builder_possible", "info", _("This can be a Report Builder report, which applies row permissions")))

	if not any(c.level == "critical" for c in checks):
		checks.insert(0, Check("sql.read_only", "ok", _("One read-only SELECT on {0}").format(", ".join(tables) or "-")))
	return checks, tables


def _placeholder_checks(query, filters):
	out = []
	used = set(placeholders(query))
	declared = _filter_names(filters)
	for name in sorted(used - set(declared)):
		out.append(Check("sql.placeholder_missing", "critical",
			_("The query uses %({0})s but no filter is named {0} (the report would fail with KeyError)").format(name)))
	for name in declared:
		if name not in used:
			out.append(Check("sql.filter_unused", "warning", _("The filter {0} is not used in the query").format(name)))
	for f in filters or []:
		if f.get("fieldname") in used and not f.get("mandatory") and f.get("default") in (None, ""):
			# The report screen leaves empty filters out of the values, so %(x)s raises KeyError, ifnull() included.
			out.append(Check("sql.filter_optional", "critical",
				_("The filter {0} is used in the query but is optional with no default: when it is left empty the report fails with KeyError (ifnull does not help). Make it mandatory or give it a default").format(f.get("fieldname"))))
	return out


def _role_widening(tables, roles):
	out = []
	for role in roles:
		if role in ("System Manager", "Administrator"):
			continue
		for doctype in tables:
			if not frappe.db.exists("DocType", doctype):
				continue
			plain, owner_only = read_roles(doctype)
			if role in plain:
				continue
			if role in owner_only:
				out.append(Check("sql.role_widening", "warning",
					_("Role {0} sees only its own {1} in the list but every {1} through this report").format(role, doctype)))
			else:
				out.append(Check("sql.role_widening", "warning",
					_("Role {0} cannot read {1} but would see its rows through this report").format(role, doctype)))
	return out


def _user_permission_checks(tables):
	out = []
	for doctype in tables:
		if frappe.db.exists("User Permission", {"allow": doctype}):
			out.append(Check("sql.user_permissions", "warning",
				_("User Permissions restrict {0} for some users; a Query Report does not apply them").format(doctype)))
	return out


# -- running ------------------------------------------------------------------


def sample_values(filters, given=None):
	"""Values for a preview run: given ones, then defaults, then sensible guesses by type."""
	given = dict(given or {})
	values = {}
	for f in filters or []:
		name = f.get("fieldname")
		if not name:
			continue
		if name in given:
			values[name] = given[name]
			continue
		if f.get("default") not in (None, ""):
			values[name] = f.get("default")
			continue
		fieldtype, options = f.get("fieldtype"), f.get("options")
		if fieldtype == "Date":
			values[name] = str(get_first_day(nowdate())) if "from" in name or "start" in name else nowdate()
		elif fieldtype == "Link" and options == "Company":
			values[name] = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company") or ""
		elif fieldtype == "Link" and options == "Fiscal Year":
			values[name] = frappe.defaults.get_user_default("fiscal_year") or ""
		elif fieldtype in ("Int", "Float", "Currency", "Check"):
			values[name] = 0
		else:
			values[name] = ""
	for name, value in given.items():
		values.setdefault(name, value)
	return values


def preview(query, values, doctype=None):
	"""First rows of the query (5-second limit), masked. Raises on a database error."""
	from ai_agent.runtime import masking

	query = (query or "").strip().rstrip(";")
	sql = f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR SELECT * FROM ({query}\n) _q LIMIT {PREVIEW_ROWS}"
	rows = frappe.db.sql(sql, values or {}, as_dict=True)
	columns = [d[0] for d in (frappe.db.get_description() or [])]
	return {"columns": columns, "rows": json_safe(masking.mask([dict(r) for r in rows], doctype))}


def json_safe(value):
	"""Dates, decimals and the like as JSON values (the preview is stored in the approval payload)."""
	import json

	return json.loads(frappe.as_json(value))


def explain(query, values):
	"""Warnings for full scans of tables larger than LARGE_TABLE_ROWS."""
	out = []
	try:
		plan = frappe.db.sql(f"EXPLAIN {query.strip().rstrip(';')}", values or {}, as_dict=True)
	except Exception:
		return out
	for row in plan:
		if str(row.get("type")).upper() == "ALL" and (row.get("rows") or 0) > LARGE_TABLE_ROWS:
			out.append(Check("sql.full_scan", "warning",
				_("Reads all ~{0} rows of {1}; add a date filter").format(row.get("rows"), row.get("table"))))
	return out
