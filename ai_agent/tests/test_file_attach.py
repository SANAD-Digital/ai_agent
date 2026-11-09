"""Attaching files to documents: with a create / update request (a document keeps the file it was made from) or on
its own (actions.request_attach_files, an Approved Method a person decides). Only files of this conversation or of
documents the requester can read; the same stored file, a new File record; executed exactly once."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.engine.tools import approval_snapshot
from ai_agent.mcp import approvals as mcp_approvals
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tools import actions, file_attach
from ai_agent.tests.utils import delete_docs, ensure_test_agent, make_test_run

ADMIN = "Administrator"


class TestFileAttach(IntegrationTestCase):
	def setUp(self):
		frappe.set_user(ADMIN)
		agent = ensure_test_agent()
		self.session = frappe.get_doc({"doctype": "AI Chat Session", "user": ADMIN, "agent": agent.name, "title": "attach test"}).insert(ignore_permissions=True)
		message = frappe.get_doc({"doctype": "AI Chat Message", "session": self.session.name, "role": "user", "seq": 1, "status": "completed",
			"user": ADMIN, "text": "extract this invoice"}).insert(ignore_permissions=True)
		self.file = frappe.get_doc({"doctype": "File", "file_name": "invoice-scan.txt", "content": b"invoice 123 total 450", "decode": False, "is_private": 1,
			"attached_to_doctype": "AI Chat Message", "attached_to_name": message.name}).insert(ignore_permissions=True)
		self.run = make_test_run()
		frappe.db.set_value("AI Run", self.run.name, "chat_session", self.session.name)
		self.engine = ApprovalEngine(PermissionContext(ADMIN))
		self.made = []
		self.addCleanup(self.cleanup)

	def cleanup(self):
		frappe.set_user(ADMIN)
		for doctype, name in self.made:
			for f in frappe.get_all("File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, pluck="name"):
				frappe.delete_doc("File", f, force=True, ignore_permissions=True)
			delete_docs(doctype, [name])
		delete_docs("AI Approval Request", frappe.get_all("AI Approval Request", filters={"run": self.run.name}, pluck="name"))
		delete_docs("File", [self.file.name])
		delete_docs("AI Chat Message", frappe.get_all("AI Chat Message", filters={"session": self.session.name}, pluck="name"))
		delete_docs("AI Chat Session", [self.session.name])
		delete_docs("AI Run", [self.run.name])
		frappe.db.commit()

	def attached(self, doctype, name):
		return frappe.get_all("File", filters={"attached_to_doctype": doctype, "attached_to_name": name}, fields=["file_url", "file_name"])

	def request(self, action_type, payload, **extra):
		return self.engine.create_approval_request(run_name=self.run.name, action_type=action_type, action_summary="test",
			payload=payload, **extra)

	def test_a_created_document_keeps_the_file_it_was_made_from(self):
		approval = self.request("Create", {"description": "from a scan", file_attach.KEY: ["invoice-scan.txt"]}, doctype_name="ToDo")
		self.assertEqual(approval_snapshot(approval)["attach_files"], ["invoice-scan.txt"], "the card says what will be attached")
		outcome = self.engine.decide(approval.name, "approve", user=ADMIN)
		todo = outcome.result["document_name"]
		self.made.append(("ToDo", todo))
		files = self.attached("ToDo", todo)
		self.assertEqual([(f.file_url, f.file_name) for f in files], [(self.file.file_url, "invoice-scan.txt")], "the same stored file")
		self.assertEqual(outcome.result["attached"][0]["file_name"], "invoice-scan.txt")
		self.assertFalse(frappe.db.get_value("ToDo", todo, "description").startswith("_"), "the key is not a field")

	def test_attach_to_an_existing_document_runs_once(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "existing"}).insert(ignore_permissions=True)
		self.made.append(("ToDo", todo.name))
		out = actions.handle_request_attach_files({"doctype": "ToDo", "document_name": todo.name, "files": [self.file.name], "run": self.run.name},
			PermissionContext(ADMIN))
		self.assertTrue(out.get("approval_required"), out)
		approval = frappe.get_doc("AI Approval Request", out["approval_id"])
		self.assertEqual(approval.action_type, "Approved Method")
		self.assertIn("invoice-scan.txt", approval.action_summary)
		card = mcp_approvals.build_card(approval, ADMIN)
		self.assertIn("invoice-scan.txt", [f["d"] for f in card["fields"]])
		self.engine.decide(approval.name, "approve", user=ADMIN)
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval.name, "status"), "Executed", "recorded, not left Approved")
		again = self.engine.decide(approval.name, "approve", user=ADMIN)
		self.assertTrue(again.repeated)
		self.assertEqual(len(self.attached("ToDo", todo.name)), 1, "never twice")

	def test_an_update_can_attach_too(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "to edit"}).insert(ignore_permissions=True)
		self.made.append(("ToDo", todo.name))
		approval = self.request("Update", {"description": "edited", file_attach.KEY: [self.file.name]}, doctype_name="ToDo", document_name=todo.name)
		self.engine.decide(approval.name, "approve", user=ADMIN)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "edited")
		self.assertEqual(len(self.attached("ToDo", todo.name)), 1)

	def test_only_files_the_requester_may_use(self):
		with self.assertRaises(PermissionDenied):
			self.request("Create", {"description": "x", file_attach.KEY: ["no-such-file.pdf"]}, doctype_name="ToDo")
		other = frappe.get_doc({"doctype": "File", "file_name": "someone-elses.txt", "content": b"other", "decode": False,
			"is_private": 1}).insert(ignore_permissions=True)
		frappe.db.set_value("File", other.name, "owner", "Guest")
		self.addCleanup(delete_docs, "File", [other.name])
		with self.assertRaises(PermissionDenied):
			self.request("Create", {"description": "x", file_attach.KEY: [other.name]}, doctype_name="ToDo")

	def test_another_conversations_attachment_is_refused(self):
		run = make_test_run()
		self.addCleanup(delete_docs, "AI Run", [run.name])
		with self.assertRaises(PermissionDenied):
			self.engine.create_approval_request(run_name=run.name, action_type="Create", doctype_name="ToDo", action_summary="test",
				payload={"description": "x", file_attach.KEY: [self.file.name]})

	def test_other_actions_never_carry_files(self):
		payload = file_attach.bind("Submit", {"doctype": "ToDo", file_attach.KEY: [self.file.name]}, ADMIN, self.run.name)
		self.assertNotIn(file_attach.KEY, payload)
