"""DocType kind: new custom DocTypes (docs/customization/01 §3.1 v3).

Each test makes DocTypes named ``Zz DT Test <hash> …`` and removes them (and their tables) afterwards.
"""

import json
import os

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.doctype import KIND, definition

ADMIN = "Administrator"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


FIELDS = [
	{"label": "Customer", "fieldname": "customer_name", "fieldtype": "Data", "reqd": 1},
	{"label": "Visit Date", "fieldtype": "Date", "default": "Today"},
	{"fieldtype": "Column Break"},
	{"label": "Status", "fieldtype": "Select", "options": "Planned\nDone"},
	{"label": "More", "fieldtype": "Section Break"},
	{"label": "ملاحظات", "fieldname": "notes", "fieldtype": "Small Text"},
]


class TestCustomizeDocType(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.prefix = f"Zz DT Test {frappe.generate_hash(length=6)}"
		self.made = []
		self.addCleanup(self.cleanup)

	def name(self, suffix="Visit"):
		name = f"{self.prefix} {suffix}"
		self.made.append(name)
		return name

	def cleanup(self):
		frappe.set_user(ADMIN)
		for name in reversed(self.made):
			if frappe.db.exists("DocType", name):
				if frappe.db.table_exists(name):
					frappe.db.delete(name)
				frappe.delete_doc("DocType", name, force=True, ignore_permissions=True)
			frappe.db.sql_ddl(f"drop table if exists `tab{name}`")
		frappe.db.commit()

	def create_args(self, name, **extra):
		return {"operation": "create", "name": name, "fields": [dict(f) for f in FIELDS], "title_field": "customer_name",
			"search_fields": ["customer_name", "status"], "purpose": "track site visits", **extra}

	# -- normalize -------------------------------------------------------------------
	def test_normalize(self):
		name = self.name()
		spec = KIND.normalize(self.create_args(name), ADMIN)
		self.assertEqual(spec["module"], "Custom")
		self.assertEqual([f["fieldname"] for f in spec["fields"]][:2], ["customer_name", "visit_date"])
		self.assertTrue(spec["fields"][2]["fieldname"].startswith("column_break_"))
		self.assertEqual(spec["properties"]["search_fields"], "customer_name,status")
		self.assertEqual((spec["properties"]["autoname"], spec["properties"]["naming_rule"]), ("hash", "Random"))
		self.assertEqual(spec["permissions"][0]["role"], "System Manager")  # always at least System Manager
		self.assertEqual(KIND.normalize(self.create_args(name, autoname="field:customer_name"), ADMIN)["properties"]["naming_rule"], "By fieldname")
		self.assertEqual(KIND.normalize(self.create_args(name, autoname="VST-.YYYY.-.#####"), ADMIN)["properties"]["naming_rule"], "Expression")

		bad = [
			{**self.create_args(name), "colour": "red"},
			{**self.create_args("زيارة")},  # not an English name
			{**self.create_args("1 Visit")},
			{**self.create_args(name), "fields": [{"label": "X", "fieldtype": "Barcode"}]},
			{**self.create_args(name), "fields": [{"label": "ملاحظات", "fieldtype": "Data"}]},  # no fieldname, Arabic label
			{**self.create_args(name), "fields": [{"label": "A", "fieldname": "a_b"}, {"label": "B", "fieldname": "a_b"}]},
			{**self.create_args(name), "fields": [{"label": "A", "fieldname": "Bad Name"}]},
			{**self.create_args(name), "autoname": "whatever"},
			{**self.create_args(name), "istable": 1, "permissions": [{"role": "System Manager", "rights": ["read"]}]},
			{**self.create_args(name), "permissions": [{"role": "No Such Role zz", "rights": ["read"]}]},
			{**self.create_args(name), "module": "No Such Module zz"},
			{"operation": "rename", "name": name},
		]
		for args in bad:
			with self.assertRaises(CustomizationError, msg=args):
				KIND.normalize(args, ADMIN)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "update", "name": "ToDo", "fields": [{"label": "X"}]}, ADMIN)
		self.assertIn("customize.request_field", " ".join(ctx.exception.hints))

	# -- checks --------------------------------------------------------------------------
	def test_criticals(self):
		def found(**override):
			args = {**self.create_args(self.name("Bad")), **override}
			return critical(KIND.check(KIND.normalize(args, ADMIN), ADMIN))

		self.assertIn("dt.name_taken", found(name="ToDo"))
		self.assertIn("dt.name_reserved", found(name="Report"))
		self.assertIn("dt.name_reserved", found(name="AI Visit Helper"))
		self.assertIn("dt.route_conflict", found(name="todo"))
		self.assertIn("field.name_reserved", found(fields=[{"label": "Owner", "fieldname": "owner"}]))
		self.assertIn("field.name_reserved", found(fields=[{"label": "Save", "fieldname": "save"}]))
		self.assertIn("field.options", found(fields=[{"label": "C", "fieldname": "cc", "fieldtype": "Link", "options": "No Such DT zz"}]))
		self.assertIn("field.options", found(fields=[{"label": "T", "fieldname": "tt", "fieldtype": "Table", "options": "ToDo"}]))
		self.assertIn("dt.title_field", found(title_field="nope"))
		self.assertIn("dt.search_fields", found(search_fields="nope"))
		self.assertIn("dt.naming", found(autoname="field:nope"))
		self.assertIn("dt.naming", found(autoname="naming_series:"))
		self.assertIn("dt.child_submittable", found(istable=1, is_submittable=1))
		self.assertIn("perm.role_automatic", found(permissions=[{"role": "Guest", "rights": ["read"]}]))
		self.assertIn("perm.role_desk_user", found(permissions=[{"role": "Desk User", "rights": ["read", "write"]}]))
		self.assertIn("dt.frappe", found(fields=[{"label": "S", "fieldname": "ss", "fieldtype": "Data", "reqd": 1, "hidden": 1}]))

	def test_warnings_and_preview(self):
		name = self.name()
		spec = KIND.normalize(self.create_args(name, autoname="Prompt",
			permissions=[{"role": "Sales User", "rights": ["read", "write", "create", "delete"]}]), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("dt.naming", codes(report, "warning"))
		self.assertIn("perm.risky_right", codes(report, "warning"))
		if frappe.conf.developer_mode:
			self.assertIn("dt.not_exported", codes(report, "info"))
		layout = report.preview["layout"]
		self.assertEqual([s["label"] for s in layout[0]["sections"]], ["", "More"])
		self.assertEqual([[f["fieldname"] for f in col] for col in layout[0]["sections"][0]["columns"]],
			[["customer_name", "visit_date"], ["status"]])
		self.assertEqual(report.preview["table"], f"tab{name}")
		self.assertTrue(report.impact)
		json.dumps(report.to_dict())
		self.assertFalse(frappe.db.exists("DocType", name))  # the check wrote nothing
		self.assertFalse(frappe.db.table_exists(name))

	# -- execute / verify / undo -------------------------------------------------------------
	def test_create_update_undo_round_trip(self):
		name = self.name()
		spec = KIND.normalize(self.create_args(name), ADMIN)
		self.assertEqual(KIND.snapshot(spec), {"name": name, "definition": None})
		result = KIND.execute(spec, ADMIN)
		checks = KIND.verify(spec, result)
		self.assertFalse([c for c in checks if c.level != "ok"], [c.message for c in checks])
		self.assertEqual(result["undo"], {"operation": "delete", "name": name})
		meta = frappe.get_meta(name)
		self.assertEqual((meta.custom, meta.module, meta.title_field), (1, "Custom", "customer_name"))
		path = frappe.get_module_path("Custom", "doctype", frappe.scrub(name))
		self.assertFalse(os.path.exists(path))  # custom = 1: never exported

		# update: add a field after visit_date and relabel status
		update = KIND.normalize({"operation": "update", "name": name, "track_changes": 0, "fields": [
			{"label": "Visit Time", "fieldtype": "Time", "insert_after": "visit_date"},
			{"fieldname": "status", "label": "Visit Status"}]}, ADMIN)
		before = KIND.snapshot(update)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		changes = {(d.get("field"), d.get("change")) for d in report.preview["diff"]}
		self.assertIn(("visit_time", "added"), changes)
		self.assertIn(("status", "changed"), changes)
		result = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, result) if c.level == "critical"])
		order = [df.fieldname for df in frappe.get_meta(name, cached=False).fields]
		self.assertEqual(order.index("visit_time"), order.index("visit_date") + 1)
		self.assertEqual(result["undo"]["operation"], "restore")

		# undo of the update brings the definition back
		undo = KIND.normalize(result["undo"], ADMIN)
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		KIND.execute(undo, ADMIN)
		self.assertEqual(KIND.snapshot(update), before)

		# a record blocks deleting; the undo of the create deletes the empty DocType and its table
		frappe.get_doc({"doctype": name, "customer_name": "Zz"}).insert(ignore_permissions=True)
		delete = KIND.normalize({"operation": "delete", "name": name}, ADMIN)
		self.assertIn("dt.has_records", critical(KIND.check(delete, ADMIN)))
		frappe.db.delete(name)
		self.assertFalse(KIND.check(delete, ADMIN).critical())
		result = KIND.execute(delete, ADMIN)
		self.assertFalse([c for c in KIND.verify(delete, result) if c.level != "ok"])
		self.assertFalse(frappe.db.table_exists(name))
		self.assertEqual(result["undo"]["operation"], "create")

	def test_child_table_and_parent(self):
		child = self.name("Item")
		parent = self.name("Order")
		spec = KIND.normalize({"operation": "create", "name": child, "istable": 1,
			"fields": [{"label": "Item", "fieldname": "item", "fieldtype": "Data", "in_list_view": 1}, {"label": "Qty", "fieldtype": "Int"}]}, ADMIN)
		self.assertEqual(spec["permissions"], [])
		self.assertFalse(KIND.check(spec, ADMIN).critical())
		KIND.execute(spec, ADMIN)
		parent_spec = KIND.normalize({"operation": "create", "name": parent, "is_submittable": 1, "fields": [
			{"label": "Customer", "fieldname": "customer_name"},
			{"label": "Items", "fieldname": "items", "fieldtype": "Table", "options": child}]}, ADMIN)
		report = KIND.check(parent_spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		KIND.execute(parent_spec, ADMIN)
		self.assertTrue(frappe.get_meta(parent).is_submittable)
		self.assertIn("amended_from", [df.fieldname for df in frappe.get_meta(parent).fields])
		# the child is used by the parent: it cannot be deleted
		self.assertIn("dt.referenced", critical(KIND.check(KIND.normalize({"operation": "delete", "name": child}, ADMIN), ADMIN)))

	def test_requester_without_role_and_summary(self):
		spec = KIND.normalize(self.create_args(self.name()), ADMIN)
		self.assertIn("roles.missing", critical(KIND.check(spec, "Guest")))
		self.assertIn(spec["name"], KIND.summary(spec))
		self.assertIsNone(definition(spec["name"]))
