"""customize.request_field kind on ToDo: add, update, remove, compensation, undo (docs/customization/01 §3.3.1).

Custom Field DDL commits implicitly, so IntegrationTestCase cannot roll it back: every test removes its
fields and drops their columns explicitly.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.custom_field import KIND

DOCTYPE = "ToDo"
ADMIN = "Administrator"


def critical(report):
	return {c.code for c in report.critical()}


class TestCustomizeFields(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6).lower()

	def name(self, base):
		fieldname = f"custom_ait_{base}_{self.suffix}"
		self.addCleanup(self.drop, fieldname)
		return fieldname

	def drop(self, fieldname):
		frappe.set_user(ADMIN)
		for name in frappe.get_all("Custom Field", filters={"dt": DOCTYPE, "fieldname": fieldname}, pluck="name"):
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		if frappe.db.has_column(DOCTYPE, fieldname):
			frappe.db.sql_ddl(f"alter table `tab{DOCTYPE}` drop column `{fieldname}`")
		frappe.clear_cache(doctype=DOCTYPE)

	def spec(self, operation, fields):
		return KIND.normalize({"doctype": DOCTYPE, "operation": operation, "fields": fields, "reason": "test"}, ADMIN)

	def test_add_check_execute_verify_undo(self):
		rank, note = self.name("rank"), self.name("note")
		spec = self.spec("add", [
			{"fieldname": rank.removeprefix("custom_"), "label": "ترتيب", "fieldtype": "Select", "options": "A\nB\nC", "insert_after": "status"},
			{"fieldname": note, "label": "Note", "fieldtype": "Small Text"},
		])
		self.assertEqual([f["fieldname"] for f in spec["fields"]], [rank, note])
		before = KIND.snapshot(spec)
		self.assertEqual(before["fields"], {rank: None, note: None})
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["layout"][0]["after"], "status")

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		self.assertTrue(frappe.db.has_column(DOCTYPE, rank))
		self.assertEqual(frappe.get_meta(DOCTYPE, cached=False).get_field(rank).insert_after, "status")
		self.assertEqual(KIND.snapshot(result["undo"]), KIND.snapshot(spec))  # undo compares the same target

		undo = result["undo"]
		self.assertEqual(undo["operation"], "remove")
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		KIND.execute(undo, ADMIN)
		self.assertIsNone(frappe.get_meta(DOCTYPE, cached=False).get_field(rank))

	def test_update_and_undo(self):
		field = self.name("upd")
		KIND.execute(self.spec("add", [{"fieldname": field, "label": "Old", "fieldtype": "Data"}]), ADMIN)
		spec = self.spec("update", [{"fieldname": field, "label": "New", "bold": 1}])
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical())
		self.assertIn({"field": field, "label": "label", "before": "Old", "after": "New"}, report.preview["diff"])
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		self.assertEqual(frappe.get_meta(DOCTYPE, cached=False).get_field(field).label, "New")
		KIND.execute(result["undo"], ADMIN)
		df = frappe.get_meta(DOCTYPE, cached=False).get_field(field)
		self.assertEqual((df.label, df.bold), ("Old", 0))

	def test_fieldtype_change_outside_groups_is_critical(self):
		field = self.name("ft")
		KIND.execute(self.spec("add", [{"fieldname": field, "label": "Amount", "fieldtype": "Float"}]), ADMIN)
		self.assertIn("field.fieldtype_change", critical(KIND.check(self.spec("update", [{"fieldname": field, "fieldtype": "Date"}]), ADMIN)))
		self.assertFalse(KIND.check(self.spec("update", [{"fieldname": field, "fieldtype": "Currency"}]), ADMIN).critical())

	def test_compensation_when_second_field_fails(self):
		good, bad = self.name("good"), self.name("bad")
		spec = {"doctype": DOCTYPE, "operation": "add", "fields": [
			{"fieldname": good, "label": "Good", "fieldtype": "Data"},
			{"fieldname": bad, "label": "Bad", "fieldtype": "Link", "options": "No Such DocType AI"},
		]}
		with self.assertRaises(frappe.ValidationError) as ctx:
			KIND.execute(spec, ADMIN)
		self.assertEqual(len(ctx.exception.created_before_compensation), 1)
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": DOCTYPE, "fieldname": good}))
		self.assertIn("field.options", critical(KIND.check(spec, ADMIN)))  # the check would have caught it

	def test_arabic_label_without_fieldname_is_rejected(self):
		with self.assertRaises(CustomizationError):
			self.spec("add", [{"label": "اسم العميل", "fieldtype": "Data"}])
		with self.assertRaises(CustomizationError):
			self.spec("add", [{"fieldname": "اسم", "label": "x", "fieldtype": "Data"}])

	def test_unknown_keys_are_rejected_not_dropped(self):
		with self.assertRaises(CustomizationError):
			self.spec("add", [{"fieldname": "x_y", "label": "X", "fieldtype": "Data", "code": "1"}])
		with self.assertRaises(CustomizationError):
			KIND.normalize({"doctype": DOCTYPE, "operation": "add", "fields": [{"label": "X"}], "system_engine": 1}, ADMIN)

	def test_checks_catch_bad_definitions(self):
		cases = [
			({"fieldname": "status", "label": "Status", "fieldtype": "Data"}, None),  # becomes custom_status: fine
			({"fieldname": self.name("lnk"), "label": "L", "fieldtype": "Link", "options": "ToDo Missing"}, "field.options"),
			({"fieldname": self.name("sel"), "label": "S", "fieldtype": "Select"}, "field.options"),
			({"fieldname": self.name("aft"), "label": "A", "fieldtype": "Data", "insert_after": "nope"}, "field.insert_after"),
			({"fieldname": self.name("fch"), "label": "F", "fieldtype": "Data", "fetch_from": "status.name"}, "field.fetch_from"),
			({"fieldname": self.name("hid"), "label": "H", "fieldtype": "Data", "hidden": 1, "reqd": 1}, "field.frappe"),
		]
		for field, code in cases:
			with self.subTest(field=field):
				found = critical(KIND.check(self.spec("add", [field]), ADMIN))
				if code:
					self.assertIn(code, found)
				else:
					self.assertFalse(found)

	def test_duplicate_name_and_core_doctype(self):
		self.assertIn("field.name_conflict", critical(KIND.check(
			{"doctype": DOCTYPE, "operation": "add", "fields": [{"fieldname": "status", "label": "S", "fieldtype": "Data"}]}, ADMIN)))
		self.assertIn("field.doctype", critical(KIND.check(
			{"doctype": "User", "operation": "add", "fields": [{"fieldname": "custom_x", "label": "X", "fieldtype": "Data"}]}, ADMIN)))

	def test_remove_admin_field_needs_administrator(self):
		field = self.name("adm")
		KIND.execute(self.spec("add", [{"fieldname": field, "label": "Adm", "fieldtype": "Data"}]), ADMIN)
		spec = self.spec("remove", [{"fieldname": field}])
		self.assertIn("field.admin_owned", critical(KIND.check(spec, "someone-else@example.com")))
		self.assertNotIn("field.admin_owned", critical(KIND.check(spec, ADMIN)))
		result = KIND.execute(spec, ADMIN)
		self.assertEqual(result["undo"]["operation"], "add")
		self.assertEqual(result["undo"]["fields"][0]["fieldname"], field)
