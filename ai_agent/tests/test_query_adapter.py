import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.query.v16_adapter import V16Adapter
from ai_agent.runtime.permission_context import PermissionContext


class TestQueryAdapter(IntegrationTestCase):
	def setUp(self):
		self.adapter = V16Adapter()
		self.ctx = PermissionContext("Administrator")

	def _tree_parent_with_children(self):
		for parent in frappe.get_all("Account", filters={"is_group": 1}, pluck="name", limit=20):
			if frappe.db.exists("Account", {"parent_account": parent}):
				return parent
		return None

	def test_descendants_of_filters_correctly(self):
		# Regression: the operator used to be silently dropped, returning
		# unfiltered rows presented as filtered.
		parent = self._tree_parent_with_children()
		if not parent:
			self.skipTest("No Account tree data on this site")
		result = self.adapter.execute(
			{
				"doctype": "Account",
				"fields": ["name"],
				"filters": [{"field": "name", "operator": "descendants of", "value": parent}],
				"limit": 500,
			},
			self.ctx,
		)
		got = {row["name"] for row in result["rows"]}
		expected = set(
			frappe.get_all(
				"Account",
				filters={"name": ["descendants of", parent]},
				pluck="name",
				limit_page_length=0,
			)
		)
		self.assertTrue(got)
		self.assertEqual(got, expected)
		self.assertNotIn(parent, got)

	def test_descendants_of_rejects_non_tree_doctype(self):
		with self.assertRaises(ValueError):
			self.adapter.execute(
				{
					"doctype": "ToDo",
					"fields": ["name"],
					"filters": [{"field": "name", "operator": "descendants of", "value": "X"}],
					"limit": 10,
				},
				self.ctx,
			)

	def test_unknown_operator_raises_instead_of_silent_drop(self):
		with self.assertRaises(ValueError):
			self.adapter.execute(
				{
					"doctype": "ToDo",
					"fields": ["name"],
					"filters": [{"field": "status", "operator": "made-up-op", "value": "Open"}],
					"limit": 10,
				},
				self.ctx,
			)

	def test_grouped_query_still_executes(self):
		result = self.adapter.execute(
			{
				"doctype": "Account",
				"fields": [
					"root_type",
					{"field": "name", "aggregate": "COUNT", "alias": "total"},
				],
				"filters": [],
				"group_by": ["root_type"],
				"limit": 50,
			},
			self.ctx,
		)
		self.assertIsInstance(result["rows"], list)
		if result["rows"]:
			self.assertIn("total", result["rows"][0])
