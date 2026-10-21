"""Phase D: open-document context, diagnosis, user guide search, setup check,
ERPNext native tools. All read-only except mapped drafts (approval only)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_manager import save_file

from ai_agent.knowledge import indexer
from ai_agent.runtime import document_context
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.test_permission_isolation import (
	USER_A, USER_B, _disable_test_users, _ensure_user, _make_project, _restrict_to_company,
)
from ai_agent.tools import diagnose, erpnext_native, knowledge, setup

TEST_ITEM = "AI-DIAG-TEST-ITEM"


def fake_sales_invoice_mapper(source_name):
	"""Stands in for ERPNext's make_sales_invoice (unsaved mapped document)."""
	return frappe.get_doc({
		"doctype": "Sales Invoice", "customer": "C", "docstatus": 1, "owner": "someone@x.com",
		"items": [{"item_code": "I", "qty": 2, "so_detail": "row1", "sales_order": source_name}],
	})


class TestOpenDocumentContext(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company_a, cls.company_b = frappe.get_all("Company", pluck="name", order_by="creation asc", limit=2)
		_ensure_user(USER_A, ["Projects User", "Desk User"])
		_ensure_user(USER_B, ["Projects User", "Desk User"])
		_restrict_to_company(USER_A, cls.company_a)
		_restrict_to_company(USER_B, cls.company_b)
		cls.project_a = _make_project(cls.company_a, USER_A)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("Project", cls.project_a, ignore_permissions=True, force=True)
		for user in (USER_A, USER_B):
			frappe.db.delete("User Permission", {"user": user})
		_disable_test_users()
		frappe.db.commit()
		super().tearDownClass()

	def test_owner_sees_document_summary(self):
		summary = document_context.build("Project", self.project_a, USER_A)
		self.assertIn(self.project_a, summary)
		self.assertIn("company", summary)

	def test_document_outside_user_permissions_is_not_added(self):
		self.assertEqual(document_context.build("Project", self.project_a, USER_B), "")

	def test_untrusted_input_is_normalized(self):
		self.assertEqual(document_context.normalize("No Such DocType", "X"), (None, None))
		self.assertEqual(document_context.normalize("Sales Invoice Item", "X"), (None, None))  # child table
		self.assertEqual(document_context.normalize("Project", ""), (None, None))


class TestDiagnose(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.ctx = PermissionContext("Administrator")
		if not frappe.db.exists("Item", TEST_ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": TEST_ITEM, "item_name": TEST_ITEM,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
				"stock_uom": frappe.db.get_value("UOM", {}, "name"), "is_stock_item": 1,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", TEST_ITEM, {"disabled": 1, "is_sales_item": 0, "is_purchase_item": 1})

	def tearDown(self):
		frappe.delete_doc("Item", TEST_ITEM, ignore_permissions=True, force=True)

	def test_item_not_in_search(self):
		result = diagnose.handle_document({"doctype": "Item", "name": TEST_ITEM}, self.ctx)
		self.assertIn("disabled", result["failed"])
		self.assertIn("is_sales_item", result["failed"])
		self.assertNotIn("is_purchase_item", result["failed"])

	def test_stock_checks_for_invoice_without_update_stock(self):
		doc = frappe._dict(
			doctype="Sales Invoice", name="SINV-X", docstatus=1, update_stock=0,
			items=[frappe._dict(item_code=TEST_ITEM, warehouse=None, delivery_note=None)],
		)
		with patch.object(diagnose, "_count", return_value=0):
			checks = {c["check"]: c["status"] for c in diagnose._stock_checks(doc)}
		self.assertEqual(checks["update_stock"], diagnose.FAIL)
		self.assertEqual(checks["warehouse"], diagnose.FAIL)
		self.assertEqual(checks["stock_ledger_entry"], diagnose.FAIL)

	def test_linked_delivery_note_explains_stock(self):
		doc = frappe._dict(
			doctype="Sales Invoice", name="SINV-X", docstatus=1, update_stock=0,
			items=[frappe._dict(item_code=TEST_ITEM, warehouse="W", delivery_note="DN-1")],
		)
		with patch.object(diagnose, "_count", return_value=0):
			checks = {c["check"]: c for c in diagnose._stock_checks(doc)}
		self.assertEqual(checks["update_stock"]["status"], diagnose.INFO)
		self.assertIn("DN-1", checks["update_stock"]["detail"])

	def test_unreadable_related_data_is_unknown_not_guessed(self):
		doc = frappe._dict(doctype="Payment Entry", name="PE-X", docstatus=1, company=None, posting_date=None)
		with patch.object(diagnose, "_count", return_value=None):
			checks = {c["check"]: c["status"] for c in diagnose._gl_checks(doc)}
		self.assertEqual(checks["gl_entry"], diagnose.UNKNOWN)

	def test_draft_is_reported(self):
		doc = frappe._dict(doctype="Journal Entry", docstatus=0)
		self.assertEqual(diagnose._docstatus_check(doc)["status"], diagnose.FAIL)

	def test_diagnose_is_read_only(self):
		before = frappe.db.get_value("Item", TEST_ITEM, "modified")
		diagnose.handle_document({"doctype": "Item", "name": TEST_ITEM}, self.ctx)
		self.assertEqual(frappe.db.get_value("Item", TEST_ITEM, "modified"), before)


class TestKnowledgeSearch(IntegrationTestCase):
	GUIDE = (
		"# إنشاء فاتورة مبيعات\n\nمن قائمة المبيعات اختر فاتورة المبيعات ثم جديد، واختر العميل والأصناف ثم احفظ.\n\n"
		"# إقفال الفترة\n\nلإقفال السنة المالية أنشئ قيد إقفال الفترة من المحاسبة.\n"
	)

	def setUp(self):
		frappe.set_user("Administrator")
		self.docs = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.docs:
			frappe.delete_doc("AI Knowledge Document", name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _guide(self, roles=""):
		doc = frappe.get_doc({"doctype": "AI Knowledge Document", "title": "دليل الاختبار", "file": "/private/files/x.md",
							  "allowed_roles": roles})
		doc.flags.ignore_links = True
		with patch("frappe.enqueue"):
			doc.insert(ignore_permissions=True)
		file_doc = save_file("guide.md", self.GUIDE.encode(), "AI Knowledge Document", doc.name, is_private=1)
		doc.db_set("file", file_doc.file_url)
		indexer.index_document(doc.name)
		self.docs.append(doc.name)
		return doc.name

	def test_index_and_search_with_source(self):
		self._guide()
		self.assertEqual(frappe.db.get_value("AI Knowledge Document", self.docs[0], "indexing_status"), "Indexed")
		results = knowledge.search("كيف انشئ فاتوره مبيعات", "Administrator")
		self.assertTrue(results)
		self.assertEqual(results[0]["source"], "دليل الاختبار")
		self.assertEqual(results[0]["section"], "إنشاء فاتورة مبيعات")
		self.assertIn("العميل", results[0]["snippet"])

	def test_role_restricted_guide_is_hidden(self):
		self._guide(roles="System Manager")
		_ensure_user(USER_A, ["Projects User"])
		try:
			self.assertEqual(knowledge.search("فاتورة مبيعات", USER_A), [])
			self.assertTrue(knowledge.search("فاتورة مبيعات", "Administrator"))
		finally:
			_disable_test_users()

	def test_arabic_normalization(self):
		self.assertEqual(indexer.normalize("إقفالُ الفترةِ"), "اقفال الفتره")


class TestSetupCheck(IntegrationTestCase):
	def test_every_area_reports(self):
		ctx = PermissionContext("Administrator")
		company = frappe.get_all("Company", pluck="name", limit=1)[0]
		for area in setup.AREAS:
			result = setup.handle_check({"area": area, "company": company}, ctx)
			self.assertEqual(result["area"], area)
			self.assertTrue(result["checks"], area)
			self.assertTrue(all(c["status"] in ("ok", "missing", "warn", "unknown") for c in result["checks"]))

	def test_unknown_area(self):
		self.assertIn("error", setup.handle_check({"area": "hr"}, PermissionContext("Administrator")))


class TestErpnextNativeTools(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.ctx = PermissionContext("Administrator")

	def test_fiscal_year_and_exchange_rate(self):
		company = frappe.get_all("Company", pluck="name", limit=1)[0]
		year = erpnext_native.handle_fiscal_year({"company": company}, self.ctx)
		self.assertIn("fiscal_year", year)
		currency = frappe.get_cached_value("Company", company, "default_currency")
		rate = erpnext_native.handle_exchange_rate({"from_currency": currency, "to_currency": currency}, self.ctx)
		self.assertEqual(rate["rate"], 1)

	def test_unsupported_mapping(self):
		result = erpnext_native.handle_request_mapped_draft(
			{"source_doctype": "Customer", "source_name": "X", "target_doctype": "Sales Invoice"}, self.ctx
		)
		self.assertIn("Unsupported mapping", result["error"])

	def test_mapped_payload_is_a_clean_draft(self):
		mapper = {("Sales Order", "Sales Invoice"): "ai_agent.tests.test_phase_d_tools.fake_sales_invoice_mapper"}
		with patch.object(erpnext_native, "_readable_doc", return_value=True), \
				patch.dict(erpnext_native.MAPPERS, mapper):
			payload = erpnext_native.build_mapped_payload("Sales Order", "SO-1", "Sales Invoice", self.ctx)
		self.assertEqual(payload["docstatus"], 0)
		self.assertNotIn("owner", payload)
		row = payload["items"][0]
		self.assertEqual((row["so_detail"], row["sales_order"]), ("row1", "SO-1"))
		self.assertFalse({"parent", "parenttype", "parentfield", "name"} & set(row))

	def test_mapped_draft_needs_readable_source(self):
		with patch.object(erpnext_native, "_readable_doc", return_value=None):
			result = erpnext_native.handle_request_mapped_draft(
				{"source_doctype": "Sales Order", "source_name": "SO-X", "target_doctype": "Sales Invoice"}, self.ctx
			)
		self.assertEqual(result, {"error": erpnext_native.NOT_ACCESSIBLE})

	def test_invoice_payments_reject_other_doctypes(self):
		self.assertIn("error", erpnext_native.handle_invoice_payments({"doctype": "Item", "name": "X"}, self.ctx))
