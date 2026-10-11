"""Workflow tools follow the user's own permissions: the transitions offered are the
ones Frappe would let that user take, a request for any other action is refused, and
the tools no longer disappear when submit/cancel is turned off."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine.tools import TOOL_GROUPS, ToolExecutor
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tools.workflow import handle_get_transitions

WORKFLOW = "AI Test Note Workflow"
ROLE = "AI Workflow Test Escalator"
USER = "ai-workflow-test@example.com"


class TestWorkflowTools(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
		if not frappe.db.exists("User", USER):
			user = frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Workflow Test", "send_welcome_email": 0})
			user.insert(ignore_permissions=True)
			user.add_roles("System Manager")
		for state in ("WF Draft", "WF Approved", "WF Escalated"):
			if not frappe.db.exists("Workflow State", state):
				frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(ignore_permissions=True)
		for action in ("WF Approve", "WF Escalate"):
			if not frappe.db.exists("Workflow Action Master", action):
				frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(ignore_permissions=True)
		# the workflow adds a workflow_state field to Note; remove it afterwards unless it was already there
		cls.made_field = not frappe.db.exists("Custom Field", "Note-workflow_state")
		if frappe.db.exists("Workflow", WORKFLOW):
			frappe.delete_doc("Workflow", WORKFLOW, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Workflow", "workflow_name": WORKFLOW, "document_type": "Note", "is_active": 1,
			"workflow_state_field": "workflow_state", "send_email_alert": 0,
			"states": [
				{"state": "WF Draft", "doc_status": "0", "allow_edit": "System Manager"},
				{"state": "WF Approved", "doc_status": "0", "allow_edit": "System Manager"},
				{"state": "WF Escalated", "doc_status": "0", "allow_edit": "System Manager"},
			],
			"transitions": [
				{"state": "WF Draft", "action": "WF Approve", "next_state": "WF Approved", "allowed": "System Manager", "allow_self_approval": 1},
				{"state": "WF Draft", "action": "WF Escalate", "next_state": "WF Escalated", "allowed": ROLE, "allow_self_approval": 1},
			],
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all("Note", filters={"title": ["like", "workflow-tool-test%"]}, pluck="name"):
			frappe.delete_doc("Note", name, force=True, ignore_permissions=True)
		frappe.delete_doc("Workflow", WORKFLOW, force=True, ignore_permissions=True)
		if cls.made_field and frappe.db.exists("Custom Field", "Note-workflow_state"):
			frappe.delete_doc("Custom Field", "Note-workflow_state", force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user(USER)
		self.note = frappe.get_doc({"doctype": "Note", "title": "workflow-tool-test " + frappe.generate_hash(length=6), "public": 1}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_only_the_users_own_transitions_are_offered(self):
		out = handle_get_transitions({"doctype": "Note", "document_name": self.note.name}, PermissionContext(USER))
		self.assertEqual(out["state"], "WF Draft")
		self.assertEqual([a["action"] for a in out["actions"]], ["WF Approve"])
		self.assertTrue(out["actions"][0]["agent_may_request"])
		self.assertEqual([o["action"] for o in out["needs_another_user"]], ["WF Escalate"])

	def test_a_request_for_another_roles_action_is_refused(self):
		engine = ApprovalEngine(PermissionContext(USER))
		with self.assertRaises(PermissionDenied) as caught:
			engine.assert_action_allowed("Workflow", "Note", self.note.name, {"action": "WF Escalate"}, USER)
		self.assertIn("WF Approve", str(caught.exception), "the error names what the user may do")
		engine.assert_action_allowed("Workflow", "Note", self.note.name, {"action": "WF Approve"}, USER)

	def test_workflow_tools_stay_when_submit_cancel_is_off(self):
		previous = frappe.db.get_single_value("AI Settings", "allow_submit_cancel_actions")
		frappe.db.set_single_value("AI Settings", "allow_submit_cancel_actions", 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		try:
			agent = frappe.get_doc("AI Agent", "ERP Power Operator")
			ex = ToolExecutor(PermissionContext(USER, agent.name), agent, None)
			self.assertTrue(ex.allowed("actions.request_workflow_action"))
			self.assertTrue(ex.allowed("workflow.get_transitions"))
			self.assertFalse(ex.allowed("actions.request_submit"))
			self.assertIn("actions.request_workflow_action", TOOL_GROUPS["workflow"]["tools"])
		finally:
			frappe.db.set_single_value("AI Settings", "allow_submit_cancel_actions", previous or 0)
			frappe.clear_document_cache("AI Settings", "AI Settings")
