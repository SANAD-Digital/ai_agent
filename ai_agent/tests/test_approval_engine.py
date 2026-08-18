import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import (
	ApprovalAlreadyExecuted,
	ApprovalExpired,
	ConcurrencyConflict,
	IdempotencyConflict,
	PermissionDenied,
)
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.utils import delete_docs, make_test_run


TEST_APPROVER_ROLE = "AI Test Approver Role"
TEST_APPROVER_USER = "ai-approver-test@example.com"


class TestApprovalEngine(IntegrationTestCase):
	def setUp(self):
		self.engine = ApprovalEngine(PermissionContext("Administrator"))
		self.run = make_test_run()
		self._cleanup = {"AI Approval Request": [], "Note": [], "AI Run": [self.run.name]}
		self._previous_policy = {
			"approver_role": frappe.db.get_single_value("AI Settings", "approver_role"),
			"require_four_eyes": frappe.db.get_single_value("AI Settings", "require_four_eyes"),
		}

	def tearDown(self):
		self._set_policy(
			approver_role=self._previous_policy["approver_role"],
			require_four_eyes=self._previous_policy["require_four_eyes"] or 0,
		)
		# The engine commits as it works, so test records outlive the framework
		# rollback and must be deleted explicitly.
		for doctype, names in self._cleanup.items():
			delete_docs(doctype, names)
		frappe.db.commit()

	def _set_policy(self, approver_role=None, require_four_eyes=0):
		frappe.db.set_single_value("AI Settings", "approver_role", approver_role or "")
		frappe.db.set_single_value("AI Settings", "require_four_eyes", require_four_eyes)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def _make_approver_user(self, with_role=True):
		if not frappe.db.exists("Role", TEST_APPROVER_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": TEST_APPROVER_ROLE}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("User", TEST_APPROVER_USER):
			user = frappe.get_doc({
				"doctype": "User",
				"email": TEST_APPROVER_USER,
				"first_name": "AI Approver Test",
				"send_welcome_email": 0,
			})
			user.flags.no_welcome_mail = True
			user.insert(ignore_permissions=True)
		user = frappe.get_doc("User", TEST_APPROVER_USER)
		has_role = any(r.role == TEST_APPROVER_ROLE for r in user.roles)
		if with_role and not has_role:
			user.append("roles", {"role": TEST_APPROVER_ROLE})
			user.save(ignore_permissions=True)
		elif not with_role and has_role:
			user.roles = [r for r in user.roles if r.role != TEST_APPROVER_ROLE]
			user.save(ignore_permissions=True)
		frappe.clear_cache(user=TEST_APPROVER_USER)
		return TEST_APPROVER_USER

	def _make_approval(self, **overrides):
		payload = overrides.pop("payload", {"doctype": "Note", "title": "AI approval test note"})
		approval = self.engine.create_approval_request(
			run_name=self.run.name,
			action_type=overrides.pop("action_type", "Create"),
			doctype_name=overrides.pop("doctype_name", "Note"),
			action_summary="ai_agent automated test",
			payload=payload,
			**overrides,
		)
		self._cleanup["AI Approval Request"].append(approval.name)
		return approval

	def test_payload_tampering_blocks_approval(self):
		approval = self._make_approval()
		frappe.db.set_value(
			"AI Approval Request",
			approval.name,
			"payload_json",
			json.dumps({"doctype": "Note", "title": "tampered"}),
			update_modified=False,
		)
		with self.assertRaises(PermissionDenied):
			self.engine.approve(approval.name, user="Administrator")

	def test_expired_approval_cannot_be_approved(self):
		approval = self._make_approval()
		frappe.db.set_value(
			"AI Approval Request",
			approval.name,
			"expires_at",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5),
			update_modified=False,
		)
		with self.assertRaises(ApprovalExpired):
			self.engine.approve(approval.name, user="Administrator")
		self.assertEqual(
			frappe.db.get_value("AI Approval Request", approval.name, "status"), "Expired"
		)

	def test_guest_cannot_approve(self):
		approval = self._make_approval()
		with self.assertRaises(PermissionDenied):
			self.engine.approve(approval.name, user="Guest")

	def test_idempotency_key_conflicts_while_pending(self):
		key = frappe.generate_hash(length=16)
		self._make_approval(idempotency_key=key)
		with self.assertRaises(IdempotencyConflict):
			self._make_approval(idempotency_key=key)

	def test_approved_create_executes_and_marks_executed(self):
		approval = self._make_approval()
		self.engine.approve(approval.name, user="Administrator")
		result = self.engine.execute_approved_action(approval.name)
		self._cleanup["Note"].append(result["document_name"])
		self.assertTrue(frappe.db.exists("Note", result["document_name"]))
		self.assertEqual(
			frappe.db.get_value("AI Approval Request", approval.name, "status"), "Executed"
		)

	def test_executed_approval_cannot_be_approved_again(self):
		approval = self._make_approval()
		self.engine.approve(approval.name, user="Administrator")
		result = self.engine.execute_approved_action(approval.name)
		self._cleanup["Note"].append(result["document_name"])
		with self.assertRaises(ApprovalAlreadyExecuted):
			self.engine.approve(approval.name, user="Administrator")

	def test_pending_approval_cannot_be_executed(self):
		approval = self._make_approval()
		with self.assertRaises(ApprovalAlreadyExecuted):
			self.engine.execute_approved_action(approval.name)

	def test_four_eyes_blocks_self_approval(self):
		self._set_policy(require_four_eyes=1)
		approval = self._make_approval()
		with self.assertRaises(PermissionDenied) as ctx:
			self.engine.approve(approval.name, user=approval.requested_by)
		self.assertIn("Four-eyes", str(ctx.exception))

	def test_custom_approver_role_authorizes_non_system_manager(self):
		approver = self._make_approver_user(with_role=True)
		self._set_policy(approver_role=TEST_APPROVER_ROLE)
		approval = self._make_approval()
		approved = self.engine.approve(approval.name, user=approver)
		self.assertEqual(approved.status, "Approved")
		self.assertEqual(approved.approved_by, approver)

	def test_non_system_manager_without_role_cannot_approve(self):
		approver = self._make_approver_user(with_role=False)
		approval = self._make_approval()
		with self.assertRaises(PermissionDenied):
			self.engine.approve(approval.name, user=approver)

	def test_update_with_stale_expected_modified_conflicts(self):
		note = frappe.get_doc({"doctype": "Note", "title": "concurrency test"}).insert(
			ignore_permissions=True
		)
		self._cleanup["Note"].append(note.name)
		approval = self._make_approval(
			action_type="Update",
			document_name=note.name,
			payload={"title": "updated title"},
			expected_modified="2000-01-01 00:00:00.000000",
		)
		self.engine.approve(approval.name, user="Administrator")
		with self.assertRaises(ConcurrencyConflict):
			self.engine.execute_approved_action(approval.name)
