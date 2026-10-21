"""Print Format kind: custom Jinja print formats (docs/customization/01 §3.1 v2, §3.5)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.print_format import KIND, current_default

ADMIN = "Administrator"
GOOD_HTML = """<div class="print-format">
  <h2>{{ _("Task") }} {{ doc.name }}</h2>
  <p>{{ doc.description | striptags }}</p>
  <p>AI-PF-MARKER {{ doc.status }}</p>
</div>"""


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizePrintFormat(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)
		self.default_before = current_default("ToDo")
		todo = frappe.get_doc({"doctype": "ToDo", "description": f"AI print test {self.suffix}", "status": "Open"}).insert()
		self.todo = todo.name
		self.addCleanup(self._cleanup_todo)

	def _cleanup_todo(self):
		frappe.set_user(ADMIN)
		frappe.delete_doc("ToDo", self.todo, force=True, ignore_permissions=True)
		from ai_agent.customization.kinds.print_format import set_default

		if current_default("ToDo") != self.default_before:
			set_default("ToDo", self.default_before)

	def track(self, name):
		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists("Print Format", name):
				frappe.delete_doc("Print Format", name, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return name

	def spec(self, name, **extra):
		args = {"operation": "create", "name": name, "doc_type": "ToDo", "html": GOOD_HTML,
			"sample_document": self.todo, "purpose": "test print format"}
		args.update(extra)
		return KIND.normalize(args, ADMIN)

	# -- normalize ------------------------------------------------------------------
	def test_normalize_rejects_bad_arguments(self):
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "create", "name": "AI PF x", "doc_type": "ToDo", "html": "x", "script": 1}, ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "AI PF x", "doc_type": "ToDo"}, ADMIN)  # no html
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "rename", "name": "AI PF x"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "name": f"AI PF missing {self.suffix}", "html": "x"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "AI PF x", "doc_type": "ToDo", "html": "x", "font_size": 99}, ADMIN)
		standard = frappe.db.get_value("Print Format", {"standard": "Yes", "print_format_for": "DocType"}, "name")
		if standard:
			with self.assertRaises(CustomizationError):
				KIND.normalize({"operation": "update", "name": standard, "html": "x"}, ADMIN)

	# -- create / verify / undo ---------------------------------------------------------
	def test_create_renders_and_undo_disables(self):
		name = self.track(f"AI Test PF {self.suffix}")
		spec = self.spec(name, font_size=12, margin_top=10)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("print.render", codes(report, "ok"))
		self.assertIn("AI-PF-MARKER", report.preview["html"])
		self.assertEqual(report.preview["document"], {"doctype": "ToDo", "name": self.todo})
		self.assertTrue(report.impact)
		json.dumps(report.to_dict())
		before = KIND.snapshot(spec)
		self.assertFalse(before["exists"])

		result = KIND.execute(spec, ADMIN, approval="APR-TEST")
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		doc = frappe.get_doc("Print Format", name)
		self.assertEqual((doc.standard, doc.custom_format, doc.print_format_type, doc.font_size), ("No", 1, "Jinja", 12))
		self.assertIn("ai_agent:", doc.html.splitlines()[0])
		self.assertEqual(result["undo"], {"operation": "disable", "name": name, "doc_type": "ToDo"})
		self.assertEqual(KIND.snapshot(KIND.normalize(result["undo"], ADMIN)), KIND.snapshot(spec))

		undo = KIND.normalize(result["undo"], ADMIN)
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		undo_result = KIND.execute(undo, ADMIN)
		self.assertFalse([c for c in KIND.verify(undo, undo_result) if c.level == "critical"])
		self.assertEqual(frappe.db.get_value("Print Format", name, "disabled"), 1)
		self.assertEqual(undo_result["undo"]["operation"], "enable")

	def test_update_and_undo_restores_template(self):
		name = self.track(f"AI Test PF upd {self.suffix}")
		KIND.execute(self.spec(name), ADMIN)
		update = KIND.normalize({"operation": "update", "name": name, "html": GOOD_HTML.replace("Task", "Job"),
			"sample_document": self.todo}, ADMIN)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("+  <h2>{{ _(\"Job\") }}", report.preview["code"]["diff"])
		result = KIND.execute(update, ADMIN)
		self.assertIn("Job", frappe.db.get_value("Print Format", name, "html"))
		undo = KIND.normalize(result["undo"], ADMIN)
		KIND.execute(undo, ADMIN)
		html = frappe.db.get_value("Print Format", name, "html")
		self.assertIn("Task", html)
		self.assertNotIn("Job", html)

	def test_default_switch_and_undo(self):
		name = self.track(f"AI Test PF default {self.suffix}")
		spec = self.spec(name, default_print_format=1)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["default_after"], name)
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		self.assertEqual(current_default("ToDo"), name)
		self.assertEqual(frappe.get_meta("ToDo").default_print_format, name)
		self.assertEqual(result["undo"].get("restore_default"), self.default_before)

		undo = KIND.normalize(result["undo"], ADMIN)
		KIND.execute(undo, ADMIN)
		self.assertEqual(current_default("ToDo"), self.default_before)
		self.assertEqual(frappe.db.get_value("Print Format", name, "disabled"), 1)

		# set_default / unset_default on their own, then the undo of set_default
		KIND.execute(KIND.normalize({"operation": "enable", "name": name}, ADMIN), ADMIN)
		set_spec = KIND.normalize({"operation": "set_default", "name": name}, ADMIN)
		self.assertFalse(KIND.check(set_spec, ADMIN).critical())
		result = KIND.execute(set_spec, ADMIN)
		self.assertEqual(current_default("ToDo"), name)
		self.assertEqual(result["undo"]["operation"], "unset_default")
		self.assertEqual(KIND.snapshot(KIND.normalize(result["undo"], ADMIN)), KIND.snapshot(set_spec))
		KIND.execute(KIND.normalize(result["undo"], ADMIN), ADMIN)
		self.assertEqual(current_default("ToDo"), self.default_before)
		unset = KIND.normalize({"operation": "unset_default", "name": name}, ADMIN)
		self.assertIn("print.not_default", critical(KIND.check(unset, ADMIN)))

	# -- checks ---------------------------------------------------------------------------
	def test_template_checks(self):
		cases = {
			"jinja.syntax": "{% if doc.name %}<p>{{ doc.name }}</p>",
			"jinja.write": "{{ frappe.sendmail(recipients=['a@b.c'], subject='x') }}",
			"jinja.attr_filter": "{{ (frappe|attr('send' ~ 'mail'))() }}",
			"jinja.dynamic_access": "{% set k = 'db' %}{{ frappe[k] }}",
			"jinja.dunder": "{{ doc.__class__ }}",
			"print.render": "<p>{{ 1 / 0 }}</p>",
		}
		for code, html in cases.items():
			report = KIND.check(self.spec(f"AI PF chk {self.suffix}", html=html), ADMIN)
			self.assertIn(code, critical(report), (code, [c.message for c in report.checks]))
		saving = KIND.check(self.spec(f"AI PF chk {self.suffix}", html="{% set x = doc.save() %}"), ADMIN)
		self.assertIn("jinja.write", critical(saving))
		syntax = KIND.check(self.spec(f"AI PF chk {self.suffix}", html="<p>a</p>\n{% for %}"), ADMIN)
		self.assertEqual([c.line for c in syntax.critical() if c.code == "jinja.syntax"], [2])

		warnings = KIND.check(self.spec(f"AI PF chk {self.suffix}", html=(
			"<p>{{ frappe.db.get_value('User', doc.owner, 'full_name') }}</p>\n"
			"<img src=\"https://evil.example.org/x.png\">\n<script>alert(1)</script>"),
			css="body { background: url(https://cdn.example.net/bg.png) }"), ADMIN)
		self.assertFalse(warnings.critical(), [c.message for c in warnings.critical()])
		self.assertTrue({"jinja.unpermitted_read", "jinja.external_url", "jinja.script"} <= codes(warnings, "warning"))
		self.assertEqual(warnings.risk, "medium")

		css = KIND.check(self.spec(f"AI PF chk {self.suffix}", css="</style><script>x</script>"), ADMIN)
		self.assertIn("print.css", critical(css))

	def test_doctype_and_sample_checks(self):
		report = KIND.check(KIND.normalize({"operation": "create", "name": f"AI PF user {self.suffix}", "doc_type": "User",
			"html": "<p>{{ doc.name }}</p>"}, ADMIN), ADMIN)
		self.assertIn("print.doctype", critical(report))
		report = KIND.check(self.spec(f"AI PF s {self.suffix}", sample_document="no-such-todo"), ADMIN)
		self.assertIn("print.sample", critical(report))

	def test_renders_on_latest_sales_invoice(self):
		if not frappe.db.count("Sales Invoice"):
			self.skipTest("no Sales Invoice on this site")
		spec = KIND.normalize({"operation": "create", "name": f"AI PF SI {self.suffix}", "doc_type": "Sales Invoice",
			"html": "<h2>{{ doc.name }}</h2>{% for row in doc.items %}<p>{{ row.item_name }} {{ row.get_formatted('amount', doc) }}</p>{% endfor %}"
			"<b>{{ doc.get_formatted('grand_total') }}</b>"}, ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["document"]["doctype"], "Sales Invoice")
		self.assertIn(report.preview["document"]["name"], report.preview["html"])
