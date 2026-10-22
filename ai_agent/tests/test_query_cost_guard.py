"""QueryCostGuard: a large or child-table query without a narrowing filter asks for one; a big limit or a wide
group-by is lowered; anything narrowed runs as asked."""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from ai_agent.query.cost_guard import QueryCostGuard


class TestQueryCostGuard(IntegrationTestCase):
	def setUp(self):
		self.guard = QueryCostGuard()

	def evaluate(self, high_volume=False, **spec):
		with patch.object(QueryCostGuard, "_is_high_volume", return_value=high_volume):
			return self.guard.evaluate(spec, None)

	def test_small_doctype_runs_as_asked(self):
		self.assertEqual(self.evaluate(doctype="Note", limit=20), "allow")

	def test_large_doctype_without_a_filter_asks_for_one(self):
		self.assertEqual(self.evaluate(True, doctype="Sales Invoice", limit=20), "require_more_filters")

	def test_date_filter_narrows_a_large_doctype(self):
		spec = {"doctype": "Sales Invoice", "limit": 20, "filters": [{"field": "posting_date", "operator": ">=", "value": "2026-01-01"}]}
		self.assertEqual(self.evaluate(True, **spec), "allow")

	def test_a_non_date_field_is_not_a_date_filter(self):
		spec = {"doctype": "Sales Invoice", "limit": 20, "filters": [{"field": "remarks", "operator": "=", "value": "x"}]}
		self.assertEqual(self.evaluate(True, **spec), "require_more_filters")

	def test_child_table_scoped_to_one_parent_runs(self):
		spec = {"doctype": "Sales Invoice Item", "limit": 20, "filters": [{"field": "parent", "operator": "=", "value": "SINV-1"}]}
		self.assertEqual(self.evaluate(True, **spec), "allow")

	def test_child_table_filtered_on_the_parent_date_runs(self):
		spec = {"doctype": "Sales Invoice Item", "limit": 20, "filters": [
			{"field": "parenttype", "operator": "=", "value": "Sales Invoice"},
			{"field": "parent.posting_date", "operator": ">=", "value": "2026-01-01"},
		]}
		self.assertEqual(self.evaluate(True, **spec), "allow")

	def test_two_selective_equality_filters_narrow(self):
		spec = {"doctype": "Sales Invoice", "limit": 20, "filters": [
			{"field": "customer", "operator": "=", "value": "C1"},
			{"field": "company", "operator": "=", "value": "Co"},
		]}
		self.assertEqual(self.evaluate(True, **spec), "allow")

	def test_one_selective_filter_is_not_enough(self):
		spec = {"doctype": "Sales Invoice", "limit": 20, "filters": [{"field": "customer", "operator": "=", "value": "C1"}]}
		self.assertEqual(self.evaluate(True, **spec), "require_more_filters")

	def test_short_text_search_runs(self):
		spec = {"doctype": "Sales Invoice", "limit": 20, "filters": [{"field": "name", "operator": "like", "value": "%SINV-00%"}]}
		self.assertEqual(self.evaluate(True, **spec), "allow")
		spec["limit"] = 100
		self.assertEqual(self.evaluate(True, **spec), "require_more_filters", "a text search counts only with a small limit")

	def test_aggregate_on_a_large_doctype_is_lowered_not_refused(self):
		spec = {"doctype": "Sales Invoice", "limit": 500, "fields": [{"field": "grand_total", "aggregate": "sum"}]}
		self.assertEqual(self.evaluate(True, **spec), "allow_with_lower_limit")

	def test_big_limit_on_a_large_doctype_is_lowered(self):
		spec = {"doctype": "Sales Invoice", "limit": 500, "filters": [{"field": "posting_date", "operator": ">=", "value": "2026-01-01"}]}
		self.assertEqual(self.evaluate(True, **spec), "allow_with_lower_limit")

	def test_wide_group_by_is_lowered(self):
		self.assertEqual(self.evaluate(doctype="Note", limit=20, group_by=["a", "b", "c", "d"]), "allow_with_lower_limit")

	def test_child_tables_count_as_large(self):
		self.assertTrue(QueryCostGuard()._is_high_volume("Sales Invoice Item"))
		self.assertFalse(QueryCostGuard()._is_high_volume("No Such DocType"), "an unknown DocType fails in the query itself")
