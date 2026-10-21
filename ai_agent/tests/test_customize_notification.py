"""Notification kind: Frappe Notifications (docs/customization/01 §3.1 v2, §4.2)."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.notification import KIND

ADMIN = "Administrator"
MESSAGE = """<p>{{ _("Task") }} {{ doc.name }} is {{ doc.status }}.</p>
<p>AI-NT-MARKER {{ doc.description | striptags }}</p>"""


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeNotification(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)
		todo = frappe.get_doc({"doctype": "ToDo", "description": f"AI notification test {self.suffix}", "status": "Open",
			"allocated_to": ADMIN}).insert()
		self.todo = todo.name
		self.addCleanup(lambda: frappe.delete_doc("ToDo", self.todo, force=True, ignore_permissions=True))

	def track(self, name):
		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists("Notification", name):
				frappe.delete_doc("Notification", name, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return name

	def args(self, name, **extra):
		args = {"operation": "create", "name": name, "document_type": "ToDo", "event": "New", "channel": "Email",
			"condition": "doc.status == 'Open'", "subject": "{{ doc.name }} needs attention", "message": MESSAGE,
			"recipients": [{"receiver_by_role": "System Manager"}, {"receiver_by_document_field": "allocated_to"},
				{"receiver_by_document_field": "owner"}],
			"sample_document": self.todo, "purpose": "test notification"}
		args.update(extra)
		return args

	def check(self, name, **extra):
		return KIND.check(KIND.normalize(self.args(name, **extra), ADMIN), ADMIN)

	# -- normalize ------------------------------------------------------------------
	def test_normalize_rejects_bad_arguments(self):
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize(self.args("AI NT x", webhook="x"), ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize(self.args("AI NT x", event="Custom"), ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError):
			KIND.normalize(self.args("AI NT x", channel="WhatsApp"), ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize(self.args("AI NT x", recipients={"receiver_by_role": "System Manager"}), ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize(self.args("AI NT x", recipients=[{"condition": "1"}]), ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "name": f"AI NT missing {self.suffix}", "subject": "x"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "AI NT x", "document_type": "ToDo", "event": "New"}, ADMIN)

	# -- create / update / undo -------------------------------------------------------------
	def test_create_preview_execute_undo(self):
		name = self.track(f"AI Test NT {self.suffix}")
		spec = KIND.normalize(self.args(name), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertTrue({"notification.validate", "notification.condition", "notification.render"} <= codes(report, "ok"), report.checks)
		self.assertTrue(report.preview["meets_condition"])
		self.assertEqual(report.preview["subject_rendered"], f"{self.todo} needs attention")
		self.assertIn("AI-NT-MARKER", report.preview["message_rendered"])
		self.assertGreaterEqual(report.preview["resolved"]["count"], 1)
		self.assertTrue(any("per day" in line for line in report.impact), report.impact)
		self.assertTrue(any("System Manager" in line for line in report.impact), report.impact)
		json.dumps(report.to_dict())

		result = KIND.execute(spec, ADMIN, approval="APR-TEST")
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		doc = frappe.get_doc("Notification", name)
		self.assertEqual((doc.enabled, doc.is_standard, doc.event, doc.channel, len(doc.recipients)), (1, 0, "New", "Email", 3))
		self.assertIn("ai_agent:", doc.message.splitlines()[0])
		self.assertEqual(result["undo"], {"operation": "disable", "name": name})
		self.assertEqual(KIND.snapshot(KIND.normalize(result["undo"], ADMIN)), KIND.snapshot(spec))

		undo = KIND.normalize(result["undo"], ADMIN)
		self.assertFalse(KIND.check(undo, ADMIN).critical())
		undo_result = KIND.execute(undo, ADMIN)
		self.assertFalse([c for c in KIND.verify(undo, undo_result) if c.level == "critical"])
		self.assertEqual(frappe.db.get_value("Notification", name, "enabled"), 0)

	def test_update_and_undo_restores(self):
		name = self.track(f"AI Test NT upd {self.suffix}")
		KIND.execute(KIND.normalize(self.args(name), ADMIN), ADMIN)
		update = KIND.normalize({"operation": "update", "name": name, "subject": "Changed {{ doc.name }}",
			"recipients": [{"receiver_by_role": "System Manager"}], "sample_document": self.todo}, ADMIN)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		result = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, result) if c.level == "critical"])
		self.assertEqual(len(frappe.get_doc("Notification", name).recipients), 1)
		undo = KIND.normalize(result["undo"], ADMIN)
		self.assertFalse(KIND.check(undo, ADMIN).critical(), [c.message for c in KIND.check(undo, ADMIN).critical()])
		KIND.execute(undo, ADMIN)
		doc = frappe.get_doc("Notification", name)
		self.assertEqual(doc.subject, "{{ doc.name }} needs attention")
		self.assertEqual(len(doc.recipients), 3)

	# -- checks ---------------------------------------------------------------------------
	def test_critical_checks(self):
		name = f"AI NT chk {self.suffix}"
		cases = {
			"notification.set_property": {"set_property_after_alert": "status", "property_value": "Closed"},
			"condition.syntax": {"condition": "doc.status =="},
			"condition.write": {"condition": "frappe.db.set_value('ToDo', doc.name, 'status', 'Closed') or True"},
			"notification.condition": {"condition": "doc.status.lower().nope()"},
			"jinja.write": {"message": "{{ frappe.sendmail(recipients=['x@example.com']) }}"},
			"notification.render": {"message": "<p>{{ 1 / 0 }}</p>"},
			"external.confirm": {"recipients": [{"cc": "someone.outside@example.org"}]},
			"notification.role": {"recipients": [{"receiver_by_role": f"No Such Role {self.suffix}"}]},
			"notification.field": {"recipients": [{"receiver_by_document_field": "description"}]},
			"notification.recipients": {"recipients": []},
			"notification.print": {"attach_print": 1, "print_format": f"No PF {self.suffix}"},
			"notification.event": {"event": "Value Change"},
			"notification.subject": {"subject": ""},
		}
		for code, extra in cases.items():
			report = self.check(name, **extra)
			found = critical(report)
			if code == "notification.condition":
				self.assertTrue(found & {"notification.condition", "notification.validate"}, (code, [c.message for c in report.checks]))
			else:
				self.assertIn(code, found, (code, [c.message for c in report.checks]))
		if not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
			self.assertIn("notification.channel", critical(self.check(name, channel="SMS",
				recipients=[{"receiver_by_document_field": "owner"}])))
		self.assertIn("notification.channel", critical(self.check(name, channel="Slack", slack_webhook_url=f"none {self.suffix}")))
		self.assertIn("notification.doctype", critical(self.check(name, document_type="User")))

	def test_external_address_with_confirmation_is_a_warning(self):
		report = self.check(f"AI NT ext {self.suffix}", recipients=[{"cc": "someone.outside@example.org"}], allow_external=True)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("external.address", codes(report, "warning"))
		self.assertEqual(report.risk, "high")
		unread = self.check(f"AI NT read {self.suffix}", message="{{ frappe.db.get_value('User', doc.owner, 'full_name') }}")
		self.assertIn("jinja.unpermitted_read", codes(unread, "warning"))

	def test_system_notification_on_sales_invoice_submit(self):
		if not frappe.db.count("Sales Invoice"):
			self.skipTest("no Sales Invoice on this site")
		report = KIND.check(KIND.normalize({"operation": "create", "name": f"AI NT SI {self.suffix}",
			"document_type": "Sales Invoice", "event": "Submit", "channel": "System Notification",
			"condition": "doc.grand_total > 0", "subject": "Invoice {{ doc.name }} submitted",
			"message": "{{ doc.customer }}: {{ doc.get_formatted('grand_total') }}",
			"recipients": [{"receiver_by_role": "Accounts Manager"}, {"receiver_by_document_field": "owner"}]}, ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["document"]["doctype"], "Sales Invoice")
		self.assertEqual(report.risk, "low")
		bad = KIND.check(KIND.normalize({"operation": "create", "name": f"AI NT SI2 {self.suffix}",
			"document_type": "Sales Invoice", "event": "New", "channel": "Email", "subject": "x", "message": "x",
			"recipients": [{"receiver_by_document_field": "customer"}]}, ADMIN), ADMIN)
		self.assertIn("notification.field", critical(bad))
