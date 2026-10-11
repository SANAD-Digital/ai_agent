"""files.read reads a file attached to an ERP document when, and only when, the user
may read that document; another conversation's attachments stay closed."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tools.chat_files import handle_read

USER = "ai-files-test@example.com"


class TestDocumentFiles(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Files Test", "send_welcome_email": 0}).insert(ignore_permissions=True)
		frappe.get_doc("User", USER).remove_roles("System Manager", "Accounts User", "Accounts Manager")
		self.company = frappe.get_all("Company", pluck="name", limit=1)[0]
		self.file = frappe.get_doc({"doctype": "File", "file_name": "doc-files-test.txt", "content": "رقم الفاتورة 98765",
			"attached_to_doctype": "Company", "attached_to_name": self.company, "is_private": 1}).insert(ignore_permissions=True)
		self.chat_file = frappe.get_doc({"doctype": "File", "file_name": "other-chat.txt", "content": "secret",
			"attached_to_doctype": "AI Chat Message", "attached_to_name": "not-mine", "is_private": 1}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _read(self, user, **args):
		frappe.set_user(user)
		return handle_read(args, PermissionContext(user))

	def test_a_user_who_can_read_the_document_reads_its_file(self):
		out = self._read("Administrator", file=self.file.name)
		self.assertIn("98765", out.get("content", ""), out)

	def test_by_file_name_on_the_named_document(self):
		out = self._read("Administrator", file="doc-files-test.txt", doctype="Company", document_name=self.company)
		self.assertEqual(out.get("file"), self.file.name, out)

	def test_without_read_permission_on_the_document_it_is_refused(self):
		out = self._read(USER, file=self.file.name)
		self.assertIn("error", out)
		self.assertNotIn("content", out)

	def test_another_conversations_attachment_is_never_readable(self):
		out = self._read("Administrator", file=self.chat_file.name)
		self.assertIn("another conversation", out.get("error", ""))
