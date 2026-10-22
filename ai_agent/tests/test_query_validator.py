import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.query.validator import QueryValidator
from ai_agent.runtime.exceptions import QueryValidationError
from ai_agent.runtime.permission_context import PermissionContext


class TestQueryValidator(IntegrationTestCase):
	def setUp(self):
		self.validator = QueryValidator(PermissionContext("Administrator"))

	def test_valid_spec_passes_and_clamps_limit(self):
		spec = self.validator.validate({
			"doctype": "ToDo",
			"fields": ["name", "status"],
			"filters": [{"field": "status", "operator": "=", "value": "Open"}],
			"limit": 9999,
		})
		self.assertEqual(spec["limit"], 500)

	def test_missing_doctype_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"fields": ["name"]})

	def test_unknown_doctype_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "No Such DocType", "fields": ["name"]})

	def test_wildcard_field_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "ToDo", "fields": ["*"]})

	def test_unknown_field_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "ToDo", "fields": ["no_such_field"]})

	def test_disallowed_operator_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({
				"doctype": "ToDo",
				"fields": ["name"],
				"filters": [{"field": "status", "operator": "regexp", "value": ".*"}],
			})

	def test_too_many_fields_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "ToDo", "fields": ["name"] * 51})

	def test_negative_offset_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "ToDo", "fields": ["name"], "offset": -1})

	def test_field_object_without_field_name_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({"doctype": "ToDo", "fields": [{"$text": "name"}]})

	def test_malformed_between_value_is_rejected(self):
		# A wrapped value used to be dropped silently and return a wrong count.
		with self.assertRaises(QueryValidationError):
			self.validator.validate({
				"doctype": "ToDo",
				"fields": ["name"],
				"filters": [{"field": "date", "operator": "between", "value": {"item": ["2026-01-01", "2026-12-31"]}}],
			})

	def test_group_by_object_without_bucket_is_plain_field(self):
		spec = self.validator.validate({
			"doctype": "ToDo",
			"fields": ["status", {"field": "name", "aggregate": "COUNT", "alias": "n"}],
			"group_by": [{"field": "status"}],
		})
		self.assertEqual(spec["group_by"], ["status"])


class TestDateBucketGroupBy(IntegrationTestCase):
	def setUp(self):
		import frappe

		self.validator = QueryValidator(PermissionContext("Administrator"))
		self.marker = f"date-bucket-{frappe.generate_hash(length=8)}"
		self._todos = []
		for date in ("2026-01-15", "2026-01-20", "2026-02-01"):
			todo = frappe.get_doc({
				"doctype": "ToDo",
				"description": f"{self.marker} on {date}",
				"date": date,
			}).insert(ignore_permissions=True)
			self._todos.append(todo.name)

	def tearDown(self):
		import frappe

		for name in self._todos:
			frappe.delete_doc("ToDo", name, ignore_permissions=True, force=True)

	def test_monthly_bucket_groups_rows(self):
		from ai_agent.tools.data import handle_query

		result = handle_query(
			{
				"doctype": "ToDo",
				"fields": [{"field": "name", "aggregate": "COUNT", "alias": "total"}],
				"filters": [{"field": "description", "operator": "like", "value": f"%{self.marker}%"}],
				"group_by": [{"field": "date", "bucket": "month", "alias": "month"}],
				"order_by": [{"field": "month", "direction": "asc"}],
			},
			PermissionContext("Administrator"),
		)
		self.assertNotIn("error", result)
		buckets = {row["month"]: row["total"] for row in result["rows"]}
		self.assertEqual(buckets.get("2026-01"), 2)
		self.assertEqual(buckets.get("2026-02"), 1)

	def test_invalid_bucket_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({
				"doctype": "ToDo",
				"fields": ["name"],
				"group_by": [{"field": "date", "bucket": "decade"}],
			})

	def test_bucket_on_non_date_field_is_rejected(self):
		with self.assertRaises(QueryValidationError):
			self.validator.validate({
				"doctype": "ToDo",
				"fields": ["name"],
				"group_by": [{"field": "status", "bucket": "month"}],
			})


class TestProtectedFieldsAndChildTables(IntegrationTestCase):
	"""Wave 1 security: masking keys on output names, so protected fields are only read as is."""

	def setUp(self):
		self.validator = QueryValidator(PermissionContext("Administrator"))
		if "child_table" not in self._testMethodName and not frappe.db.exists("DocType", "Salary Slip"):
			self.skipTest("HRMS is not installed: the pay document these cases use does not exist")

	def _refused(self, spec, text):
		with self.assertRaises(QueryValidationError) as cm:
			self.validator.validate(spec)
		self.assertIn(text, str(cm.exception))

	def test_renaming_or_aggregating_a_salary_field_is_refused(self):
		self._refused({"doctype": "Salary Slip", "fields": [{"field": "net_pay", "alias": "x"}]}, "protected field")
		self._refused({"doctype": "Salary Slip", "fields": [{"field": "net_pay", "aggregate": "sum", "alias": "total"}]}, "protected field")

	def test_reserved_alias_in_a_pay_document_is_refused(self):
		self._refused({"doctype": "Salary Slip", "fields": [{"field": "payment_days", "aggregate": "sum", "alias": "count"}]}, "reserved")

	def test_filter_group_and_sort_on_a_protected_field_are_refused(self):
		self._refused({"doctype": "Salary Slip", "fields": ["name"], "filters": [{"field": "net_pay", "operator": ">", "value": 1000}]}, "protected field")
		self._refused({"doctype": "Salary Slip", "fields": ["name"], "order_by": [{"field": "net_pay", "direction": "desc"}]}, "protected field")

	def test_plain_protected_field_is_still_allowed(self):
		self.validator.validate({"doctype": "Salary Slip", "fields": ["name", "net_pay"]})   # comes back masked

	def test_child_table_needs_a_readable_parent(self):
		self._refused({"doctype": "Sales Taxes and Charges", "fields": ["account_head", {"field": "tax_amount", "aggregate": "sum", "alias": "t"}], "group_by": ["account_head"]}, "parent_doctype")
		spec = self.validator.validate({"doctype": "Sales Taxes and Charges", "parent_doctype": "Sales Invoice", "fields": ["account_head"]})
		self.assertEqual(spec["parent_doctype"], "Sales Invoice")
		self._refused({"doctype": "Sales Taxes and Charges", "parent_doctype": "ToDo", "fields": ["account_head"]}, "No read access")
