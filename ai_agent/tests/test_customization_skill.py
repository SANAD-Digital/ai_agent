"""The customization skill (design §3.7) and how it is installed (§3.6.1: granted by role only)."""

import os
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent import install
from ai_agent.ai_agent.doctype.ai_skill_grant.ai_skill_grant import grant_standard_skills
from ai_agent.runtime.skill_registry import SKILLS_DIR, SkillRegistry
from ai_agent.runtime.tool_registry import ToolRegistry

SKILL = "customization"
REFERENCES = ("client-scripts", "dashboards", "doctypes", "fields", "list-settings", "naming", "permissions",
	"print-formats-notifications", "properties", "reports", "server-scripts", "web", "workflow", "workspaces")


def _frontmatter():
	registry = SkillRegistry()
	with open(os.path.join(SKILLS_DIR, SKILL, "SKILL.md"), encoding="utf-8") as handle:
		return registry, registry._parse_frontmatter(handle.read())


class TestCustomizationSkillFiles(IntegrationTestCase):
	def test_frontmatter(self):
		registry, meta = _frontmatter()
		self.assertEqual(meta["name"], SKILL)
		self.assertEqual(meta["title"], "Customization")
		self.assertIn("not to enter data", meta["description"])
		self.assertFalse(registry._as_bool(meta.get("allow_implicit_invocation")), "loaded on demand, never always-on")
		tools = registry._parse_csv(meta["tools"])
		self.assertEqual(len(tools), len(set(tools)))
		for name in ("customize.inspect", "customize.get", "customize.check", "customize.request_field", "customize.request_properties",
				"customize.request_client_script", "customize.request_server_script", "customize.request_report", "customize.request_undo"):
			self.assertIn(name, tools)

	def test_every_declared_tool_is_registered(self):
		registry, meta = _frontmatter()
		registered = {tool["name"] for tool in ToolRegistry().list_tools()}
		missing = [t for t in registry._parse_csv(meta["tools"]) if t not in registered]
		self.assertEqual(missing, [], "a skill row naming an unregistered tool fails the skill sync on migrate")

	def test_references(self):
		registry = SkillRegistry()
		self.assertEqual(registry.list_references(SKILL), sorted(REFERENCES))
		for ref in REFERENCES:
			text = registry.read_reference(SKILL, ref)
			self.assertTrue(text and text.startswith("# "), ref)
		self.assertIsNone(registry.read_reference(SKILL, "../system/SKILL"))

	def test_server_script_reference_matches_safe_exec(self):
		"""The cheat sheet's claims about Frappe v16 safe_exec hold on this bench."""
		from frappe.utils.safe_exec import UNSAFE_ATTRIBUTES, get_safe_globals

		text = SkillRegistry().read_reference(SKILL, "server-scripts")
		g = get_safe_globals()
		self.assertIn("format", UNSAFE_ATTRIBUTES)
		self.assertNotIn("has_permission", g.frappe, "the reference says there is no frappe.has_permission")
		for name in ("get_list", "get_all", "get_doc", "msgprint", "throw", "session", "form_dict", "utils", "qb"):
			self.assertIn(name, g.frappe, name)
		for name in ("nowdate", "today", "add_days", "getdate", "flt", "cint", "fmt_money", "date_diff"):
			self.assertIn(name, g.frappe.utils, name)
			self.assertIn(f"`{name}`", text)
		from frappe.core.doctype.server_script.server_script_utils import EVENT_MAP

		self.assertEqual(EVENT_MAP["validate"], "Before Save")
		self.assertEqual(EVENT_MAP["on_update"], "After Save")

	def test_properties_reference_lists_frappe_whitelists(self):
		from frappe.custom.doctype.customize_form.customize_form import docfield_properties, doctype_properties

		text = SkillRegistry().read_reference(SKILL, "properties")
		for prop in list(doctype_properties) + list(docfield_properties):
			self.assertIn(f"`{prop}`", text, prop)


class TestCustomizationSkillInstall(IntegrationTestCase):
	"""Runs the real sync and grant code inside the test transaction (no commit), then rolls back."""

	def setUp(self):
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		install.sync_tool_name_options()
		SkillRegistry().sync_app_skills()

	def test_sync_creates_the_skill_with_its_tools(self):
		skill = frappe.get_doc("AI Skill", SKILL)
		self.assertEqual(skill.source_type, "App File")
		self.assertEqual(skill.skill_path, SKILL)
		self.assertFalse(skill.allow_implicit_invocation)
		self.assertIn("customize.request_server_script", [row.tool_name for row in skill.tools])

	def test_granted_to_system_manager_only(self):
		grant_standard_skills()
		grants = frappe.get_all("AI Skill Grant", filters={"skill": SKILL}, fields=["agent", "grant_type", "role", "enabled"])
		self.assertFalse([g for g in grants if g.grant_type != "Role"], "never an All Users / User grant from the app")
		self.assertTrue(grants)
		self.assertEqual({g.role for g in grants}, {"System Manager"})
		analyst = frappe.db.exists("AI Agent", "ERP Analyst")
		self.assertFalse([g for g in grants if g.agent == analyst], "the read-only analyst never gets it")
		# customization belongs to the agent that allows it (Implementation), not to the other acting agents
		self.assertIn("Implementation", {g.agent for g in grants if g.enabled})
		self.assertNotIn("ERP Operations Agent", {g.agent for g in grants if g.enabled})
		# idempotent: a second run adds nothing
		grant_standard_skills()
		self.assertEqual(len(frappe.get_all("AI Skill Grant", filters={"skill": SKILL})), len(grants))

	def test_not_offered_without_the_role(self):
		grant_standard_skills()
		operations = frappe.db.exists("AI Agent", "ERP Operations Agent")
		if not operations:
			self.skipTest("no ERP Operations Agent on this site")
		with patch.object(frappe, "get_roles", return_value=["Accounts User"]):
			grants = SkillRegistry()._get_matching_grants("someone@example.com", operations)
		self.assertNotIn(SKILL, {g.skill for g in grants})
