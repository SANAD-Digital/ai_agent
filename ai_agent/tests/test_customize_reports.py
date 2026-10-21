"""Report kind: Report Builder and Query Report (docs/customization/01 §3.3.5)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.report import KIND

ADMIN = "Administrator"
NO_READ = "ai-customize-no-read@example.com"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeReports(IntegrationTestCase):
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

	def test_report_builder_preview_create_undo(self):
		name = self.track(f"AI Test Open ToDos {self.suffix}")
		spec = KIND.normalize({"operation": "create", "report_name": name, "ref_doctype": "ToDo",
			"report_type": "Report Builder", "purpose": "open todos",
			"builder": {"fields": [["name", "ToDo"], ["status", "ToDo"], ["priority", "ToDo"]],
				"filters": [["ToDo", "status", "=", "Open"]], "order_by": "`tabToDo`.`modified` desc"}}, ADMIN)
		self.assertEqual(KIND.roles_for(spec), ("System Manager",))
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["rows"]["columns"], ["name", "status", "priority"])
		self.assertIn("builder.preview", codes(report, "ok"))
		json.dumps(report.to_dict())  # stored in the approval payload

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		doc = frappe.get_doc("Report", name)
		self.assertEqual((doc.is_standard, doc.report_type), ("No", "Report Builder"))
		self.assertEqual(result["undo"], {"operation": "disable", "report_name": name})
		self.assertEqual(KIND.snapshot(result["undo"]), KIND.snapshot(spec))
		KIND.execute(result["undo"], ADMIN)
		self.assertEqual(frappe.db.get_value("Report", name, "disabled"), 1)

	def test_builder_unknown_field_is_critical(self):
		spec = KIND.normalize({"operation": "create", "report_name": f"AI Bad {self.suffix}", "ref_doctype": "ToDo",
			"report_type": "Report Builder", "builder": {"fields": [["nope", "ToDo"]], "filters": [["ToDo", "status", "~", 1]]}}, ADMIN)
		found = critical(KIND.check(spec, ADMIN))
		self.assertTrue({"builder.fields", "builder.filters"} <= found, found)

	def test_query_report_create_update_undo(self):
		name = self.track(f"AI Test ToDo by status {self.suffix}")
		args = {"operation": "create", "report_name": name, "ref_doctype": "ToDo", "report_type": "Query Report",
			"query": "select status as `Status:Data:120`, count(*) as `Count:Int:80` from `tabToDo` where creation >= %(from_date)s group by status",
			"filters": [{"fieldname": "from_date", "label": "From", "fieldtype": "Date", "mandatory": 1}],
			"purpose": "todos by status"}
		spec = KIND.normalize(args, ADMIN)
		self.assertEqual(KIND.roles_for(spec), ("System Manager", "Script Manager"))
		self.assertTrue(spec["roles"])  # default = roles that read every table
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("sql.preview", codes(report, "ok"))
		self.assertIn("from_date", report.preview["sample_filters"])
		json.dumps(report.to_dict())

		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		stored = frappe.db.get_value("Report", name, "query")
		self.assertTrue(stored.lower().startswith("select"))  # the provenance comment goes last
		self.assertIn("ai_agent:", stored)

		update = KIND.normalize({"operation": "update", "report_name": name,
			"query": "select status, count(*) as total from `tabToDo` where creation >= %(from_date)s group by status"}, ADMIN)
		self.assertFalse(KIND.check(update, ADMIN).critical())
		result = KIND.execute(update, ADMIN)
		self.assertIn("as total", frappe.db.get_value("Report", name, "query"))
		KIND.execute(result["undo"], ADMIN)
		self.assertIn("Count:Int:80", frappe.db.get_value("Report", name, "query"))

	def test_query_placeholder_without_filter(self):
		spec = KIND.normalize({"operation": "create", "report_name": f"AI Missing {self.suffix}", "ref_doctype": "ToDo",
			"report_type": "Query Report", "query": "select name from `tabToDo` where date >= %(from_date)s"}, ADMIN)
		self.assertIn("sql.placeholder_missing", critical(KIND.check(spec, ADMIN)))

	def test_requester_without_read(self):
		if not frappe.db.exists("User", NO_READ):
			frappe.get_doc({"doctype": "User", "email": NO_READ, "first_name": "No Read", "send_welcome_email": 0,
				"user_type": "Website User"}).insert(ignore_permissions=True)
		spec = KIND.normalize({"operation": "create", "report_name": f"AI NoRead {self.suffix}", "ref_doctype": "Note",
			"report_type": "Query Report", "query": "select name from `tabNote`", "roles": ["System Manager"]}, ADMIN)
		found = critical(KIND.check(spec, NO_READ))
		self.assertIn("sql.table_permission", found)
		self.assertIn("roles.missing", found)

	def test_role_widening_warns(self):
		role = "ai_customize_report_role"
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
		spec = KIND.normalize({"operation": "create", "report_name": f"AI Wide {self.suffix}", "ref_doctype": "ToDo",
			"report_type": "Query Report", "query": "select t.name, n.title from `tabToDo` t left join `tabNote` n on n.name = t.reference_name",
			"roles": [role]}, ADMIN)
		self.assertIn("sql.role_widening", codes(KIND.check(spec, ADMIN), "warning"))

	def test_salary_tables_are_blocked(self):
		if not frappe.db.exists("DocType", "Salary Slip"):
			self.skipTest("HRMS is not installed")
		spec = KIND.normalize({"operation": "create", "report_name": f"AI Pay {self.suffix}", "ref_doctype": "ToDo",
			"report_type": "Query Report", "query": "select employee, net_pay from `tabSalary Slip`", "roles": ["System Manager"]}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertIn("sql.table_blocked", critical(report))
		self.assertNotIn("rows", report.preview)  # nothing was run

	def test_normalize_rules(self):
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "report_name": "x", "ref_doctype": "ToDo", "report_type": "Script Report"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "report_name": "x", "ref_doctype": "ToDo", "report_type": "Query Report",
				"query": "select 1", "filters": [{"fieldname": "a b", "label": "A", "fieldtype": "Data"}]}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "report_name": "No Such Report AI"}, ADMIN)
