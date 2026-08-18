from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.exceptions import ToolDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.tests.utils import delete_docs, make_test_run


ACTION_AGENT = frappe._dict({"read_only": 0, "allow_actions": 1, "maximum_tool_calls": 25})
READ_ONLY_AGENT = frappe._dict({"read_only": 1, "allow_actions": 0, "maximum_tool_calls": 25})
CREATE_DRAFT_ARGS = {"doctype": "Note", "payload": {"doctype": "Note", "title": "gateway test"}}


def make_gateway(skill_tools=None):
	return ToolGateway(PermissionContext("Administrator"), ToolRegistry(), skill_tools=skill_tools)


class TestToolGateway(IntegrationTestCase):
	def test_unregistered_tool_is_denied(self):
		with self.assertRaises(ToolDenied):
			make_gateway().authorize_and_execute("shell.exec", {"cmd": "ls"})

	def test_dangerous_top_level_argument_is_denied(self):
		with self.assertRaises(ToolDenied) as ctx:
			make_gateway().authorize_and_execute(
				"data.count", {"doctype": "ToDo", "ignore_permissions": True}
			)
		self.assertIn("Disallowed", str(ctx.exception))

	def test_dangerous_nested_argument_is_denied(self):
		with self.assertRaises(ToolDenied) as ctx:
			make_gateway().authorize_and_execute(
				"data.query",
				{
					"doctype": "ToDo",
					"fields": ["name"],
					"filters": [{"field": "status", "operator": "=", "value": "Open", "run_as": "Administrator"}],
				},
			)
		self.assertIn("Disallowed", str(ctx.exception))

	def test_actions_blocked_for_read_only_agent(self):
		with self.assertRaises(ToolDenied) as ctx:
			make_gateway().authorize_and_execute(
				"actions.request_create_draft", dict(CREATE_DRAFT_ARGS), agent_doc=READ_ONLY_AGENT
			)
		self.assertIn("not allowed by this agent", str(ctx.exception))

	def test_approval_tools_cannot_execute_directly(self):
		with self.assertRaises(ToolDenied) as ctx:
			make_gateway().authorize_and_execute(
				"actions.request_create_draft", dict(CREATE_DRAFT_ARGS), agent_doc=ACTION_AGENT
			)
		self.assertIn("requires approval", str(ctx.exception))

	def test_call_limit_counts_executed_calls(self):
		run = make_test_run()
		try:
			gateway = make_gateway()
			agent = frappe._dict({"read_only": 0, "allow_actions": 1, "maximum_tool_calls": 2})
			for _ in range(2):
				gateway.authorize_and_execute(
					"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name, agent_doc=agent
				)
			with self.assertRaises(ToolDenied) as ctx:
				gateway.authorize_and_execute(
					"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name, agent_doc=agent
				)
			self.assertIn("call limit", str(ctx.exception))
		finally:
			delete_docs(
				"AI Tool Call",
				frappe.get_all("AI Tool Call", filters={"run": run.name}, pluck="name"),
			)
			delete_docs("AI Run", [run.name])
			frappe.db.commit()

	def test_denied_call_does_not_consume_budget(self):
		# Regression: a call denied at validation used to burn the per-tool
		# budget permanently — lethal for skill tools with maximum_calls=1.
		run = make_test_run()
		try:
			gateway = make_gateway()
			agent = frappe._dict({"read_only": 0, "allow_actions": 1, "maximum_tool_calls": 1})
			with self.assertRaises(ToolDenied) as ctx:
				gateway.authorize_and_execute("schema.describe_doctype", {}, run_name=run.name, agent_doc=agent)
			self.assertIn("missing required", str(ctx.exception))
			gateway.authorize_and_execute(
				"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name, agent_doc=agent
			)
			with self.assertRaises(ToolDenied) as ctx:
				gateway.authorize_and_execute(
					"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name, agent_doc=agent
				)
			self.assertIn("call limit", str(ctx.exception))
		finally:
			delete_docs(
				"AI Tool Call",
				frappe.get_all("AI Tool Call", filters={"run": run.name}, pluck="name"),
			)
			delete_docs("AI Run", [run.name])
			frappe.db.commit()

	def test_skill_allowlist_blocks_undeclared_tools(self):
		gateway = make_gateway(skill_tools=[{"tool_name": "data.query"}])
		gateway.enforce_skill_tools = True
		with self.assertRaises(ToolDenied) as ctx:
			gateway.authorize_and_execute("reports.search", {"query": "sales"})
		self.assertIn("not allowed by the loaded skill", str(ctx.exception))

	def test_missing_required_argument_is_denied(self):
		with self.assertRaises(ToolDenied) as ctx:
			make_gateway().authorize_and_execute("data.get_document", {"doctype": "ToDo"})
		self.assertIn("missing required", str(ctx.exception))

	def test_successful_call_is_audited(self):
		run = make_test_run()
		try:
			make_gateway().authorize_and_execute(
				"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name
			)
			self.assertTrue(
				frappe.db.exists(
					"AI Tool Call",
					{"run": run.name, "tool_name": "schema.describe_doctype", "status": "Executed"},
				)
			)
		finally:
			delete_docs(
				"AI Tool Call",
				frappe.get_all("AI Tool Call", filters={"run": run.name}, pluck="name"),
			)
			delete_docs("AI Run", [run.name])
			frappe.db.commit()

	def test_audit_failure_aborts_the_call(self):
		run = make_test_run()
		try:
			with patch(
				"frappe.model.document.Document.insert", side_effect=Exception("audit store down")
			):
				with self.assertRaises(ToolDenied) as ctx:
					make_gateway().authorize_and_execute(
						"schema.describe_doctype", {"doctype": "ToDo"}, run_name=run.name
					)
			self.assertIn("audited", str(ctx.exception))
		finally:
			delete_docs("AI Run", [run.name])
			frappe.db.commit()
