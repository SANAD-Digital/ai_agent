"""Dashboard kind: Number Card + Dashboard Chart, optionally placed on a workspace (customize.request_dashboard)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.dashboard import KIND, norm_filters

ADMIN = "Administrator"
PLAIN = "ai-cust-dash-plain@example.com"  # a desk user without System Manager
ROLE = "ai_cust_dash_role"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeDashboard(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)

	def track(self, doctype, name):
		def remove():
			frappe.set_user(ADMIN)
			for table, field in (("Workspace Number Card", "number_card_name"), ("Workspace Chart", "chart_name")):
				frappe.db.delete(table, {field: name})
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return name

	def workspace(self):
		"""A plain (non-app) public workspace to place widgets on."""
		title = f"AI Dash WS {self.suffix}"
		from ai_agent.customization.kinds.workspace import _site_module

		doc = frappe.get_doc({"doctype": "Workspace", "label": title, "title": title, "public": 1, "type": "Workspace",
			"module": _site_module(), "content": json.dumps([{"id": "h1", "type": "header", "data": {"text": "Hi", "col": 12}}])})
		doc.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.db.exists("Workspace", title) and frappe.delete_doc(
			"Workspace", title, force=True, ignore_permissions=True, ignore_on_trash=True))
		return title

	# -- normalize -------------------------------------------------------------------------------------
	def test_normalize_rules(self):
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "dashboard", "name": "x", "definition": {}}, ADMIN)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "disable", "object": "chart", "name": "x"}, ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"type": "Custom", "method": "x.y"}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "chart", "name": "x", "definition": {"chart_type": "Custom", "source": "x"}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "chart", "name": "x", "definition": {"document_type": "ToDo"}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"document_type": "ToDo", "colour": "red"}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "object": "number_card", "name": f"No Card {self.suffix}", "definition": {"label": "y"}}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"document_type": "ToDo"},
				"workspace": {"name": "W", "col": 5}}, ADMIN)
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": "x",
			"definition": {"document_type": "ToDo", "filters": {"status": "Open", "priority": ["in", ["High", "Medium"]]}}}, ADMIN)
		self.assertEqual(spec["definition"]["filters"], [["ToDo", "status", "=", "Open", False], ["ToDo", "priority", "in", ["High", "Medium"], False]])
		self.assertEqual(spec["definition"]["function"], "Count")
		chart = KIND.normalize({"operation": "create", "object": "chart", "name": "x", "definition": {"chart_type": "Count", "document_type": "ToDo", "based_on": "date"}}, ADMIN)
		self.assertEqual((chart["definition"]["timeseries"], chart["definition"]["timespan"]), (1, "Last Year"))
		self.assertEqual(norm_filters([["status", "timespan", "this month"]], "ToDo")[0][2], "Timespan")

	# -- number card: create on a workspace, update, delete, restore --------------------------------------------
	def test_number_card_round_trip(self):
		ws = self.workspace()
		name = self.track("Number Card", f"AI Open ToDos {self.suffix}")
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": name, "purpose": "open todos",
			"definition": {"document_type": "ToDo", "function": "Count", "filters": [["status", "=", "Open"]]},
			"workspace": {"name": ws, "label": "Open", "col": 3}}, ADMIN)
		self.assertEqual(KIND.roles_for(spec), ("System Manager", "Workspace Manager"))
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		expected = len(frappe.get_list("ToDo", filters={"status": "Open"}, pluck="name", limit_page_length=0))
		self.assertEqual(report.preview["value"], expected)
		self.assertIn("dashboard.frappe", codes(report, "ok"))
		json.dumps(report.to_dict())
		self.assertFalse(frappe.db.exists("Number Card", name), "the check must not write")

		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		card = frappe.get_doc("Number Card", name)
		self.assertEqual((card.is_standard, card.module or "", card.label), (0, "", name))
		page = frappe.get_doc("Workspace", ws)
		self.assertEqual([(r.number_card_name, r.label) for r in page.number_cards], [(name, "Open")])
		blocks = [b for b in json.loads(page.content) if b["type"] == "number_card"]
		self.assertEqual(blocks[0]["data"], {"number_card_name": "Open", "col": 3})
		self.assertEqual(result["undo"], {"operation": "delete", "object": "number_card", "name": name})

		update = KIND.normalize({"operation": "update", "object": "number_card", "name": name,
			"definition": {"filters": [["status", "=", "Closed"]], "is_public": 1}}, ADMIN)
		self.assertFalse(KIND.check(update, ADMIN).critical())
		updated = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, updated) if c.level == "critical"])
		self.assertIn("Closed", frappe.db.get_value("Number Card", name, "filters_json"))
		KIND.execute(updated["undo"], ADMIN)
		self.assertIn("Open", frappe.db.get_value("Number Card", name, "filters_json"))
		self.assertEqual(frappe.db.get_value("Number Card", name, "is_public"), 0)

		delete = result["undo"]
		self.assertEqual(KIND.roles_for(delete), ("System Manager", "Workspace Manager"))  # it sits on a workspace
		self.assertFalse(KIND.check(delete, ADMIN).critical())
		deleted = KIND.execute(delete, ADMIN)
		self.assertFalse([c for c in KIND.verify(delete, deleted) if c.level == "critical"])
		page.reload()
		self.assertFalse(page.number_cards)
		self.assertFalse([b for b in json.loads(page.content) if b["type"] == "number_card"])
		restore = deleted["undo"]
		self.assertEqual(restore["operation"], "create")
		self.assertFalse(KIND.check(restore, ADMIN).critical(), [c.message for c in KIND.check(restore, ADMIN).critical()])
		KIND.execute(restore, ADMIN)
		page.reload()
		self.assertEqual([(r.number_card_name, r.label) for r in page.number_cards], [(name, "Open")])

	def test_card_checks(self):
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": f"AI Bad {self.suffix}",
			"definition": {"document_type": "ToDo", "function": "Sum", "aggregate_function_based_on": "description",
				"filters": [["nope", "=", 1], ["status", "Timespan", "someday"], ["date", "between", "2026-01-01"], ["status", "is", "x"],
					["Note", "title", "=", "x"]]}}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertTrue({"dashboard.field", "dashboard.filter"} <= critical(report), critical(report))
		messages = " ".join(c.message for c in report.critical())
		for part in ("has no field nope", "Timespan", "between", "'is'", "Note is neither"):
			self.assertIn(part, messages)
		self.assertNotIn("dashboard.preview", codes(report, "ok"))  # nothing runs with criticals
		for dt in ("User", "System Settings"):
			spec = KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"document_type": dt}}, ADMIN)
			self.assertIn("dashboard.doctype", critical(KIND.check(spec, ADMIN)))
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"document_type": "Note Seen By"}}, ADMIN)
		self.assertIn("dashboard.parent", critical(KIND.check(spec, ADMIN)))
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": "x",
			"definition": {"type": "Report", "report_name": f"No Report {self.suffix}"}}, ADMIN)
		self.assertTrue({"dashboard.report", "dashboard.field", "dashboard.function"} <= critical(KIND.check(spec, ADMIN)))

	def test_name_taken_and_standard(self):
		standard = frappe.get_all("Number Card", filters={"is_standard": 1}, pluck="name", limit=1)
		if standard:
			spec = KIND.normalize({"operation": "create", "object": "number_card", "name": standard[0], "definition": {"document_type": "ToDo"}}, ADMIN)
			self.assertIn("dashboard.name_taken", critical(KIND.check(spec, ADMIN)))
			spec = KIND.normalize({"operation": "update", "object": "number_card", "name": standard[0], "definition": {"color": "#ff0000"}}, ADMIN)
			self.assertIn("dashboard.standard", critical(KIND.check(spec, ADMIN)))
		app_ws = frappe.get_all("Workspace", filters={"standard": 1}, pluck="name", limit=1)  # an app's own page
		if app_ws:
			spec = KIND.normalize({"operation": "create", "object": "number_card", "name": f"AI X {self.suffix}",
				"definition": {"document_type": "ToDo"}, "workspace": app_ws[0]}, ADMIN)
			self.assertIn("dashboard.workspace", critical(KIND.check(spec, ADMIN)))

	# -- charts -------------------------------------------------------------------------------------------------
	def test_chart_round_trip(self):
		name = self.track("Dashboard Chart", f"AI ToDo Trend {self.suffix}")
		spec = KIND.normalize({"operation": "create", "object": "chart", "name": name,
			"definition": {"chart_type": "Count", "document_type": "ToDo", "based_on": "creation", "timespan": "Last Quarter",
				"time_interval": "Monthly", "type": "Bar"}}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertTrue(report.preview["data"]["labels"])
		self.assertIn("dashboard.preview", codes(report, "ok"))
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		doc = frappe.get_doc("Dashboard Chart", name)
		self.assertEqual((doc.timeseries, doc.is_standard, doc.filters_json), (1, 0, "[]"))

		update = KIND.normalize({"operation": "update", "object": "chart", "name": name, "definition": {"time_interval": "Weekly"}}, ADMIN)
		updated = KIND.execute(update, ADMIN)
		self.assertEqual(frappe.db.get_value("Dashboard Chart", name, "time_interval"), "Weekly")
		KIND.execute(updated["undo"], ADMIN)
		self.assertEqual(frappe.db.get_value("Dashboard Chart", name, "time_interval"), "Monthly")
		deleted = KIND.execute(result["undo"], ADMIN)
		self.assertFalse(frappe.db.exists("Dashboard Chart", name))
		KIND.execute(deleted["undo"], ADMIN)
		self.assertEqual(frappe.db.get_value("Dashboard Chart", name, "timeseries"), 1)

	def test_group_by_and_date_range_preview(self):
		spec = KIND.normalize({"operation": "create", "object": "chart", "name": f"AI ToDo Status {self.suffix}",
			"definition": {"chart_type": "Group By", "document_type": "ToDo", "group_by_based_on": "status", "type": "Donut"}}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("datasets", report.preview["data"])
		spec = KIND.normalize({"operation": "create", "object": "chart", "name": f"AI Range {self.suffix}",
			"definition": {"chart_type": "Count", "document_type": "ToDo", "based_on": "date", "timespan": "Select Date Range",
				"from_date": "2026-01-01", "to_date": "2026-06-30", "time_interval": "Monthly"}}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(len(report.preview["data"]["labels"]), 6)

	def test_chart_checks(self):
		bad = KIND.normalize({"operation": "create", "object": "chart", "name": "x",
			"definition": {"chart_type": "Sum", "document_type": "ToDo", "based_on": "status", "value_based_on": "description",
				"timespan": "Select Date Range", "time_interval": "Yearly"}}, ADMIN)
		report = KIND.check(bad, ADMIN)
		self.assertTrue({"dashboard.field", "dashboard.timespan"} <= critical(report), critical(report))
		heat = KIND.normalize({"operation": "create", "object": "chart", "name": "x",
			"definition": {"chart_type": "Group By", "document_type": "ToDo", "group_by_based_on": "nope", "type": "Heatmap"}}, ADMIN)
		report = KIND.check(heat, ADMIN)
		self.assertTrue({"dashboard.shape", "dashboard.field"} <= critical(report), critical(report))
		self.assertIn("dashboard.heatmap", codes(report, "warning"))
		interval = KIND.normalize({"operation": "create", "object": "chart", "name": "x",
			"definition": {"chart_type": "Count", "document_type": "ToDo", "based_on": "creation", "timespan": "Last Week", "time_interval": "Monthly", "type": "Pie"}}, ADMIN)
		warnings = codes(KIND.check(interval, ADMIN), "warning")
		self.assertTrue({"dashboard.timespan", "dashboard.shape"} <= warnings, warnings)
		report_chart = KIND.normalize({"operation": "create", "object": "chart", "name": "x",
			"definition": {"chart_type": "Report", "report_name": f"No Report {self.suffix}"}}, ADMIN)
		self.assertIn("dashboard.report", critical(KIND.check(report_chart, ADMIN)))

	# -- exposure ---------------------------------------------------------------------------------------------
	def test_public_and_role_exposure_warnings(self):
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
		card = KIND.normalize({"operation": "create", "object": "number_card", "name": f"AI Errors {self.suffix}",
			"definition": {"document_type": "Error Log", "is_public": 1}}, ADMIN)
		report = KIND.check(card, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("dashboard.public_roles", codes(report, "warning"))
		chart = KIND.normalize({"operation": "create", "object": "chart", "name": f"AI Errors Chart {self.suffix}",
			"definition": {"chart_type": "Count", "document_type": "Error Log", "based_on": "creation", "roles": [ROLE]}}, ADMIN)
		report = KIND.check(chart, ADMIN)
		widening = [c.message for c in report.checks if c.code == "dashboard.role_widening"]
		self.assertTrue(widening and ROLE in widening[0], widening)
		todo = KIND.normalize({"operation": "create", "object": "number_card", "name": "x", "definition": {"document_type": "ToDo", "is_public": 1}}, ADMIN)
		self.assertNotIn("dashboard.public_roles", codes(KIND.check(todo, ADMIN), "warning"))  # everyone reads ToDo

	def test_requester_roles_and_read(self):
		from ai_agent.tests.utils import quota_bypass

		if not frappe.db.exists("User", PLAIN):
			with quota_bypass():
				frappe.get_doc({"doctype": "User", "email": PLAIN, "first_name": "Dash Plain", "send_welcome_email": 0,
					"user_type": "System User"}).insert(ignore_permissions=True)
		spec = KIND.normalize({"operation": "create", "object": "number_card", "name": f"AI Plain {self.suffix}",
			"definition": {"document_type": "Error Log"}}, PLAIN)
		found = critical(KIND.check(spec, PLAIN))
		self.assertTrue({"roles.missing", "dashboard.read"} <= found, found)
