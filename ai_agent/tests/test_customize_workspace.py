"""Workspace kind on Frappe 16.53: Workspace, its module (and that module's site sidebar layer) and Desktop Icon
(customize.request_workspace)."""

import json
import os

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.workspace import KIND, current, sidebar_text

ADMIN = "Administrator"
SM_ONLY = "ai-cust-ws-sm@example.com"  # System Manager without Workspace Manager
ROLE = "ai_cust_ws_role"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


def app_files(title):
	"""JSON files Frappe would write into an app for this title (developer mode export)."""
	scrubbed = frappe.scrub(title)
	found = []
	for app in frappe.get_installed_apps():
		base = frappe.get_app_path(app)
		for folder in ("workspace_sidebar", "desktop_icon"):
			path = os.path.join(base, folder, f"{scrubbed}.json")
			if os.path.exists(path):
				found.append(path)
	return found


class TestCustomizeWorkspace(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)

	def track(self, title, private_for=None):
		name = f"{title}-{private_for}" if private_for else title

		def remove():
			frappe.set_user(ADMIN)
			if frappe.db.exists("Desktop Icon", title):
				frappe.delete_doc("Desktop Icon", title, force=True, ignore_permissions=True)
			if frappe.db.exists("Workspace", name):
				frappe.delete_doc("Workspace", name, force=True, ignore_permissions=True)
			for layer in frappe.get_all("Custom Sidebar", filters={"module": title}, pluck="name"):
				frappe.delete_doc("Custom Sidebar", layer, force=True, ignore_permissions=True)
			if frappe.db.get_value("Module Def", title, "custom"):
				frappe.delete_doc("Module Def", title, force=True, ignore_permissions=True)

		self.addCleanup(remove)
		return title

	def create_args(self, title, **extra):
		args = {"operation": "create", "name": title, "icon": "list-todo", "purpose": "test",
			"shortcuts": [{"label": "Open ToDos", "type": "DocType", "link_to": "ToDo", "doc_view": "List"},
				{"label": "Notes", "type": "DocType", "link_to": "Note"}],
			"cards": [{"label": "Lists", "links": [{"label": "ToDo", "link_type": "DocType", "link_to": "ToDo"}]}],
			"sidebar": {"items": [
				{"label": "Home", "link_type": "Workspace", "link_to": title, "icon": "home"},
				{"type": "Section Break", "label": "Work", "items": [
					{"label": "ToDo", "link_to": "ToDo", "icon": "list-todo"},
					{"label": "Note", "link_to": "Note"}]}]}}
		args.update(extra)
		return args

	# -- normalize -------------------------------------------------------------------------------
	def test_normalize_rules(self):
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "x", "bogus": 1}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "rename", "name": "x"}, ADMIN)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "create", "name": "x", "public": 0, "sidebar": {"items": []}}, ADMIN)
		self.assertTrue(ctx.exception.hints)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "update", "name": f"No Such WS {self.suffix}", "icon": "home"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "x", "copy_from": f"No Such WS {self.suffix}"}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "x", "shortcuts": [{"label": "a", "type": "Webhook", "link_to": "b"}]}, ADMIN)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "create", "name": "x", "sidebar": {"items": [{"type": "Link", "label": "a", "items": []}]}}, ADMIN)
		spec = KIND.normalize(self.create_args(f"AI WS N {self.suffix}"), ADMIN)
		items = spec["sidebar"]["items"]
		self.assertEqual([i["type"] for i in items], ["Link", "Section Break", "Link", "Link"])
		self.assertEqual(items[1]["indent"], 1)
		self.assertEqual([i.get("child") for i in items[2:]], [1, 1])  # nested items become children
		self.assertEqual(spec["public"], 1)
		self.assertEqual(spec["desktop_icon"], 1)

	# -- create / verify / undo (delete) / restore -----------------------------------------------------
	def test_create_public_round_trip(self):
		title = self.track(f"AI WS Test {self.suffix}")
		spec = KIND.normalize(self.create_args(title), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("workspace.frappe", codes(report, "ok"))
		self.assertIn("▾ Work", report.preview["sidebar"]["after"])
		self.assertEqual(report.preview["shortcuts"], ["Open ToDos", "Notes"])
		json.dumps(report.to_dict())
		self.assertFalse(frappe.db.exists("Workspace", title), "the check must not write")

		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		state = current(title)
		self.assertTrue(state["exists"])
		# a page with a sidebar of its own sits in a custom module of its own (never an app's, never exported)
		self.assertEqual((state["module"], state["app"], state["standard"]), (title, "", 0))
		self.assertEqual(frappe.db.get_value("Module Def", title, "custom"), 1)
		self.assertEqual(state["sidebar"]["standard"], 0)
		self.assertEqual(state["sidebar"]["app"], "")
		self.assertEqual(state["desktop_icon"]["owner"], "Administrator")
		self.assertEqual(app_files(title), [])
		blocks = [b["type"] for b in state["content"]]
		self.assertIn("shortcut", blocks)
		self.assertIn("card", blocks)
		self.assertEqual(result["undo"], {"operation": "delete", "name": title})

		# undo = delete, and the undo of that delete rebuilds the same workspace
		delete = result["undo"]
		self.assertFalse(KIND.check(delete, ADMIN).critical())
		undo2 = KIND.execute(delete, ADMIN)
		self.assertFalse([c for c in KIND.verify(delete, undo2) if c.level == "critical"])
		self.assertFalse(frappe.db.exists("Workspace", title))
		self.assertFalse(frappe.db.exists("Module Def", title), "its own module goes with it")
		self.assertFalse(frappe.db.exists("Custom Sidebar", {"module": title}))
		self.assertFalse(frappe.db.exists("Desktop Icon", title))
		restore = undo2["undo"]
		self.assertEqual(restore["operation"], "create")
		self.assertFalse(KIND.check(restore, ADMIN).critical(), [c.message for c in KIND.check(restore, ADMIN).critical()])
		KIND.execute(restore, ADMIN)
		again = current(title)
		for key in ("shortcuts", "cards", "content"):
			self.assertEqual(again[key], state[key], key)
		self.assertEqual(again["sidebar"]["items"], state["sidebar"]["items"])

	def test_update_sections_and_undo(self):
		title = self.track(f"AI WS Upd {self.suffix}")
		spec = KIND.normalize(self.create_args(title), ADMIN)
		KIND.execute(spec, ADMIN)
		before = current(title)

		update = KIND.normalize({"operation": "update", "name": title,
			"shortcuts": [{"label": "Notes", "type": "DocType", "link_to": "Note"},
				{"label": "Open ToDos", "type": "DocType", "link_to": "ToDo", "doc_view": "List"},
				{"label": "Errors", "type": "DocType", "link_to": "Error Log"}],
			"sidebar": {"items": before["sidebar"]["items"] + [{"label": "Errors", "link_to": "Error Log", "icon": "bug"}]}}, ADMIN)
		report = KIND.check(update, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("Errors → DocType: Error Log", report.preview["sidebar"]["after"])
		self.assertNotIn("Errors", report.preview["sidebar"]["before"])
		result = KIND.execute(update, ADMIN)
		self.assertFalse([c for c in KIND.verify(update, result) if c.level == "critical"])
		after = current(title)
		self.assertEqual([s["label"] for s in after["shortcuts"]], ["Notes", "Open ToDos", "Errors"])
		shortcut_blocks = [b["data"]["shortcut_name"] for b in after["content"] if b["type"] == "shortcut"]
		self.assertEqual(shortcut_blocks, ["Notes", "Open ToDos", "Errors"])  # slots reordered, new one appended
		self.assertEqual(len(after["sidebar"]["items"]), len(before["sidebar"]["items"]) + 1)

		undo = result["undo"]
		self.assertEqual(undo["operation"], "update")
		KIND.execute(undo, ADMIN)
		restored = current(title)
		for key in ("shortcuts", "content"):
			self.assertEqual(restored[key], before[key], key)
		self.assertEqual(restored["sidebar"]["items"], before["sidebar"]["items"])

	def test_private_workspace(self):
		title = self.track(f"AI WS Mine {self.suffix}", private_for=ADMIN)
		spec = KIND.normalize({"operation": "create", "name": title, "public": 0,
			"shortcuts": [{"label": "ToDo", "type": "DocType", "link_to": "ToDo"}]}, ADMIN)
		name = spec["name"]
		self.assertEqual(name, f"{title}-{ADMIN}")
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn(f"Only {ADMIN}", " ".join(report.impact))
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		# Frappe 16.53 lists a private page in its owner's own sidebar layer
		listed = {"parenttype": "Custom Sidebar", "link_type": "Workspace", "link_to": name}
		self.assertTrue(frappe.db.exists("Sidebar Item", listed))
		self.assertEqual(frappe.db.get_value("Workspace", name, "module"), "Private")
		KIND.execute(result["undo"], ADMIN)
		self.assertFalse(frappe.db.exists("Workspace", name))
		self.assertFalse(frappe.db.exists("Sidebar Item", listed))

	def test_disable_enable(self):
		title = self.track(f"AI WS Hide {self.suffix}")
		KIND.execute(KIND.normalize(self.create_args(title), ADMIN), ADMIN)
		spec = KIND.normalize({"operation": "disable", "name": title}, ADMIN)
		self.assertFalse(KIND.check(spec, ADMIN).critical())
		result = KIND.execute(spec, ADMIN)
		self.assertEqual(frappe.db.get_value("Workspace", title, "is_hidden"), 1)
		self.assertEqual(frappe.db.get_value("Desktop Icon", title, "hidden"), 1)
		self.assertEqual(result["undo"], {"operation": "enable", "name": title})
		KIND.execute(result["undo"], ADMIN)
		self.assertEqual(frappe.db.get_value("Workspace", title, "is_hidden"), 0)

	# -- checks -------------------------------------------------------------------------------------------
	def test_app_workspace_is_not_edited_in_place(self):
		standard = frappe.get_all("Workspace", filters={"standard": 1, "public": 1}, pluck="name", limit=1)
		if not standard:
			self.skipTest("no app workspace on this site")
		spec = KIND.normalize({"operation": "update", "name": standard[0], "icon": "home"}, ADMIN)
		self.assertIn("workspace.standard", critical(KIND.check(spec, ADMIN)))
		spec = KIND.normalize({"operation": "disable", "name": standard[0]}, ADMIN)
		self.assertIn("workspace.standard", critical(KIND.check(spec, ADMIN)))
		# its sidebar is its app module's, which other pages share: arranged in Desk, not here
		spec = KIND.normalize({"operation": "update", "name": standard[0], "sidebar": {"items": [{"label": "ToDo", "link_to": "ToDo"}]}}, ADMIN)
		self.assertIn("workspace.shared_sidebar", critical(KIND.check(spec, ADMIN)))

	def test_copy_from_app_workspace(self):
		standard = frappe.get_all("Workspace", filters={"standard": 1, "public": 1}, pluck="name", limit=1)
		if standard:  # an app page: its sections are copied; its sidebar is its app module's and stays there
			title = self.track(f"AI WS Copy {self.suffix}")
			spec = KIND.normalize({"operation": "create", "name": title, "copy_from": standard[0]}, ADMIN)
			self.assertEqual(spec["copied_from"], standard[0])
			self.assertEqual(spec["shortcuts"], current(standard[0])["shortcuts"])
			self.assertNotIn("sidebar", spec)
			report = KIND.check(spec, ADMIN)
			self.assertNotIn("workspace.standard", critical(report))
			self.assertEqual(report.preview["copied_from"], standard[0])
		# a page with a sidebar of its own: the copy gets one too, opening on the copy
		source = self.track(f"AI WS Src {self.suffix}")
		KIND.execute(KIND.normalize(self.create_args(source), ADMIN), ADMIN)
		title = self.track(f"AI WS Copy2 {self.suffix}")
		spec = KIND.normalize({"operation": "create", "name": title, "copy_from": source}, ADMIN)
		src = current(source)
		self.assertEqual(len(spec["sidebar"]["items"]), len(src["sidebar"]["items"]))
		self.assertFalse([i for i in spec["sidebar"]["items"] if i.get("link_type") == "Workspace" and i.get("link_to") == source])
		self.assertEqual(spec["module"], title)
		self.assertFalse(KIND.check(spec, ADMIN).critical())

	def test_check_catches_bad_targets_icons_and_labels(self):
		title = f"AI WS Bad {self.suffix}"
		spec = KIND.normalize({"operation": "create", "name": title, "icon": "no-such-icon-xyz",
			"shortcuts": [{"label": "Dup", "type": "DocType", "link_to": "ToDo"}, {"label": "Dup", "type": "DocType", "link_to": "Note"},
				{"label": "Gone", "type": "DocType", "link_to": f"No DocType {self.suffix}"},
				{"label": "Web", "type": "URL", "url": "javascript:alert(1)"}],
			"number_cards": [f"No Card {self.suffix}"],
			"sidebar": {"items": [{"type": "Section Break", "label": "Empty"}]}}, ADMIN)
		report = KIND.check(spec, ADMIN)
		found = critical(report)
		for code in ("workspace.icon", "workspace.duplicate", "workspace.link_missing", "workspace.url"):
			self.assertIn(code, found)
		# a sidebar always opens on its page: the Home link is added when the request has none
		self.assertEqual(spec["sidebar"]["items"][0], {"type": "Link", "label": "Home", "link_type": "Workspace", "link_to": title, "icon": "home"})
		self.assertNotIn("workspace.frappe", codes(report, "ok"))  # no dry run with criticals
		emoji = KIND.normalize({"operation": "create", "name": f"AI WS Emoji {self.suffix}", "icon": "📊"}, ADMIN)
		self.assertNotIn("workspace.icon", critical(KIND.check(emoji, ADMIN)))

	def test_route_conflict_and_name_taken(self):
		spec = KIND.normalize({"operation": "create", "name": "ToDo"}, ADMIN)
		self.assertIn("workspace.route_conflict", critical(KIND.check(spec, ADMIN)))
		existing = frappe.get_all("Workspace", filters={"public": 1}, pluck="name", limit=1)
		if existing:
			spec = KIND.normalize({"operation": "create", "name": existing[0]}, ADMIN)
			self.assertIn("workspace.name_taken", critical(KIND.check(spec, ADMIN)))

	def test_audience_warning(self):
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
		spec = KIND.normalize({"operation": "create", "name": f"AI WS Aud {self.suffix}", "roles": [ROLE],
			"shortcuts": [{"label": "Errors", "type": "DocType", "link_to": "Error Log"},
				{"label": "ToDo", "type": "DocType", "link_to": "ToDo"}]}, ADMIN)
		report = KIND.check(spec, ADMIN)
		warnings = [c.message for c in report.checks if c.code == "workspace.link_audience"]
		self.assertEqual(len(warnings), 1, warnings)  # ToDo is readable by everyone
		self.assertIn(ROLE, warnings[0])
		self.assertIn(ROLE, " ".join(report.impact))

	def test_requester_needs_workspace_manager(self):
		from ai_agent.tests.utils import quota_bypass

		if not frappe.db.exists("User", SM_ONLY):
			with quota_bypass():
				frappe.get_doc({"doctype": "User", "email": SM_ONLY, "first_name": "WS SM", "send_welcome_email": 0,
					"user_type": "System User", "roles": [{"role": "System Manager"}]}).insert(ignore_permissions=True)
		spec = KIND.normalize({"operation": "create", "name": f"AI WS Role {self.suffix}"}, SM_ONLY)
		self.assertIn("roles.missing", critical(KIND.check(spec, SM_ONLY)))
		self.assertEqual(KIND.roles_for(spec), ("System Manager", "Workspace Manager"))

	def test_sidebar_text(self):
		text = sidebar_text([{"type": "Link", "label": "Home", "link_type": "Workspace", "link_to": "X", "icon": "home"},
			{"type": "Section Break", "label": "Reports", "indent": 1},
			{"type": "Link", "label": "Sales", "link_type": "Report", "link_to": "Sales Register", "child": 1}])
		self.assertEqual(text.split("\n"), ["- Home → Workspace: X (home)", "▾ Reports", "    - Sales → Report: Sales Register"])
