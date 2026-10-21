"""Naming kind: a DocType's autoname / naming series and Document Naming Rules (docs/customization/01 §3.1, §4.2).

DocType naming round trips run on ToDo (autoname hash) and Quality Inspection (naming_series, no documents on the
dev site); every Property Setter, Document Naming Rule, Series counter and ToDo made here is removed in cleanup.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.naming import KIND, naming_rule_for

ADMIN = "Administrator"
NO_SM = "ai-customize-no-read@example.com"
SERIES_DT = "Quality Inspection"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


def series_value(prefix):
	return frappe.db.get_value("Series", prefix, "current", order_by="name")


class TestCustomizeNaming(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.tag = f"AIN{frappe.generate_hash(length=5).upper()}"
		self.setters = {dt: set(frappe.get_all("Property Setter", filters={"doc_type": dt}, pluck="name")) for dt in ("ToDo", SERIES_DT)}
		self.rules = set(frappe.get_all("Document Naming Rule", pluck="name"))
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user(ADMIN)
		for dt, initial in self.setters.items():
			for name in frappe.get_all("Property Setter", filters={"doc_type": dt}, pluck="name"):
				if name not in initial:
					frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)
			frappe.clear_cache(doctype=dt)
		for name in frappe.get_all("Document Naming Rule", pluck="name"):
			if name not in self.rules:
				frappe.delete_doc("Document Naming Rule", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("ToDo", filters={"name": ["like", f"{self.tag}%"]}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("ToDo", filters={"description": ["like", f"%{self.tag}%"]}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.sql("delete from `tabSeries` where name like %s", (f"{self.tag}%",))
		frappe.db.commit()

	def spec(self, **args):
		return KIND.normalize(args, ADMIN)

	# -- normalize ------------------------------------------------------------------------------
	def test_normalize(self):
		bad = [
			{"operation": "set_doctype_naming", "doctype": "ToDo", "autoname": "hash", "colour": 1},
			{"operation": "rename"},
			{"operation": "set_doctype_naming", "doctype": "ToDo"},
			{"operation": "set_doctype_naming", "doctype": "ToDo", "prefix": "X-"},
			{"operation": "set_doctype_naming", "doctype": SERIES_DT, "naming_series_options": [""]},
			{"operation": "create_rule", "doctype": "ToDo"},
			{"operation": "create_rule", "doctype": "ToDo", "prefix": "X-", "rule": "abc"},
			{"operation": "create_rule", "doctype": "ToDo", "prefix": "X-", "autoname": "hash"},
			{"operation": "create_rule", "doctype": "ToDo", "prefix": "X-", "conditions": [{"field": "status", "condition": "like", "value": "x"}]},
			{"operation": "create_rule", "doctype": "ToDo", "prefix": "X-", "conditions": [{"field": "status", "condition": "="}]},
			{"operation": "update_rule", "rule": "no-such-rule-ai"},
		]
		for args in bad:
			with self.subTest(args=args), self.assertRaises(CustomizationError):
				KIND.normalize(args, ADMIN)
		spec = self.spec(operation="set_doctype_naming", doctype="ToDo", autoname="FIELD:allocated_to")
		self.assertEqual((spec["autoname"], spec["naming_rule"]), ("field:allocated_to", "By fieldname"))
		self.assertEqual(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname="Prompt")["autoname"], "prompt")
		self.assertEqual(naming_rule_for("format:X-{#####}"), "Expression (old style)")
		self.assertEqual(naming_rule_for("X-.#####"), "Expression")
		rule = self.spec(operation="create_rule", doctype="ToDo", prefix="X-")
		self.assertEqual((rule["prefix_digits"], rule["priority"], rule["conditions"]), (5, 0, []))

	# -- check: DocType naming --------------------------------------------------------------------
	def test_doctype_naming_criticals(self):
		cases = [
			({"autoname": "field:status"}, "naming.field_duplicates"),  # ToDo status repeats
			({"autoname": "field:description"}, "naming.field"),  # Text Editor cannot name
			({"autoname": "field:no_such_field"}, "naming.field"),
			({"autoname": "autoincrement"}, "naming.column_type"),
			({"autoname": "UUID"}, "naming.column_type"),
			({"autoname": f"{self.tag}-.YYYY."}, "naming.expression"),  # no # counter
			({"autoname": f"{self.tag}-.YYYY.-####"}, "naming.expression"),  # no dot before #
			({"autoname": "naming_series:"}, "naming.series_field"),  # ToDo has no naming_series
			({"autoname": f"format:{self.tag}-{{no_such_field}}-{{####}}"}, "naming.format"),
			({"naming_series_options": [f"{self.tag}-.YYYY.-"]}, "naming.series_field"),
		]
		for args, code in cases:
			with self.subTest(args=args):
				found = critical(KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", **args), ADMIN))
				self.assertIn(code, found, found)
		used = frappe.get_meta("Sales Invoice").get_naming_series_options() if frappe.db.exists("DocType", "Sales Invoice") else []
		used = [o for o in used if o]
		if used:
			found = critical(KIND.check(self.spec(operation="set_doctype_naming", doctype=SERIES_DT, naming_series_options=[used[0]]), ADMIN))
			self.assertIn("naming.series_taken", found)
		found = critical(KIND.check(self.spec(operation="set_doctype_naming", doctype=SERIES_DT, naming_series_options=[f"{self.tag}-$-"]), ADMIN))
		self.assertIn("naming.series_invalid", found)
		self.assertIn("naming.doctype", critical(KIND.check(self.spec(operation="set_doctype_naming", doctype="User", autoname="hash"), ADMIN)))

	def test_doctype_naming_warnings_and_preview(self):
		report = KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname="field:allocated_to"), ADMIN)
		self.assertIn("naming.field_optional", codes(report, "warning"))
		report = KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname="prompt"), ADMIN)
		self.assertIn("naming.prompt", codes(report, "warning"))
		report = KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname=f"format:{self.tag}-{{status}}"), ADMIN)
		self.assertIn("naming.no_counter", codes(report, "warning"))
		self.assertIn("naming.old_style", codes(report, "info"))

		report = KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname=f"{self.tag}-.YY.-.####"), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		yy = frappe.utils.now_datetime().strftime("%y")
		self.assertEqual(report.preview["next_names"], [f"{self.tag}-{yy}-000{i}" for i in (1, 2, 3)])
		self.assertIsNone(series_value(f"{self.tag}-{yy}-"))  # the preview does not consume the counter
		self.assertTrue(any("existing names do not change" in line for line in report.impact))
		json.dumps(report.to_dict())

	def test_collision_is_critical(self):
		yy = frappe.utils.now_datetime().strftime("%y")
		todo = frappe.get_doc({"doctype": "ToDo", "description": self.tag}).insert(ignore_permissions=True)
		frappe.db.sql("update `tabToDo` set name = %s where name = %s", (f"{self.tag}-{yy}-0001", todo.name))
		report = KIND.check(self.spec(operation="set_doctype_naming", doctype="ToDo", autoname=f"{self.tag}-.YY.-.####"), ADMIN)
		self.assertIn("naming.collision", critical(report))

	def test_requester_without_system_manager(self):
		if not frappe.db.exists("User", NO_SM):
			frappe.get_doc({"doctype": "User", "email": NO_SM, "first_name": "No Read", "send_welcome_email": 0,
				"user_type": "Website User"}).insert(ignore_permissions=True)
		spec = self.spec(operation="set_doctype_naming", doctype="ToDo", autoname="hash")
		self.assertIn("roles.missing", critical(KIND.check(spec, NO_SM)))

	# -- execute: DocType naming ------------------------------------------------------------------
	def run_spec(self, spec):
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		return result, report

	def test_doctype_naming_round_trip(self):
		spec = self.spec(operation="set_doctype_naming", doctype="ToDo", autoname=f"{self.tag}-.YY.-.####", purpose="test")
		before = KIND.snapshot(spec)
		result, report = self.run_spec(spec)
		self.assertEqual(frappe.get_meta("ToDo", cached=False).autoname, spec["autoname"])
		todo = frappe.get_doc({"doctype": "ToDo", "description": self.tag}).insert(ignore_permissions=True)
		self.assertEqual(todo.name, report.preview["next_names"][0])  # the preview named it right
		self.assertEqual(result["undo"]["autoname"], before["autoname"])
		self.run_spec(result["undo"])
		self.assertEqual(frappe.get_meta("ToDo", cached=False).autoname, before["autoname"])
		self.assertEqual(KIND.snapshot(spec), before)

	def test_series_options_round_trip(self):
		before_options = frappe.get_meta(SERIES_DT, cached=False).get_naming_series_options()
		options = [f"{self.tag}-.YYYY.-"] + [o for o in before_options if o]
		spec = self.spec(operation="set_doctype_naming", doctype=SERIES_DT, naming_series_options=options)
		result, report = self.run_spec(spec)
		self.assertIn(f"{self.tag}-.YYYY.-", report.preview["series"])
		field = frappe.get_meta(SERIES_DT, cached=False).get_field("naming_series")
		self.assertEqual((field.options.split("\n"), field.default), (options, options[0]))
		self.run_spec(result["undo"])
		self.assertEqual([o for o in frappe.get_meta(SERIES_DT, cached=False).get_naming_series_options() if o],
			[o for o in before_options if o])

	# -- Document Naming Rule ----------------------------------------------------------------------
	def rule_args(self, **over):
		base = {"operation": "create_rule", "doctype": "ToDo", "prefix": f"{self.tag}-", "prefix_digits": 4,
			"priority": 7, "conditions": [{"field": "priority", "condition": "=", "value": "High"}]}
		base.update(over)
		return base

	def test_rule_criticals_and_warnings(self):
		cases = [
			(self.rule_args(prefix=f"{self.tag}-.####"), "naming.prefix"),
			(self.rule_args(prefix_digits=0), "naming.prefix_digits"),
			(self.rule_args(conditions=[{"field": "no_such_field", "condition": "=", "value": "1"}]), "naming.condition_field"),
			(self.rule_args(doctype="User"), "naming.doctype"),
			(self.rule_args(doctype="Sales Invoice Item") if frappe.db.exists("DocType", "Sales Invoice Item") else self.rule_args(doctype="Has Role"), "naming.doctype"),
		]
		for args, code in cases:
			with self.subTest(args=args):
				self.assertIn(code, critical(KIND.check(self.spec(**args), ADMIN)))
		report = KIND.check(self.spec(**self.rule_args(conditions=[{"field": "description", "condition": "=", "value": self.tag + "-none"}])), ADMIN)
		self.assertIn("naming.no_match", codes(report, "warning"))
		self.assertEqual(report.preview["next_names"], [f"{self.tag}-000{i}" for i in (1, 2, 3)])

	def test_rule_round_trip(self):
		spec = self.spec(**self.rule_args())
		result, report = self.run_spec(spec)
		rule = result["targets"][0]["name"]
		self.assertEqual(result["undo"], {"operation": "disable_rule", "rule": rule, "doctype": "ToDo", "name": rule,
			"purpose": result["undo"]["purpose"]})
		high = frappe.get_doc({"doctype": "ToDo", "description": f"{self.tag} high", "priority": "High"}).insert(ignore_permissions=True)
		self.assertEqual(high.name, report.preview["next_names"][0])
		low = frappe.get_doc({"doctype": "ToDo", "description": f"{self.tag} low", "priority": "Low"}).insert(ignore_permissions=True)
		self.assertFalse(low.name.startswith(self.tag))

		# a second rule with the same priority and no conditions: tie + shadowing warnings
		other = KIND.check(self.spec(**self.rule_args(prefix=f"{self.tag}B-", conditions=[])), ADMIN)
		self.assertIn("naming.priority_tie", codes(other, "warning"))
		self.assertEqual(other.preview["other_rules"][0]["rule"], rule)

		update = self.spec(operation="update_rule", rule=rule, prefix=f"{self.tag}U-", priority=3)
		result2, _report = self.run_spec(update)
		self.assertEqual(frappe.db.get_value("Document Naming Rule", rule, ["prefix", "priority"]), (f"{self.tag}U-", 3))
		self.run_spec(result2["undo"])
		self.assertEqual(frappe.db.get_value("Document Naming Rule", rule, ["prefix", "priority"]), (f"{self.tag}-", 7))

		self.run_spec(result["undo"])  # disable
		self.assertEqual(frappe.db.get_value("Document Naming Rule", rule, "disabled"), 1)
		again = frappe.get_doc({"doctype": "ToDo", "description": f"{self.tag} again", "priority": "High"}).insert(ignore_permissions=True)
		self.assertFalse(again.name.startswith(self.tag))
		enable = self.spec(operation="enable_rule", rule=rule)
		result3, _report = self.run_spec(enable)
		self.assertEqual(result3["undo"]["operation"], "disable_rule")
		self.assertEqual(frappe.db.get_value("Document Naming Rule", rule, "disabled"), 0)
		self.run_spec(result3["undo"])
