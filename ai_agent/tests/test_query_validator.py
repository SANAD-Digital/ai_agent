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
