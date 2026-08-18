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


class TestApprovalEngine(IntegrationTestCase):
	def setUp(self):
		self.engine = ApprovalEngine(PermissionContext("Administrator"))
		self.run = make_test_run()
		self._cleanup = {"AI Approval Request": [], "Note": [], "AI Run": [self.run.name]}

	def tearDown(self):
		# The engine commits as it works, so test records outlive the framework
		# rollback and must be deleted explicitly.
		for doctype, names in self._cleanup.items():
			delete_docs(doctype, names)
		frappe.db.commit()

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
