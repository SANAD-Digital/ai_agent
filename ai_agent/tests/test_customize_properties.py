"""customize.request_properties kind on ToDo: Customize Form semantics (docs/customization/01 §3.3.2)."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.properties import KIND

DOCTYPE = "ToDo"
ADMIN = "Administrator"


def critical(report):
	return {c.code for c in report.critical()}


def setters():
	return {(p.field_name or "", p.property): p.value for p in frappe.get_all(
		"Property Setter", filters={"doc_type": DOCTYPE}, fields=["field_name", "property", "value"])}


def custom_fields():
	return set(frappe.get_all("Custom Field", filters={"dt": DOCTYPE}, pluck="name"))


class TestCustomizeProperties(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.initial = set(frappe.get_all("Property Setter", filters={"doc_type": DOCTYPE}, pluck="name"))
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user(ADMIN)
		for name in frappe.get_all("Property Setter", filters={"doc_type": DOCTYPE}, pluck="name"):
			if name not in self.initial:
				frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype=DOCTYPE)

	def spec(self, **args):
		return KIND.normalize({"doctype": DOCTYPE, **args}, ADMIN)

	def test_customize_form_refusals_are_critical(self):
		cases = [
			({"description": {"reqd": 0}}, "properties.refused"),  # mandatory standard field
			({"assignment_rule": {"read_only": 0}}, "properties.refused"),  # read-only standard field
			({"date": {"allow_on_submit": 1}}, "properties.refused"),  # allow_on_submit on a standard field
			({"status": {"options": "Open\nDone"}}, "properties.refused"),  # options only for Read Only/HTML/Data
			({"section_break_6": {"in_list_view": 1}}, "properties.refused"),  # not allowed in list view
			({"column_break_2": {"in_list_view": 1}}, "properties.refused"),
			({"description": {"hidden": 1}}, "properties.hidden_reqd"),
		]
		for field_properties, code in cases:
			with self.subTest(field_properties=field_properties):
				self.assertIn(code, critical(KIND.check(self.spec(field_properties=field_properties), ADMIN)))

	def test_check_leaves_no_trace(self):
		before_ps, before_cf = setters(), custom_fields()
		KIND.check(self.spec(field_properties={"description": {"reqd": 0}, "color": {"in_list_view": 1}},
			doctype_properties={"track_views": 1}), ADMIN)
		self.assertEqual(setters(), before_ps)
		self.assertEqual(custom_fields(), before_cf)
		self.assertFalse(frappe.get_meta(DOCTYPE).get_field("color").in_list_view)

	def test_execute_verify_undo(self):
		before_ps = setters()
		spec = self.spec(field_properties={"color": {"in_list_view": 1}, "assigned_by": {"in_standard_filter": 1}},
			doctype_properties={"track_views": 1})
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("color", report.preview["list_columns"])
		self.assertIn("assigned_by", report.preview["standard_filters"])
		self.assertTrue(any(d["field"] == "color" and d["after"] == "1" for d in report.preview["diff"]))

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		meta = frappe.get_meta(DOCTYPE, cached=False)
		self.assertEqual((meta.get_field("color").in_list_view, meta.track_views), (1, 1))
		self.assertEqual(KIND.snapshot(result["undo"]), KIND.snapshot(spec))

		undo = result["undo"]
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		KIND.execute(undo, ADMIN)
		meta = frappe.get_meta(DOCTYPE, cached=False)
		self.assertEqual((meta.get_field("color").in_list_view, meta.track_views), (0, 0))
		self.assertEqual(setters(), before_ps)  # the undo deletes the Property Setters it no longer needs

	def test_references_and_whitelist(self):
		self.assertIn("properties.reference", critical(KIND.check(self.spec(doctype_properties={"title_field": "nope"}), ADMIN)))
		self.assertIn("properties.reference", critical(KIND.check(self.spec(doctype_properties={"search_fields": "description, nope"}), ADMIN)))
		self.assertIn("properties.field", critical(KIND.check(self.spec(field_properties={"nope": {"hidden": 1}}), ADMIN)))
		with self.assertRaises(CustomizationError):
			self.spec(doctype_properties={"istable": 1})
		with self.assertRaises(CustomizationError):
			self.spec(field_properties={"status": {"fieldtype": "Data"}})
		with self.assertRaises(CustomizationError):
			self.spec()

	def test_not_customizable(self):
		self.assertIn("properties.doctype", critical(KIND.check(
			KIND.normalize({"doctype": "User", "doctype_properties": {"track_views": 1}}, ADMIN), ADMIN)))
		self.assertIn("properties.doctype", critical(KIND.check(
			KIND.normalize({"doctype": "System Settings", "doctype_properties": {"track_views": 1}}, ADMIN), ADMIN)))

	def test_field_order_must_be_the_same_set(self):
		self.assertIn("properties.field_order", critical(KIND.check(self.spec(field_order=["status", "date"]), ADMIN)))
