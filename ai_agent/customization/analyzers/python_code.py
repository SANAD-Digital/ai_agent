"""Static analysis of Server Script code (docs/customization/01 §3.3.4).

``analyze(src, script_type, reference_doctype=None) -> list[Check]`` is pure (no database).
``dry_run(script, reference_doctype, doctype_event, user)`` runs a ``Before *`` script once on a copy
of a document the user can read, inside a savepoint that is rolled back, with every write, network,
mail and queue function replaced by a stub that raises.

Frappe only *warns* on RestrictedPython compile errors; here they are critical. ``safe_exec`` is not a
data-security boundary (exec mode exposes full get_doc/get_all/sendmail/make_post_request), so the
AST rules below are the defence.
"""

from __future__ import annotations

import ast
import re

import frappe
from frappe import _

from ai_agent.customization.contract import Check

MAX_CHARS = 20000
WARN_LINES = 400

# Called (or even referenced) anywhere: critical.
NETWORK = {
	"frappe.make_get_request", "frappe.make_post_request", "frappe.make_put_request",
	"frappe.make_patch_request", "frappe.make_delete_request", "FrappeClient", "frappe.integrations",
}
OUTBOUND = {
	"frappe.sendmail", "frappe.enqueue", "frappe.call", "run_script", "frappe.get_print",
	"frappe.attach_print", "frappe.publish_realtime", "frappe.render_template",
}
DESTRUCTIVE = {"frappe.delete_doc", "frappe.rename_doc", "frappe.db.add_index"}
TRANSACTION = {"frappe.db.commit", "frappe.db.rollback", "frappe.db.before_commit", "frappe.db.after_commit",
	"frappe.db.before_rollback", "frappe.db.after_rollback"}
IDENTITY = {"frappe.set_user", "frappe.local", "frappe.db.sql", "frappe.qb", "frappe.db.escape"}
DYNAMIC = {"getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec", "compile",
	"__import__", "open", "__builtins__"}

# Reads that skip permission checks in safe_exec exec mode.
UNCHECKED_READS = {"frappe.get_all", "frappe.db.get_all", "frappe.db.get_value", "frappe.get_doc",
	"frappe.get_cached_doc", "frappe.get_last_doc", "frappe.get_cached_value", "frappe.db.get_single_value"}
# Calls whose first argument (or {"doctype": ...}) names the DocType they touch.
DOCTYPE_CALLS = UNCHECKED_READS | {"frappe.new_doc", "frappe.get_list", "frappe.db.get_list", "frappe.db.set_value",
	"frappe.db.exists", "frappe.db.count", "frappe.copy_doc", "frappe.get_mapped_doc"}
# Not in get_safe_globals() on Frappe v16: a script calling them fails at run time.
MISSING_IN_SAFE_EXEC = {"frappe.has_permission", "frappe.get_roles"}
# Reads a later doc.check_permission(...) covers (the loaded document is checked).
DOC_READS = {"frappe.get_doc", "frappe.get_cached_doc", "frappe.get_last_doc"}
WRITE_METHODS = {"save", "insert", "submit", "cancel", "delete", "db_set", "db_update", "db_insert", "run_method"}

CODE_PROTECTED = frozenset({
	"User", "Has Role", "Role", "Role Profile", "User Permission", "DocPerm", "Custom DocPerm", "Server Script",
	"Client Script", "Webhook", "Email Account", "AI Settings", "Company", "Fiscal Year", "Accounting Period",
})

HTML_SCRIPT_RE = re.compile(r"<\s*script|<\s*iframe|\bon[a-z]+\s*=|javascript:", re.I)


def protected_doctype(name):
	"""DocTypes a server script may never name (read or write)."""
	if not isinstance(name, str) or not name:
		return False
	if name in CODE_PROTECTED or name.startswith("OAuth") or name.endswith("Settings"):
		return True
	from ai_agent.runtime.policy import CODE_AND_ACCESS_DOCTYPES

	return name in CODE_AND_ACCESS_DOCTYPES


def dotted(node):
	"""``frappe.db.get_value`` for an Attribute/Name chain, else None."""
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return ".".join(reversed(parts))
	return None


def _matches(name, names):
	"""``name`` is one of ``names`` or an attribute under one (frappe.local.x matches frappe.local)."""
	return any(name == n or name.startswith(n + ".") for n in names)


def _const_str(node):
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value
	if isinstance(node, ast.JoinedStr):
		return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
	return None


def _call_doctype(call):
	"""The literal DocType a call names, from its first argument or {"doctype": "..."}."""
	if call.args:
		first = call.args[0]
		value = _const_str(first) if isinstance(first, ast.Constant) else None
		if value:
			return value
		if isinstance(first, ast.Dict):
			for key, val in zip(first.keys, first.values):
				if _const_str(key) == "doctype" and isinstance(val, ast.Constant):
					return _const_str(val)
	for kw in call.keywords:
		if kw.arg in ("doctype", "dt") and isinstance(kw.value, ast.Constant):
			return _const_str(kw.value)
	return None


def _is_doc_doctype(node):
	"""``doc.doctype`` — the event's own DocType."""
	return isinstance(node, ast.Attribute) and node.attr == "doctype" and isinstance(node.value, ast.Name) and node.value.id == "doc"


def _syntax_check(src):
	try:
		return compile(src, "<server script>", "exec"), None
	except SyntaxError as e:
		message = e.msg or str(e)
		if "'return' outside function" in message:
			message = _("'return' outside a function: a Server Script runs top to bottom. For an API set frappe.response[\"message\"] = ... instead of return")
		else:
			message = _("Invalid Python: {0}").format(message)
		return None, Check("py.syntax", "critical", message, e.lineno)


def _restricted_check(src):
	from RestrictedPython import compile_restricted
	from frappe.utils.safe_exec import FrappeTransformer

	try:
		compile_restricted(src, filename="<server script>", mode="exec", policy=FrappeTransformer)
	except SyntaxError as e:
		errors = e.args[0] if e.args else str(e)
		lines = errors if isinstance(errors, list | tuple) else [str(errors)]
		out = []
		for text in lines:
			match = re.search(r"Line (\d+)", str(text))
			out.append(Check("py.restricted", "critical",
				_("Not allowed in a Server Script: {0}").format(text), int(match.group(1)) if match else None))
		return out
	except Exception as e:
		return [Check("py.restricted", "critical", _("Not allowed in a Server Script: {0}").format(e))]
	return []


class _Visitor(ast.NodeVisitor):
	def __init__(self, script_type, reference_doctype):
		self.script_type = script_type
		self.reference_doctype = reference_doctype
		self.checks: list[Check] = []
		self.seen = set()
		self.permission_checked = set()  # DocTypes passed to frappe.has_permission
		self.unchecked_reads = []  # (doctype or None, name, line)
		self.function_depth = 0
		self.inner = set()  # Attribute nodes inside a longer chain: judged by the outermost one

	def add(self, code, level, message, line):
		key = (code, line, message)
		if key not in self.seen:
			self.seen.add(key)
			self.checks.append(Check(code, level, message, line))

	# -- structure ------------------------------------------------------------
	def visit_Import(self, node):
		self.add("py.import", "critical", _("'import' is not available in a Server Script (ImportError: __import__ not found). frappe, json and frappe.utils are already there"), node.lineno)

	visit_ImportFrom = visit_Import

	def _function(self, node):
		if node.decorator_list:
			self.add("py.decorator", "critical", _("Decorators (such as @frappe.whitelist) do not work in a Server Script; the script itself is the endpoint"), node.lineno)
		self.function_depth += 1
		self.generic_visit(node)
		self.function_depth -= 1

	visit_FunctionDef = _function
	visit_AsyncFunctionDef = _function

	def visit_ClassDef(self, node):
		self.add("py.class", "critical", _("Classes are not supported in a Server Script"), node.lineno)
		self.generic_visit(node)

	def visit_While(self, node):
		if isinstance(node.test, ast.Constant) and node.test.value and not any(isinstance(n, ast.Break) for n in ast.walk(node)):
			self.add("py.loop", "warning", _("'while True' without a break never ends"), node.lineno)
		self.generic_visit(node)

	# -- names and attributes -------------------------------------------------
	def visit_Name(self, node):
		if node.id in DYNAMIC and isinstance(node.ctx, ast.Load):
			self.add("py.dynamic", "critical", _("'{0}' is not allowed: it can reach anything").format(node.id), node.lineno)
		elif node.id in ("FrappeClient",):
			self.add("py.network", "critical", _("Network access ({0}) is blocked: data must not leave the system").format(node.id), node.lineno)
		elif node.id == "run_script":
			self.add("py.outbound", "critical", _("run_script is blocked: it runs another script outside this review"), node.lineno)
		elif node.id == "doc" and isinstance(node.ctx, ast.Load) and self.script_type == "API":
			self.add("py.doc_in_api", "warning", _("'doc' is not defined in an API script; read the request from frappe.form_dict"), node.lineno)
		self.generic_visit(node)

	def visit_Attribute(self, node):
		name = dotted(node)
		line = node.lineno
		if node.attr in ("format", "format_map"):
			self.add("py.format", "critical", _("'.{0}()' is blocked in Server Scripts (unsafe attribute); use an f-string").format(node.attr), line)
		if node.attr.startswith("_"):
			self.add("py.private", "critical", _("Names starting with '_' are not allowed: {0}").format(node.attr), line)
		if node.attr in ("ignore_permissions", "ignore_user_permissions"):
			self.add("py.ignore_permissions", "critical", _("Bypassing permissions ({0}) is blocked").format(node.attr), line)
		if isinstance(node.value, ast.Attribute):
			self.inner.add(id(node.value))
		if name and id(node) not in self.inner:
			self._dotted(name, node, line)
		self.generic_visit(node)

	def _dotted(self, name, node, line):
		if _matches(name, NETWORK):
			self.add("py.network", "critical", _("Network access ({0}) is blocked: data must not leave the system").format(name), line)
		elif _matches(name, OUTBOUND):
			self.add("py.outbound", "critical", _("{0} is blocked: it sends data out or runs other code outside this review").format(name), line)
		elif _matches(name, DESTRUCTIVE):
			self.add("py.destructive", "critical", _("{0} is blocked: it deletes, renames or changes the database schema").format(name), line)
		elif _matches(name, TRANSACTION):
			self.add("py.transaction", "critical", _("{0} is blocked: Frappe commits for you").format(name), line)
		elif _matches(name, IDENTITY):
			reason = _("it bypasses row permissions; build a Query Report for SQL") if name.startswith(("frappe.db.sql", "frappe.qb")) else _("it changes who the code runs as")
			self.add("py.identity", "critical", _("{0} is blocked: {1}").format(name, reason), line)
		elif name.startswith("frappe.session") and isinstance(node.ctx, ast.Store | ast.Del):
			self.add("py.identity", "critical", _("Changing frappe.session is blocked"), line)
		elif name.startswith("frappe.response") and self.script_type == "DocType Event":
			self.add("py.response_in_event", "warning", _("frappe.response has no effect in a DocType Event"), line)

	# -- calls ----------------------------------------------------------------
	def visit_Call(self, node):
		name = dotted(node.func)
		line = node.lineno
		for kw in node.keywords:
			if kw.arg in ("ignore_permissions", "ignore_user_permissions"):
				self.add("py.ignore_permissions", "critical", _("Bypassing permissions ({0}) is blocked").format(kw.arg), line)
			if kw.arg is None and isinstance(kw.value, ast.Dict):  # **{"ignore_permissions": True}
				self._dict_keys(kw.value, line)
		for arg in node.args:
			if isinstance(arg, ast.Dict):
				self._dict_keys(arg, line)

		if name in MISSING_IN_SAFE_EXEC:
			self.add("py.not_available", "critical",
				_("{0} does not exist inside a Server Script and fails at run time. Read through frappe.get_list (it applies the user's permissions) or call doc.check_permission(\"read\") on a loaded document").format(name), line)
		if isinstance(node.func, ast.Attribute) and node.func.attr == "check_permission":
			self.doc_permission_checked = True
		if name in DOCTYPE_CALLS:
			doctype = _call_doctype(node)
			if doctype and protected_doctype(doctype):
				self.add("py.protected_doctype", "critical", _("{0} touches {1}, which agent scripts may not read or change").format(name, doctype), line)
			if name in UNCHECKED_READS:
				own = bool(node.args) and _is_doc_doctype(node.args[0])
				if not own and not (doctype and doctype == self.reference_doctype and self.script_type == "DocType Event"):
					self.unchecked_reads.append((doctype, name, line))
			if name == "frappe.db.set_value":
				self.add("py.write", "warning", _("{0} writes to another record outside this document").format(name), line)

		if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS and name not in DOCTYPE_CALLS:
			self.add("py.write", "warning", _("'.{0}()' writes a record (side effect outside this document)").format(node.func.attr), line)

		if name in ("frappe.msgprint", "frappe.throw"):
			for arg in list(node.args) + [kw.value for kw in node.keywords]:
				text = _const_str(arg)
				if text and HTML_SCRIPT_RE.search(text):
					self.add("py.html_message", "critical", _("Messages may not contain scripts or event handlers"), line)
		self.generic_visit(node)

	def _dict_keys(self, node, line):
		for key in node.keys:
			if _const_str(key) in ("ignore_permissions", "ignore_user_permissions"):
				self.add("py.ignore_permissions", "critical", _("Bypassing permissions ({0}) is blocked").format(_const_str(key)), line)

	def visit_Dict(self, node):
		self._dict_keys(node, node.lineno)
		for key, value in zip(node.keys, node.values):
			if _const_str(key) == "doctype" and isinstance(value, ast.Constant) and protected_doctype(_const_str(value)):
				self.add("py.protected_doctype", "critical", _("The script names {0}, which agent scripts may not read or change").format(_const_str(value)), node.lineno)
		self.generic_visit(node)

	def finish(self, tree):
		level = "warning" if self.script_type == "API" else "info"
		for doctype, name, line in self.unchecked_reads:
			if doctype and doctype in self.permission_checked:
				continue
			if name in DOC_READS and getattr(self, "doc_permission_checked", False):
				continue
			target = doctype or _("a DocType")
			self.add("py.unchecked_read", level,
				_("{0} reads {1} without checking permission: in a Server Script it sees every record. Use frappe.get_list (it applies the user's permissions) or call doc.check_permission(\"read\") on the loaded document").format(name, target), line)
		if self.script_type == "API":
			body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
			defs = [n for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
			if defs and all(isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign) for n in body):
				self.add("py.api_def", "critical",
					_("The API logic sits inside 'def {0}' that nothing calls. Write the logic at the top level and set frappe.response[\"message\"]").format(defs[0].name), defs[0].lineno)


def analyze(src, script_type="DocType Event", reference_doctype=None):
	"""Checks for one Server Script body. Critical = blocks the request."""
	src = src or ""
	checks = []
	if not src.strip():
		return [Check("py.empty", "critical", _("The script is empty"))]
	if len(src) > MAX_CHARS:
		return [Check("py.size", "critical", _("The script is longer than {0} characters").format(MAX_CHARS))]
	lines = src.count("\n") + 1
	if lines > WARN_LINES:
		checks.append(Check("py.size", "warning", _("The script has {0} lines; long scripts are hard to review").format(lines)))

	_code, syntax_error = _syntax_check(src)
	if syntax_error:
		checks.append(syntax_error)
		return checks
	tree = ast.parse(src)
	visitor = _Visitor(script_type, reference_doctype)
	visitor.visit(tree)
	visitor.finish(tree)
	checks.extend(visitor.checks)

	# RestrictedPython after our rules: its messages duplicate ours for imports/underscores.
	if not any(c.level == "critical" for c in checks):
		checks.extend(_restricted_check(src))
	if not any(c.level == "critical" for c in checks):
		checks.insert(0, Check("py.syntax", "ok", _("Python syntax is valid and it compiles in the Server Script sandbox")))
	return checks


# -- dry run --------------------------------------------------------------------


class DryRunBlocked(Exception):
	pass


def _stub(name, calls):
	def blocked(*args, **kwargs):
		calls.append(name)
		raise DryRunBlocked(name)

	return blocked


STUBBED_FRAPPE = ("sendmail", "enqueue", "call", "make_get_request", "make_post_request", "make_put_request",
	"make_patch_request", "make_delete_request", "delete_doc", "rename_doc", "get_print", "attach_print",
	"publish_realtime", "log_error")
STUBBED_DB = ("set_value", "commit", "rollback", "add_index", "sql")


def _stubbed_globals(calls):
	from frappe.utils.safe_exec import NamespaceDict, get_safe_globals

	base = get_safe_globals()
	fr = NamespaceDict(base.frappe)
	for name in STUBBED_FRAPPE:
		fr[name] = _stub(f"frappe.{name}", calls)
	db = NamespaceDict(base.frappe.db)
	for name in STUBBED_DB:
		db[name] = _stub(f"frappe.db.{name}", calls)
	fr["db"] = db
	fr["qb"] = None
	return {"frappe": fr, "run_script": _stub("run_script", calls), "FrappeClient": _stub("FrappeClient", calls)}


def _plain(value):
	if isinstance(value, int | float | str | bool) or value is None:
		return value
	return str(value)


def dry_run(script, reference_doctype, doctype_event, user=None):
	"""Run a ``Before *`` DocType Event script once on a copy of a readable document; nothing persists.

	Returns ``{"ran", "document", "threw", "changed": {field: [before, after]}, "blocked": [...], "checks"}``.
	"""
	from frappe.utils.safe_exec import is_safe_exec_enabled, safe_exec

	out = {"ran": False, "document": None, "threw": None, "changed": {}, "blocked": [], "checks": []}
	if not (doctype_event or "").startswith("Before") or doctype_event == "Before Delete":
		return out
	if not is_safe_exec_enabled():
		return out
	user = user or frappe.session.user
	names = frappe.get_list(reference_doctype, fields=["name"], limit=1, order_by="creation desc", user=user)
	if names:
		doc = frappe.get_doc(reference_doctype, names[0].name)
		out["document"] = doc.name
	else:
		doc = frappe.new_doc(reference_doctype)
		out["document"] = _("(new {0})").format(reference_doctype)
	before = doc.as_dict(no_default_fields=True)
	copy = frappe.get_doc(reference_doctype, doc.name) if names else doc  # a separate object, never saved
	calls = []
	savepoint = "ai_customize_dry_run"
	frappe.db.savepoint(savepoint)
	message_log = list(frappe.local.message_log or [])
	try:
		safe_exec(script, _globals=_stubbed_globals(calls), _locals={"doc": copy}, restrict_commit_rollback=True,
			script_filename="ai_agent_dry_run")
		out["ran"] = True
	except DryRunBlocked as e:
		out["ran"] = True
		out["checks"].append(Check("py.dry_run_blocked", "info", _("The trial run stopped at {0} (not run in a trial)").format(e)))
	except frappe.ValidationError as e:
		out["ran"] = True
		out["threw"] = frappe.utils.strip_html(str(e))[:300]
		out["checks"].append(Check("py.dry_run_throw", "info", _("On {0} the script stops the save with: {1}").format(out["document"], out["threw"])))
	except Exception as e:
		out["ran"] = True
		out["checks"].append(Check("py.dry_run_error", "critical", _("The script fails when it runs: {0}: {1}").format(type(e).__name__, str(e)[:300])))
	finally:
		frappe.db.rollback(save_point=savepoint)
		frappe.local.message_log = message_log
	after = copy.as_dict(no_default_fields=True)
	for key, value in after.items():
		if isinstance(value, list) or key in ("name", "modified", "creation"):
			continue
		if _plain(before.get(key)) != _plain(value):
			out["changed"][key] = [_plain(before.get(key)), _plain(value)]
	out["blocked"] = calls
	if out["ran"] and not out["checks"]:
		out["checks"].append(Check("py.dry_run", "ok", _("Trial run on {0} finished without errors").format(out["document"])))
	return out
