"""List settings kind: List View Settings + in_list_view / sort Property Setters (customize.request_list_settings)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.list_settings import KIND, columns_of, state

ADMIN = "Administrator"
DOCTYPE = "Note"  # title field "title"; fields public, notify_on_login, expire_notification_on, content, seen_by (Table)


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeListSettings(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		initial = state(DOCTYPE)

		def restore():
			frappe.set_user(ADMIN)
			now = state(DOCTYPE)
			KIND._apply(DOCTYPE, {"record": initial["record"],
				"in_list_view": {f: initial["in_list_view"].get(f) for f in set(now["in_list_view"]) | set(initial["in_list_view"])},
				"sort": dict(initial["sort"])})

		self.addCleanup(restore)
		self.initial = initial

	def test_normalize_rules(self):
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": DOCTYPE, "group_by": ["owner"]}, ADMIN)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "group_by", "doctype": DOCTYPE}, ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": "No Such DocType X"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": DOCTYPE}, ADMIN)  # nothing to change
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": DOCTYPE, "columns": ["Expire Notification On"]}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": DOCTYPE, "settings": {"disable_everything": 1}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "doctype": DOCTYPE, "sort_order": "up"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "reset", "doctype": DOCTYPE, "columns": ["public"]}, ADMIN)
		spec = KIND.normalize({"operation": "update", "doctype": DOCTYPE, "settings": {"disable_count": "1"}, "sort_order": "asc"}, ADMIN)
		self.assertEqual((spec["settings"], spec["sort_order"], spec["name"]), ({"disable_count": 1}, "ASC", DOCTYPE))

	def test_update_round_trip(self):
		spec = KIND.normalize({"operation": "update", "doctype": DOCTYPE, "purpose": "test",
			"columns": ["expire_notification_on", "public"], "settings": {"disable_count": 1, "disable_comment_count": 1},
			"sort_field": "modified", "sort_order": "ASC"}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("list_settings.frappe", codes(report, "ok"))
		self.assertEqual(report.preview["columns_after"], ["Title", "Expire Notification On", "Public"])
		self.assertIn("disable_count", [d["label"] for d in report.preview["diff"]])
		json.dumps(report.to_dict())
		self.assertEqual(state(DOCTYPE), self.initial, "the check must not write")

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		doc = frappe.get_doc("List View Settings", DOCTYPE)
		self.assertEqual((doc.disable_count, doc.disable_comment_count), (1, 1))
		self.assertEqual([f["fieldname"] for f in json.loads(doc.fields)], ["title", "expire_notification_on", "public"])
		meta = frappe.get_meta(DOCTYPE, cached=False)
		self.assertEqual((meta.sort_field, meta.sort_order), ("modified", "ASC"))
		self.assertEqual(meta.get_field("expire_notification_on").in_list_view, 1)
		self.assertEqual(columns_of(DOCTYPE), ["title", "expire_notification_on", "public"])

		undo = result["undo"]
		self.assertEqual(undo["operation"], "restore")
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		undone = KIND.execute(undo, ADMIN)
		self.assertFalse([c for c in KIND.verify(undo, undone) if c.level == "critical"])
		self.assertEqual(state(DOCTYPE), self.initial)

	def test_columns_removed_lose_in_list_view(self):
		KIND.execute(KIND.normalize({"operation": "update", "doctype": DOCTYPE, "columns": ["public", "notify_on_login"]}, ADMIN), ADMIN)
		spec = KIND.normalize({"operation": "update", "doctype": DOCTYPE, "columns": ["public"]}, ADMIN)
		plan = KIND.plan(spec)
		self.assertEqual(plan["in_list_view"].get("notify_on_login"), "0")  # like Frappe's removed_listview_fields
		self.assertEqual(plan["in_list_view"].get("public"), "1")
		KIND.execute(spec, ADMIN)
		self.assertEqual(frappe.get_meta(DOCTYPE, cached=False).get_field("notify_on_login").in_list_view, 0)

	def test_reset_and_undo(self):
		KIND.execute(KIND.normalize({"operation": "update", "doctype": DOCTYPE, "settings": {"show_tags": 1}}, ADMIN), ADMIN)
		before = state(DOCTYPE)
		spec = KIND.normalize({"operation": "reset", "doctype": DOCTYPE}, ADMIN)
		self.assertFalse(KIND.check(spec, ADMIN).critical())
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		self.assertFalse(frappe.db.exists("List View Settings", DOCTYPE))
		KIND.execute(result["undo"], ADMIN)
		self.assertEqual(state(DOCTYPE), before)
		self.assertEqual(frappe.db.get_value("List View Settings", DOCTYPE, "show_tags"), 1)

	def test_checks(self):
		if frappe.db.exists("List View Settings", DOCTYPE):
			frappe.delete_doc("List View Settings", DOCTYPE, ignore_permissions=True)
		reset = KIND.normalize({"operation": "reset", "doctype": DOCTYPE}, ADMIN)
		self.assertIn("list_settings.reset", critical(KIND.check(reset, ADMIN)))
		spec = KIND.normalize({"operation": "update", "doctype": DOCTYPE, "columns": ["nope", "seen_by", "seen_by_section", "title"],
			"sort_field": "nothing_here"}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertTrue({"list_settings.column", "list_settings.sort"} <= critical(report), critical(report))
		self.assertIn("list_settings.column", codes(report, "info"))  # the title column is always first
		self.assertEqual(len([c for c in report.critical() if c.code == "list_settings.column"]), 3)
		wide = KIND.normalize({"operation": "update", "doctype": "ToDo", "columns": [df.fieldname for df in frappe.get_meta("ToDo").fields
			if df.fieldtype in ("Data", "Select", "Link", "Date", "Check")][:9]}, ADMIN)
		if len(wide["columns"]) > 8:
			self.assertIn("list_settings.columns_many", codes(KIND.check(wide, ADMIN), "warning"))
		for doctype in ("System Settings", "Note Seen By", "User"):
			spec = KIND.normalize({"operation": "update", "doctype": doctype, "settings": {"disable_count": 1}}, ADMIN)
			self.assertIn("list_settings.doctype", critical(KIND.check(spec, ADMIN)), doctype)
