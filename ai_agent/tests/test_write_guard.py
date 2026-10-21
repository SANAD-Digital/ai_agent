import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.write_guard import blocked_fields, replacement_hints

DOCTYPE = "ToDo"


class TestWriteGuardHiddenFields(IntegrationTestCase):
	"""Updates skip hidden fields, except those hidden only by Customize Form."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(frappe.clear_cache, doctype=DOCTYPE)

	def _hide_by_customization(self, fieldname):
		ps = frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doctype_or_field": "DocField",
				"doc_type": DOCTYPE,
				"field_name": fieldname,
				"property": "hidden",
				"property_type": "Check",
				"value": "1",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Property Setter", ps.name, force=True, ignore_permissions=True))
		frappe.clear_cache(doctype=DOCTYPE)

	def test_field_hidden_by_customize_form_stays_writable(self):
		self._hide_by_customization("description")
		self.assertTrue(frappe.get_meta(DOCTYPE).get_field("description").hidden)
		self.assertEqual(blocked_fields(DOCTYPE, {"description": "x"}, "Administrator"), [])

	def test_field_hidden_by_the_doctype_stays_blocked(self):
		meta = frappe.get_meta(DOCTYPE)
		native_hidden = next(
			(
				df.fieldname
				for df in meta.fields
				if df.hidden and not df.read_only and df.fieldtype not in ("Section Break", "Column Break", "Tab Break")
				and not frappe.db.exists("Property Setter", {"doc_type": DOCTYPE, "field_name": df.fieldname, "property": "hidden"})
			),
			None,
		)
		if not native_hidden:
			self.skipTest(f"{DOCTYPE} has no natively hidden editable field on this site")
		self.assertEqual(blocked_fields(DOCTYPE, {native_hidden: "x"}, "Administrator"), [native_hidden])

	def test_read_only_field_stays_blocked_even_if_customized(self):
		meta = frappe.get_meta(DOCTYPE)
		read_only = next((df.fieldname for df in meta.fields if df.read_only and df.fieldname), None)
		if not read_only:
			self.skipTest(f"{DOCTYPE} has no read-only field")
		self._hide_by_customization(read_only)
		self.assertEqual(blocked_fields(DOCTYPE, {read_only: "x"}, "Administrator"), [read_only])


class TestWriteGuardReplacementHints(IntegrationTestCase):
	"""A field the DocType retired by hiding it points the model to the writable one."""

	def test_journal_entry_user_remark_points_to_remark(self):
		meta = frappe.get_meta("Journal Entry")
		if not (meta.get_field("user_remark") and meta.get_field("user_remark").hidden and meta.get_field("remark")):
			self.skipTest("this ERPNext version keeps Journal Entry user_remark visible")
		problems = blocked_fields("Journal Entry", {"user_remark": "x"}, "Administrator")
		self.assertEqual(problems, ["user_remark"])
		hints = replacement_hints("Journal Entry", problems, "Administrator")
		self.assertEqual(len(hints), 1)
		self.assertIn("remark", hints[0].split("use ", 1)[1])
		self.assertEqual(blocked_fields("Journal Entry", {"remark": "x", "custom_remark": 1}, "Administrator"), [])


class TestForeignChildRows(IntegrationTestCase):
	"""An update may only name child rows the target document already has (no re-parenting)."""

	def _doc(self, *row_names):
		doc = frappe.new_doc("Journal Entry")
		for name in row_names:
			doc.append("accounts", {"account": "x", "name": name})
		return doc

	def test_own_rows_and_new_rows_pass(self):
		from ai_agent.runtime.write_guard import foreign_child_rows

		doc = self._doc("row-a", "row-b")
		payload = {"accounts": [{"name": "row-a", "debit": 5}, {"account": "y", "credit": 5}]}
		self.assertEqual(foreign_child_rows(doc, payload), [])

	def test_a_row_of_another_document_is_refused(self):
		from ai_agent.runtime.write_guard import foreign_child_rows

		doc = self._doc("row-a")
		self.assertEqual(foreign_child_rows(doc, {"accounts": [{"name": "row-of-other-je"}]}), ["accounts[0].name"])

	def test_create_may_not_name_child_rows(self):
		problems = blocked_fields("Journal Entry", {"accounts": [{"name": "row-of-other-je", "account": "x"}]}, "Administrator", mode="create")
		self.assertIn("accounts[0].name", problems)
