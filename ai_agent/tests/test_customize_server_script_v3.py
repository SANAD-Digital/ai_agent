"""Server Script kind, v3 types (docs/customization/01 §3.1): Scheduler Event and Permission Query."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.server_script import KIND as SERVER
from ai_agent.customization.kinds.server_script import validate_condition

ADMIN = "Administrator"
USER_A = "ai-customize-pq-a@example.com"
USER_B = "ai-customize-pq-b@example.com"
OWNER_ONLY = 'conditions = f"`tabToDo`.`owner` = {frappe.db.escape(user)}"\n'


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


def condition_codes(text, level="critical"):
	return {c.code for c in validate_condition(text, "u@example.com", "ToDo") if c.level == level}


class V3TestCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.suffix = frappe.generate_hash(length=6)

	def track(self, name):
		def remove():
			frappe.set_user(ADMIN)
			jobs = frappe.get_all("Scheduled Job Type", filters={"server_script": name}, pluck="name")
			if frappe.db.exists("Server Script", name):
				frappe.delete_doc("Server Script", name, force=True, ignore_permissions=True)
			for job in jobs:
				frappe.delete_doc("Scheduled Job Type", job, force=True, ignore_permissions=True)
			frappe.client_cache.delete_value("server_script_map")

		self.addCleanup(remove)
		return name


class TestSchedulerEvent(V3TestCase):
	SCRIPT = 'open_count = frappe.db.count("ToDo", {"status": "Open"})\nfrappe.log_error("ToDo count", f"open: {open_count}")\n'

	def args(self, **extra):
		return {"operation": "create", "script_type": "Scheduler Event", "script": self.SCRIPT,
			"purpose": f"Log open ToDos {self.suffix}", **extra}

	def test_create_verify_undo(self):
		spec = SERVER.normalize(self.args(event_frequency="Daily"), ADMIN)
		name = self.track(spec["name"])
		self.assertEqual(name, f"Log open ToDos {self.suffix} - Daily")
		self.assertNotIn("reference_doctype", spec)
		report = SERVER.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("script.runs_as_admin", codes(report, "warning"))
		self.assertEqual(report.risk, "high")
		self.assertEqual(len(report.preview["next_runs"]), 3)
		self.assertTrue(report.preview["next_runs"][0].endswith("00:00:00"))
		json.dumps(report.to_dict())

		result = SERVER.execute(spec, ADMIN)
		verify = SERVER.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		job = frappe.get_all("Scheduled Job Type", filters={"server_script": name}, fields=["frequency", "stopped"])
		self.assertEqual([(j.frequency, j.stopped) for j in job], [("Daily", 0)])
		self.assertEqual(result["undo"], {"operation": "disable", "name": name})
		self.assertEqual(SERVER.snapshot(result["undo"])["event_frequency"], "Daily")

		update = SERVER.normalize({"operation": "update", "name": name, "event_frequency": "Cron", "cron_format": "30 6 * * 1"}, ADMIN)
		self.assertFalse(SERVER.check(update, ADMIN).critical())
		result = SERVER.execute(update, ADMIN)
		self.assertFalse([c for c in SERVER.verify(update, result) if c.level == "critical"])
		self.assertEqual(frappe.db.get_value("Scheduled Job Type", {"server_script": name}, "cron_format"), "30 6 * * 1")
		self.assertEqual((result["undo"]["event_frequency"], result["undo"]["cron_format"]), ("Daily", None))
		SERVER.execute(result["undo"], ADMIN)
		self.assertEqual(frappe.db.get_value("Server Script", name, "event_frequency"), "Daily")

		SERVER.execute({"operation": "disable", "name": name, "requested_by": ADMIN}, ADMIN)
		self.assertEqual(frappe.db.get_value("Scheduled Job Type", {"server_script": name}, "stopped"), 1)

	def test_frequency_and_cron_checks(self):
		with self.assertRaises(CustomizationError):  # explicit frequency required
			SERVER.normalize(self.args(), ADMIN)
		with self.assertRaises(CustomizationError):
			SERVER.normalize(self.args(event_frequency="Every minute"), ADMIN)
		with self.assertRaises(CustomizationError):  # Cron needs cron_format
			SERVER.normalize(self.args(event_frequency="Cron"), ADMIN)
		with self.assertRaises(CustomizationError):  # cron_format only with Cron
			SERVER.normalize(self.args(event_frequency="Daily", cron_format="0 1 * * *"), ADMIN)
		bad = SERVER.normalize(self.args(event_frequency="Cron", cron_format="61 * * * *"), ADMIN)
		self.assertIn("script.cron", critical(SERVER.check(bad, ADMIN)))
		frequent = SERVER.normalize(self.args(event_frequency="Cron", cron_format="*/2 * * * *"), ADMIN)
		report = SERVER.check(frequent, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("script.cron_frequent", codes(report, "warning"))
		self.assertIn("script.cron_frequent", codes(SERVER.check(SERVER.normalize(self.args(event_frequency="All"), ADMIN), ADMIN), "warning"))
		self.assertIn("script.long_queue", codes(SERVER.check(SERVER.normalize(self.args(event_frequency="Daily Long"), ADMIN), ADMIN), "info"))

	def test_scheduler_reads_everything_warning(self):
		spec = SERVER.normalize(self.args(event_frequency="Hourly",
			script='rows = frappe.get_all("ToDo", fields=["name"])\nfrappe.log_error("n", str(len(rows)))\n'), ADMIN)
		self.assertIn("py.unchecked_read", codes(SERVER.check(spec, ADMIN), "warning"))


class TestPermissionQuery(V3TestCase):
	def users(self):
		for email in (USER_A, USER_B):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}).insert(ignore_permissions=True)
		for i, email in enumerate((USER_A, USER_A, USER_B)):
			doc = frappe.get_doc({"doctype": "ToDo", "description": f"AI PQ test {self.suffix} {i}", "allocated_to": email}).insert(ignore_permissions=True)
			self.addCleanup(frappe.delete_doc, "ToDo", doc.name, force=True, ignore_permissions=True)
		return [USER_A, USER_B]

	def args(self, script=OWNER_ONLY, doctype="ToDo", **extra):
		return {"operation": "create", "script_type": "Permission Query", "reference_doctype": doctype, "script": script,
			"purpose": f"Own ToDos only {self.suffix}", **extra}

	def test_preview_builds_the_list_query_for_two_users(self):
		users = self.users()
		from frappe.core.doctype.server_script import server_script_utils

		before_map, before_get_doc = server_script_utils.get_server_script_map, frappe.get_doc
		spec = SERVER.normalize(self.args(sample_users=users), ADMIN)
		report = SERVER.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("script.pq_condition", codes(report, "ok"))
		self.assertIn("script.permission_query_preview", codes(report, "ok"))
		self.assertIn("script.permission_query_review", codes(report, "warning"))
		self.assertIn("script.permission_hooks", codes(report, "info"))  # ToDo has an app hook
		rows = {r["user"]: r for r in report.preview["permission_query"]}
		self.assertEqual(set(rows), set(users))
		for user, row in rows.items():
			self.assertIn(f"`tabToDo`.`owner` = '{user}'", row["conditions"])
			self.assertIn(f"`tabToDo`.`owner` = '{user}'", row["query_after"])
			self.assertNotIn("`tabToDo`.`owner` =", row["query_before"])
			self.assertLessEqual(row["count_after"], row["count_before"])
		self.assertEqual(rows[USER_A]["count_before"], 2)  # allocated to A by the ToDo hook
		self.assertEqual(rows[USER_A]["count_after"], 0)  # A owns none of them
		json.dumps(report.to_dict())
		# nothing leaked out of the preview
		self.assertIs(server_script_utils.get_server_script_map, before_map)
		self.assertIs(frappe.get_doc, before_get_doc)
		self.assertNotIn("ToDo", (server_script_utils.get_server_script_map().get("permission_query") or {}))

	def test_default_sample_users(self):
		report = SERVER.check(SERVER.normalize(self.args(), ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertTrue(1 <= len(report.preview["permission_query"]) <= 2)

	def test_bad_conditions_are_critical(self):
		cases = {
			'conditions = "`tabToDo`.`owner` = " + frappe.db.escape(user) + " or 1=1"': "script.pq_top_or",
			'conditions = "1=1) or (1=1"': "script.pq_escape",
			'conditions = "(1=1"': "script.pq_escape",
			'conditions = "1=1 -- rest of the query"': "script.pq_comment",
			'conditions = "1=1; drop table x"': "script.pq_comment",
			'conditions = "`tabToDo`.`owner` in (select name from `tabUser`)"': "script.pq_table",
			'conditions = "`tabToDo`.`owner` in (select user from `__Auth`)"': "script.pq_table",
			'conditions = "1=1 order by `tabToDo`.`modified`"': "script.pq_structure",
			'conditions = "1=1 and sleep(5)=0"': "script.pq_function",
			'conditions = "`tabToDo`.`owner` = ("': "script.pq_escape",
		}
		for script, code in cases.items():
			with self.subTest(code=code, script=script):
				spec = SERVER.normalize(self.args(script=script + "\n", sample_users=[ADMIN]), ADMIN)
				self.assertIn(code, critical(SERVER.check(spec, ADMIN)))

	def test_condition_validator(self):
		self.assertEqual(condition_codes("(`tabToDo`.`owner` = 'a' or `tabToDo`.`status` = 'Open')"), set())
		self.assertEqual(condition_codes("`tabToDo`.`owner` = 'a' and `tabToDo`.`status` = 'it''s; -- fine'"), set())
		self.assertEqual(condition_codes("`tabToDo`.`reference_name` in (select name from `tabNote` where `public` = 1)"), set())
		self.assertIn("script.pq_qualify", condition_codes("owner = 'a'", "info"))
		self.assertIn("script.pq_top_or", condition_codes("a = 1 || b = 2"))
		self.assertIn("script.pq_top_or", condition_codes("a = 1 xor b = 2"))
		self.assertIn("script.pq_structure", condition_codes("a = 1 limit 1"))
		self.assertIn("script.pq_syntax", condition_codes("a = 1 union select 1"))

	def test_script_rules(self):
		raw = SERVER.normalize(self.args(script='conditions = f"`tabToDo`.`owner` = \'{user}\'"\n', sample_users=[ADMIN]), ADMIN)
		self.assertIn("py.unescaped_user", codes(SERVER.check(raw, ADMIN), "warning"))
		no_conditions = SERVER.normalize(self.args(script="x = 1\n"), ADMIN)
		self.assertIn("py.conditions", critical(SERVER.check(no_conditions, ADMIN)))
		writes = SERVER.normalize(self.args(script='frappe.db.set_value("ToDo", "x", "status", "Open")\nconditions = ""\n'), ADMIN)
		self.assertIn("py.write", critical(SERVER.check(writes, ADMIN)))
		fails = SERVER.normalize(self.args(script='conditions = "x" + 1\n', sample_users=[ADMIN]), ADMIN)
		self.assertIn("script.permission_query_error", critical(SERVER.check(fails, ADMIN)))
		protected = SERVER.normalize(self.args(doctype="User", script='conditions = ""\n'), ADMIN)
		self.assertIn("script.doctype", critical(SERVER.check(protected, ADMIN)))
		with self.assertRaises(CustomizationError):
			SERVER.normalize({"operation": "create", "script_type": "Permission Query", "script": OWNER_ONLY, "purpose": "x"}, ADMIN)
		with self.assertRaises(CustomizationError):
			SERVER.normalize(self.args(sample_users=["nobody-ai@example.com"]), ADMIN)
		with self.assertRaises(CustomizationError):  # Workflow Task stays out
			SERVER.normalize({"operation": "create", "script_type": "Workflow Task", "script": "x=1", "purpose": "p"}, ADMIN)

	def test_create_verify_undo_and_one_per_doctype(self):
		# Note: few rows and rarely listed, so the short-lived cache entry disturbs nobody
		script = 'conditions = f"`tabNote`.`owner` = {frappe.db.escape(user)}"\n'
		spec = SERVER.normalize(self.args(script=script, doctype="Note", sample_users=[ADMIN]), ADMIN)
		name = self.track(spec["name"])
		self.assertEqual(name, f"Note - Permission Query - Own ToDos only {self.suffix}")
		report = SERVER.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		result = SERVER.execute(spec, ADMIN)
		verify = SERVER.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		self.assertEqual(get_server_script_map()["permission_query"].get("Note"), name)
		other = SERVER.normalize({**self.args(script=script, doctype="Note"), "name": f"{name} 2"}, ADMIN)
		self.assertIn("script.permission_query_taken", critical(SERVER.check(other, ADMIN)))
		SERVER.execute(result["undo"], ADMIN)
		frappe.client_cache.delete_value("server_script_map")
		self.assertIsNone(get_server_script_map()["permission_query"].get("Note"))
