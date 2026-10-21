"""Static analysis of Jinja templates and notification conditions (docs/customization/01 §3.5, v2).

Used by the ``print_format`` and ``notification`` kinds.

Why the rules are strict: with the default site config (``disable_render_safe_exec`` unset),
``frappe.utils.jinja.get_jenv`` gives templates ``get_safe_globals()`` = ``exec_safe_globals()``
(frappe/utils/safe_exec.py:533-587, :1014): ``frappe.sendmail``, ``frappe.db.set_value``,
``frappe.db.commit``, ``frappe.make_post_request``, ``frappe.enqueue``, ``frappe.delete_doc``... and a
print template receives the real ``Document`` (``doc.save()`` / ``doc.db_set()`` are callable through the
sandbox). Notification conditions are ``frappe.safe_eval`` with the same ``frappe`` namespace
(notification.py ``get_context``). So anything that writes, sends, queues or runs code is critical, and
permission-less reads (``frappe.db.*``, ``frappe.get_all``, ``frappe.get_doc``) are warnings.

The checks walk the parsed Jinja / Python AST (aliases such as ``{% set s = frappe.sendmail %}`` still
show the attribute), and ``|attr(...)`` or subscripting ``frappe`` with a non-literal key is critical
because it hides the attribute name from this analysis.
"""

from __future__ import annotations

import ast
import re

import frappe
from frappe import _
from frappe.utils import cstr

from ai_agent.customization.contract import Check

MAX_CHARS = 50000

# Attribute / name anywhere in the template or condition: writes, sends, queues, runs code.
WRITES = frozenset({
	"sendmail", "set_value", "commit", "rollback", "add_index", "after_commit", "before_commit", "after_rollback",
	"before_rollback", "delete_doc", "rename_doc", "new_doc", "copy_doc", "get_mapped_doc", "insert", "save",
	"submit", "cancel", "delete", "db_set", "db_insert", "db_update", "run_method", "make_get_request",
	"make_post_request", "make_put_request", "make_patch_request", "make_delete_request", "enqueue", "call",
	"run_script", "FrappeClient", "get_print", "attach_print", "render_template", "publish_realtime",
	"add_comment", "set_user", "log_error", "set_password", "integrations",
})
# frappe.<x> / frappe.db.<x> that read without permission checks.
UNPERMITTED_READS = frozenset({
	"frappe.get_all", "frappe.get_doc", "frappe.get_cached_doc", "frappe.get_last_doc", "frappe.get_cached_value",
	"frappe.get_value", "frappe.db.get_all", "frappe.db.get_value", "frappe.db.get_single_value", "frappe.db.sql",
	"frappe.db.exists", "frappe.db.count", "frappe.db.get_default",
})
URL_RE = re.compile(r"""(?:\b(?:src|href|action|srcset)\s*=\s*["']?\s*|url\(\s*["']?\s*|@import\s+["']?)((?:https?:)?//[^\s"'<>)]+)""", re.I)
SCRIPT_RE = re.compile(r"<\s*script\b|\bon[a-z]+\s*=\s*[\"']|javascript\s*:", re.I)


def _line(text, index):
	return text.count("\n", 0, index) + 1


def _site_host():
	from urllib.parse import urlparse

	try:
		return (urlparse(frappe.utils.get_url()).hostname or "").lower()
	except Exception:
		return ""  # no host: every URL is then reported as another site's (more warnings, never fewer)


def text_checks(text, where, html=True):
	"""Checks on the raw text: size, `.__`, <script>/on*= handlers, URLs to other hosts."""
	from urllib.parse import urlparse

	text = cstr(text)
	out = []
	if len(text) > MAX_CHARS:
		out.append(Check("jinja.size", "critical", _("{0} is longer than {1} characters").format(where, MAX_CHARS)))
	index = text.find(".__")
	if index >= 0:
		# frappe/utils/jinja.py render_template: `if safe_render and ".__" in template: throw("Illegal template")`
		out.append(Check("jinja.dunder", "critical", _("{0} uses a double-underscore attribute (Frappe rejects it as an illegal template)").format(where), _line(text, index)))
	if html:
		match = SCRIPT_RE.search(text)
		if match:
			out.append(Check("jinja.script", "warning", _("{0} contains JavaScript (<script>, on*= or javascript:); it runs in every browser that opens it").format(where), _line(text, match.start())))
		host = _site_host()
		seen = set()
		for match in URL_RE.finditer(text):
			url = match.group(1)
			target = (urlparse(url if "//" in url[:8] else "https:" + url).hostname or "").lower()
			if target and target != host and target not in seen:
				seen.add(target)
				out.append(Check("jinja.external_url", "warning", _("{0} loads {1} from another site: the document's readers' browsers contact it").format(where, target), _line(text, match.start())))
	return out


def _chain(node):
	"""Dotted name of a Getattr chain (``frappe.db.get_value``), or None."""
	from jinja2 import nodes

	parts = []
	while isinstance(node, nodes.Getattr):
		parts.append(node.attr)
		node = node.node
	if isinstance(node, nodes.Name):
		parts.append(node.name)
		return ".".join(reversed(parts))
	return None


# Protected from writes elsewhere, but a print legitimately reads them ("prepared by" the owner's full name, a
# letter head, another print style).
TEMPLATE_READABLE = frozenset({"User", "Role", "Letter Head", "Print Format", "Print Style", "Email Template", "Translation"})


def _template_protected(doctype):
	"""What a print or a notification may never read: identity, access, secrets and settings (business
	masters such as Company stay readable: a letterhead needs the company's address)."""
	from ai_agent.runtime.policy import CODE_AND_ACCESS_DOCTYPES

	name = str(doctype or "")
	if name in TEMPLATE_READABLE:
		return False
	return name in CODE_AND_ACCESS_DOCTYPES or name.startswith("OAuth") or name.endswith("Settings") or name in (
		"Has Role", "User Permission", "Role Profile", "Custom DocPerm", "DocPerm", "Session Default Settings")


def compile_checks(text, where):
	"""Jinja syntax (critical, with the line) and AST rules. Returns ``(checks, ok)``."""
	from jinja2 import TemplateSyntaxError, nodes

	text = cstr(text)
	if not text.strip():
		return [], True
	jenv = frappe.get_jenv()
	try:
		tree = jenv.parse(text)
		jenv.from_string(text)  # what frappe.utils.jinja.validate_template does
	except TemplateSyntaxError as e:
		return [Check("jinja.syntax", "critical", _("{0}: Jinja syntax error: {1}").format(where, e.message), e.lineno)], False
	except Exception as e:
		return [Check("jinja.syntax", "critical", _("{0}: Jinja syntax error: {1}").format(where, str(e)[:300]))], False

	out = []
	frappe_names = {"frappe"}
	for assign in tree.find_all(nodes.Assign):
		value = assign.node
		root = _chain(value)
		if root and root.split(".")[0] in frappe_names and isinstance(assign.target, nodes.Name):
			frappe_names.add(assign.target.name)
	seen = set()

	def add(code, level, message, lineno):
		key = (code, message)
		if key not in seen:
			seen.add(key)
			out.append(Check(code, level, message, lineno))

	for node in tree.find_all((nodes.Getattr, nodes.Name, nodes.Getitem, nodes.Filter)):
		lineno = getattr(node, "lineno", None)
		if isinstance(node, nodes.Getattr):
			if node.attr in WRITES:
				add("jinja.write", "critical", _("{0} calls {1}: templates must not write, send, queue or run code").format(where, node.attr), lineno)
				continue
			chain = _chain(node)
			if chain:
				root, _sep, rest = chain.partition(".")
				dotted = "frappe." + rest if root in frappe_names and rest else chain
				if dotted in UNPERMITTED_READS:
					add("jinja.unpermitted_read", "warning", _("{0} uses {1}, which reads without permission checks: whoever receives the output sees that data").format(where, dotted), lineno)
		elif isinstance(node, nodes.Name):
			if node.name in WRITES:
				add("jinja.write", "critical", _("{0} calls {1}: templates must not write, send, queue or run code").format(where, node.name), lineno)
		elif isinstance(node, nodes.Filter):
			if node.name == "attr":
				add("jinja.attr_filter", "critical", _("{0} uses the attr filter, which hides what is called; use plain dotted access").format(where), lineno)
		elif isinstance(node, nodes.Getitem):
			root = _chain(node.node)
			if root and root.split(".")[0] in frappe_names:
				if isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
					if node.arg.value in WRITES:
						add("jinja.write", "critical", _("{0} calls {1}: templates must not write, send, queue or run code").format(where, node.arg.value), lineno)
				else:
					add("jinja.dynamic_access", "critical", _("{0} indexes frappe with a computed key; use plain dotted access").format(where), lineno)
	for call in tree.find_all(nodes.Call):
		chain = _chain(call.node)
		if not chain:
			continue
		root, _sep, rest = chain.partition(".")
		dotted = "frappe." + rest if root in frappe_names and rest else chain
		if dotted not in UNPERMITTED_READS:
			continue
		lineno = getattr(call, "lineno", None)
		if dotted == "frappe.db.sql":
			# raw SQL reads any table, users and passwords included; the output goes to whoever gets the print or mail
			add("jinja.sql", "critical", _("{0} runs frappe.db.sql: read through frappe.get_list or the document's own fields").format(where), lineno)
			continue
		first = call.args[0] if call.args else None
		if first is None or not isinstance(first, nodes.Const):
			add("jinja.dynamic_doctype", "critical", _("{0}: {1} names its DocType with a computed value; write the name as text").format(where, dotted), lineno)
		elif _template_protected(first.value):
			add("jinja.protected_doctype", "critical", _("{0}: {1} reads {2}, which templates may not read").format(where, dotted, first.value), lineno)
	return out, True


def condition_checks(expression, where):
	"""A Notification condition: one Python expression for ``frappe.safe_eval`` (no lambda, no ``:=``,
	frappe/utils/safe_exec.py ``_validate_safe_eval_syntax``), with the same rules as templates."""
	expression = cstr(expression).strip()
	if not expression:
		return [], True
	try:
		tree = ast.parse(expression, mode="eval")
	except SyntaxError as e:
		return [Check("condition.syntax", "critical", _("{0} is not a valid Python expression: {1}").format(where, e.msg), e.lineno)], False
	out = []
	for node in ast.walk(tree):
		if isinstance(node, (ast.Lambda, ast.NamedExpr)):
			out.append(Check("condition.syntax", "critical", _("{0}: lambda and := are not allowed by frappe.safe_eval").format(where)))
		elif isinstance(node, ast.Attribute):
			if node.attr.startswith("_"):
				out.append(Check("condition.dunder", "critical", _("{0} uses the private attribute {1}").format(where, node.attr)))
			elif node.attr in WRITES:
				out.append(Check("condition.write", "critical", _("{0} calls {1}: a condition must only test values").format(where, node.attr)))
		elif isinstance(node, ast.Name) and (node.id in WRITES or node.id.startswith("_")):
			out.append(Check("condition.write", "critical", _("{0} calls {1}: a condition must only test values").format(where, node.id)))
		elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "frappe":
			out.append(Check("condition.dynamic_access", "critical", _("{0} indexes frappe; use plain dotted access").format(where)))
	return out, not any(c.level == "critical" for c in out)


def error_text(exc):
	"""The useful last line of a Frappe render error (``frappe.throw`` puts the traceback in <pre>)."""
	text = frappe.utils.strip_html(cstr(getattr(exc, "message", None) or exc) or type(exc).__name__)
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	return (lines[-1] if lines else type(exc).__name__)[:300]
