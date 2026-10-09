"""Memory for the chat engine: recall block, automatic extraction with a
scripted provider, and the user-facing list/delete endpoints."""

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api
from ai_agent.engine import memory as memory_mod
from ai_agent.engine import runner
from ai_agent.runtime.memory_service import MemoryService
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.utils import ensure_test_agent


class TestParseFacts(TestCase):
    def test_parses_fenced_and_bare_json(self):
        text = '```json\n[{"type":"preference","content":"يفضّل الجداول"}]\n```'
        self.assertEqual(memory_mod.parse_facts(text)[0]["content"], "يفضّل الجداول")
        self.assertEqual(memory_mod.parse_facts("nothing here"), [])
        self.assertEqual(memory_mod.parse_facts("Sure: [] "), [])


class TestChatMemory(IntegrationTestCase):
    def setUp(self):
        ensure_test_agent()
        frappe.set_user("Administrator")
        self._sessions = []
        self._memories_before = set(frappe.get_all("AI Memory", filters={"user": "Administrator"}, pluck="name"))
        self._settings = frappe.db.get_single_value("AI Settings", "enable_memory")
        frappe.db.set_single_value("AI Settings", "enable_memory", 1)
        frappe.clear_document_cache("AI Settings", "AI Settings")

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in frappe.get_all("AI Memory", filters={"user": "Administrator"}, pluck="name"):
            if name not in self._memories_before:
                frappe.delete_doc("AI Memory", name, ignore_permissions=True, force=True)
        for name in self._sessions:
            if frappe.db.exists("AI Chat Session", name):
                for run in frappe.get_all("AI Run", filters={"session": name}, pluck="name"):
                    frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
                frappe.delete_doc("AI Chat Session", name, ignore_permissions=True, force=True)
        frappe.db.set_single_value("AI Settings", "enable_memory", self._settings or 0)
        frappe.clear_document_cache("AI Settings", "AI Settings")
        frappe.db.commit()

    def _session_with_turn(self, question, answer):
        with patch.object(runner, "enqueue"):
            out = api.send_message(text=question, agent="Unit Test Agent")
        self._sessions.append(out["session"]["name"])
        frappe.db.set_value("AI Chat Message", out["assistant_message"]["name"], {"status": "completed", "text": answer}, update_modified=False)
        frappe.db.commit()
        return out["session"]["name"]

    def test_recall_lists_preferences_first_and_skips_duplicates(self):
        service = MemoryService()
        service.store(user="Administrator", scope="User", memory_type="Preference", content="يفضّل الردود بالجداول وبالريال اليمني")
        service.store(user="Administrator", scope="Agent", memory_type="Context", content="العميل الرئيسي هو شركة SND", agent="Unit Test Agent", entity_type="Customer", entity_key="SND")
        service.store(user="Administrator", scope="Agent", memory_type="Context", content="العميل الرئيسي هو شركة SND", agent="Unit Test Agent")
        ctx = PermissionContext("Administrator", "Unit Test Agent", None)
        agent = frappe.get_cached_doc("AI Agent", "Unit Test Agent")
        text = memory_mod.recall(ctx, agent, "مبيعات SND")
        self.assertTrue(text.startswith("Preferences:"))
        self.assertEqual(text.count("العميل الرئيسي"), 1)
        self.assertIn("Facts that may apply", text)

    def test_extraction_stores_facts_once(self):
        session = self._session_with_turn("أريد الأرقام دائماً بالريال اليمني وبجدول", "حسناً، سأعرضها بالريال اليمني وبجداول.")
        facts = '[{"type":"preference","content":"يريد الأرقام بالريال اليمني وبجدول"},{"type":"context","content":"يتابع مبيعات فرع الرياض","entity_type":"Branch","entity_key":"الرياض"},{"type":"junk","content":"x"}]'
        with patch.object(memory_mod, "_ask_model", lambda s, t, a: memory_mod.parse_facts(facts)):
            stored = memory_mod.extract_session(session, force=True)
        self.assertEqual(stored, 2)
        rows = frappe.get_all("AI Memory", filters={"user": "Administrator", "content": ("like", "%الريال اليمني%")}, fields=["memory_type", "scope"])
        self.assertEqual(rows[0].memory_type, "Preference")
        self.assertEqual(rows[0].scope, "User")
        listing = api.list_memories()["memories"]
        self.assertTrue(any("فرع الرياض" in m["content"] for m in listing))
        # A second extraction of the same facts must not duplicate them.
        with patch.object(memory_mod, "_ask_model", lambda s, t, a: memory_mod.parse_facts('[{"type":"preference","content":"يريد الأرقام بالريال اليمني وبجدول"}]')):
            memory_mod.extract_session(session, force=True)
        self.assertEqual(frappe.db.count("AI Memory", {"user": "Administrator", "content": ("like", "%الريال اليمني%")}), 1)
        # delete through the API
        name = [m for m in listing if "فرع الرياض" in m["content"]][0]["name"]
        api.delete_memory(name)
        self.assertFalse(frappe.db.exists("AI Memory", name))

    def test_extraction_skips_when_nothing_new(self):
        session = self._session_with_turn("مرحبا", "أهلاً")
        if frappe.db.has_column("AI Chat Session", "memory_extracted_seq"):
            frappe.db.set_value("AI Chat Session", session, "memory_extracted_seq", 2, update_modified=False)
            with patch.object(memory_mod, "_ask_model", lambda *a: self.fail("model must not be called")):
                self.assertEqual(memory_mod.extract_session(session), 0)
