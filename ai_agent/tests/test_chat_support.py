"""The floating support bubble's backend: sessions with surface "Chatbot" on the
chat engine, fixed agent from AI Chatbot Settings, kept apart from the workspace."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.chat import api, support
from ai_agent.chatbot import settings as bot_settings
from ai_agent.engine import run_setup, runner
from ai_agent.engine.providers.fake import FakeProvider
from ai_agent.tests.utils import ensure_test_agent


class TestChatSupport(IntegrationTestCase):
    def setUp(self):
        ensure_test_agent()
        frappe.set_user("Administrator")
        self._sessions = []
        self._patches = [
            patch.object(bot_settings, "agent_name", return_value="Unit Test Agent"),
            patch.object(bot_settings, "is_enabled", return_value=True),
            patch.object(bot_settings, "company", return_value=None),
            patch.object(bot_settings, "model", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        frappe.set_user("Administrator")
        for name in self._sessions:
            if frappe.db.exists("AI Chat Session", name):
                for run in frappe.get_all("AI Run", filters={"chat_session": name}, pluck="name"):
                    frappe.delete_doc("AI Run", run, ignore_permissions=True, force=True)
                frappe.delete_doc("AI Chat Session", name, ignore_permissions=True, force=True)
        frappe.db.commit()

    def _send(self, text, **kw):
        with patch.object(runner, "enqueue"):
            out = support.send_message(text=text, **kw)
        if out["session"]["name"] not in self._sessions:
            self._sessions.append(out["session"]["name"])
        return out

    def test_bootstrap_shape(self):
        boot = support.bootstrap()
        self.assertTrue(boot["enabled"] and boot["can_access"])
        self.assertEqual(boot["agent"]["name"], "Unit Test Agent")
        self.assertTrue(boot["title"])
        self.assertTrue(boot["welcome_message"])
        self.assertIsInstance(boot["suggestions"], list)

    def test_support_session_is_separate_from_the_workspace(self):
        out = self._send("ظهرت لي رسالة خطأ")
        name = out["session"]["name"]
        self.assertEqual(frappe.db.get_value("AI Chat Session", name, "surface"), "Chatbot")
        self.assertEqual(out["session"]["agent"], "Unit Test Agent")
        frappe.db.set_value("AI Chat Message", out["assistant_message"]["name"], "status", "completed")
        self.assertNotIn(name, [s["name"] for s in api.list_sessions(limit=200)["sessions"]])
        self.assertIn(name, [s["name"] for s in support.list_sessions()["sessions"]])
        self.assertEqual(support.bootstrap()["session"]["name"], name)

    def test_cannot_post_into_a_workspace_session(self):
        with patch.object(runner, "enqueue"):
            desk = api.send_message(text="hi", agent="Unit Test Agent")
        self._sessions.append(desk["session"]["name"])
        frappe.db.set_value("AI Chat Message", desk["assistant_message"]["name"], "status", "completed")
        with self.assertRaises(frappe.PermissionError):
            support.send_message(text="x", session=desk["session"]["name"])

    def test_disabled_or_role_restricted(self):
        with patch.object(bot_settings, "is_enabled", return_value=False):
            self.assertEqual(support.bootstrap(), {"enabled": False})
            with self.assertRaises(frappe.PermissionError):
                support.send_message(text="x")

    def test_support_prompt_and_open_document_reach_the_model(self):
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        out = self._send("ما هذا المستند؟", context_doctype="Company", context_name=company)
        provider = FakeProvider([[("text", "هذه شركة")], [("text", "عنوان")]])
        with patch.object(run_setup, "provider_for", lambda *a, **k: (provider, "Default", "fake-1")), patch.object(runner, "provider_for", lambda *a, **k: (provider, "Default", "fake-1")), patch("frappe.publish_realtime", lambda *a, **k: None), \
             patch.object(bot_settings, "system_prompt", return_value="SUPPORT-DESK-PROMPT"):
            runner.execute_message(out["assistant_message"]["name"], user="Administrator")
        req = provider.requests[0]
        self.assertIn("SUPPORT-DESK-PROMPT", req.system)
        self.assertIn("floating support panel", req.system)
        self.assertIn("Document the user has open", req.system_dynamic)
        self.assertIn(company, req.system_dynamic)

    def test_an_open_conversation_follows_the_support_window_provider(self):
        """The bubble's user never picks a model: a provider changed in AI Chatbot Settings applies to the
        conversation already open (one opened before the change kept failing on the old provider)."""
        made = []
        for name, model in (("Support Test Provider One", "one-model"), ("Support Test Provider Two", "two-model")):
            frappe.get_doc({"doctype": "AI Provider", "provider_name": name, "provider_type": "Ollama", "enabled": 1,
                "models": [{"model_name": model, "model_type": "Chat", "supports_tools": 1, "is_default": 1}]}).insert(ignore_permissions=True)
            made.append(name)
        self.addCleanup(lambda: [frappe.delete_doc("AI Provider", n, force=True, ignore_permissions=True) for n in made])
        with patch("ai_agent.runtime.policy.assert_provider_allowed", lambda *_: None):
            with patch.object(bot_settings, "provider", return_value=made[0]):
                name = self._send("أول رسالة")["session"]["name"]
            self.assertEqual(frappe.db.get_value("AI Chat Session", name, ["provider", "model"]), (made[0], "one-model"))
            frappe.db.set_value("AI Chat Message", {"session": name, "role": "assistant"}, "status", "completed")
            with patch.object(bot_settings, "provider", return_value=made[1]):
                self._send("رسالة ثانية", session=name)
            self.assertEqual(frappe.db.get_value("AI Chat Session", name, ["provider", "model"]), (made[1], "two-model"))


class TestChatbotAssignment(IntegrationTestCase):
    """AI Chatbot Assignment: the support window of one user, with the settings of AI Chatbot Settings at the
    user's level (an empty field keeps the site's value)."""

    USER = "ai-bubble-assign@example.com"

    def setUp(self):
        frappe.set_user("Administrator")
        ensure_test_agent()
        if not frappe.db.exists("User", self.USER):
            frappe.get_doc({"doctype": "User", "email": self.USER, "first_name": "Bubble Assign", "send_welcome_email": 0}).insert(ignore_permissions=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        frappe.set_user("Administrator")
        if frappe.db.exists("AI Chatbot Assignment", self.USER):
            frappe.delete_doc("AI Chatbot Assignment", self.USER, force=True, ignore_permissions=True)
        frappe.local.ai_chatbot_assignments = {}

    def test_an_empty_company_is_not_filled_with_the_creators_default(self):
        company = frappe.get_all("Company", pluck="name", limit=1)
        if not company:
            self.skipTest("needs a company")
        frappe.local.new_doc_templates.pop("AI Chatbot Assignment", None)  # Frappe caches a new document's defaults
        self.addCleanup(lambda: frappe.local.new_doc_templates.pop("AI Chatbot Assignment", None))
        with patch.object(frappe.defaults, "get_defaults", return_value=frappe._dict(company=company[0])):
            doc = frappe.get_doc({"doctype": "AI Chatbot Assignment", "user": self.USER, "company": None})
            doc.insert(ignore_permissions=True)
        self.assertFalse(doc.company)

    def assign(self, **values):
        doc = frappe.get_doc({"doctype": "AI Chatbot Assignment", "user": self.USER, "company": "", **values})
        doc.insert(ignore_permissions=True)
        frappe.local.ai_chatbot_assignments = {}
        return doc

    def as_user(self):
        frappe.set_user(self.USER)
        frappe.local.ai_chatbot_assignments = {}

    def test_the_users_values_win_and_empty_ones_keep_the_site(self):
        site_title = bot_settings.widget_title()
        self.assign(agent="Unit Test Agent", welcome_message="أهلاً بك يا فريق المبيعات")
        self.as_user()
        self.assertEqual(bot_settings.agent_name(), "Unit Test Agent")
        self.assertEqual(bot_settings.welcome_message(), "أهلاً بك يا فريق المبيعات")
        self.assertEqual(bot_settings.widget_title(), site_title, "an empty field keeps the site's value")

    def test_assignment_decides_access(self):
        self.as_user()
        self.assertFalse(bot_settings.can_access(), "no role, no assignment")
        frappe.set_user("Administrator")
        self.assign()
        self.as_user()
        self.assertTrue(bot_settings.can_access(), "an enabled assignment grants it without the roles")
        frappe.set_user("Administrator")
        frappe.db.set_value("AI Chatbot Assignment", self.USER, "enabled", 0)
        self.as_user()
        self.assertFalse(bot_settings.can_access())
        self.assertNotEqual(bot_settings.agent_name(), "x", "a disabled assignment sets nothing")

    def test_site_level_ignores_the_session_users_assignment(self):
        self.assign(system_prompt="فقط لهذا المستخدم")
        self.as_user()
        self.assertEqual(bot_settings.system_prompt(), "فقط لهذا المستخدم")
        with bot_settings.site_level():
            self.assertNotEqual(bot_settings.system_prompt(), "فقط لهذا المستخدم")
