"""Static analysis of Server Script and Script Report code (docs/customization/01 §3.3.4, §3.1 v2/v3).

``analyze(src, script_type, reference_doctype=None) -> list[Check]`` is pure (no database).
``script_type`` is a Server Script type (DocType Event, API, Scheduler Event, Permission Query) or
"Script Report" (a non-standard report's ``report_script``, run by ``Report.execute_script``).
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
	"frappe.sendmail", "frappe.enqueue", "run_script", "frappe.get_print",
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
	"frappe.get_cached_doc", "frappe.get_last_doc", "frappe.db.get_single_value"}   # frappe.get_cached_value: not in safe_exec
# Calls whose first argument (or {"doctype": ...}) names the DocType they touch.
DOCTYPE_CALLS = UNCHECKED_READS | {"frappe.new_doc", "frappe.get_list", "frappe.db.get_list", "frappe.db.set_value",
	"frappe.db.exists", "frappe.db.count", "frappe.copy_doc", "frappe.get_mapped_doc"}
# Not in get_safe_globals() on Frappe v16: a script calling them fails at run time.
MISSING_IN_SAFE_EXEC = {"frappe.has_permission", "frappe.get_roles"}
# Reads a later doc.check_permission(...) covers (the loaded document is checked).
DOC_READS = {"frappe.get_doc", "frappe.get_cached_doc", "frappe.get_last_doc"}
WRITE_METHODS = {"save", "insert", "submit", "cancel", "delete", "db_set", "db_update", "db_insert", "run_method"}

# Business DocTypes in CODE_PROTECTED that a script may still *list* through frappe.get_list, which
# applies the user's permissions and User Permissions (a report's "companies I may see"). Any other call on
# them (get_doc, get_all, get_value, new_doc, set_value...) stays critical.
LISTABLE_PROTECTED = frozenset({"Company", "Fiscal Year", "Accounting Period"})
LISTING_CALLS = {"frappe.get_list", "frappe.db.get_list"}
# Script types where 'doc' does not exist (Server Script types run without a document, and Script Reports).
NO_DOC_TYPES = ("API", "Scheduler Event", "Permission Query", "Script Report")

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


def _missing_frappe_api(name):
	"""frappe.X / frappe.db.X called in a script but absent from safe_exec's namespaces (Frappe v16), e.g.
	frappe.get_cached_value, frappe.get_single, frappe.get_value. Unknown deeper chains are left alone."""
	parts = name.split(".")
	if parts[0] != "frappe" or len(parts) not in (2, 3) or (len(parts) == 3 and parts[1] != "db"):
		return False
	try:
		names = _safe_frappe_names()
	except Exception:
		return False
	return parts[-1] not in (names["db"] if len(parts) == 3 else names["frappe"])


_SAFE_NAMES = {}


def _safe_frappe_names():
	if not _SAFE_NAMES:
		from frappe.utils.safe_exec import get_safe_globals

		fr = get_safe_globals()["frappe"]
		_SAFE_NAMES["frappe"] = set(fr.keys())
		_SAFE_NAMES["db"] = set((fr.get("db") or {}).keys())
	return _SAFE_NAMES


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


def _syntax_check(src, script_type=None):
	try:
		return compile(src, "<server script>", "exec"), None
	except SyntaxError as e:
		message = e.msg or str(e)
		if "'return' outside function" in message and script_type == "Script Report":
			message = _("'return' outside a function: a Script Report runs top to bottom. Set data = [columns, rows] instead of return")
		elif "'return' outside function" in message and script_type == "Permission Query":
			message = _("'return' outside a function: a Permission Query runs top to bottom. Set conditions = \"...\" instead of return")
		elif "'return' outside function" in message:
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
		self.raw_user = []  # lines where a Permission Query puts `user` into text without frappe.db.escape
		self.called_methods = []  # (method, line) of frappe.call, checked against the server in analyze()

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
		elif node.id == "doc" and isinstance(node.ctx, ast.Load) and self.script_type in NO_DOC_TYPES:
			self.add("py.doc_in_api", "warning", _("'doc' is not defined in a {0}").format(self.script_type), node.lineno)
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
		elif name == "frappe.db.escape" and self.script_type == "Permission Query":
			pass  # the right way to put the user into a Permission Query condition
		elif _matches(name, IDENTITY):
			reason = _("it bypasses row permissions; build a Query Report for SQL") if name.startswith(("frappe.db.sql", "frappe.qb")) else _("it changes who the code runs as")
			self.add("py.identity", "critical", _("{0} is blocked: {1}").format(name, reason), line)
		elif name.startswith("frappe.session") and isinstance(node.ctx, ast.Store | ast.Del):
			self.add("py.identity", "critical", _("Changing frappe.session is blocked"), line)
		elif name.startswith("frappe.response") and self.script_type == "DocType Event":
			self.add("py.response_in_event", "warning", _("frappe.response has no effect in a DocType Event"), line)
		elif name.startswith("frappe.response") and self.script_type in ("Scheduler Event", "Permission Query", "Script Report"):
			self.add("py.response_in_event", "warning", _("frappe.response has no effect in a {0}").format(self.script_type), line)

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

		if name == "frappe.call":
			self._frappe_call(node, line)
		if name in MISSING_IN_SAFE_EXEC:
			self.add("py.not_available", "critical",
				_("{0} does not exist inside a Server Script and fails at run time. Read through frappe.get_list (it applies the user's permissions) or call doc.check_permission(\"read\") on a loaded document").format(name), line)
		elif name and _missing_frappe_api(name):
			self.add("py.not_available", "critical",
				_("{0} does not exist inside a Server Script (safe_exec) and fails at run time").format(name), line)
		if isinstance(node.func, ast.Attribute) and node.func.attr == "check_permission":
			self.doc_permission_checked = True
		if name in DOCTYPE_CALLS:
			doctype = _call_doctype(node)
			if doctype and protected_doctype(doctype) and not (name in LISTING_CALLS and doctype in LISTABLE_PROTECTED):
				self.add("py.protected_doctype", "critical", _("{0} touches {1}, which agent scripts may not read or change").format(name, doctype), line)
			if name in UNCHECKED_READS:
				own = bool(node.args) and _is_doc_doctype(node.args[0])
				if not own and not (doctype and doctype == self.reference_doctype and self.script_type == "DocType Event"):
					self.unchecked_reads.append((doctype, name, line))
			if name == "frappe.db.set_value":
				self._write(_("{0} writes to another record outside this document").format(name), line)

		if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS and name not in DOCTYPE_CALLS:
			self._write(_("'.{0}()' writes a record (side effect outside this document)").format(node.func.attr), line)

		if name in ("frappe.msgprint", "frappe.throw"):
			for arg in list(node.args) + [kw.value for kw in node.keywords]:
				text = _const_str(arg)
				if text and HTML_SCRIPT_RE.search(text):
					self.add("py.html_message", "critical", _("Messages may not contain scripts or event handlers"), line)
		self.generic_visit(node)

	def _frappe_call(self, node, line):
		"""frappe.call(method, **kwargs) runs a whitelisted method as the current user (execute_cmd checks
		is_whitelisted and the method applies its own permissions): allowed once the method is known."""
		from ai_agent.customization.analyzers.js_code import WRITE_METHODS as WRITE_API

		target = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in ("function", "method")), None)
		method = _const_str(target) if isinstance(target, ast.Constant) else None
		if not method:
			self.add("py.call_dynamic", "critical", _("frappe.call with a computed method name cannot be reviewed; write the method name as text"), line)
		elif method in WRITE_API:
			self._write(_("frappe.call(\"{0}\") changes data on the server").format(method), line)
		else:
			self.called_methods.append((method, line))

	def _write(self, message, line):
		if self.script_type == "Permission Query":
			self.add("py.write", "critical", _("A Permission Query runs on every list read and may not write: {0}").format(message), line)
		else:
			self.add("py.write", "warning", message, line)

	def _dict_keys(self, node, line):
		for key in node.keys:
			if _const_str(key) in ("ignore_permissions", "ignore_user_permissions"):
				self.add("py.ignore_permissions", "critical", _("Bypassing permissions ({0}) is blocked").format(_const_str(key)), line)

	def visit_JoinedStr(self, node):
		if self.script_type == "Permission Query":
			for value in node.values:
				if isinstance(value, ast.FormattedValue) and _names_user(value.value):
					self.raw_user.append(node.lineno)
		self.generic_visit(node)

	def visit_BinOp(self, node):
		if self.script_type == "Permission Query" and isinstance(node.op, ast.Add | ast.Mod):
			texts = [n for n in (node.left, node.right) if _const_str(n) is not None]
			if texts and any(_names_user(n) for n in (node.left, node.right) if _const_str(n) is None):
				self.raw_user.append(node.lineno)
		self.generic_visit(node)

	def visit_Dict(self, node):
		self._dict_keys(node, node.lineno)
		for key, value in zip(node.keys, node.values):
			if _const_str(key) == "doctype" and isinstance(value, ast.Constant) and protected_doctype(_const_str(value)):
				self.add("py.protected_doctype", "critical", _("The script names {0}, which agent scripts may not read or change").format(_const_str(value)), node.lineno)
		self.generic_visit(node)

	def finish(self, tree):
		level = "warning" if self.script_type in ("API", "Script Report", "Scheduler Event") else "info"
		for doctype, name, line in self.unchecked_reads:
			if doctype and doctype in self.permission_checked:
				continue
			if name in DOC_READS and getattr(self, "doc_permission_checked", False):
				continue
			target = doctype or _("a DocType")
			self.add("py.unchecked_read", level,
				_("{0} reads {1} without checking permission: in a Server Script it sees every record. Use frappe.get_list (it applies the user's permissions) or call doc.check_permission(\"read\") on the loaded document").format(name, target), line)
		stored = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
		if self.script_type == "Script Report" and not ({"data", "result"} & stored):
			self.add("py.report_output", "critical",
				_("A Script Report must set data = [columns, rows] (or result = rows with the report's columns table)"), None)
		if self.script_type == "Permission Query":
			if "conditions" not in stored:
				self.add("py.conditions", "critical", _("A Permission Query must set conditions = \"<SQL condition>\" (an empty string means no restriction)"), None)
			for line in sorted(set(self.raw_user)):
				self.add("py.unescaped_user", "warning",
					_("'user' is put into the SQL text without frappe.db.escape(user); quote it with frappe.db.escape"), line)
		if self.script_type == "API":
			body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
			defs = [n for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
			called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
			defs = [d for d in defs if d.name not in called]   # a helper the script calls is fine
			if defs and all(isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Assign) for n in body):
				self.add("py.api_def", "critical",
					_("The API logic sits inside 'def {0}' that nothing calls. Write the logic at the top level and set frappe.response[\"message\"]").format(defs[0].name), defs[0].lineno)


def _names_user(node):
	"""The expression reads the variable ``user`` directly (not through frappe.db.escape)."""
	if isinstance(node, ast.Call) and dotted(node.func) == "frappe.db.escape":
		return False
	if isinstance(node, ast.Name):
		return node.id == "user"
	return any(_names_user(child) for child in ast.iter_child_nodes(node))


def analyze(src, script_type="DocType Event", reference_doctype=None):
	"""Checks for one Server Script body or Script Report script. Critical = blocks the request."""
	src = src or ""
	checks = []
	if not src.strip():
		return [Check("py.empty", "critical", _("The script is empty"))]
	if len(src) > MAX_CHARS:
		return [Check("py.size", "critical", _("The script is longer than {0} characters").format(MAX_CHARS))]
	lines = src.count("\n") + 1
	if lines > WARN_LINES:
		checks.append(Check("py.size", "warning", _("The script has {0} lines; long scripts are hard to review").format(lines)))

	_code, syntax_error = _syntax_check(src, script_type)
	if syntax_error:
		checks.append(syntax_error)
		return checks
	tree = ast.parse(src)
	visitor = _Visitor(script_type, reference_doctype)
	visitor.visit(tree)
	visitor.finish(tree)
	checks.extend(visitor.checks)
	checks.extend(_runtime_traps(tree))
	checks.extend(_called_methods(visitor.called_methods))

	# RestrictedPython after our rules: its messages duplicate ours for imports/underscores.
	if not any(c.level == "critical" for c in checks):
		checks.extend(_restricted_check(src))
	if not any(c.level == "critical" for c in checks):
		checks.insert(0, Check("py.syntax", "ok", _("Python syntax is valid and it compiles in the Server Script sandbox")))
	return checks


def _called_methods(called):
	"""frappe.call targets must exist and be whitelisted (same rule as Client Scripts)."""
	from ai_agent.customization.analyzers.js_code import _method_exists

	checks = []
	for method, line in called:
		if _method_exists(method):
			checks.append(Check("py.call", "info", _("Calls the server method {0} as the current user").format(method), line))
		else:
			checks.append(Check("py.call_missing", "critical", _("Calls {0}, which does not exist or is not whitelisted").format(method), line))
	return checks


# -- run-time traps of the sandbox ---------------------------------------------------


def _sandbox_names():
	"""Names a Server Script can call: safe_exec's globals and its builtins (Frappe v16)."""
	from frappe.utils.safe_exec import get_safe_globals

	g = get_safe_globals()
	builtins = g.get("__builtins__") or {}
	return set(g) | set(builtins if isinstance(builtins, dict) else dir(builtins))


def _runtime_traps(tree):
	"""Code that compiles in the sandbox but fails when it runs (verified against safe_exec on Frappe v16)."""
	checks = []
	available = _sandbox_names()
	defined = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
	defined |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
	defined |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args + n.kwonlyargs}
	seen = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and any(isinstance(t, ast.Tuple | ast.List) for t in node.targets):
			checks.append(Check("py.unpack", "critical",
				_("Unpacking in an assignment (a, b = ...) fails in a Server Script (no _unpack_sequence_). Assign one name and index it: pair = ...; a = pair[0]"), node.lineno))
		elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "strftime":
			checks.append(Check("py.strftime", "critical",
				_(".strftime() fails in a Server Script (it imports at run time). Use frappe.utils.formatdate / format_datetime / getdate"), node.lineno))
		elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
			name = node.func.id
			if name not in available and name not in defined and name not in seen:
				seen.add(name)
				checks.append(Check("py.not_available", "critical",
					_("{0}() does not exist inside a Server Script and fails at run time; use a list comprehension or a frappe.utils helper instead").format(name), node.lineno))
	return checks


# -- dry run --------------------------------------------------------------------


class DryRunBlocked(Exception):
	pass


def _stub(name, calls):
	def blocked(*args, **kwargs):
		calls.append(name)
		raise DryRunBlocked(name)

	return blocked


def _guarded_call(call, calls):
	"""frappe.call in a dry run: read methods run (inside the rolled-back savepoint), write methods are blocked."""
	from ai_agent.customization.analyzers.js_code import WRITE_METHODS as WRITE_API

	def guarded(function, *args, **kwargs):
		if function in WRITE_API:
			calls.append(f"frappe.call({function})")
			raise DryRunBlocked(function)
		return call(function, *args, **kwargs)

	return guarded


STUBBED_FRAPPE = ("sendmail", "enqueue", "make_get_request", "make_post_request", "make_put_request",
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
	fr["call"] = _guarded_call(base.frappe.call, calls)
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


# -- trial runs of Script Reports and Permission Queries ------------------------------


def _rolled_back(fn, savepoint):
	"""Run ``fn`` inside a savepoint that is always rolled back; msgprint output is dropped."""
	frappe.db.savepoint(savepoint)
	message_log = list(frappe.local.message_log or [])
	try:
		return fn()
	finally:
		frappe.db.rollback(save_point=savepoint)
		frappe.local.message_log = message_log


def run_report_script(script, filters, name="ai_agent_preview"):
	"""Run a Script Report's ``report_script`` the way ``Report.execute_script`` does
	(frappe/core/doctype/report/report.py: locals ``filters``/``data``/``result``), with the write, network,
	mail and queue functions stubbed, inside a rolled-back savepoint, as the session user.

	Returns ``{"data": loc["data"], "result": loc["result"], "blocked": [...]}``; raises what the script raises
	(``DryRunBlocked`` when it reached a stubbed function).
	"""
	from frappe.utils.safe_exec import safe_exec

	calls = []
	loc = {"filters": frappe._dict(filters or {}), "data": None, "result": None}

	def run():
		safe_exec(script, _globals=_stubbed_globals(calls), _locals=loc, restrict_commit_rollback=True,
			script_filename=f"Report {name}")

	_rolled_back(run, "ai_customize_report_run")
	return {"data": loc["data"], "result": loc["result"], "blocked": calls}


def permission_conditions(script, user, active_child_tables=None, name="ai_agent_preview"):
	"""The ``conditions`` a Permission Query script produces for ``user``, run the way
	``ServerScript.get_permission_query_conditions`` does (locals ``user``, ``conditions``,
	``active_child_tables``), stubbed and rolled back. Raises what the script raises."""
	from frappe.utils.safe_exec import safe_exec

	calls = []
	loc = {"user": user, "conditions": "", "active_child_tables": list(active_child_tables or [])}

	def run():
		safe_exec(script, _globals=_stubbed_globals(calls), _locals=loc, restrict_commit_rollback=True,
			script_filename=name)

	_rolled_back(run, "ai_customize_pq_run")
	return loc["conditions"]
