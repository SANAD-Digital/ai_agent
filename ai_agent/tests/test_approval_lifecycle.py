"""The approval lifecycle over MCP, against the real engine and database:
request -> pending -> decision (chat or card) -> execute -> verify, exactly once; rejection; approval not required; original goal."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import approvals, continuation
from ai_agent.mcp.service import MCPService
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.tests.utils import delete_docs

USER = "Administrator"


def service(ui=True):
	svc = MCPService.__new__(MCPService)
	svc.user, svc.company, svc.ui_supported = USER, None, ui
	frappe.flags.mcp_ui_supported = ui
	svc.agent = frappe.get_doc("AI Agent", "ERP Power Operator")
	svc.registry = ToolRegistry()
	svc.settings = frappe._dict()
	return svc


class LifecycleCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(USER)
		self.notes, self.approvals_made = [], []
		self.policy = {"required": True, "card": True}
		for target, fn in ((approvals, "approval_required"), (approvals, "four_eyes_enabled")):
			pass
		self._patch(approvals, "approval_required", lambda: self.policy["required"])
		self._patch(approvals, "app_approval_enabled", lambda: self.policy["required"] and self.policy["card"])
		self._patch(approvals, "four_eyes_enabled", lambda: False)
		self._patch(MCPService, "_is_exposed", lambda self_, tool: True)
		self._patch(MCPService, "_action_requests_enabled", lambda self_: True)
		self._previous_four_eyes = frappe.db.get_single_value("AI Settings", "require_four_eyes")
		frappe.db.set_single_value("AI Settings", "require_four_eyes", 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def _patch(self, target, name, fn):
		patcher = patch.object(target, name, fn)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.set_single_value("AI Settings", "require_four_eyes", self._previous_four_eyes or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		titles = [t for t in self.notes]
		for name in frappe.get_all("Note", filters={"title": ["in", titles or ["-"]]}, pluck="name"):
			frappe.delete_doc("Note", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("AI Approval Request", filters={"action_summary": ["like", "%lifecycle-test%"]}, pluck="name"):
			for c in frappe.get_all("Comment", filters={"reference_doctype": "AI Approval Request", "reference_name": name}, pluck="name"):
				frappe.delete_doc("Comment", c, force=True, ignore_permissions=True)
		delete_docs("AI Approval Request", frappe.get_all("AI Approval Request", filters={"action_summary": ["like", "%lifecycle-test%"]}, pluck="name"))
		delete_docs("AI Run", frappe.get_all("AI Run", filters={"prompt": ["like", "MCP tool call: actions.request_%"], "user": USER, "creation": [">=", self._started]}, pluck="name"))
		frappe.db.commit()
		frappe.flags.mcp_ui_supported = None

	def run(self, result=None):
		self._started = frappe.utils.now_datetime()
		return super().run(result)

	def note(self, suffix):
		title = f"lifecycle-test {frappe.generate_hash(length=6)} {suffix}"
		self.notes.append(title)
		return title

	def request_note(self, svc, title, then=None):
		args = {"doctype": "Note", "payload": {"title": title, "content": "x"}}
		if then:
			args["then"] = then
		with patch("ai_agent.tools.actions.handle_request_create", wraps=None) if False else _summary_patch():
			return svc.call_tool("actions__request_create_draft", args)


def _summary_patch():
	"""Tag every request made by these tests so they can be cleaned up whatever happens."""
	from ai_agent.runtime.approval_engine import ApprovalEngine

	original = ApprovalEngine.create_approval_request

	def tagged(self, **kw):
		kw["action_summary"] = f"{kw.get('action_summary', '')} lifecycle-test"
		return original(self, **kw)

	return patch.object(ApprovalEngine, "create_approval_request", tagged)


class TestToolsList(LifecycleCase):
	def test_decide_is_a_real_model_visible_tool(self):
		tools = {t["name"]: t for t in service().list_tools()}
		for name in ("approvals__decide", "approvals__list_pending", "approvals__status"):
			self.assertIn(name, tools)
			self.assertIn("model", tools[name]["_meta"]["ui"]["visibility"])
		decide = tools["approvals__decide"]
		self.assertEqual(decide["inputSchema"]["required"], ["approval_id", "decision"])
		self.assertEqual(decide["inputSchema"]["properties"]["decision"]["enum"], ["approve", "reject"])
		self.assertFalse(decide["annotations"]["readOnlyHint"])
		self.assertEqual(tools["approvals__revise"]["_meta"]["ui"]["visibility"], ["app"])
		self.assertEqual(tools["view__event"]["_meta"]["ui"]["visibility"], ["app"])

	def test_every_tool_a_card_button_names_exists_in_tools_list(self):
		svc = service()
		listed = {t["name"] for t in svc.list_tools()}
		result = self.request_note(svc, self.note("buttons"))
		card = result["_meta"]["ai_agent/presentation"]
		named = {a["tool"] for a in card["actions"] if a.get("tool")} | {"approvals__revise"}
		self.assertTrue(named)
		self.assertLessEqual(named, listed, f"buttons point at tools the host cannot call: {named - listed}")

	def test_card_switch_off_keeps_chat_approval_and_hides_app_only_tools(self):
		self.policy["card"] = False
		listed = {t["name"] for t in service().list_tools()}
		self.assertIn("approvals__decide", listed)
		self.assertNotIn("approvals__revise", listed)


class TestChatApproval(LifecycleCase):
	def test_pending_then_approve_in_chat_creates_and_verifies_the_document(self):
		svc, title = service(), self.note("chat")
		pending = self.request_note(svc, title)
		approval_id = pending["structuredContent"]["data"]["approval_id"]
		self.assertEqual(pending["structuredContent"]["summary"]["state"], "pending")
		self.assertFalse(frappe.db.exists("Note", {"title": title}))

		done = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})   # no token: typed in chat
		self.assertFalse(done["isError"], done["content"])
		self.assertEqual(done["structuredContent"]["summary"]["state"], "verified")
		self.assertTrue(frappe.db.exists("Note", {"title": title}))
		record = frappe.get_doc("AI Approval Request", approval_id)
		self.assertEqual(record.status, "Executed")
		stored = json.loads(record.execution_result)
		self.assertTrue(stored["verified"])
		self.assertEqual(stored["approved_via"], "chat")
		self.assertIn(stored["document_name"], done["content"][0]["text"] + json.dumps(done["structuredContent"], default=str))

	def test_approving_twice_never_creates_a_second_document(self):
		svc, title = service(), self.note("twice")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		first = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		second = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertEqual(frappe.db.count("Note", {"title": title}), 1)
		self.assertFalse(second["isError"])
		self.assertEqual(second["structuredContent"]["summary"]["state"], "verified")
		self.assertEqual(second["structuredContent"]["data"]["result"]["document_name"], first["structuredContent"]["data"]["result"]["document_name"])

	def test_reject_does_not_execute_and_cannot_be_approved_afterwards(self):
		svc, title = service(), self.note("reject")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		rejected = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "reject", "reason": "changed my mind"})
		self.assertEqual(rejected["structuredContent"]["summary"]["state"], "rejected")
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval_id, "status"), "Rejected")
		after = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertTrue(after["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": title}))

	def test_expired_request_is_refused(self):
		svc, title = service(), self.note("expired")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		frappe.db.set_value("AI Approval Request", approval_id, "expires_at", frappe.utils.add_to_date(None, minutes=-5))
		out = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertTrue(out["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": title}))

	def test_tampered_payload_is_refused(self):
		svc, title = service(), self.note("tamper")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		frappe.db.set_value("AI Approval Request", approval_id, "payload_json", json.dumps({"doctype": "Note", "title": title + " evil", "content": "y"}))
		out = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertTrue(out["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": ["like", f"{title}%"]}))

	def test_someone_elses_request_is_invisible(self):
		svc, title = service(), self.note("other")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		other = service()
		other.user = "Guest"
		out = other.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertTrue(out["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": title}))

	def test_failed_execution_ends_as_failed_with_a_reason(self):
		svc, title = service(), self.note("fail")
		approval_id = self.request_note(svc, title)["structuredContent"]["data"]["approval_id"]
		from ai_agent.runtime.approval_engine import ApprovalEngine

		with patch.object(ApprovalEngine, "_execute_by_type", side_effect=frappe.ValidationError("الحقل مطلوب")):
			out = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertEqual(out["structuredContent"]["summary"]["state"], "failed")
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval_id, "status"), "Failed")
		self.assertIn("الحقل مطلوب", json.dumps(out["_meta"], ensure_ascii=False))


class TestCardApproval(LifecycleCase):
	def test_button_token_is_checked_and_bound_to_its_action(self):
		svc, title = service(), self.note("card")
		pending = self.request_note(svc, title)
		approval_id = pending["structuredContent"]["data"]["approval_id"]
		buttons = {a["key"]: a for a in pending["_meta"]["ai_agent/presentation"]["actions"]}
		wrong = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve", "token": buttons["cancel"]["arguments"]["token"]})
		self.assertTrue(wrong["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": title}))
		ok = svc.call_tool("approvals__decide", buttons["approve"]["arguments"])
		self.assertEqual(ok["structuredContent"]["summary"]["state"], "verified")
		self.assertEqual(json.loads(frappe.db.get_value("AI Approval Request", approval_id, "execution_result"))["approved_via"], "app")

	def test_a_button_press_gets_the_full_card_back_in_structured_content(self):
		svc = service()
		pending = self.request_note(svc, self.note("full"))
		self.assertTrue(pending["structuredContent"]["presentation"]["ref"])
		buttons = {a["key"]: a for a in pending["_meta"]["ai_agent/presentation"]["actions"]}
		done = svc.call_tool("approvals__decide", buttons["approve"]["arguments"])
		card = done["structuredContent"]["presentation"]
		self.assertTrue(card["full"])
		self.assertEqual(card["state"], "verified")
		self.assertTrue(card["result"]["document_name"])
		loaded = svc.call_tool("view__load", {"ref": pending["structuredContent"]["presentation"]["ref"]})
		self.assertEqual(loaded["structuredContent"]["presentation"]["state"], "verified", "a reloaded card shows the current state, not a stale button")

	def test_tokens_never_reach_model_visible_content(self):
		svc = service()
		pending = self.request_note(svc, self.note("secret"))
		tokens = [a["arguments"]["token"] for a in pending["_meta"]["ai_agent/presentation"]["actions"] if (a.get("arguments") or {}).get("token")]
		self.assertTrue(tokens)
		visible = json.dumps(pending["content"]) + json.dumps(pending["structuredContent"], default=str)
		for token in tokens:
			self.assertNotIn(token, visible)


class TestListPending(LifecycleCase):
	def _clear(self):
		for name in frappe.get_all("AI Approval Request", filters={"status": "Pending", "requested_by": USER}, pluck="name"):
			frappe.db.set_value("AI Approval Request", name, "status", "Expired")

	def test_none_one_many(self):
		self._clear()
		svc = service()
		none = svc.call_tool("approvals__list_pending", {})
		self.assertEqual(none["structuredContent"]["data"]["pending"], [])
		a = self.request_note(svc, self.note("one"))["structuredContent"]["data"]["approval_id"]
		one = svc.call_tool("approvals__list_pending", {})
		self.assertEqual(one["structuredContent"]["data"]["pending"], [a])
		self.assertEqual(one["_meta"]["ai_agent/presentation"]["type"], "approval")
		b = self.request_note(svc, self.note("two"))["structuredContent"]["data"]["approval_id"]
		many = svc.call_tool("approvals__list_pending", {})
		self.assertEqual(set(many["structuredContent"]["data"]["pending"]), {a, b})
		self.assertEqual(many["_meta"]["ai_agent/presentation"]["type"], "choice", "several pending: ask, never guess")


class TestApprovalNotRequired(LifecycleCase):
	def test_request_executes_directly_and_is_still_recorded(self):
		self.policy["required"] = False
		svc, title = service(), self.note("direct")
		out = self.request_note(svc, title)
		self.assertFalse(out["isError"], out["content"])
		self.assertIs(out["structuredContent"]["data"]["approval_required"], False)
		self.assertEqual(out["structuredContent"]["summary"]["state"], "verified")
		self.assertTrue(frappe.db.exists("Note", {"title": title}))
		record = frappe.get_doc("AI Approval Request", out["structuredContent"]["data"]["approval_id"])
		self.assertEqual(record.status, "Executed")
		self.assertEqual(json.loads(record.execution_result)["mode"], "direct")
		card = out["_meta"]["ai_agent/presentation"]
		self.assertNotIn("approve", [a["key"] for a in card["actions"]], "no approval card when approval is not required")

	def test_decide_is_refused_when_approval_is_not_required(self):
		self.policy["required"] = False
		out = service().call_tool("approvals__decide", {"approval_id": "x", "decision": "approve"})
		self.assertTrue(out["isError"])

	def test_direct_execution_still_obeys_erpnext_permissions(self):
		self.policy["required"] = False
		svc = service()
		with patch("ai_agent.runtime.permission_context.PermissionContext.has_doctype_permission", return_value=False):
			out = self.request_note(svc, self.note("denied"))
		self.assertTrue(out["isError"])
		self.assertFalse(frappe.db.exists("Note", {"title": self.notes[-1]}))


class TestOriginalGoal(LifecycleCase):
	def test_after_approval_the_next_step_is_prepared_with_the_created_records(self):
		svc, first, second = service(), self.note("prereq"), self.note("goal")
		then = {"goal": "إنشاء المستند الرئيسي", "tool": "actions__request_create_draft",
			"arguments": {"doctype": "Note", "payload": {"title": second, "content": "$created.Note"}}}
		pending = self.request_note(svc, first, then=then)
		approval_id = pending["structuredContent"]["data"]["approval_id"]
		self.assertEqual(continuation.peek(approval_id)["goal"], "إنشاء المستند الرئيسي")

		with _summary_patch():
			out = svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		self.assertFalse(out["isError"], out["content"])
		created = frappe.db.get_value("Note", {"title": first}, "name")
		self.assertTrue(created)
		data = out["structuredContent"]
		self.assertEqual(data["meta"]["continued_from"], approval_id)
		self.assertEqual(data["summary"]["state"], "pending", "the main document now waits for its own approval")
		next_id = data["data"]["approval_id"]
		self.assertNotEqual(next_id, approval_id)
		payload = json.loads(frappe.db.get_value("AI Approval Request", next_id, "payload_json"))
		self.assertEqual(payload["content"], created, "placeholder replaced by the record just created")
		self.assertEqual(out["_meta"]["ai_agent/presentation"]["approval_id"], next_id, "the card shown is the next step's")

		final = svc.call_tool("approvals__decide", {"approval_id": next_id, "decision": "approve"})
		self.assertEqual(final["structuredContent"]["summary"]["state"], "verified")
		self.assertTrue(frappe.db.exists("Note", {"title": second}))

	def test_a_continuation_runs_once_even_if_approval_is_repeated(self):
		svc, first, second = service(), self.note("once-a"), self.note("once-b")
		then = {"goal": "g", "tool": "actions__request_create_draft", "arguments": {"doctype": "Note", "payload": {"title": second, "content": "c"}}}
		approval_id = self.request_note(svc, first, then=then)["structuredContent"]["data"]["approval_id"]
		with _summary_patch():
			svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
			svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "approve"})
		pending = frappe.get_all("AI Approval Request", filters={"payload_json": ["like", f"%{second}%"]}, pluck="name")
		self.assertEqual(len(pending), 1)

	def test_rejecting_drops_the_goal(self):
		svc, first, second = service(), self.note("rej-a"), self.note("rej-b")
		then = {"goal": "g", "tool": "actions__request_create_draft", "arguments": {"doctype": "Note", "payload": {"title": second, "content": "c"}}}
		approval_id = self.request_note(svc, first, then=then)["structuredContent"]["data"]["approval_id"]
		svc.call_tool("approvals__decide", {"approval_id": approval_id, "decision": "reject"})
		self.assertFalse(frappe.get_all("AI Approval Request", filters={"payload_json": ["like", f"%{second}%"]}))

	def test_only_request_tools_can_be_chained(self):
		svc = service()
		for bad in ({"goal": "g", "tool": "data__query", "arguments": {}}, {"goal": "", "tool": "actions__request_create_draft", "arguments": {}}, "x"):
			out = self.request_note(svc, self.note("bad"), then=bad)
			self.assertTrue(out["isError"], bad)
			self.assertFalse(frappe.db.exists("Note", {"title": self.notes[-1]}))

	def test_placeholder_for_something_not_created_is_reported(self):
		with self.assertRaises(continuation.ContinuationError):
			continuation.resolve({"customer": "$created.Customer"}, [{"doctype": "Item", "document_name": "I"}])
		self.assertEqual(continuation.resolve({"a": ["$created.Item", "$created.Item.1"], "b": "plain $created.Item"},
			[{"doctype": "Item", "document_name": "I1"}, {"doctype": "Item", "document_name": "I2"}]), {"a": ["I1", "I2"], "b": "plain $created.Item"})


class TestApprovalSwitch(IntegrationTestCase):
	"""An unset switch must mean "approval required": it is only ever turned off on purpose."""

	def _with(self, value):
		doc = frappe._dict(mcp_require_approval=value)
		doc.get = lambda key, default=None: dict.get(doc, key, default)
		return patch("frappe.get_cached_doc", return_value=doc)

	def test_unset_or_empty_means_required(self):
		for value in (None, "", 1, "1"):
			with self._with(value):
				self.assertTrue(approvals.approval_required(), repr(value))

	def test_only_an_explicit_zero_switches_it_off(self):
		for value in (0, "0"):
			with self._with(value):
				self.assertFalse(approvals.approval_required(), repr(value))


class TestSafeDefaults(IntegrationTestCase):
	def test_unset_setting_means_approval_is_required(self):
		for value, expected in ((None, True), ("", True), (1, True), ("1", True), (0, False), ("0", False)):
			with patch("frappe.get_cached_doc", return_value=frappe._dict(mcp_require_approval=value)):
				self.assertIs(approvals.approval_required(), expected, repr(value))
