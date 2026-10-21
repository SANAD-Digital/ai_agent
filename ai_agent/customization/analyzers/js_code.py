"""Client Script checks through node + acorn (docs/customization/01 §3.3.3).

``analyze(src, view, doctype) -> list[Check]``. node comes from ``site_config.node_path`` or PATH;
acorn from ``apps/frappe/node_modules``. When either is missing the check is critical (fail closed, D7).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import frappe
from frappe import _

from ai_agent.customization.contract import Check

MAX_CHARS = 20000
WARN_LINES = 400
TIMEOUT_SECONDS = 5
CHECKER = os.path.join(os.path.dirname(__file__), "js_check.mjs")

# frappe.call targets that change data directly.
WRITE_METHODS = {
	"frappe.client.delete", "frappe.client.set_value", "frappe.client.submit", "frappe.client.cancel",
	"frappe.client.insert", "frappe.client.insert_many", "frappe.client.save", "frappe.client.bulk_update",
	"frappe.client.rename_doc", "frappe.desk.form.save.savedocs", "frappe.desk.form.save.cancel",
	"frappe.desk.reportview.delete_items", "frappe.model.delete_doc.delete_doc",
}


def node_path():
	"""site_config.node_path, then PATH, then an nvm install of the bench user: supervisord starts
	gunicorn with a system PATH that does not include nvm, where bench usually gets node from."""
	found = frappe.conf.get("node_path") or shutil.which("node")
	if found:
		return found
	import glob

	versions = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")), reverse=True)
	return next((p for p in versions if os.access(p, os.X_OK)), None)


def acorn_path():
	path = os.path.abspath(frappe.get_app_path("frappe", "..", "node_modules", "acorn", "dist", "acorn.js"))
	return path if os.path.exists(path) else None


def child_doctypes(doctype):
	try:
		return [df.options for df in frappe.get_meta(doctype).get_table_fields() if df.options]
	except frappe.DoesNotExistError:
		return []  # an unknown DocType is reported by the checker itself
	except Exception:
		frappe.log_error(frappe.get_traceback(), "ai_agent customize: js check could not read child tables")
		return []


def run_checker(src, view, doctype, children):
	"""Raw checker output, or a dict with ``error`` when node/acorn cannot run."""
	node = node_path()
	if not node or not os.path.exists(node):
		return {"error": _("node was not found (set node_path in site_config.json)")}
	acorn = acorn_path()
	if not acorn:
		return {"error": _("acorn was not found in apps/frappe/node_modules")}
	payload = json.dumps({"source": src, "acorn": acorn, "view": view, "doctype": doctype, "child_doctypes": children})
	try:
		proc = subprocess.run([node, CHECKER], input=payload, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
	except subprocess.TimeoutExpired:
		return {"error": _("the check took longer than {0} seconds").format(TIMEOUT_SECONDS)}
	except OSError as e:
		return {"error": str(e)}
	try:
		out = json.loads(proc.stdout or "")
	except ValueError:
		return {"error": (proc.stderr or proc.stdout or "no output")[:300]}
	if out.get("fatal"):
		return {"error": out["fatal"][:300]}
	return out


def _message(finding):
	code, data = finding.get("code"), finding.get("data") or {}
	if code == "js.syntax":
		return _("JavaScript syntax error: {0}").format(data.get("error"))
	if code == "js.form_target":
		return _("frappe.ui.form.on targets {0}; a Form script for this DocType may only target it or its child tables").format(data.get("target"))
	if code == "js.form_missing":
		return _("A Form script must call frappe.ui.form.on(\"{0}\", {{...}})").format(data.get("doctype"))
	if code == "js.list_target":
		return _("frappe.listview_settings is set for {0}, not for this DocType").format(data.get("target"))
	if code == "js.list_missing":
		return _("A List script must set frappe.listview_settings[\"{0}\"] = {{...}}").format(data.get("doctype"))
	if code == "js.dynamic_code":
		return _("{0} runs text as code and is blocked").format(data.get("what"))
	if code == "js.storage":
		return _("{0} is blocked: it exposes the session or the browser's stored data").format(data.get("what"))
	if code == "js.network":
		url = data.get("url") or _("a computed address")
		return _("{0} to {1} is blocked: only relative /api/ calls are allowed (use frappe.call)").format(data.get("call"), url)
	if code == "js.navigation":
		url = data.get("url") or _("a computed address")
		return _("Sending the user to {0} ({1}) is blocked: only relative addresses are allowed").format(url, data.get("call"))
	if code == "js.html_injection":
		return _("{0} with content that is not fixed text can inject HTML; prefer frappe.utils.escape_html").format(data.get("what"))
	if code == "js.call_dynamic":
		return _("{0} with a computed method name cannot be reviewed").format(data.get("call"))
	return code


def _method_exists(method):
	from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

	if method in (get_server_script_map().get("_api") or {}):
		return True
	try:
		fn = frappe.get_attr(method)
	except Exception:
		return False  # fails closed: a method that cannot be loaded is reported as missing
	return fn in frappe.whitelisted or getattr(fn, "__func__", None) in frappe.whitelisted


def analyze(src, view="Form", doctype=None):
	"""Checks for one Client Script. Critical = blocks the request."""
	src = src or ""
	if not src.strip():
		return [Check("js.empty", "critical", _("The script is empty"))]
	if len(src) > MAX_CHARS:
		return [Check("js.size", "critical", _("The script is longer than {0} characters").format(MAX_CHARS))]
	checks = []
	lines = src.count("\n") + 1
	if lines > WARN_LINES:
		checks.append(Check("js.size", "warning", _("The script has {0} lines; long scripts are hard to review").format(lines)))

	out = run_checker(src, view, doctype, child_doctypes(doctype) if doctype else [])
	if out.get("error"):
		checks.append(Check("js.unavailable", "critical", _("Could not check the JavaScript syntax: {0}").format(out["error"])))
		return checks

	for finding in out.get("findings") or []:
		checks.append(Check(finding.get("code"), finding.get("level") or "critical", _message(finding), finding.get("line")))

	for item in out.get("methods") or []:
		method, line = item.get("method"), item.get("line")
		if method in WRITE_METHODS:
			checks.append(Check("js.call_write", "warning", _("Calls {0}, which changes data on the server").format(method), line))
		elif not _method_exists(method):
			checks.append(Check("js.call_missing", "critical", _("Calls {0}, which does not exist or is not whitelisted").format(method), line))
		else:
			checks.append(Check("js.call", "info", _("Calls the server method {0}").format(method), line))

	if not any(c.level == "critical" for c in checks):
		checks.insert(0, Check("js.syntax", "ok", _("JavaScript syntax is valid")))
	return checks
