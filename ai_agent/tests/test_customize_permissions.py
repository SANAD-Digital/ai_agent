"""Permissions kind: role permissions on a DocType through Custom DocPerm (docs/customization/01 §3.1 v3).

Every test works on a throwaway custom DocType, role and user made in setUp and removed afterwards.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.contract import CustomizationError
from ai_agent.customization.kinds.permissions import KIND, custom_rows, effective_rows

ADMIN = "Administrator"


def critical(report):
	return {c.code for c in report.critical()}


def codes(report, level):
	return {c.code for c in report.checks if c.level == level}


class TestCustomizePermissions(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		suffix = frappe.generate_hash(length=6)
		self.doctype = f"Zz Perm Test {suffix}"
		self.role = f"Zz Perm Role {suffix}"
		self.user = f"zz-perm-{suffix}@example.com"
		frappe.get_doc({"doctype": "Role", "role_name": self.role, "desk_access": 1}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "DocType", "name": self.doctype, "module": "Custom", "custom": 1, "autoname": "hash",
			"fields": [{"fieldname": "title", "label": "Title", "fieldtype": "Data"},
				{"fieldname": "secret", "label": "Secret", "fieldtype": "Data", "permlevel": 1}],
			"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1}],
		}).insert(ignore_permissions=True)
		user = frappe.get_doc({"doctype": "User", "email": self.user, "first_name": "Zz Perm", "send_welcome_email": 0,
			"user_type": "System User"}).insert(ignore_permissions=True)
		user.add_roles(self.role)
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user(ADMIN)
		frappe.db.delete("Custom DocPerm", {"parent": self.doctype})
		if frappe.db.exists("User", self.user):
			frappe.delete_doc("User", self.user, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", self.doctype):
			frappe.delete_doc("DocType", self.doctype, force=True, ignore_permissions=True)
		frappe.db.sql_ddl(f"drop table if exists `tab{self.doctype}`")
		if frappe.db.exists("Role", self.role):
			frappe.delete_doc("Role", self.role, force=True, ignore_permissions=True)
		frappe.db.commit()

	def spec(self, **args):
		return KIND.normalize({"doctype": self.doctype, **args}, ADMIN)

	# -- normalize -------------------------------------------------------------------
	def test_normalize(self):
		spec = self.spec(operation="grant", role=self.role, rights=["write", "read"], purpose="  let them edit  ")
		self.assertEqual(spec["rights"], ["read", "write"])  # Frappe's order
		self.assertEqual((spec["permlevel"], spec["if_owner"], spec["name"]), (0, 0, self.doctype))
		self.assertEqual(spec["purpose"], "let them edit")
		self.assertEqual(self.spec(operation="grant", role=self.role, rights="read, print")["rights"], ["read", "print"])
		self.assertEqual(self.spec(operation="revoke", role=self.role)["rights"], [])
		self.assertTrue(KIND.four_eyes_required)

		bad = [
			{"operation": "grant", "role": self.role, "rights": ["read"], "colour": "red"},  # unknown key
			{"operation": "allow", "role": self.role, "rights": ["read"]},
			{"operation": "grant", "role": self.role},  # no rights
			{"operation": "grant", "role": self.role, "rights": ["fly"]},
			{"operation": "grant", "role": "No Such Role zz", "rights": ["read"]},
			{"operation": "grant", "role": "Administrator", "rights": ["read"]},
			{"operation": "grant", "role": "Guest", "rights": ["read"]},  # automatic role, not explicit
			{"operation": "grant", "role": self.role, "rights": ["read"], "permlevel": 12},
			{"operation": "reset", "role": self.role},
		]
		for args in bad:
			with self.assertRaises(CustomizationError, msg=args):
				self.spec(**args)
		with self.assertRaises(CustomizationError):
			KIND.normalize({"operation": "grant", "doctype": "No Such DocType zz", "role": self.role, "rights": ["read"]}, ADMIN)
		with self.assertRaises(CustomizationError) as ctx:
			KIND.normalize({"operation": "grant", "doctype": "Sales Invoice Item", "role": self.role, "rights": ["read"]}, ADMIN)
		self.assertTrue(ctx.exception.hints)
		explicit = self.spec(operation="grant", role="Guest", rights=["read"], allow_automatic_role=1)
		self.assertEqual(explicit["allow_automatic_role"], 1)

	# -- grant / undo round trip ---------------------------------------------------------
	def test_grant_execute_verify_undo(self):
		original = effective_rows(self.doctype)
		spec = self.spec(operation="grant", role=self.role, rights=["read", "write", "create"])
		before = KIND.snapshot(spec)
		self.assertEqual(before["custom_rows"], [])
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertIn("perm.valid", codes(report, "ok"))
		self.assertIn("perm.four_eyes", codes(report, "info"))
		self.assertEqual(report.risk, "high")
		users = {u["user"]: u for u in report.preview["users"]}
		self.assertEqual(users[self.user]["gains"], ["create", "read", "write"])
		self.assertTrue(any(self.user in line for line in report.impact))
		self.assertEqual([c["change"] for c in report.preview["changes"]], ["added"])
		json.dumps(report.to_dict())

		result = KIND.execute(spec, ADMIN)
		checks = KIND.verify(spec, result)
		self.assertFalse([c for c in checks if c.level == "critical"], [c.message for c in checks])
		self.assertTrue(frappe.has_permission(self.doctype, "write", user=self.user))
		self.assertEqual(result["undo"], {"operation": "restore", "doctype": self.doctype, "name": self.doctype, "rows": []})
		self.assertEqual(len(custom_rows(self.doctype)), 2)  # System Manager copied + the new rule

		undo = KIND.normalize(result["undo"], ADMIN)
		undo_report = KIND.check(undo, ADMIN)
		self.assertFalse(undo_report.critical(), [c.message for c in undo_report.critical()])
		self.assertEqual(undo_report.preview["users"][0]["loses"], ["create", "read", "write"])
		KIND.execute(undo, ADMIN)
		self.assertFalse([c for c in KIND.verify(undo, {}) if c.level == "critical"])
		self.assertEqual(KIND.snapshot(spec), before)
		self.assertEqual(effective_rows(self.doctype), original)
		self.assertFalse(frappe.has_permission(self.doctype, "read", user=self.user))

	def test_revoke_rights_then_rule_and_reset(self):
		KIND.execute(self.spec(operation="grant", role=self.role, rights=["read", "write", "delete"]), ADMIN)
		spec = self.spec(operation="revoke", role=self.role, rights=["delete", "write"])
		report = KIND.check(spec, ADMIN)
		self.assertFalse(report.critical(), [c.message for c in report.critical()])
		self.assertEqual(report.preview["changes"][0]["lost"], ["write", "delete"])
		result = KIND.execute(spec, ADMIN)
		self.assertFalse([c for c in KIND.verify(spec, result) if c.level == "critical"])
		self.assertEqual(len(result["undo"]["rows"]), 2)

		whole = self.spec(operation="revoke", role=self.role, rights=["read"])
		self.assertIn("perm.rule_removed", codes(KIND.check(whole, ADMIN), "info"))
		KIND.execute(whole, ADMIN)
		self.assertFalse([r for r in custom_rows(self.doctype) if r["role"] == self.role])

		reset = self.spec(operation="reset")
		self.assertFalse(KIND.check(reset, ADMIN).critical())
		result = KIND.execute(reset, ADMIN)
		self.assertEqual(custom_rows(self.doctype), [])
		self.assertFalse([c for c in KIND.verify(reset, result) if c.level == "critical"])
		self.assertIn("perm.no_change", critical(KIND.check(reset, ADMIN)))

	# -- checks --------------------------------------------------------------------------
	def test_warnings(self):
		report = KIND.check(self.spec(operation="grant", role=self.role, rights=["read", "write", "delete"]), ADMIN)
		self.assertIn("perm.risky_right", codes(report, "warning"))
		self.assertFalse(report.critical())
		report = KIND.check(self.spec(operation="grant", role=self.role, rights=["read"], permlevel=3), ADMIN)
		self.assertIn("perm.permlevel_unused", codes(report, "warning"))
		self.assertIn("perm.frappe", critical(report))  # level 3 without a level 0 rule (doctype.py check_level_zero_is_set)
		report = KIND.check(self.spec(operation="grant", role="Desk User", rights=["read"], allow_automatic_role=1), ADMIN)
		self.assertIn("perm.role_desk_user", codes(report, "warning"))

	def test_criticals(self):
		def found(**args):
			return critical(KIND.check(self.spec(**args), ADMIN))

		self.assertIn("perm.role_automatic", found(operation="grant", role="Guest", rights=["read"], allow_automatic_role=1))
		self.assertIn("perm.role_automatic", found(operation="grant", role="All", rights=["read"], allow_automatic_role=1))
		self.assertIn("perm.role_desk_user", found(operation="grant", role="Desk User", rights=["read", "write"], allow_automatic_role=1))
		self.assertIn("perm.no_rule", found(operation="revoke", role=self.role))
		self.assertIn("perm.report_if_owner", found(operation="grant", role=self.role, rights=["read", "report"], if_owner=1))
		self.assertIn("perm.frappe", found(operation="grant", role=self.role, rights=["read", "submit"]))  # not submittable
		self.assertIn("perm.no_change", found(operation="grant", role="System Manager", rights=["read"]))
		self.assertIn("perm.no_change", found(operation="reset"))
		last = found(operation="revoke", role="System Manager")
		self.assertIn("perm.empty", last)
		last_write = found(operation="revoke", role="System Manager", rights=["write"])
		self.assertIn("perm.last_write", last_write)
		for doctype in ("User", "Custom DocPerm", "Server Script", "AI Settings", "Company"):
			if not frappe.db.exists("DocType", doctype):
				continue
			spec = {"operation": "grant", "doctype": doctype, "name": doctype, "role": self.role, "permlevel": 0,
				"if_owner": 0, "rights": ["read"]}
			self.assertIn("perm.doctype_protected", critical(KIND.check(spec, ADMIN)), doctype)

	def test_requester_without_role(self):
		report = KIND.check(self.spec(operation="grant", role=self.role, rights=["read"]), self.user)
		self.assertIn("roles.missing", critical(report))

	def test_summary(self):
		self.assertIn(self.role, KIND.summary(self.spec(operation="grant", role=self.role, rights=["read"])))
		self.assertIn(self.doctype, KIND.summary(self.spec(operation="reset")))
