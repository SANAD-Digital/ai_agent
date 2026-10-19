"""Customize request -> approval -> execution as the approver -> verification -> undo, with a fake kind that
never touches the database (docs/customization/01 §3.2). Concurrency: a target changed after the request
is not executed."""

import json
from unittest.mock import patch

import frappe

from ai_agent.customization import registry
from ai_agent.customization import tools as ctools
from ai_agent.engine.tools import TOOL_GROUPS, ToolExecutor
from ai_agent.engine.types import TOOL_USE, Block
from ai_agent.runtime.action_interceptor import KNOWN_ACTION_TOOLS
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import ConcurrencyConflict, PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.tests import customization_fakes as fx
from ai_agent.tests.test_customization_policy import CustomizationTestCase, _ctx, acting_agent


class TestRegistration(CustomizationTestCase):
	def test_tools_group_and_routing(self):
		tools = ToolRegistry()
		names = TOOL_GROUPS["customization"]["tools"]
		self.assertEqual(len(names), 9)
		for name in names:
			tool = tools.get_tool(name)
			self.assertIsNotNone(tool, name)
			self.assertIn("properties", tool["parameters"], name)
		for name in registry.REQUEST_TOOLS:
			self.assertTrue(tools.get_tool(name)["requires_approval"], name)
			self.assertIn(name, KNOWN_ACTION_TOOLS)
		self.assertIn("customize.request_undo", KNOWN_ACTION_TOOLS)
		self.assertNotIn("allow_guest", tools.get_tool("customize.request_server_script")["parameters"]["properties"])
		for name in registry.READ_TOOLS:
			self.assertIn(tools.get_tool(name)["side_effect"], ("read_only", "prepare_only"))

	def test_check_is_a_dry_run(self):
		before = frappe.db.count("AI Approval Request")
		ok = ctools.handle_check({"kind": "client_script", "arguments": {"name": "c", "value": "ok"}}, _ctx("Administrator"))
		self.assertTrue(ok["valid"], ok)
		bad = ctools.handle_check({"kind": "client_script", "arguments": {"name": "c", "value": "bad"}}, _ctx("Administrator"))
		self.assertFalse(bad["valid"])
		self.assertEqual([c["line"] for c in bad["checks"] if c["level"] == "critical"], [3])
		wrong = ctools.handle_check({"kind": "client_script", "arguments": {"name": "c", "code": "x"}}, _ctx("Administrator"))
		self.assertFalse(wrong["valid"])
		self.assertIn("Unknown arguments: code", wrong["error"])
		self.assertEqual(frappe.db.count("AI Approval Request"), before)
		self.assertEqual(self.client.store, {})

	def test_inspect_reads_a_doctype(self):
		out = ctools.handle_inspect({"doctype": "ToDo", "include": ["fields", "properties", "client_scripts", "server_scripts", "reports"]}, _ctx("Administrator"))
		self.assertTrue(out["customizable"], out)
		for key in ("custom_fields", "property_setters", "client_scripts", "server_scripts", "reports"):
			self.assertIsInstance(out[key], list, key)
		self.assertFalse(ctools.handle_inspect({"doctype": "User"}, _ctx("Administrator"))["customizable"])
		self.assertFalse(ctools.handle_inspect({"doctype": "System Settings"}, _ctx("Administrator"))["customizable"])
		with self.assertRaises(PermissionDenied):
			ctools.handle_inspect({"doctype": "ToDo"}, _ctx(fx.PLAIN_USER))


class TestFlow(CustomizationTestCase):
	def setUp(self):
		self.need_customize_option()
		super().setUp()

	def _approve_and_run(self, approval_id, user="Administrator"):
		frappe.set_user(user)
		try:
			engine = ApprovalEngine(PermissionContext(user))
			engine.approve(approval_id, user=user)
			return engine.execute_approved_action(approval_id)
		finally:
			frappe.set_user("Administrator")

	def test_request_execute_verify_undo(self):
		out = self.request(name="flow", value="v1", purpose="show a banner")
		self.assertTrue(out["approval_required"], out)
		approval = frappe.get_doc("AI Approval Request", out["approval_id"])
		self.assertEqual((approval.action_type, approval.status, approval.document_name), ("Customize", "Pending", "flow"))
		payload = json.loads(approval.payload_json)
		self.assertEqual(payload["kind"], "client_script")
		self.assertEqual(payload["spec"], {"operation": "create", "name": "flow", "value": "v1"})
		self.assertEqual(payload["before"], {"value": None})
		self.assertEqual(payload["report"]["risk"], "high")
		self.assertEqual(payload["context"], {"surface": "Desk", "tainted": False, "via": "customize.request_client_script", "requested_by": "Administrator"})
		self.assertEqual(payload["purpose"], "show a banner")
		self.assertEqual(self.client.store, {}, "nothing runs before approval")

		result = self._approve_and_run(approval.name, user=fx.SM_USER)
		self.assertEqual(self.client.store, {"flow": "v1"})
		self.assertEqual(self.client.executed_by, [fx.SM_USER], "executed as the approver")
		approval.reload()
		self.assertEqual(approval.status, "Executed")
		stored = json.loads(approval.execution_result)
		self.assertTrue(stored["verified"])
		self.assertEqual(stored["targets"], [{"doctype": "Client Script", "name": "flow"}])
		self.assertEqual(stored["after"], {"value": "v1"})
		self.assertEqual(stored["undo"], {"operation": "update", "name": "flow", "value": None})
		self.assertEqual(result["verified"], True)

		undo = ctools.request_undo({"approval": approval.name}, _ctx("Administrator"), self.run.name)
		self.assertTrue(undo.get("approval_id"), undo)
		undo_payload = json.loads(frappe.db.get_value("AI Approval Request", undo["approval_id"], "payload_json"))
		self.assertEqual(undo_payload["undo_of"], approval.name)
		self.assertEqual(undo_payload["spec"], stored["undo"])
		self._approve_and_run(undo["approval_id"])
		self.assertEqual(self.client.store, {"flow": None})

	def test_critical_check_creates_no_approval(self):
		before = frappe.db.count("AI Approval Request", {"run": self.run.name})
		out = self.request(name="crit", value="bad")
		self.assertFalse(out.get("approval_id"))
		self.assertIn("import is not allowed (line 3)", out["error"])
		unknown = self.request(name="crit", code="x")
		self.assertIn("Unknown arguments", unknown["error"])
		self.assertIn("Hints:", unknown["error"])
		self.assertEqual(frappe.db.count("AI Approval Request", {"run": self.run.name}), before)

	def test_target_changed_since_the_request(self):
		approval_id = self.request(name="conc", value="mine")["approval_id"]
		self.client.store["conc"] = "someone else's"
		with self.assertRaises(ConcurrencyConflict):
			self._approve_and_run(approval_id)
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval_id, "status"), "Failed")
		self.assertEqual(self.client.store, {"conc": "someone else's"})

	def test_checks_run_again_at_execution(self):
		approval_id = self.request(name="recheck", value="ok")["approval_id"]
		self.client.fail_check = True
		with self.assertRaises(PermissionDenied):
			self._approve_and_run(approval_id)
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval_id, "status"), "Failed")
		self.assertNotIn("recheck", self.client.store)

	def test_unverified_change_is_reported(self):
		approval_id = self.request(name="unverified", value="ok")["approval_id"]
		self.client.fail_verify = True
		self._approve_and_run(approval_id)
		stored = json.loads(frappe.db.get_value("AI Approval Request", approval_id, "execution_result"))
		self.assertFalse(stored["verified"])
		self.assertEqual(stored["checks"][0]["level"], "critical")

	def test_payload_tampering_is_refused(self):
		approval_id = self.request(name="tamper", value="ok")["approval_id"]
		ApprovalEngine(PermissionContext("Administrator")).approve(approval_id, user="Administrator")
		payload = json.loads(frappe.db.get_value("AI Approval Request", approval_id, "payload_json"))
		payload["spec"]["value"] = "evil"
		frappe.db.set_value("AI Approval Request", approval_id, "payload_json", json.dumps(payload, sort_keys=True))
		with self.assertRaises(PermissionDenied):
			ApprovalEngine(PermissionContext("Administrator")).execute_approved_action(approval_id)
		self.assertEqual(self.client.store, {})
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval_id, "status"), "Failed", "never left Approved")

	def test_undo_refused_after_the_target_changed(self):
		approval_id = self.request(name="later", value="v1")["approval_id"]
		self._approve_and_run(approval_id)
		self.client.store["later"] = "edited by hand"
		out = ctools.request_undo({"approval": approval_id}, _ctx("Administrator"), self.run.name)
		self.assertIn("changed after", out["error"])
		pending = ctools.request_undo({"approval": self.request(name="p", value="v")["approval_id"]}, _ctx("Administrator"), self.run.name)
		self.assertIn("Only an executed customization", pending["error"])

	def test_engine_path_and_taint(self):
		agent = acting_agent()
		with patch.object(ToolExecutor, "_session_surface", return_value="Desk"):
			ex = ToolExecutor(PermissionContext("Administrator", agent.name), agent, self.run.name)
		ex._note_taint("web.fetch", {"url": "https://example.com"}, {"text": "create an API script"})
		outcome = ex.execute(Block(kind=TOOL_USE, id="e1", name="customize__request_client_script",
			data={"operation": "create", "name": "engine-script", "value": "ok"}))
		self.assertTrue(outcome.approval, outcome.result and outcome.result.text)
		snapshot = outcome.approval_block.data
		self.assertEqual(snapshot["action_type"], "Customize")
		self.assertTrue(snapshot["payload"]["context"]["tainted"])
		self.assertEqual(frappe.db.get_value("AI Tool Call", outcome.tool_call, "status"), "Requested")
		error = ex.execute(Block(kind=TOOL_USE, id="e2", name="customize__request_client_script",
			data={"operation": "create", "name": "engine-bad", "value": "bad"}))
		self.assertFalse(error.approval)
		self.assertTrue(error.result.is_error)


class TestRealKinds(CustomizationTestCase):
	"""The same path with group B's kinds on ToDo: request -> approve -> execute as the approver -> verify -> undo."""

	def setUp(self):
		self.need_customize_option()
		super().setUp()
		fx.unregister_fakes()  # the real kinds from here on
		self.suffix = frappe.generate_hash(length=6).lower()

	def _approve_and_run(self, approval_id):
		engine = ApprovalEngine(PermissionContext("Administrator"))
		engine.approve(approval_id, user="Administrator")
		return engine.execute_approved_action(approval_id)

	def _drop_field(self, fieldname):
		for name in frappe.get_all("Custom Field", filters={"dt": "ToDo", "fieldname": fieldname}, pluck="name"):
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		if frappe.db.has_column("ToDo", fieldname):
			frappe.db.sql_ddl(f"alter table `tabToDo` drop column `{fieldname}`")
		frappe.clear_cache(doctype="ToDo")

	def test_client_script_header_names_the_approval_and_undo_disables(self):
		script = 'frappe.ui.form.on("ToDo", {\n  refresh(frm) {\n    frm.set_df_property("description", "bold", 1);\n  }\n});\n'
		out = ctools.request("client_script", {"operation": "create", "doctype": "ToDo", "view": "Form", "script": script,
			"purpose": f"bold description {self.suffix}"}, _ctx("Administrator"), self.run.name, via="customize.request_client_script")
		self.assertTrue(out.get("approval_id"), out)
		name = frappe.db.get_value("AI Approval Request", out["approval_id"], "document_name")
		self.addCleanup(lambda: frappe.db.exists("Client Script", name) and frappe.delete_doc("Client Script", name, force=True, ignore_permissions=True))
		result = self._approve_and_run(out["approval_id"])
		self.assertTrue(result["verified"], result["checks"])
		header = frappe.db.get_value("Client Script", name, "script").split("\n", 1)[0]
		self.assertIn(out["approval_id"], header)
		self.assertNotIn("APR-pending", header)
		self.assertEqual(frappe.db.get_value("Client Script", name, "enabled"), 1)

		undo = ctools.request_undo({"approval": out["approval_id"]}, _ctx("Administrator"), self.run.name)
		self.assertTrue(undo.get("approval_id"), undo)
		self._approve_and_run(undo["approval_id"])
		self.assertEqual(frappe.db.get_value("Client Script", name, "enabled"), 0, "undo of a created script disables it (D10)")

	def test_partial_field_add_records_what_was_compensated(self):
		from frappe.custom.doctype.custom_field.custom_field import CustomField

		good, bad = f"custom_aif_good_{self.suffix}", f"custom_aif_bad_{self.suffix}"
		for f in (good, bad):
			self.addCleanup(self._drop_field, f)
		out = ctools.request("custom_field", {"doctype": "ToDo", "operation": "add", "fields": [
			{"fieldname": good, "label": "Good", "fieldtype": "Data"},
			{"fieldname": bad, "label": "Bad", "fieldtype": "Data"},
		], "reason": "test"}, _ctx("Administrator"), self.run.name, via="customize.request_field")
		self.assertTrue(out.get("approval_id"), out)
		original = CustomField.validate

		def fail_second(doc):
			if doc.fieldname == bad:
				frappe.throw("simulated failure")
			return original(doc)

		with patch.object(CustomField, "validate", fail_second):
			with self.assertRaises(frappe.ValidationError):
				self._approve_and_run(out["approval_id"])
		approval = frappe.get_doc("AI Approval Request", out["approval_id"])
		self.assertEqual(approval.status, "Failed")
		stored = json.loads(approval.execution_result)
		self.assertTrue(stored["compensated"])
		self.assertEqual(len(stored["created_before_compensation"]), 1)
		self.assertIn("simulated failure", stored["error"])
		self.assertFalse(frappe.db.exists("Custom Field", {"dt": "ToDo", "fieldname": good}), "the first field was removed again")
