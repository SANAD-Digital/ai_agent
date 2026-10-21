"""Workflow kind: states, transitions, activation (docs/customization/01 §3.1, §4.2).

Round trips run on Quality Inspection (submittable, no documents on the dev site); checks that count documents
run on Journal Entry without saving anything. Every workflow, Workflow State and Workflow Action made here carries
the test prefix and is removed in cleanup, and no workflow is left active.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.workflow import KIND

ADMIN = "Administrator"
DOCTYPE = "Quality Inspection"
NO_SM = "ai-customize-no-read@example.com"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizeWorkflow(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		self.prefix = f"AIWF{frappe.generate_hash(length=5)}"
		self.had_field = bool(frappe.db.exists("Custom Field", {"dt": DOCTYPE, "fieldname": "workflow_state"}))
		self.had_column = "workflow_state" in frappe.db.get_table_columns(DOCTYPE)
		self.active_before = frappe.db.get_value("Workflow", {"document_type": DOCTYPE, "is_active": 1}, "name")
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user(ADMIN)
		for name in frappe.get_all("Workflow", filters={"name": ["like", f"{self.prefix}%"]}, pluck="name"):
			frappe.delete_doc("Workflow", name, force=True, ignore_permissions=True)
		for doctype in ("Workflow State", "Workflow Action Master"):
			for name in frappe.get_all(doctype, filters={"name": ["like", f"{self.prefix}%"]}, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		if not self.had_field:
			for name in frappe.get_all("Custom Field", filters={"dt": DOCTYPE, "fieldname": "workflow_state"}, pluck="name"):
				frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		if not self.had_column and "workflow_state" in frappe.db.get_table_columns(DOCTYPE):
			frappe.db.sql_ddl(f"alter table `tab{DOCTYPE}` drop column `workflow_state`")
		if self.active_before:
			frappe.db.set_value("Workflow", self.active_before, "is_active", 1)
		frappe.db.commit()
		frappe.clear_cache(doctype=DOCTYPE)
		frappe.cache.hdel("workflow", DOCTYPE)

	def s(self, name):
		return f"{self.prefix} {name}"

	def args(self, **over):
		base = {
			"operation": "create", "workflow_name": self.s("QI Approval"), "doctype": DOCTYPE,
			"states": [
				{"state": self.s("Draft"), "doc_status": 0, "allow_edit": "System Manager", "style": "Info"},
				{"state": self.s("Pending"), "doc_status": 0, "allow_edit": "System Manager"},
				{"state": self.s("Approved"), "doc_status": 1, "allow_edit": "System Manager"},
				{"state": self.s("Rejected"), "doc_status": 0, "allow_edit": "System Manager"},
				{"state": self.s("Cancelled"), "doc_status": 2, "allow_edit": "System Manager"},
			],
			"transitions": [
				{"state": self.s("Draft"), "action": self.s("Review"), "next_state": self.s("Pending"), "allowed": "System Manager"},
				{"state": self.s("Pending"), "action": "Approve", "next_state": self.s("Approved"), "allowed": "System Manager",
					"condition": "doc.docstatus == 0", "allow_self_approval": 0},
				{"state": self.s("Pending"), "action": "Reject", "next_state": self.s("Rejected"), "allowed": "System Manager"},
			],
			"purpose": "test",
		}
		base.update(over)
		return base

	# -- normalize ------------------------------------------------------------------------------
	def test_normalize_rejects_bad_arguments(self):
		cases = [
			{"operation": "create", "workflow_name": "x", "doctype": DOCTYPE, "states": [], "transitions": [], "colour": 1},
			{"operation": "remove", "workflow_name": "x"},
			{"operation": "create", "workflow_name": "x", "doctype": DOCTYPE},
			self.args(states=[{"state": "A", "doc_status": 3, "allow_edit": "System Manager"}]),
			self.args(states=[{"state": "A", "allow_edit": "System Manager", "colour": "red"}]),
			self.args(states=[{"state": "A", "allow_edit": "System Manager", "update_value": "x"}]),
			self.args(transitions=[{"state": "A", "action": "B"}]),
			{"operation": "update", "workflow_name": self.s("missing"), "states": []},
			{"operation": "activate", "workflow_name": self.s("missing")},
		]
		for args in cases:
			with self.subTest(args=args), self.assertRaises(CustomizationError) as ctx:
				KIND.normalize(args, ADMIN)
			self.assertTrue(ctx.exception.message)
		spec = KIND.normalize(self.args(), ADMIN)
		self.assertEqual(spec["activate"], 1)
		self.assertEqual(spec["states"][2]["doc_status"], "1")
		self.assertEqual(spec["transitions"][0]["allow_self_approval"], 1)  # Frappe's default
		self.assertEqual(spec["transitions"][1]["allow_self_approval"], 0)

	# -- check ----------------------------------------------------------------------------------
	def test_frappe_rules_are_critical(self):
		D, P, A, C = self.s("Draft"), self.s("Pending"), self.s("Approved"), self.s("Cancelled")
		cases = [
			(self.args(states=[{"state": A, "doc_status": 1, "allow_edit": "System Manager"},
				{"state": D, "doc_status": 0, "allow_edit": "System Manager"}],
				transitions=[{"state": A, "action": "X", "next_state": D, "allowed": "System Manager"}]),
				{"workflow.first_state", "workflow.docstatus"}),  # first state submitted; 1 -> 0
			(self.args(transitions=[{"state": D, "action": "X", "next_state": C, "allowed": "System Manager"}]),
				{"workflow.docstatus"}),  # 0 -> 2
			(self.args(transitions=[{"state": D, "action": "X", "next_state": A, "allowed": "System Manager"},
				{"state": C, "action": "Y", "next_state": A, "allowed": "System Manager"}]),
				{"workflow.docstatus"}),  # 2 -> anything
			(self.args(transitions=[{"state": D, "action": "X", "next_state": self.s("Nowhere"), "allowed": "System Manager"}]),
				{"workflow.transition_state"}),
			(self.args(transitions=[{"state": D, "action": "X", "next_state": P, "allowed": "No Such Role AI"}]),
				{"workflow.role"}),
			(self.args(transitions=[{"state": D, "action": "X", "next_state": P, "allowed": "Guest"}]), {"workflow.role"}),
			(self.args(transitions=[{"state": D, "action": "X", "next_state": P, "allowed": "System Manager", "condition": "doc.status =="}]),
				{"workflow.condition"}),
			(self.args(transitions=[{"state": D, "action": "X", "next_state": P, "allowed": "System Manager", "condition": "doc.no_such_field.upper()"}]),
				{"workflow.condition"}),  # runs on a real document and fails
			(self.args(states=[{"state": D, "allow_edit": "System Manager", "update_field": "no_such_field", "update_value": "1"}],
				transitions=[{"state": D, "action": "X", "next_state": D, "allowed": "System Manager"}]), {"workflow.update_field"}),
			(self.args(states=[{"state": D, "allow_edit": "System Manager", "update_field": "workflow_state", "update_value": "1"}],
				transitions=[{"state": D, "action": "X", "next_state": D, "allowed": "System Manager"}]), {"workflow.update_field"}),
			(self.args(transitions=[{"state": D, "action": "X", "next_state": P, "allowed": "System Manager"}]),
				{"workflow.unreachable"}),  # Approved / Rejected never reached
			(self.args(states=[{"state": D, "allow_edit": "System Manager"}, {"state": D, "allow_edit": "System Manager"}],
				transitions=[{"state": D, "action": "X", "next_state": D, "allowed": "System Manager"}]), {"workflow.state_duplicate"}),
			(self.args(doctype="ToDo"), {"workflow.doc_status"}),  # submitted / cancelled states on a non-submittable DocType
			(self.args(doctype="User"), {"workflow.doctype"}),  # core DocType
		]
		for args, expected in cases:
			with self.subTest(expected=expected):
				found = critical(KIND.check(KIND.normalize(args, ADMIN), ADMIN))
				self.assertTrue(expected <= found, found)

	def test_valid_workflow_preview_and_warnings(self):
		role = f"{self.prefix} Nobody"
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Role", role, force=True, ignore_permissions=True))
		args = self.args()
		args["transitions"].append({"state": self.s("Pending"), "action": "Reject", "next_state": self.s("Rejected"), "allowed": "System Manager"})
		args["transitions"].append({"state": self.s("Draft"), "action": "Hold", "next_state": self.s("Draft"), "allowed": role})
		report = KIND.check(KIND.normalize(args, ADMIN), ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		warnings = codes(report, "warning")
		self.assertIn("workflow.dead_end", warnings)  # Rejected is a draft state with no exit
		self.assertIn("workflow.transition_duplicate", warnings)
		self.assertIn("workflow.role_unheld", warnings)
		self.assertIn("workflow.role_permission", warnings)  # the new role has no write on Quality Inspection
		self.assertIn("workflow.final", codes(report, "info"))
		self.assertEqual([r["state"] for r in report.preview["states"]["rows"]][:2], [self.s("Draft"), self.s("Pending")])
		self.assertTrue(any("(loop)" in p for p in report.preview["paths"]))
		self.assertTrue(any(p.endswith(self.s("Approved")) for p in report.preview["paths"]))
		self.assertTrue(any("Workflow State" in line for line in report.impact))
		self.assertTrue(any("workflow_state" in line for line in report.impact))  # Frappe adds the field
		json.dumps(report.to_dict())

		no_submit = self.args(transitions=[{"state": self.s("Draft"), "action": "Reject", "next_state": self.s("Rejected"), "allowed": "System Manager"}],
			states=[{"state": self.s("Draft"), "allow_edit": "System Manager"}, {"state": self.s("Rejected"), "allow_edit": "System Manager"}])
		self.assertIn("workflow.never_submits", codes(KIND.check(KIND.normalize(no_submit, ADMIN), ADMIN), "warning"))

	def test_submit_cancel_policy(self):
		spec = KIND.normalize(self.args(), ADMIN)
		with patch("ai_agent.runtime.policy.submit_cancel_allowed", return_value=False):
			self.assertIn("workflow.submit_cancel_policy", critical(KIND.check(spec, ADMIN)))
		with patch("ai_agent.runtime.policy.submit_cancel_allowed", return_value=True):
			self.assertNotIn("workflow.submit_cancel_policy", critical(KIND.check(spec, ADMIN)))

	def test_requester_without_system_manager(self):
		if not frappe.db.exists("User", NO_SM):
			frappe.get_doc({"doctype": "User", "email": NO_SM, "first_name": "No Read", "send_welcome_email": 0,
				"user_type": "Website User"}).insert(ignore_permissions=True)
		self.assertIn("roles.missing", critical(KIND.check(KIND.normalize(self.args(), ADMIN), NO_SM)))

	def test_documents_without_a_matching_state_are_counted(self):
		doctype = "Journal Entry"
		if not frappe.db.exists("DocType", doctype):
			self.skipTest("ERPNext is not installed")
		cancelled = frappe.db.count(doctype, {"docstatus": 2})
		drafts = frappe.db.count(doctype, {"docstatus": 0})
		args = self.args(doctype=doctype, workflow_name=self.s("JE"),
			states=[{"state": self.s("Draft"), "allow_edit": "System Manager"},
				{"state": self.s("Approved"), "doc_status": 1, "allow_edit": "System Manager"}],
			transitions=[{"state": self.s("Draft"), "action": "Approve", "next_state": self.s("Approved"), "allowed": "System Manager"}])
		report = KIND.check(KIND.normalize(args, ADMIN), ADMIN)
		text = "\n".join(report.impact)
		if cancelled:
			self.assertIn(f"{cancelled} Journal Entry document(s) (Cancelled) match no state", text)
		if drafts:
			self.assertIn(f"{drafts} Journal Entry document(s) (Draft) without a state are set to {self.s('Draft')}", text)
		self.assertFalse(frappe.db.exists("Workflow", self.s("JE")))  # check saved nothing

	# -- execute / verify / undo ---------------------------------------------------------------
	def run_spec(self, spec):
		self.assertFalse(KIND.check(spec, ADMIN).critical(), [c.message for c in KIND.check(spec, ADMIN).critical()])
		result = KIND.execute(spec, ADMIN)
		verify = KIND.verify(spec, result)
		self.assertFalse([c for c in verify if c.level == "critical"], [c.message for c in verify])
		return result

	def active(self):
		frappe.cache.hdel("workflow", DOCTYPE)
		return frappe.db.get_value("Workflow", {"document_type": DOCTYPE, "is_active": 1}, "name")

	def test_round_trip(self):
		first = KIND.normalize(self.args(), ADMIN)
		before = KIND.snapshot(first)
		self.assertFalse(before["exists"])
		result = self.run_spec(first)
		name = first["workflow_name"]
		self.assertEqual(self.active(), name)
		self.assertTrue(frappe.get_meta(DOCTYPE, cached=False).get_field("workflow_state"))
		self.assertTrue(frappe.db.exists("Workflow State", self.s("Draft")))
		self.assertEqual(frappe.db.get_value("Workflow State", self.s("Draft"), "style"), "Info")
		self.assertTrue(frappe.db.exists("Workflow Action Master", self.s("Review")))
		undo_first = result["undo"]
		self.assertEqual(undo_first["operation"], "delete")
		self.assertIn(self.s("Draft"), undo_first["remove_states"])
		self.assertNotIn("Approve", undo_first["remove_actions"])  # it existed before

		# a second active workflow deactivates the first; its undo brings the first back
		second = KIND.normalize(self.args(workflow_name=self.s("QI Second")), ADMIN)
		report = KIND.check(second, ADMIN)
		self.assertTrue(any(name in line and "Deactivates" in line for line in report.impact), report.impact)
		result2 = self.run_spec(second)
		self.assertEqual(self.active(), second["workflow_name"])
		self.assertEqual(frappe.db.get_value("Workflow", name, "is_active"), 0)
		self.assertEqual(result2["undo"]["reactivate"], name)
		self.assertEqual(result2["undo"]["remove_states"], [])  # the first one made them
		self.run_spec(result2["undo"])
		self.assertFalse(frappe.db.exists("Workflow", second["workflow_name"]))
		self.assertEqual(self.active(), name)
		self.assertTrue(frappe.db.exists("Workflow State", self.s("Draft")))  # still used by the first workflow

		# update: a new action; undo restores the transitions and removes the action it created
		update_args = {"operation": "update", "workflow_name": name, "transitions": self.args()["transitions"] + [
			{"state": self.s("Rejected"), "action": self.s("Reopen"), "next_state": self.s("Draft"), "allowed": "System Manager"}]}
		update = KIND.normalize(update_args, ADMIN)
		self.assertNotIn("workflow.dead_end", codes(KIND.check(update, ADMIN), "warning"))
		result3 = self.run_spec(update)
		self.assertTrue(frappe.db.exists("Workflow Action Master", self.s("Reopen")))
		self.assertEqual(len(frappe.get_doc("Workflow", name).transitions), 4)
		self.assertEqual(result3["undo"]["remove_actions"], [self.s("Reopen")])
		self.run_spec(result3["undo"])
		self.assertEqual(len(frappe.get_doc("Workflow", name).transitions), 3)
		self.assertFalse(frappe.db.exists("Workflow Action Master", self.s("Reopen")))

		# deactivate -> undo activates again
		deactivate = KIND.normalize({"operation": "deactivate", "workflow_name": name}, ADMIN)
		result4 = self.run_spec(deactivate)
		self.assertIsNone(self.active())
		self.assertEqual(result4["undo"]["operation"], "activate")
		self.run_spec(result4["undo"])
		self.assertEqual(self.active(), name)

		# undo of the create: the workflow and the records it created are gone
		self.assertEqual(KIND.snapshot(first)["active_workflow"], name)
		self.run_spec(undo_first)
		self.assertFalse(frappe.db.exists("Workflow", name))
		self.assertFalse(frappe.db.exists("Workflow State", self.s("Draft")))
		self.assertFalse(frappe.db.exists("Workflow Action Master", self.s("Review")))
		self.assertTrue(frappe.db.exists("Workflow Action Master", "Approve"))
		self.assertIsNone(self.active())

	def test_create_inactive_changes_nothing(self):
		spec = KIND.normalize(self.args(activate=0), ADMIN)
		report = KIND.check(spec, ADMIN)
		self.assertTrue(any("inactive" in line for line in report.impact), report.impact)
		self.run_spec(spec)
		self.assertEqual(frappe.db.get_value("Workflow", spec["workflow_name"], "is_active"), 0)
		self.assertEqual(self.active(), self.active_before)
