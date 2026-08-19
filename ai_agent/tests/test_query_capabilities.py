import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.query.v16_adapter import normalize_arabic_text
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tools.data import handle_query
from ai_agent.tools.reports import _declared_report_filters, _map_filter_keys


class TestArabicNormalizedLookup(IntegrationTestCase):
	def setUp(self):
		self.marker = f"ar-norm-{frappe.generate_hash(length=8)}"
		self.todo = frappe.get_doc({
			"doctype": "ToDo",
			"description": f"{self.marker} شركة المروة للمقاولات",
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.delete_doc("ToDo", self.todo.name, ignore_permissions=True, force=True)

	def test_normalize_arabic_text(self):
		self.assertEqual(normalize_arabic_text("شركة المروة إلى الأمام"), "شركه المروه الي الامام")

	def test_like_normalized_matches_orthographic_variants(self):
		result = handle_query(
			{
				"doctype": "ToDo",
				"fields": ["name", "description"],
				"filters": [
					{"field": "description", "operator": "like_normalized", "value": f"%{self.marker} شركه المروه%"},
				],
			},
			PermissionContext("Administrator"),
		)
		self.assertNotIn("error", result)
		self.assertEqual(result["row_count"], 1)

	def test_plain_like_misses_the_variant(self):
		result = handle_query(
			{
				"doctype": "ToDo",
				"fields": ["name"],
				"filters": [
					{"field": "description", "operator": "like", "value": f"%{self.marker} شركه المروه%"},
				],
			},
			PermissionContext("Administrator"),
		)
		self.assertEqual(result.get("row_count"), 0)


class TestReportFilterMap(IntegrationTestCase):
	DECLARED = [
		{"fieldname": "from_date", "label": "From Date", "fieldtype": "Date", "reqd": 1},
		{"fieldname": "company", "label": "الشركة", "fieldtype": "Link"},
	]

	def test_exact_fieldnames_pass_through(self):
		mapped, warnings = _map_filter_keys({"from_date": "2026-01-01"}, self.DECLARED)
		self.assertEqual(mapped, {"from_date": "2026-01-01"})
		self.assertEqual(warnings, [])

	def test_label_and_spacing_are_mapped(self):
		mapped, warnings = _map_filter_keys(
			{"From Date": "2026-01-01", "الشركة": "شركة تجربة"}, self.DECLARED
		)
		self.assertEqual(mapped["from_date"], "2026-01-01")
		self.assertEqual(mapped["company"], "شركة تجربة")
		self.assertEqual(len(warnings), 2)

	def test_undeclared_key_is_kept_with_warning(self):
		mapped, warnings = _map_filter_keys({"totally_unknown": 1}, self.DECLARED)
		self.assertEqual(mapped, {"totally_unknown": 1})
		self.assertIn("may be ignored", warnings[0])

	def test_no_declared_filters_passes_everything_silently(self):
		mapped, warnings = _map_filter_keys({"anything": 1}, [])
		self.assertEqual(mapped, {"anything": 1})
		self.assertEqual(warnings, [])

	def test_declared_filters_from_json_dict_shape(self):
		report = frappe._dict(
			filters_json="",
			json='{"filters": [{"fieldname": "status", "label": "Status"}], "columns": []}',
			filters=None,
		)
		declared = _declared_report_filters(report)
		self.assertEqual(declared, [{"fieldname": "status", "label": "Status"}])
