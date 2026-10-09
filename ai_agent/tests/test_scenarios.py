"""The reference scenarios end to end at the presenter / tool level (companies, invoice approval, statement, sales analysis, stock error, choice)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import approvals
from ai_agent.presentation import present
from ai_agent.presentation.presenters import lists
from ai_agent.tests.test_mcp_service_views import make_service, run_tool


class TestCompanies(IntegrationTestCase):
	def test_company_list_has_index_new_action_and_honest_capital(self):
		with patch("ai_agent.presentation.presenters.lists._company_capital", return_value=None):
			env = present("data.query", {"specification": {"doctype": "Company"}}, {"rows": [{"name": "A", "default_currency": "YER"}, {"name": "B", "default_currency": "USD"}], "row_count": 2}, None)
		p = env["presentation"]
		self.assertEqual([c["key"] for c in p["columns"]][:2], ["_index", "name"])
		self.assertEqual({r["cells"]["capital"]["d"] for r in p["rows"]}, {"غير مسجل"})
		self.assertEqual(p["paging"]["noun"], "شركات")
		self.assertTrue(all(a["overflow"] for r in p["rows"] for a in r["actions"]))
		self.assertFalse(any("mixed_currency" == w["code"] for w in p["warnings"]), "no amounts are shown, so no cross-currency notice")

	def test_recorded_zero_capital_is_not_hidden(self):
		with patch("ai_agent.presentation.presenters.lists._company_capital", return_value=0):
			env = present("data.query", {"specification": {"doctype": "Company"}}, {"rows": [{"name": "A", "default_currency": "YER"}], "row_count": 1}, None)
		self.assertEqual(env["presentation"]["rows"][0]["cells"]["capital"]["v"], 0)

	def test_capital_comes_from_equity_ledger_or_is_unknown(self):
		self.assertIsNone(lists._company_capital("Company That Does Not Exist", "Administrator"))


class TestStatement(IntegrationTestCase):
	REPORT = {"report": "General Ledger", "row_count": 4, "is_truncated": False, "filters": {"party": ["Ali"], "party_type": "Customer"}, "columns": [
		{"fieldname": "posting_date", "label": "d", "fieldtype": "Date"}, {"fieldname": "voucher_no", "label": "v", "fieldtype": "Data"}, {"fieldname": "voucher_type", "label": "t", "fieldtype": "Data"},
		{"fieldname": "debit", "label": "d", "fieldtype": "Currency"}, {"fieldname": "credit", "label": "c", "fieldtype": "Currency"}, {"fieldname": "balance", "label": "b", "fieldtype": "Currency"}],
		"result": [{"posting_date": "2026-09-01", "voucher_no": "SINV-1", "voucher_type": "Sales Invoice", "debit": 100, "credit": 0, "balance": 100},
			{"posting_date": "2026-09-05", "voucher_no": "PE-1", "voucher_type": "Payment Entry", "debit": 0, "credit": 40, "balance": 60},
			{"account": "'Closing (Opening + Total)'", "debit": 100, "credit": 40, "balance": 60}]}

	def _p(self, overdue=None):
		with patch("ai_agent.presentation.presenters.reports._default_currency", return_value="YER"), patch("ai_agent.presentation.presenters.reports._overdue", return_value=overdue), \
				patch("ai_agent.presentation.presenters.reports._can", return_value=True):
			return present("reports.run", {}, self.REPORT, None)["presentation"]

	def test_tabs_dates_and_kpis_follow_the_reference(self):
		p = self._p({"count": 3, "amount": 210000, "currency": "YER", "query": {"specification": {"doctype": "Sales Invoice"}}})
		self.assertEqual([t["label"] for t in p["tabs"]], ["الكل", "الفواتير", "المدفوعات", "القيود"])
		self.assertEqual(p["tabs"][1]["in"], ["Sales Invoice", "Purchase Invoice"])
		self.assertEqual([k["key"] for k in p["kpis"]], ["debit", "credit", "overdue", "balance"])
		self.assertEqual(p["kpis"][2]["tone"], "danger")
		self.assertEqual(p["filters"][0]["type"], "date_range")
		self.assertTrue(any(w["level"] == "critical" and "3 فواتير" in w["text"] for w in p["warnings"]))
		self.assertEqual([a["key"] for a in p["actions"]], ["overdue", "pay", "export"])
		vt = next(c for c in p["columns"] if c["key"] == "voucher_type")
		self.assertTrue(vt["hidden"])
		self.assertEqual(p["rows"][0]["cells"]["voucher_type"]["d"], "فاتورة بيع")
		self.assertEqual(p["rows"][1]["cells"]["voucher_type"]["d"], "سند دفع")

	def test_no_overdue_means_no_overdue_kpi_or_warning(self):
		p = self._p(None)
		self.assertNotIn("overdue", [k["key"] for k in p["kpis"]])
		self.assertFalse([w for w in p["warnings"] if w["code"] == "overdue"])


class TestSalesAnalysis(IntegrationTestCase):
	RESULT = {"period": {"key": "this_month", "from": "2026-09-01", "to": "2026-09-29", "label": "هذا الشهر"}, "company": "SND", "currency": "YER", "truncated": False,
		"totals": {"sales": 4800000.0, "invoices": 94, "customers": 27, "avg_invoice": 51000.0}, "top3_share": 0.51,
		"top": [{"customer": f"C{i}", "customer_name": f"N{i}", "sales": s, "invoices": 10, "share": s / 4800000} for i, s in enumerate([1200000, 860000, 720000, 480000, 310000])]}

	def test_presenter_builds_kpis_chart_ranking_and_insight(self):
		env = present("analysis.sales", {}, self.RESULT, None)
		p = env["presentation"]
		self.assertEqual([k["label"] for k in p["kpis"]], ["إجمالي المبيعات", "عدد العملاء", "عدد الفواتير", "متوسط الفاتورة"])
		self.assertEqual(p["kpis"][0]["d"], "4.8M YER")
		self.assertEqual(p["kpis"][0]["exact"], "4,800,000 YER")
		self.assertEqual(p["chart"]["kind"], "bar")
		self.assertEqual(len(p["chart"]["labels"]), 5)
		finding = p["insights"][0]
		self.assertEqual(finding["observation"], "أعلى 3 عملاء يمثلون 51% من مبيعات الفترة.")
		for part in ("title", "cause", "impact", "action", "level"):
			self.assertTrue(finding.get(part), part)
		self.assertIn("سبتمبر 2026", p["title"])
		self.assertEqual(p["rows"][0]["cells"]["share"]["d"], "25%")

	def test_tool_is_registered_and_reads_with_the_callers_permissions(self):
		from ai_agent.runtime.tool_registry import ToolRegistry

		self.assertIsNotNone(ToolRegistry().get_tool("analysis.sales"))
		svc = make_service(True)
		result = run_tool(svc, "analysis__sales", {"period": "this_month", "top": 3})
		self.assertFalse(result["isError"])
		self.assertIn("totals", result["structuredContent"]["data"])

	def test_multi_currency_companies_are_refused_not_added(self):
		from ai_agent.runtime.permission_context import PermissionContext
		from ai_agent.tools import analysis

		rows = [frappe._dict(name="1", customer="a", customer_name="a", base_grand_total=5, company="C1"), frappe._dict(name="2", customer="b", customer_name="b", base_grand_total=7, company="C2")]
		with patch("frappe.get_list", return_value=rows), patch("frappe.get_cached_value", side_effect=lambda dt, c, f: {"C1": "YER", "C2": "USD"}[c]):
			out = analysis.handle_sales({"period": "this_month"}, PermissionContext("Administrator"))
		self.assertIn("error", out)


class TestStockBlockedApproval(IntegrationTestCase):
	def setUp(self):
		patcher = patch.object(approvals, "four_eyes_enabled", return_value=False)   # independent of the site's policy
		patcher.start()
		self.addCleanup(patcher.stop)

	def _item(self, available):
		return {"idx": 1, "item": "عصير", "item_code": "J", "qty": 100, "rate": 1500, "amount": 150000, "warehouse": "W1", "uom": "كرتون", "image": None,
			"stock": {"available": available, "d": str(available), "ok": available >= 100}, "d": {"qty": "100", "rate": "1,500 YER", "amount": "150,000 YER"}}

	def test_shortage_blocks_only_when_posting_would_deduct_stock(self):
		warnings, blocked = approvals._stock_check("Sales Invoice", [self._item(60)], {"update_stock": 1})
		self.assertEqual(warnings[0]["level"], "critical")
		self.assertIsNotNone(blocked)
		warnings, blocked = approvals._stock_check("Sales Invoice", [self._item(60)], {"update_stock": 0})
		self.assertEqual(warnings[0]["level"], "info", "an invoice that does not move stock is never blocked by stock")
		self.assertIn("لا تخصم من المخزون", warnings[0]["text"])
		self.assertIsNone(blocked)
		self.assertEqual(approvals._stock_check("Purchase Invoice", [self._item(0)], {"update_stock": 1}), ([], None), "incoming stock has no shortage")
		self.assertEqual(approvals._stock_check("Sales Invoice", [self._item(240)], {"update_stock": 1}), ([], None))

	def test_blocked_card_swaps_approve_for_view_stock_choose_edit_cancel(self):
		frappe.flags.mcp_ui_supported = True
		items = [self._item(60)]
		warns, blocked = approvals._stock_check("Sales Invoice", items, {"update_stock": 1})
		with patch("frappe.has_permission", return_value=False):
			blk = approvals._blocked("Sales Invoice", blocked, {})
		self.assertEqual(blk["title"], "لا يمكن اعتماد الفاتورة")
		preview = {"fields": [], "items": items, "kpis": [], "warnings": warns, "valid": True, "photo": None, "blocked": blk}
		apr = frappe._dict(name="APR-B", action_type="Create", doctype_name="Sales Invoice", document_name=None, status="Pending", payload_hash="h", action_summary="x",
			payload_json=json.dumps({"doctype": "Sales Invoice", "items": [{"item_code": "J", "qty": 100}]}), impact_summary="", expires_at=None, run=None, requested_by="Administrator")
		with patch.object(approvals, "_preview", return_value=preview), patch.object(approvals, "app_approval_enabled", return_value=True):
			card = approvals.build_card(apr, "Administrator")
		self.assertEqual([a["key"] for a in card["actions"]], ["view_stock", "choose_warehouse", "edit", "cancel"])
		self.assertEqual(card["blocked"]["available"], 60)
		self.assertNotIn("approve", [a["key"] for a in card["actions"]])
		frappe.flags.mcp_ui_supported = None

	def test_ready_card_is_labelled_like_the_reference(self):
		frappe.flags.mcp_ui_supported = True
		preview = {"fields": [], "items": [self._item(240)], "kpis": [], "warnings": [], "valid": True, "photo": None, "blocked": None}
		apr = frappe._dict(name="APR-R", action_type="Create", doctype_name="Sales Invoice", document_name=None, status="Pending", payload_hash="h", action_summary="x",
			payload_json=json.dumps({"doctype": "Sales Invoice", "items": [{"item_code": "J", "qty": 100}]}), impact_summary="", expires_at=None, run=None, requested_by="Administrator")
		with patch.object(approvals, "_preview", return_value=preview), patch.object(approvals, "app_approval_enabled", return_value=True):
			card = approvals.build_card(apr, "Administrator")
		self.assertEqual(card["title"], "فاتورة بيع — جاهزة للإنشاء")
		self.assertEqual(card["status_label"], "تحتاج موافقة")
		self.assertEqual({a["key"]: a["label"] for a in card["actions"]}["approve"], "موافقة وإنشاء الفاتورة")
		frappe.flags.mcp_ui_supported = None

	def test_revising_the_warehouse_moves_every_line(self):
		payload = {"doctype": "Sales Invoice", "items": [{"item_code": "J", "qty": 1, "warehouse": "W1"}, {"item_code": "K", "qty": 1, "warehouse": "W1"}]}
		apr = frappe._dict(name="APR-W", action_type="Create", doctype_name="Sales Invoice", document_name=None, status="Pending", payload_hash="h", action_summary="x", run=None,
			payload_json=json.dumps(payload), impact_summary="", requested_by="Administrator", expires_at=None)
		captured = {}

		class FakeEngine:
			def __init__(self, ctx): pass
			def create_approval_request(self, **kw):
				captured.update(kw)
				return frappe._dict(apr, name="APR-W2", payload_json=json.dumps(kw["payload"]))
			def reject(self, *a, **k): return apr

		token = approvals.issue_token("APR-W", "h", "Administrator", "revise")
		with patch.object(approvals, "_load", return_value=apr), patch.object(approvals, "ApprovalEngine", FakeEngine), patch.object(approvals, "card_envelope", return_value={"ok": True}), \
				patch("frappe.get_meta") as meta:
			meta.return_value.has_field.return_value = True
			approvals.revise("Administrator", "APR-W", token, {"fields": {"set_warehouse": "W2"}})
		self.assertEqual(captured["payload"]["set_warehouse"], "W2")
		self.assertEqual([r["warehouse"] for r in captured["payload"]["items"]], ["W2", "W2"])


class TestChoice(IntegrationTestCase):
	def test_company_choice_lists_only_what_the_user_can_read(self):
		svc = make_service(True)
		result = svc.call_tool("app__choose", {"source": "companies"})
		self.assertFalse(result["isError"])
		p = result["_meta"]["ai_agent/presentation"]
		self.assertEqual(p["type"], "choice")
		self.assertEqual(p["reply_template"], "اخترت الشركة: {label}")
		self.assertTrue(p["options"])
		self.assertIn("1. ", result["content"][0]["text"])           # numbered list works in text-only hosts
		self.assertNotIn("_meta", make_service(False).call_tool("app__choose", {"source": "companies"}))

	def test_explicit_options_are_cleaned_and_capped(self):
		svc = make_service(True)
		result = svc.call_tool("app__choose", {"title": "أي مخزن؟", "options": [{"label": f"W{i}", "description": "d"} for i in range(30)] + ["junk", {"nolabel": 1}]})
		self.assertEqual(len(result["_meta"]["ai_agent/presentation"]["options"]), 20)

	def test_empty_choice_is_an_error_not_a_blank_card(self):
		result = make_service(True).call_tool("app__choose", {"options": []})
		self.assertTrue(result["isError"])


class TestBulkApproval(IntegrationTestCase):
	PAYLOAD = {"documents": [
		{"doctype": "Customer", "payload": {"customer_name": "حمود صالح", "customer_type": "Individual", "territory": "Yemen", "owner": "x"}},
		{"doctype": "Item", "payload": {"item_code": "JUICE", "item_name": "عصير سنتوب", "stock_uom": "Box", "standard_rate": 1500, "is_stock_item": 1}}]}

	def test_documents_are_readable_blocks_without_technical_fields(self):
		docs = approvals._bulk_documents(self.PAYLOAD)
		self.assertEqual([d["title"] for d in docs], ["حمود صالح", "عصير سنتوب"])
		self.assertEqual([d["type_label"] for d in docs], ["عميل", "صنف"])
		self.assertEqual([d["icon"] for d in docs], ["user", "package"])
		self.assertNotIn("owner", json.dumps(docs, ensure_ascii=False))

	def test_card_lists_documents_and_offers_one_approval(self):
		frappe.flags.mcp_ui_supported = True
		apr = frappe._dict(name="APR-BULK", action_type="Bulk Create", doctype_name=None, document_name=None, status="Pending", payload_hash="h", run=None,
			action_summary="إنشاء العميل والصنف تمهيدًا لفاتورة", payload_json=json.dumps(self.PAYLOAD), impact_summary="2 draft documents will be created after one approval.",
			expires_at=None, requested_by="Administrator")
		with patch.object(approvals, "app_approval_enabled", return_value=True), patch.object(approvals, "four_eyes_enabled", return_value=False):
			card = approvals.build_card(apr, "Administrator")
		self.assertEqual(card["title"], "إنشاء مستندين — جاهزان للإنشاء")
		self.assertEqual(card["subtitle"], "إنشاء العميل والصنف تمهيدًا لفاتورة")
		self.assertEqual(len(card["documents"]), 2)
		self.assertEqual({a["key"]: a["label"] for a in card["actions"]}["approve"], "موافقة وإنشاء المستندات")
		self.assertNotIn("edit", [a["key"] for a in card["actions"]])    # a bulk request is approved or cancelled as a whole
		self.assertIsNone(card["impact"])
		from ai_agent.presentation import text as presentation_text

		text = presentation_text.render(card)
		self.assertIn("عميل: حمود صالح", text)
		self.assertIn("صنف: عصير سنتوب", text)
		frappe.flags.mcp_ui_supported = None


class TestDocumentView(IntegrationTestCase):
	"""Mapping before display: sections, no duplicate labels, the right currency on every amount, no noise."""

	INVOICE = {"doctype": "Sales Invoice", "name": "SINV-T", "company": "_C", "customer": "CUST-1", "customer_name": "حمود صالح", "posting_date": "2026-09-29",
		"currency": "USD", "conversion_rate": 530, "total": 20000, "net_total": 20000, "grand_total": 20000, "rounded_total": 20000,
		"base_total": 10600000, "base_net_total": 10600000, "base_grand_total": 10600000, "base_rounded_total": 10600000, "outstanding_amount": 20000,
		"party_account_currency": "USD", "update_stock": 0, "is_return": 0, "set_posting_time": 0, "status": "Unpaid", "owner": "a@b.c", "docstatus": 1,
		"items": [{"name": "row-hash-1", "idx": 1, "item_code": "JUICE", "item_name": "عصير سنتوب", "qty": 100, "stock_qty": 1200, "uom": "كرتون", "stock_uom": "حبة",
			"conversion_factor": 12, "rate": 200, "amount": 20000, "base_rate": 106000, "base_amount": 10600000, "owner": "a@b.c"}]}

	def _build(self, doc, company_currency="YER"):
		from ai_agent.presentation.presenters import documents

		with patch("ai_agent.presentation.money.company_currency", return_value=company_currency):
			return documents.build_document(doc["doctype"], doc["name"], doc)[0]

	def _fields(self, p, include_more=True):
		return {f["key"]: f for s in p["sections"] if include_more or not s.get("collapsed") for f in s["fields"]}

	def test_foreign_currency_amounts_carry_their_own_currency(self):
		p = self._build(self.INVOICE)
		fields = self._fields(p)
		self.assertEqual(fields["grand_total"]["d"], "20,000 USD")
		self.assertEqual(fields["base_grand_total"]["d"], "10,600,000 YER", "company-currency total is never printed in the transaction currency")
		self.assertEqual(fields["conversion_rate"]["d"], "1 USD = 530 YER")
		self.assertEqual([(k["d"], k.get("hint")) for k in p["kpis"]][:2], [("20,000 USD", None), ("10,600,000 YER", "1 USD = 530 YER")])
		item = p["tables"][0]["rows"][0]["cells"]
		self.assertEqual((item["rate"]["d"], item["amount"]["d"], item["base_amount"]["d"]), ("200 USD", "20,000 USD", "10,600,000 YER"))

	def test_no_label_appears_twice_and_equal_amounts_are_shown_once(self):
		p = self._build(self.INVOICE)
		labels = [f["label"] for s in p["sections"] for f in s["fields"]]
		self.assertEqual(len(labels), len(set(labels)), labels)
		shown = self._fields(p, include_more=False)
		for repeated in ("total", "net_total", "rounded_total", "customer_name", "base_total"):
			self.assertNotIn(repeated, shown)
		self.assertEqual(shown["customer"]["d"], "حمود صالح")

	def test_single_currency_documents_drop_the_company_currency_twins(self):
		p = self._build({**self.INVOICE, "currency": "YER", "conversion_rate": 1, "party_account_currency": "YER"})
		fields = self._fields(p)
		for key in ("base_grand_total", "conversion_rate", "base_total", "party_account_currency"):
			self.assertNotIn(key, fields)
		self.assertEqual(len(p["kpis"]), 2)       # total and outstanding, no "company currency" card

	def test_sections_are_business_sections_and_noise_is_hidden(self):
		p = self._build(self.INVOICE)
		titles = [s["title"] for s in p["sections"]]
		self.assertEqual(titles[:3], ["المعلومات الأساسية", "الطرف", "المبالغ"])
		fields = self._fields(p)
		self.assertEqual(fields["update_stock"]["d"], "لا", "whether stock moves is always stated")
		for noise in ("set_posting_time", "is_return", "owner", "docstatus"):
			self.assertNotIn(noise, fields)
		self.assertEqual(p["status"], {"d": "غير مدفوعة", "tone": "warning"})
		self.assertIn("owner", [t["label"] for t in p["developer"]["technical_fields"]])

	def test_item_table_has_business_columns_and_distinct_quantity_labels(self):
		table = self._build(self.INVOICE)["tables"][0]
		self.assertEqual(table["title"], "الأصناف")
		visible = [c["key"] for c in table["columns"] if not c.get("collapsed")]
		self.assertEqual(visible, ["_index", "item_name", "item_code", "qty", "uom", "rate", "amount"])
		labels = {c["key"]: c["label"] for c in table["columns"]}
		self.assertNotIn("name", labels)
		self.assertEqual((labels["qty"], labels["stock_qty"], labels["uom"], labels["stock_uom"]), ("الكمية", "الكمية بوحدة المخزون", "الوحدة", "وحدة المخزون"))
		self.assertEqual(len(set(labels.values())), len(labels))

	def test_percent_fields_are_not_multiplied(self):
		from ai_agent.presentation import formatters

		self.assertEqual(formatters.percent_value(100), "100%")
		self.assertEqual(formatters.percent_value(12.5), "12.5%")


class TestErrorsForPeople(IntegrationTestCase):
	def test_common_errors_get_a_title_a_reason_and_a_next_step(self):
		from ai_agent.presentation import errors

		cases = {
			"No read permission on 'Sales Invoice'": ("permission", False),
			"Document not found or not accessible.": ("not_found", False),
			"Report execution failed: Fiscal Year 2027 not found": ("validation", True),
			"Tool 'data.query' missing required argument(s): doctype.": ("validation", True),
			"Query denied due to high cost. Please add more filters.": ("validation", True),
			"Company is system configuration; it cannot be created or changed through the agent": ("policy", False),
			"Tool execution failed.": ("failure", True),
		}
		for raw, (kind, retry) in cases.items():
			view = errors.friendly(raw)
			self.assertEqual((view["kind"], view["retry"]), (kind, retry), raw)
			self.assertTrue(view["title"] and view["reason"])
			self.assertNotIn(raw, view["title"] + view["reason"], "the raw tool error is not what the user reads")
			self.assertEqual(view["raw"], raw)

	def test_service_errors_are_presented_not_dumped(self):
		svc = make_service(True)
		result = svc._error("No read permission on 'Sales Invoice'", "data.query")
		self.assertTrue(result["isError"])
		self.assertEqual(result["_meta"]["ai_agent/presentation"]["type"], "error")
		self.assertIn("لا تملك الصلاحية", result["content"][0]["text"])
		self.assertEqual(result["structuredContent"]["error"], "No read permission on 'Sales Invoice'")   # the model can still correct itself


class TestFindings(IntegrationTestCase):
	def test_trial_balance_flags_balances_on_the_wrong_side(self):
		from ai_agent.presentation import insights

		rows = [{"account": "Cash - C", "account_name": "Cash", "closing_debit": 0, "closing_credit": 5000},
			{"account": "Sales - C", "account_name": "Sales", "closing_debit": 0, "closing_credit": 9000},
			{"account": "Bank - C", "account_name": "Bank", "closing_debit": 100, "closing_credit": 0}]
		roots = {"Cash - C": "Asset", "Sales - C": "Income", "Bank - C": "Asset"}
		filters = {"company": "C", "from_date": "2026-01-01", "to_date": "2026-12-31"}
		with patch.object(insights, "_root_type", side_effect=roots.get):
			found = insights.abnormal_balances(rows, filters, "YER")
		self.assertEqual(len(found), 1)
		self.assertIn("Cash", found[0]["title"])
		for part in ("observation", "cause", "impact", "action", "drill"):
			self.assertTrue(found[0].get(part), part)
		self.assertEqual(found[0]["drill"]["arguments"]["filters"]["account"], ["Cash - C"])

	def test_trial_balance_is_a_balance_statement_with_a_ledger_drill_per_account(self):
		report = {"report": "Trial Balance", "row_count": 2, "filters": {"company": "C", "from_date": "2026-01-01", "to_date": "2026-12-31"}, "columns": [
			{"fieldname": "account", "label": "Account", "fieldtype": "Link"}, {"fieldname": "debit", "label": "Debit", "fieldtype": "Currency"}, {"fieldname": "credit", "label": "Credit", "fieldtype": "Currency"},
			{"fieldname": "closing_debit", "label": "Closing (Dr)", "fieldtype": "Currency"}, {"fieldname": "closing_credit", "label": "Closing (Cr)", "fieldtype": "Currency"}],
			"result": [{"account": "Cash - C", "debit": 10, "credit": 0, "closing_debit": 10, "closing_credit": 0}, {"account": "Sales - C", "debit": 0, "credit": 10, "closing_debit": 0, "closing_credit": 10}]}
		with patch("ai_agent.presentation.presenters.reports._default_currency", return_value="YER"), patch("ai_agent.presentation.insights._root_type", return_value=None):
			p = present("reports.run", {}, report, None)["presentation"]
		self.assertEqual(p["variant"], "financial")
		self.assertIsNone(p["group_by"], "one row per account is not grouped")
		self.assertNotIn("balance", [k["key"] for k in p["kpis"]])
		action = p["rows"][0]["actions"][0]
		self.assertEqual((action["tool"], action["arguments"]["report_name"], action["arguments"]["filters"]["account"]), ("reports__run", "General Ledger", ["Cash - C"]))

	def test_customer_credit_balances_and_dominant_voucher(self):
		from ai_agent.presentation import insights

		aging = [{"party": "A", "party_name": "أحمد", "outstanding": -300, "voucher_no": "PE-1"}, {"party": "B", "outstanding": 500}]
		found = insights.opposite_party_balances(aging, {}, "YER")
		self.assertEqual(len(found), 1)
		self.assertIn("300 YER", found[0]["observation"])
		self.assertEqual(insights.opposite_party_balances([{"party": "B", "outstanding": 500}], {}, "YER"), [])
		ledger = [{"voucher_no": "V1", "voucher_type": "Journal Entry", "debit": 900, "credit": 0}, {"voucher_no": "V2", "debit": 50, "credit": 0}, {"voucher_no": "V3", "debit": 50, "credit": 0}]
		self.assertIn("90%", insights.dominant_voucher(ledger, "YER")[0]["title"])
		self.assertEqual(insights.dominant_voucher(ledger[1:], "YER"), [])
