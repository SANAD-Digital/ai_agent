import json
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import present, schema
from ai_agent.presentation import text as presentation_text
from ai_agent.presentation.i18n import t
from ai_agent.presentation.labels import field_tier, label_for, plural_label, status_display


class TestFormatters(IntegrationTestCase):
	def test_money_and_nulls(self):
		self.assertEqual(fmt.money(150000, "YER"), "150,000 YER")
		self.assertEqual(fmt.money(1234.5, "USD"), "1,234.50 USD")
		self.assertEqual(fmt.money(None, "YER"), t("Not recorded"))
		self.assertEqual(fmt.money(0, "YER", zero_as_unset=True), t("Not recorded"))
		self.assertEqual(fmt.money(0, "YER"), "0 YER")

	def test_no_technical_null_text(self):
		for raw in (None, "", "None", "null", "N/A"):
			self.assertEqual(fmt.text(raw), "—")

	def test_percent_date_bool(self):
		self.assertEqual(fmt.percent(0.134), "13.4%")
		self.assertEqual([fmt.percent(v, 0) for v in (0.9, 0.1, 1, 0.505)], ["90%", "10%", "100%", "50%"])
		self.assertEqual([fmt.percent(v) for v in (0.9, 0.1, 1)], ["90%", "10%", "100%"])
		self.assertEqual(fmt.date("2026-09-29"), f"29 {t('September')} 2026")
		self.assertEqual(fmt.boolean(1), t("Yes"))
		self.assertEqual(fmt.boolean(0), t("No"))

	def test_compact(self):
		self.assertEqual(fmt.compact(1_400_000), "1.4M")
		self.assertEqual(fmt.compact(320_000), "320K")

	def test_status_registry(self):
		self.assertEqual(status_display("Partly Paid")[1], "warning")
		self.assertEqual(status_display("Overdue")[1], "danger")
		self.assertTrue(status_display("Cancelled")[0])


class TestFieldOrder(IntegrationTestCase):
	def test_technical_fields_are_hidden_and_business_order_wins(self):
		keys = ["owner", "creation", "modified", "docstatus", "idx", "status", "grand_total", "customer", "posting_date", "name"]
		visible, technical = schema.order_keys(keys)
		self.assertEqual(set(technical), {"owner", "creation", "modified", "docstatus", "idx"})
		self.assertEqual(visible[0], "name")
		self.assertLess(visible.index("customer"), visible.index("remarks") if "remarks" in visible else 99)
		self.assertEqual(field_tier("owner"), 4)

	def test_column_definition_schema(self):
		col = schema.column("grand_total", type="currency", priority="primary")
		for key in ("key", "label", "type", "priority", "align", "sortable", "filterable"):
			self.assertIn(key, col)
		self.assertEqual(col["label"], label_for("grand_total"))
		self.assertEqual(col["align"], "end")


class TestListPresenter(IntegrationTestCase):
	def _present(self, doctype, rows, **spec):
		with patch("ai_agent.presentation.presenters.lists._default_currency", return_value="YER"):
			return present("data.query", {"specification": {"doctype": doctype, **spec}},
				{"rows": rows, "row_count": len(rows)}, None)

	def test_invoice_rows_use_business_order_and_hide_technical(self):
		rows = [{"name": "ACC-SINV-2026-001", "owner": "a@b.c", "creation": "2026-09-01 10:00:00", "docstatus": 1, "customer": "CUST-1",
			"grand_total": 150000, "posting_date": "2026-09-29", "status": "Paid", "outstanding_amount": 0}]
		env = self._present("Sales Invoice", rows)
		p = env["presentation"]
		keys = [c["key"] for c in p["columns"]]
		self.assertEqual(keys[0], "name")
		self.assertNotIn("owner", keys)
		self.assertNotIn("docstatus", keys)
		self.assertLess(keys.index("customer"), keys.index("grand_total"))
		self.assertLess(keys.index("grand_total"), keys.index("status"))
		row = p["rows"][0]["cells"]
		self.assertEqual(row["grand_total"]["d"], "150,000 YER")
		self.assertEqual(row["status"]["d"], status_display("Paid")[0])
		self.assertEqual(row["status"]["tone"], "success")
		self.assertEqual(row["posting_date"]["d"], fmt.date("2026-09-29"))
		self.assertIn("owner", p["developer"]["hidden_fields"])

	def test_company_capital_unrecorded_is_not_zero(self):
		rows = [{"name": "SND", "default_currency": "YER", "capital": 0}, {"name": "ACME", "default_currency": "USD", "capital": None}]
		p = self._present("Company", rows)["presentation"]
		self.assertEqual(p["title"], plural_label("Company"))
		self.assertEqual([c["key"] for c in p["columns"]][:4], ["_index", "name", "default_currency", "capital"])
		self.assertEqual(p["icon"], "building")
		self.assertEqual(p["search_placeholder"], t("Search companies"))
		self.assertEqual({r["cells"]["capital"]["d"] for r in p["rows"]}, {t("Not recorded")})

	def test_mixed_currency_is_not_summed(self):
		rows = [{"name": "A", "grand_total": 100, "currency": "YER"}, {"name": "B", "grand_total": 5, "currency": "USD"}]
		env = self._present("Sales Invoice", rows)
		p = env["presentation"]
		self.assertIsNone(p["totals"])
		self.assertEqual(p["kpis"], [])
		self.assertTrue(any(w["code"] == "mixed_currency" for w in p["warnings"]))
		self.assertIn("currency", [c["key"] for c in p["columns"]])

	def test_truncation_is_never_silent(self):
		env = self._present("Sales Invoice", [{"name": "A", "grand_total": 1}])
		env2 = present("data.query", {"specification": {"doctype": "Sales Invoice"}}, {"rows": [{"name": "A", "grand_total": 1}], "row_count": 1248}, None)
		p = env2["presentation"]
		self.assertEqual(p["paging"]["total"], 1248)
		self.assertTrue(any(w["code"] == "truncated" for w in p["warnings"]))
		self.assertIn("1,248", p["warnings"][-1]["text"])
		self.assertEqual(p["paging"]["total_pages"], 3)  # default window is 500 rows

	def test_empty_state_is_specific(self):
		p = self._present("Sales Invoice", [])["presentation"]
		self.assertEqual(p["empty"]["title"], t("No matching {0}", plural_label("Sales Invoice", "Records")))

	def test_lite_copy_carries_no_rows_or_action_arguments(self):
		p = self._present("Sales Invoice", [{"name": "A", "grand_total": 1}])["presentation"]
		p["actions"] = [{"key": "x", "label": "x", "kind": "tool", "arguments": {"token": "SECRET"}}]
		lite = schema.lite(p)
		self.assertNotIn("rows", lite)
		self.assertNotIn("SECRET", json.dumps(lite))

	def test_ranking_view_adds_rank_and_share(self):
		rows = [{"customer": "A", "total": 300}, {"customer": "B", "total": 100}]
		p = self._present("Sales Invoice", rows, group_by=["customer"])["presentation"]
		self.assertEqual(p["variant"], "ranking")
		self.assertEqual(p["columns"][0]["key"], "_rank")
		self.assertEqual(p["rows"][0]["cells"]["_share"]["d"], "75%")

	def test_text_fallback_is_ordered_markdown(self):
		env = self._present("Company", [{"name": "SND", "default_currency": "YER", "capital": None}])
		text = presentation_text.render(env["presentation"], env["summary"]["text"])
		self.assertIn(f"**{plural_label('Company')}**", text)
		self.assertIn(f"| # | {t('Company name')} | {t('Currency')} | {label_for('capital')} |", text)
		self.assertIn(t("Not recorded"), text)
		self.assertNotIn("None", text)

	def test_filter_bar_definitions_come_from_the_server(self):
		rows = [{"name": "A", "customer": "X", "posting_date": "2026-09-01", "grand_total": 1, "status": "Paid", "currency": "YER"},
			{"name": "B", "customer": "Y", "posting_date": "2026-09-02", "grand_total": 2, "status": "Overdue", "currency": "YER"}]
		p = self._present("Sales Invoice", rows)["presentation"]
		by_key = {f["key"]: f for f in p["filters"]}
		self.assertEqual(by_key["status"]["type"], "select")
		values = {o["v"] for o in by_key["status"]["options"]}
		self.assertTrue({"Paid", "Overdue"} <= values)  # the full Select list, not just what the page shows
		self.assertEqual(by_key["posting_date"]["type"], "date_range")
		self.assertLessEqual(len(p["filters"]), 3)

	def test_wide_rows_fold_extra_columns(self):
		rows = [{"name": "A", "customer": "X", "posting_date": "2026-09-01", "grand_total": 1, "status": "Paid", "outstanding_amount": 1,
			"due_date": "2026-10-01", "remarks": "r", "branch": "b"}]
		p = self._present("Sales Invoice", rows)["presentation"]
		collapsed = [c["key"] for c in p["columns"] if c.get("collapsed")]
		self.assertTrue(collapsed)
		self.assertNotIn("name", collapsed)
		self.assertTrue(all(not c.get("collapsed") for c in p["columns"][:6]))

	def test_count_tool_has_no_presentation(self):
		self.assertIsNone(present("data.count", {"doctype": "Company"}, {"count": 4}, None))


class TestReportPresenter(IntegrationTestCase):
	def test_gl_groups_by_account_when_several(self):
		result = {"report": "General Ledger", "row_count": 3, "filters": {}, "columns": [
			{"fieldname": "posting_date", "label": "d", "fieldtype": "Date"}, {"fieldname": "account", "label": "a", "fieldtype": "Link"},
			{"fieldname": "debit", "label": "d", "fieldtype": "Currency"}, {"fieldname": "credit", "label": "c", "fieldtype": "Currency"}],
			"result": [{"posting_date": "2026-09-02", "account": "B", "debit": 1, "credit": 0}, {"posting_date": "2026-09-01", "account": "A", "debit": 2, "credit": 0},
				{"posting_date": "2026-09-03", "account": "A", "debit": 0, "credit": 1}]}
		with patch("ai_agent.presentation.presenters.reports._default_currency", return_value="YER"):
			p = present("reports.run", {}, result, None)["presentation"]
		self.assertEqual(p["group_by"], "account")
		self.assertEqual([r["cells"]["account"]["v"] for r in p["rows"]], ["A", "A", "B"])

	def test_ledger_totals_skip_opening_total_closing_rows(self):
		result = {"report": "General Ledger", "row_count": 5, "is_truncated": False, "filters": {}, "columns": [
			{"fieldname": "posting_date", "label": "Posting Date", "fieldtype": "Date"},
			{"fieldname": "account", "label": "Account", "fieldtype": "Link"},
			{"fieldname": "debit", "label": "Debit", "fieldtype": "Currency"},
			{"fieldname": "credit", "label": "Credit", "fieldtype": "Currency"},
			{"fieldname": "balance", "label": "Balance", "fieldtype": "Currency"}],
			"result": [
				{"account": "'Opening'", "debit": 0, "credit": 0, "balance": 0},
				{"posting_date": "2026-09-01", "account": "Debtors", "debit": 100, "credit": 0, "balance": 100},
				{"posting_date": "2026-09-02", "account": "Debtors", "debit": 0, "credit": 40, "balance": 60},
				{"account": "'Total'", "debit": 100, "credit": 40, "balance": 60},
				{"account": "'Closing (Opening + Total)'", "debit": 100, "credit": 40, "balance": 60}]}
		with patch("ai_agent.presentation.presenters.reports._default_currency", return_value="YER"):
			env = present("reports.run", {}, result, None)
		p = env["presentation"]
		self.assertEqual(p["variant"], "statement")
		self.assertEqual(p["totals"]["cells"]["debit"]["v"], 100)
		self.assertEqual(p["totals"]["cells"]["credit"]["v"], 40)
		kinds = [r.get("kind") for r in p["rows"]]
		self.assertEqual(kinds[0], "opening")
		self.assertEqual(kinds[-1], "closing")
		self.assertEqual(len(p["rows"]), 4)
		self.assertEqual(p["rows"][0]["cells"]["account"]["d"], t("Opening balance"))
		self.assertEqual(next(k for k in p["kpis"] if k["key"] == "balance")["v"], 60)
