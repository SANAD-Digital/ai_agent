"""Phase B1: semantic metric layer (periods, engine, resolvers, permissions)."""

from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from ai_agent.metrics import engine, periods
from ai_agent.metrics.defaults import ensure_default_metrics
from ai_agent.tests.utils import disable_users, quota_bypass

NOW = datetime(2026, 9, 28, 14, 30, 0)
USER_A = "ai-metric-a@example.com"
USER_B = "ai-metric-b@example.com"


class TestPeriods(IntegrationTestCase):
	def _p(self, key):
		with patch.object(periods, "fiscal_year", side_effect=lambda day, company=None: (
			getdate(f"{getdate(day).year}-01-01"), getdate(f"{getdate(day).year}-12-31"))):
			return periods.resolve_period(key, now=NOW)

	def test_today_keeps_current_time(self):
		period = self._p("today")
		self.assertEqual((str(period["from"]), str(period["to"])), ("2026-09-28", "2026-09-28"))
		self.assertEqual(str(period["until_time"]), "14:30:00")

	def test_named_periods(self):
		expected = {
			"yesterday": ("2026-09-27", "2026-09-27"),
			"this_month": ("2026-09-01", "2026-09-28"),
			"last_month": ("2026-08-01", "2026-08-31"),
			"this_quarter": ("2026-07-01", "2026-09-28"),
			"last_quarter": ("2026-04-01", "2026-06-30"),
			"this_fiscal_year": ("2026-01-01", "2026-09-28"),
			"last_fiscal_year": ("2025-01-01", "2025-12-31"),
			"previous_fiscal_year": ("2024-01-01", "2024-12-31"),
		}
		for key, (start, end) in expected.items():
			period = self._p(key)
			self.assertEqual((str(period["from"]), str(period["to"])), (start, end), key)

	def test_yesterday_same_time_comparison(self):
		compare = periods.comparison_period(self._p("today"), "yesterday_same_time")
		self.assertEqual(str(compare["from"]), "2026-09-27")
		self.assertEqual(str(compare["until_time"]), "14:30:00")

	def test_this_month_compares_with_same_days_of_last_month(self):
		compare = periods.comparison_period(self._p("this_month"), "previous_period")
		self.assertEqual((str(compare["from"]), str(compare["to"])), ("2026-08-01", "2026-08-28"))

	def test_unknown_period_is_an_error(self):
		with self.assertRaises(periods.PeriodError):
			periods.resolve_period("next_century")


class TestFormula(IntegrationTestCase):
	def test_formula_arithmetic_only(self):
		self.assertEqual(engine.evaluate_formula("a - b", {"a": 10, "b": 4}), 6)
		self.assertEqual(engine.evaluate_formula("r / g * 100", {"r": 5, "g": 50}), 10)
		self.assertIsNone(engine.evaluate_formula("r / g", {"r": 5, "g": 0}))
		for bad in ("__import__('os')", "a.b", "a ** 2", "[a]"):
			with self.assertRaises(engine.MetricError, msg=bad):
				engine.evaluate_formula(bad, {"a": 1})

	def test_value_parts(self):
		self.assertEqual(engine._value_parts("debit - credit"), [("debit", 1), ("credit", -1)])
		self.assertEqual(engine._value_parts("base_net_total"), [("base_net_total", 1)])


class TestMetricEngine(IntegrationTestCase):
	"""Runs against real Sales Invoices created for two companies."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_default_metrics()
		companies = frappe.get_all("Company", pluck="name", order_by="creation asc", limit=2)
		cls.company_a, cls.company_b = companies
		with quota_bypass():
			for user, company in ((USER_A, cls.company_a), (USER_B, cls.company_b)):
				if not frappe.db.exists("User", user):
					frappe.get_doc({"doctype": "User", "email": user, "first_name": user.split("@")[0],
									"send_welcome_email": 0}).insert(ignore_permissions=True)
				frappe.db.set_value("User", user, "enabled", 1, update_modified=False)
				frappe.get_doc("User", user).add_roles("Accounts User", "Sales User")
				frappe.db.delete("User Permission", {"user": user})
				frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Company",
								"for_value": company, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for user in (USER_A, USER_B):
			frappe.db.delete("User Permission", {"user": user})
		disable_users([USER_A, USER_B])
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_every_default_metric_runs(self):
		frappe.set_user(USER_A)
		for key in frappe.get_all("AI Metric", filters={"enabled": 1}, pluck="name"):
			result = engine.query(key, period="this_fiscal_year", company=self.company_a, user=USER_A)
			self.assertIn("total", result, key)
			self.assertIn("assumption", result, key)
		for key in frappe.get_all("AI Metric Composite", filters={"enabled": 1}, pluck="name"):
			result = engine.query(key, period="this_fiscal_year", company=self.company_a, user=USER_A)
			self.assertEqual(result["metric"], key)

	def test_output_is_compact(self):
		frappe.set_user(USER_A)
		result = engine.query("net_sales", period="this_fiscal_year", dimensions=["customer"],
							  company=self.company_a, user=USER_A)
		self.assertLessEqual(len(result["rows"]), engine.MAX_ROWS)
		self.assertLess(len(frappe.as_json(result)), 8000)
		self.assertEqual(result["source"]["doctype"], "Sales Invoice")

	def test_user_permissions_apply(self):
		"""User B (company B only) asking for company A gets nothing from A."""
		frappe.set_user("Administrator")
		admin_total = engine.query("net_sales", period="this_fiscal_year", company=self.company_a)["total"]
		frappe.set_user(USER_B)
		result = engine.query("net_sales", period="this_fiscal_year", company=self.company_a, user=USER_B)
		self.assertEqual(result["total"], 0)
		if admin_total:
			self.assertNotEqual(result["total"], admin_total)
		grouped = engine.query("net_sales", period="this_fiscal_year", dimensions=["company"], user=USER_B)
		self.assertTrue(all(row["company"] == self.company_b for row in grouped["rows"]))

	def test_child_dimension_applies_parent_user_permissions(self):
		frappe.set_user(USER_B)
		result = engine.query("net_sales", period="this_fiscal_year", dimensions=["item"],
							  company=self.company_a, user=USER_B)
		self.assertEqual(result["rows"], [])

	def test_unknown_dimension_is_explained(self):
		frappe.set_user(USER_A)
		with self.assertRaises(engine.MetricError) as ctx:
			engine.query("net_sales", dimensions=["colour"], user=USER_A)
		self.assertIn("Allowed", str(ctx.exception))

	def test_missing_field_dimension_is_explained(self):
		frappe.set_user(USER_A)
		if frappe.get_meta("Sales Invoice").has_field("branch"):
			self.skipTest("site has a branch field")
		result = engine.query("net_sales", dimensions=["branch", "customer"], user=USER_A)
		self.assertEqual(result["dimensions"], ["customer"])
		self.assertTrue(any("branch" in note for note in result["notes"]))

	def test_role_restricted_metric(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("AI Metric", "gross_sales", "roles", "Accounts Manager")
		frappe.clear_document_cache("AI Metric", "gross_sales")
		try:
			frappe.set_user(USER_A)
			with self.assertRaises(engine.MetricError):
				engine.query("gross_sales", user=USER_A)
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("AI Metric", "gross_sales", "roles", "")
			frappe.clear_document_cache("AI Metric", "gross_sales")


class TestPaymentModeResolver(IntegrationTestCase):
	"""POS/in-invoice payments by mode of payment; everything else = آجل."""

	def _definition(self):
		return engine.get_definition("net_sales")

	def test_pos_and_credit_split(self):
		ensure_default_metrics()
		definition = self._definition()
		dim = definition.dimensions["payment_mode"]
		period = {"key": "custom", "from": getdate("2026-01-01"), "to": getdate("2026-12-31"), "until_time": None}

		def fake_get_list(doctype, **kwargs):
			if doctype == "Sales Invoice Payment":
				return [frappe._dict(mode_of_payment="Cash", v=300), frappe._dict(mode_of_payment="Card", v=200)]
			return []

		sums = {
			("base_change_amount", 1): 50,  # change handed back on cash
			("base_grand_total", 1): 500,   # POS invoices total
			("base_grand_total", 0): 1000,  # non-POS invoices total
		}

		def fake_sum(doctype, field, filters, **kwargs):
			is_pos = next(f[2] for f in filters if f[0] == "is_pos")
			return sums.get((field, is_pos), 0)

		with patch("ai_agent.metrics.resolvers.frappe.get_list", side_effect=fake_get_list), \
				patch("ai_agent.metrics.resolvers._sum", side_effect=fake_sum), \
				patch("ai_agent.metrics.resolvers.frappe.get_cached_value",
					  side_effect=lambda dt, name, field: "Cash" if name == "Cash" else "Bank"), \
				patch("ai_agent.metrics.engine._document_count", return_value=3):
			from ai_agent.metrics.resolvers import payment_mode

			result = payment_mode(definition, period, dim, [], {}, None)
		values = {row["payment_mode"]: row["value"] for row in result["rows"]}
		# Cash 300 - 50 change = 250; POS unpaid = 500 - (250 + 200) = 50 -> credit.
		self.assertEqual(values, {"Cash": 250, "Card": 200, "آجل": 1050})
		self.assertEqual(result["total"], 1500)

	def test_only_credit_sales(self):
		ensure_default_metrics()
		definition = self._definition()
		dim = definition.dimensions["payment_mode"]
		period = {"key": "custom", "from": getdate("2026-01-01"), "to": getdate("2026-12-31"), "until_time": None}
		with patch("ai_agent.metrics.resolvers.frappe.get_list", return_value=[]), \
				patch("ai_agent.metrics.resolvers._sum",
					  side_effect=lambda dt, field, filters, **k: 700 if field == "base_grand_total"
					  and ["is_pos", "=", 0] in filters else 0), \
				patch("ai_agent.metrics.engine._document_count", return_value=1):
			from ai_agent.metrics.resolvers import payment_mode

			result = payment_mode(definition, period, dim, [], {}, None)
		self.assertEqual(result["rows"], [{"payment_mode": "آجل", "value": 700}])
