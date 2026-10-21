"""Integration tests for the chat API and the runner, with a scripted
provider in place of the real model. Tools run for real through the gateway."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api
from ai_agent.engine import runner
from ai_agent.engine.errors import user_message
from ai_agent.engine.providers.base import CancelToken
from ai_agent.engine.providers.fake import FakeProvider
from ai_agent.tests.utils import ensure_test_agent


def fake_provider(scripts):
    provider = FakeProvider(scripts)

    def _provider_for(provider_name=None, model=None):
        return provider, "Default", model or "fake-1"

    return provider, _provider_for


class TestChatApi(IntegrationTestCase):
    def setUp(self):
        ensure_test_agent()
        frappe.set_user("Administrator")
        self._sessions = []

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in self._sessions:
            if frappe.db.exists("AI Chat Session", name):
                for run in frappe.get_all("AI Run", filters={"chat_session": name}, pluck="name"):
                    frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
                frappe.delete_doc("AI Chat Session", name, ignore_permissions=True, force=True)
        frappe.db.commit()

    def _send(self, text, **kwargs):
        with patch.object(runner, "enqueue") as enqueue:
            out = api.send_message(text=text, agent="Unit Test Agent", **kwargs)
            enqueue.assert_called_once()
        self._sessions.append(out["session"]["name"])
        return out

    def test_send_message_creates_session_user_and_pending_assistant(self):
        out = self._send("كم عدد الشركات؟")
        self.assertEqual(out["user_message"]["seq"], 1)
        self.assertEqual(out["assistant_message"]["seq"], 2)
        self.assertEqual(out["assistant_message"]["status"], "pending")
        self.assertEqual(out["session"]["message_count"], 2)
        self.assertEqual(out["session"]["title"], "كم عدد الشركات؟")
        run = frappe.get_doc("AI Run", out["assistant_message"]["run"])
        self.assertEqual(run.chat_session, out["session"]["name"])
        self.assertEqual(run.message, out["assistant_message"]["name"])
        self.assertEqual(frappe.db.get_value("AI Chat Session", out["session"]["name"], "surface"), "Desk")

    def test_second_message_waits_for_the_running_one(self):
        out = self._send("first")
        with self.assertRaises(frappe.ValidationError):
            api.send_message(text="second", session=out["session"]["name"])

    def test_stop_pending_message(self):
        out = self._send("first")
        res = api.stop_message(out["assistant_message"]["name"])
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(frappe.db.get_value("AI Run", out["assistant_message"]["run"], "status"), "Cancelled")

    def test_runner_streams_tool_and_text_into_blocks(self):
        out = self._send("كم عدد الشركات؟")
        provider, provider_for = fake_provider([
            [("thinking", "أحتاج عدّ الشركات."), ("tool", "data__count", {"doctype": "Company"})],
            [("text", "عدد الشركات معروف الآن."), ("stop", "end_turn")],
        ])
        published = []
        with patch.object(runner, "provider_for", provider_for), patch("frappe.publish_realtime", lambda *a, **k: published.append(a[1])):
            runner.execute_message(out["assistant_message"]["name"], user="Administrator")
        published = [p for p in published if isinstance(p, dict) and "event" in p]   # drop Frappe's own doc-update pings
        msg = frappe.get_doc("AI Chat Message", out["assistant_message"]["name"])
        self.assertEqual(msg.status, "completed")
        self.assertEqual(msg.text, "عدد الشركات معروف الآن.")
        self.assertEqual(msg.steps, 2)
        self.assertEqual(msg.tool_calls, 1)
        kinds = frappe.get_all("AI Message Block", filters={"message": msg.name}, fields=["block_type", "status", "content_json"], order_by="seq asc")
        self.assertEqual([k.block_type for k in kinds], ["thinking", "tool_use", "tool_result", "text"])
        self.assertTrue(all(k.status == "completed" for k in kinds))
        result = frappe.parse_json(kinds[2].content_json)
        self.assertIn("count", result)
        self.assertEqual(frappe.db.get_value("AI Run", msg.run, "status"), "Completed")
        types = [p["event"]["type"] for p in published]
        self.assertEqual(types[0], "message.started")
        self.assertIn("block.delta", types)
        self.assertIn("tool.result", types)
        self.assertIn("message.done", types)
        seqs = [p["seq"] for p in published]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        # The transcript endpoint returns the same blocks.
        got = api.get_messages(out["session"]["name"])["messages"]
        self.assertEqual([m["role"] for m in got], ["user", "assistant"])
        self.assertEqual([b["kind"] for b in got[1]["blocks"]], ["thinking", "tool_use", "tool_result", "text"])
        # Second turn replays the first as real messages.
        with patch.object(runner, "enqueue"):
            second = api.send_message(text="شكراً", session=out["session"]["name"])
        provider2, provider_for2 = fake_provider([[("text", "عفواً")]])
        with patch.object(runner, "provider_for", provider_for2), patch("frappe.publish_realtime", lambda *a, **k: None):
            runner.execute_message(second["assistant_message"]["name"], user="Administrator")
        roles = [m.role for m in provider2.requests[0].messages]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(provider2.requests[0].messages[2].blocks[0].kind, "tool_result")

    def test_provider_failure_marks_message_failed(self):
        out = self._send("hi")
        provider, provider_for = fake_provider([[("error", "invalid api key", "auth", False)]])
        # No other model to fall back to (the default is the one that failed).
        with patch.object(runner, "provider_for", provider_for), patch("frappe.publish_realtime", lambda *a, **k: None), \
                patch.object(runner.Runner, "can_fall_back", return_value=False):
            runner.execute_message(out["assistant_message"]["name"], user="Administrator")
        msg = frappe.get_doc("AI Chat Message", out["assistant_message"]["name"])
        self.assertEqual(msg.status, "failed")
        # The user gets a plain sentence; the raw provider text stays on the run for admins.
        self.assertNotIn("invalid api key", msg.error)
        self.assertEqual(msg.error, user_message("auth", user="Administrator"))
        self.assertEqual(frappe.db.get_value("AI Run", msg.run, "status"), "Failed")
        self.assertIn("invalid api key", frappe.db.get_value("AI Run", msg.run, "error_summary"))
        self.assertIn("invalid api key", api.message_dict(msg)["error_detail"], "System Managers see the detail")

    def test_retry_resets_and_edit_truncates(self):
        out = self._send("hi")
        provider, provider_for = fake_provider([[("text", "first answer")]])
        with patch.object(runner, "provider_for", provider_for), patch("frappe.publish_realtime", lambda *a, **k: None):
            runner.execute_message(out["assistant_message"]["name"], user="Administrator")
        with patch.object(runner, "enqueue"):
            res = api.retry_message(out["assistant_message"]["name"])
        self.assertEqual(res["assistant_message"]["status"], "pending")
        self.assertEqual(frappe.db.count("AI Message Block", {"message": out["assistant_message"]["name"]}), 0)
        with patch.object(runner, "enqueue"):
            edited = api.edit_message(out["user_message"]["name"], "hello again")
        self.assertEqual(edited["user_message"]["text"], "hello again")
        self.assertFalse(frappe.db.exists("AI Chat Message", out["assistant_message"]["name"]))
        self.assertEqual(edited["assistant_message"]["seq"], 2)

    def test_delete_session_cascades(self):
        out = self._send("bye")
        api.delete_session(out["session"]["name"])
        self.assertFalse(frappe.db.exists("AI Chat Message", out["user_message"]["name"]))
        self._sessions.remove(out["session"]["name"])

    def test_other_users_cannot_read(self):
        out = self._send("private")
        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                api.get_messages(out["session"]["name"])
        finally:
            frappe.set_user("Administrator")

    def test_cancel_token_reads_flag(self):
        out = self._send("slow")
        runner.request_cancel(out["assistant_message"]["name"])
        doc = frappe.get_doc("AI Chat Message", out["assistant_message"]["name"])
        token = runner.Runner(doc).cancel_token()
        self.assertTrue(token.cancelled)
        self.assertIsInstance(token, CancelToken)


    def test_cancel_token_sees_a_flag_set_by_another_process(self):
        """The job reads the flag before Stop is pressed; the web worker then sets it in Redis only."""
        import pickle

        out = self._send("slow")
        name = out["assistant_message"]["name"]
        doc = frappe.get_doc("AI Chat Message", name)
        token = runner.Runner(doc).cancel_token()
        self.assertFalse(token.cancelled)   # first read: a miss
        cache = frappe.cache()
        cache.set(cache.make_key(runner.CANCEL_KEY.format(name)), pickle.dumps("1"))   # no local cache update
        token._last = 0.0
        self.assertTrue(token.cancelled)

    def test_stop_settles_a_streaming_message_whose_job_is_gone(self):
        out = self._send("slow")
        name = out["assistant_message"]["name"]
        frappe.db.set_value("AI Chat Message", name, "status", "streaming")
        with patch.object(runner, "job_is_alive", return_value=False):
            self.assertEqual(api.stop_message(name)["status"], "cancelled")
        self.assertEqual(frappe.db.get_value("AI Chat Message", name, "status"), "cancelled")

    def test_stop_waits_for_a_live_job(self):
        out = self._send("slow")
        name = out["assistant_message"]["name"]
        frappe.db.set_value("AI Chat Message", name, "status", "streaming")
        with patch.object(runner, "job_is_alive", return_value=True):
            self.assertEqual(api.stop_message(name)["status"], "cancelling")

    def test_unusable_model_falls_back_to_the_default(self):
        out = self._send("hello")
        broken = FakeProvider([[("error", "invalid x-api-key", "auth", False)]])
        default = FakeProvider([[("text", "answered by the default"), ("stop", "end_turn")]])

        def provider_for(provider_name=None, model=None):
            if provider_name is None and model is None:
                return default, "Default", "default-model"
            return (broken, "Broken", "bad-model") if model != "default-model" else (default, "Default", "default-model")

        with patch.object(runner, "provider_for", provider_for), patch("frappe.publish_realtime", lambda *a, **k: None):
            runner.execute_message(out["assistant_message"]["name"], user="Administrator")
        msg = frappe.get_doc("AI Chat Message", out["assistant_message"]["name"])
        self.assertEqual(msg.status, "completed")
        self.assertEqual(msg.text, "answered by the default")
        self.assertEqual(msg.model, "default-model")
        kinds = frappe.get_all("AI Message Block", filters={"message": msg.name}, pluck="block_type", order_by="seq asc")
        self.assertEqual(kinds[0], "status")

    def test_search_finds_words_inside_messages(self):
        word = "zqx" + frappe.generate_hash(length=6)
        out = self._send(f"please look at invoice {word} again")
        frappe.db.set_value("AI Chat Session", out["session"]["name"], "title", "untitled")
        found = api.list_sessions(search=word)["sessions"]
        hit = next(s for s in found if s["name"] == out["session"]["name"])
        self.assertIn(word, hit["preview"])
        self.assertFalse([s for s in api.list_sessions(search=word + "-none")["sessions"] if s["name"] == out["session"]["name"]])

    def test_export_is_markdown_of_the_conversation(self):
        out = self._send("سؤال التصدير")
        frappe.db.set_value("AI Chat Message", out["assistant_message"]["name"], {"status": "completed", "text": "جواب التصدير"})
        exported = api.export_session(out["session"]["name"])
        self.assertTrue(exported["filename"].endswith(".md"))
        self.assertIn("سؤال التصدير", exported["content"])
        self.assertIn("جواب التصدير", exported["content"])

    def test_a_new_message_settles_the_reply_waiting_for_approval(self):
        out = self._send("first")
        waiting = out["assistant_message"]["name"]
        frappe.db.set_value("AI Chat Message", waiting, "status", "awaiting_approval")
        self._send("go on", session=out["session"]["name"])
        status, reason = frappe.db.get_value("AI Chat Message", waiting, ["status", "stop_reason"])
        self.assertEqual((status, reason), ("completed", "superseded"))


class TestChatViewHost(IntegrationTestCase):
    """The Desk chat renders tool results with the same MCP App view as ChatGPT."""

    def setUp(self):
        ensure_test_agent()
        frappe.set_user("Administrator")
        with patch.object(runner, "enqueue"):
            self.out = api.send_message(text="عملاء", agent="Unit Test Agent")
        self.session = self.out["session"]["name"]

    def tearDown(self):
        frappe.set_user("Administrator")
        for run in frappe.get_all("AI Run", filters={"chat_session": self.session}, pluck="name"):
            frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
        frappe.delete_doc("AI Chat Session", self.session, ignore_permissions=True, force=True)
        frappe.db.commit()

    def test_tool_result_carries_the_mcp_view(self):
        from ai_agent.engine.tools import ToolExecutor
        from ai_agent.engine.types import TOOL_USE, Block
        from ai_agent.runtime.permission_context import PermissionContext

        agent = frappe.get_cached_doc("AI Agent", "Unit Test Agent")
        ex = ToolExecutor(PermissionContext("Administrator", agent.name, None), agent, self.out["assistant_message"]["run"])
        outcome = ex.execute(Block(kind=TOOL_USE, id="t1", name="data__query", data={"doctype": "Company", "fields": ["name"], "limit": 5}))
        view = outcome.result.data.get("view")
        self.assertIsNotNone(view)
        self.assertEqual(view["structuredContent"]["presentation"]["type"], "table")
        self.assertTrue(view["structuredContent"]["presentation"].get("full"))
        self.assertIn("ai_agent/presentation", view["_meta"])

    def test_view_html_is_the_mcp_app(self):
        from ai_agent.mcp.protocol import build_main_view

        self.assertEqual(api.view_html()["html"], build_main_view())

    def test_view_call_allows_reads_and_refuses_writes(self):
        res = api.view_call(self.session, "data__count", {"doctype": "Company"})
        self.assertFalse(res.get("isError"))
        with self.assertRaises(frappe.PermissionError):
            api.view_call(self.session, "actions__request_create_draft", {"doctype": "Note", "payload": {"title": "x"}})
        with self.assertRaises(frappe.PermissionError):
            api.view_call(self.session, "approvals__decide", {"approval_id": "x", "decision": "approve"})


class TestEngineInterceptorHooks(IntegrationTestCase):
    """The legacy interceptor records an approval step through add_step/trace; in
    the chat engine those hooks must be harmless, never None (a None trace crashed
    after the approval was saved, leaving requests the user never saw)."""

    def test_record_approval_step_is_a_no_op(self):
        from ai_agent.engine.tools import ToolExecutor
        from ai_agent.runtime.permission_context import PermissionContext

        ensure_test_agent()
        agent = frappe.get_cached_doc("AI Agent", "Unit Test Agent")
        ex = ToolExecutor(PermissionContext("Administrator", agent.name, None), agent, None)
        ex.interceptor._record_approval_step("Approval required: test", "APRQ-TEST")


class TestChatAttachments(IntegrationTestCase):
    """Attachments stay readable for the whole conversation: their text is kept
    on the user message, and files.read lists / re-reads them on demand."""

    def setUp(self):
        ensure_test_agent()
        frappe.set_user("Administrator")
        f = frappe.get_doc({"doctype": "File", "file_name": "note.txt", "content": "رقم السند 12743", "is_private": 1}).insert(ignore_permissions=True)
        with patch.object(runner, "enqueue"):
            self.out = api.send_message(text="هذا سند", agent="Unit Test Agent", files=[f.name])
        self.session = self.out["session"]["name"]
        self.file = f.name

    def tearDown(self):
        frappe.set_user("Administrator")
        for run in frappe.get_all("AI Run", filters={"chat_session": self.session}, pluck="name"):
            frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
        frappe.delete_doc("AI Chat Session", self.session, ignore_permissions=True, force=True)
        frappe.db.commit()

    def test_attachment_text_is_kept_for_later_turns(self):
        from ai_agent.engine.context import history

        # reply, the auto-title call, then the second reply
        provider, provider_for = fake_provider([[("text", "قرأته")], [("text", "عنوان")], [("text", "ما زلت أراه")]])
        with patch.object(runner, "provider_for", provider_for), patch("frappe.publish_realtime", lambda *a, **k: None):
            runner.execute_message(self.out["assistant_message"]["name"], user="Administrator")
            with patch.object(runner, "enqueue"):
                second = api.send_message(text="أدخله", session=self.session)
            runner.execute_message(second["assistant_message"]["name"], user="Administrator")
        first_user_turn = provider.requests[-1].messages[0]
        texts = " ".join(b.text for b in first_user_turn.blocks)
        self.assertIn("12743", texts, "the attachment text is replayed in the next turn")
        self.assertIn("هذا سند", texts)
        self.assertTrue(history.load(self.session))

    def test_files_read_lists_and_reads(self):
        from ai_agent.runtime.permission_context import PermissionContext
        from ai_agent.tools.chat_files import handle_read

        ctx = PermissionContext("Administrator", "Unit Test Agent", None)
        ctx.run_name = self.out["assistant_message"]["run"]
        listing = handle_read({}, ctx)
        self.assertEqual(listing["count"], 1)
        read = handle_read({"file": "note.txt"}, ctx)
        self.assertIn("12743", read["content"])
        self.assertIn("error", handle_read({"file": "other.png"}, ctx))


class TestCoerceToSchema(IntegrationTestCase):
    """Models send JSON text for objects and one name for a list of names; the gateway repairs both."""

    def test_repairs(self):
        from ai_agent.runtime.tool_gateway import coerce_to_schema

        schema = {"type": "object", "properties": {
            "arguments": {"type": "object", "properties": {"roles": {"type": "array", "items": {"type": "string"}}}},
            "roles": {"type": "array", "items": {"type": "string"}},
            "fields": {"type": "array"},
        }}
        out = coerce_to_schema(schema, {"arguments": '{"roles": "A, B"}', "roles": "Accounts User", "fields": '["a"]'})
        self.assertEqual(out, {"arguments": {"roles": ["A", "B"]}, "roles": ["Accounts User"], "fields": ["a"]})
        self.assertEqual(coerce_to_schema(schema, {"arguments": "not json"}), {"arguments": "not json"})
