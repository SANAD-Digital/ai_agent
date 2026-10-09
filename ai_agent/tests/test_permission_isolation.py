"""Phase A security tests: two users restricted to two companies by User
Permissions must never reach each other's records through any agent tool,
write request or conversation endpoint."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.tests.utils import delete_docs, disable_users, make_test_run, quota_bypass
from ai_agent.tools import data

USER_A = "ai-iso-a@example.com"
USER_B = "ai-iso-b@example.com"
USER_SM = "ai-iso-sm@example.com"


TEST_USERS = (USER_A, USER_B, USER_SM)


def _disable_test_users():
	disable_users(TEST_USERS)


def _ensure_user(email, roles):
	with quota_bypass():
		return _create_user(email, roles)


def _create_user(email, roles):
	if frappe.db.get_value("User", email, "enabled") == 0:
		frappe.db.set_value("User", email, "enabled", 1, update_modified=False)
	if not frappe.db.exists("User", email):
		frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"enabled": 1,
		}).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.set("roles", [])
	user.add_roles(*roles)
	return email


def _restrict_to_company(user, company):
	frappe.db.delete("User Permission", {"user": user})
	frappe.get_doc({
		"doctype": "User Permission",
		"user": user,
		"allow": "Company",
		"for_value": company,
		"apply_to_all_doctypes": 1,
	}).insert(ignore_permissions=True)


def _make_project(company, member):
	project = frappe.get_doc({
		"doctype": "Project",
		"project_name": f"AI isolation {company} {frappe.generate_hash(length=6)}",
		"company": company,
		"users": [{"user": member, "welcome_email_sent": 1}],
	})
	project.insert(ignore_permissions=True)
	return project.name


def _listable_runs():
	"""AI Run names the session user can list via desk/REST (none if no role grants AI Run)."""
	try:
		return frappe.get_list("AI Run", pluck="name", limit_page_length=0)
	except frappe.PermissionError:
		return []


class TestPermissionIsolation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company_a, cls.company_b = frappe.get_all(
			"Company", pluck="name", order_by="creation asc", limit=2
		)
		_ensure_user(USER_A, ["Projects User", "Desk User"])
		_ensure_user(USER_B, ["Projects User", "Desk User"])
		_ensure_user(USER_SM, ["System Manager", "Projects User"])
		_restrict_to_company(USER_A, cls.company_a)
		_restrict_to_company(USER_B, cls.company_b)
		cls.project_a = _make_project(cls.company_a, USER_A)
		cls.project_b = _make_project(cls.company_b, USER_B)
		cls.run_a = make_test_run(user=USER_A).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"AI Approval Request",
			filters={"document_name": ["in", [cls.project_a, cls.project_b]]},
			pluck="name",
		):
			frappe.delete_doc("AI Approval Request", name, ignore_permissions=True, force=True)
		for name in (cls.project_a, cls.project_b):
			frappe.delete_doc("Project", name, ignore_permissions=True, force=True)
		delete_docs("AI Run", [cls.run_a])
		for user in TEST_USERS:
			frappe.db.delete("User Permission", {"user": user})
		frappe.db.set_single_value("AI Settings", "allow_admin_conversation_audit", 0)
		_disable_test_users()
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _as(self, user):
		frappe.set_user(user)
		return PermissionContext(user)

	# -- A1 data.get_document ------------------------------------------------

	def test_get_document_denies_other_company(self):
		for user, own, other in ((USER_A, self.project_a, self.project_b), (USER_B, self.project_b, self.project_a)):
			ctx = self._as(user)
			self.assertEqual(data.handle_get_document({"doctype": "Project", "name": own}, ctx)["name"], own)
			denied = data.handle_get_document({"doctype": "Project", "name": other}, ctx)
			self.assertEqual(denied, {"error": data.NOT_ACCESSIBLE_ERROR})

	def test_get_document_does_not_reveal_existence(self):
		ctx = self._as(USER_A)
		missing = data.handle_get_document({"doctype": "Project", "name": "NO-SUCH-PROJECT"}, ctx)
		forbidden = data.handle_get_document({"doctype": "Project", "name": self.project_b}, ctx)
		self.assertEqual(missing, forbidden)

	def _without_permlevel_1(self):
		"""Simulate a user who lacks permlevel 1 on Project (where `users` lives)."""
		from frappe.model.meta import Meta

		original = Meta.get_permlevel_access

		def restricted(meta, permission_type="read", parenttype=None, *, user=None):
			levels = original(meta, permission_type, parenttype, user=user)
			if (parenttype or meta.name) == "Project":
				return [level for level in levels if level != 1]
			return levels

		return patch.object(Meta, "get_permlevel_access", restricted)

	def test_get_document_child_rows_respect_permlevel(self):
		ctx = self._as(USER_A)
		with self._without_permlevel_1():
			result = data.handle_get_document({"doctype": "Project", "name": self.project_a}, ctx)
		self.assertEqual(result["name"], self.project_a)
		self.assertNotIn("users", result)
		self.assertIn("users", data.handle_get_document({"doctype": "Project", "name": self.project_a}, ctx))

	# -- A2 data.get_child_records ------------------------------------------

	def test_get_child_records_denies_other_company(self):
		for user, own, other in ((USER_A, self.project_a, self.project_b), (USER_B, self.project_b, self.project_a)):
			ctx = self._as(user)
			ok = data.handle_get_child_records(
				{"parent_doctype": "Project", "parent_name": own, "child_field": "users"}, ctx
			)
			self.assertEqual(ok["row_count"], 1)
			denied = data.handle_get_child_records(
				{"parent_doctype": "Project", "parent_name": other, "child_field": "users"}, ctx
			)
			self.assertEqual(denied, {"error": data.NOT_ACCESSIBLE_ERROR})

	def test_get_child_records_respects_parent_field_permlevel(self):
		ctx = self._as(USER_A)
		with self._without_permlevel_1():
			result = data.handle_get_child_records(
				{"parent_doctype": "Project", "parent_name": self.project_a, "child_field": "users"}, ctx
			)
		self.assertIn("error", result)
		self.assertNotIn("rows", result)

	def test_get_child_records_rejects_non_table_field(self):
		ctx = self._as(USER_A)
		result = data.handle_get_child_records(
			{"parent_doctype": "Project", "parent_name": self.project_a, "child_field": "company"}, ctx
		)
		self.assertIn("error", result)

	# -- A5 data.count -----------------------------------------------------

	def test_count_applies_user_permissions(self):
		both = [self.project_a, self.project_b]
		for user in (USER_A, USER_B):
			ctx = self._as(user)
			result = data.handle_count(
				{"doctype": "Project", "filters": [{"field": "name", "operator": "in", "value": both}]}, ctx
			)
			self.assertEqual(result, {"count": 1})

	# -- data.query (plain and grouped paths) ---------------------------------

	def test_query_plain_path_applies_user_permissions(self):
		both = [self.project_a, self.project_b]
		for user, own in ((USER_A, self.project_a), (USER_B, self.project_b)):
			ctx = self._as(user)
			result = data.handle_query({
				"doctype": "Project",
				"fields": ["name", "company"],
				"filters": [{"field": "name", "operator": "in", "value": both}],
				"limit": 10,
			}, ctx)
			self.assertNotIn("error", result, result)
			self.assertEqual([row["name"] for row in result["rows"]], [own])

	def test_query_grouped_path_applies_user_permissions(self):
		both = [self.project_a, self.project_b]
		for user, company in ((USER_A, self.company_a), (USER_B, self.company_b)):
			ctx = self._as(user)
			result = data.handle_query({
				"doctype": "Project",
				"fields": ["company", {"field": "name", "aggregate": "COUNT", "alias": "projects"}],
				"filters": [{"field": "name", "operator": "in", "value": both}],
				"group_by": ["company"],
				"limit": 10,
			}, ctx)
			self.assertNotIn("error", result, result)
			self.assertEqual(result["rows"], [{"company": company, "projects": 1}])

	def test_query_grouped_path_applies_user_permissions_to_system_manager(self):
		_restrict_to_company(USER_SM, self.company_a)
		try:
			ctx = self._as(USER_SM)
			result = data.handle_query({
				"doctype": "Project",
				"fields": ["company", {"field": "name", "aggregate": "COUNT", "alias": "projects"}],
				"filters": [{"field": "name", "operator": "in", "value": [self.project_a, self.project_b]}],
				"group_by": ["company"],
				"limit": 10,
			}, ctx)
			self.assertNotIn("error", result, result)
			self.assertEqual([row["company"] for row in result["rows"]], [self.company_a])
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("User Permission", {"user": USER_SM})
			frappe.clear_cache(user=USER_SM)

	# -- A4 write requests ----------------------------------------------------

	def _engine(self, user):
		return ApprovalEngine(self._as(user))

	def _request_update(self, user, name, payload):
		return self._engine(user).create_approval_request(
			run_name=self.run_a,
			action_type="Update",
			doctype_name="Project",
			document_name=name,
			action_summary="test",
			payload=payload,
		)

	def test_update_request_blocks_system_and_lifecycle_fields(self):
		for field, value in (
			("docstatus", 1), ("name", "X"), ("owner", USER_B), ("creation", "2020-01-01"),
			("modified", "2020-01-01"), ("modified_by", USER_B), ("amended_from", "X"), ("idx", 3),
		):
			with self.assertRaises(PermissionDenied, msg=field):
				self._request_update(USER_A, self.project_a, {field: value})

	def test_update_request_blocks_read_only_and_permlevel_fields(self):
		with self.assertRaises(PermissionDenied):
			self._request_update(USER_A, self.project_a, {"total_costing_amount": 1})
		with self._without_permlevel_1(), self.assertRaises(PermissionDenied):
			self._request_update(USER_A, self.project_a, {"copied_from": "X"})

	def test_update_request_denied_on_other_company_document(self):
		with self.assertRaises(PermissionDenied):
			self._request_update(USER_A, self.project_b, {"notes": "x"})

	def test_update_request_allows_plain_field_and_records_modified(self):
		approval = self._request_update(USER_A, self.project_a, {"notes": "hello"})
		self.assertEqual(approval.status, "Pending")
		self.assertTrue(approval.expected_modified)

	def _approved(self, requester, approver, name, payload, action_type="Update"):
		payload_json = json.dumps(payload, sort_keys=True, default=str)
		approval = frappe.get_doc({
			"doctype": "AI Approval Request",
			"run": self.run_a,
			"approval_id": frappe.generate_hash(length=10),
			"action_type": action_type,
			"doctype_name": "Project",
			"document_name": name,
			"action_summary": "legacy request",
			"payload_json": payload_json,
			"payload_hash": ApprovalEngine(None)._compute_hash(payload_json),
			"status": "Approved",
			"requested_by": requester,
			"approved_by": approver,
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=30),
		})
		approval.insert(ignore_permissions=True)
		return approval

	def test_execution_rechecks_payload_fields(self):
		# A request stored before this fix (or tampered in the DB with a
		# recomputed hash) must still be refused when executed.
		approval = self._approved(USER_A, USER_A, self.project_a, {"docstatus": 1})
		with self.assertRaises(PermissionDenied):
			self._engine(USER_A).execute_approved_action(approval.name)
		self.assertEqual(frappe.db.get_value("Project", self.project_a, "docstatus"), 0)

	def test_execution_rechecks_row_level_access_of_approver(self):
		approval = self._approved(USER_A, USER_B, self.project_a, {"notes": "from B"})
		with self.assertRaises(PermissionDenied):
			self._engine(USER_B).execute_approved_action(approval.name)
		self.assertNotEqual(frappe.db.get_value("Project", self.project_a, "notes"), "from B")

	def test_create_request_must_be_draft_of_requested_doctype(self):
		engine = self._engine(USER_A)
		base = {"doctype": "Project", "project_name": "x", "company": self.company_a}
		with self.assertRaises(PermissionDenied):
			engine.create_approval_request(
				run_name=self.run_a, action_type="Create", doctype_name="Project",
				action_summary="t", payload=dict(base, docstatus=1),
			)
		with self.assertRaises(PermissionDenied):
			engine.create_approval_request(
				run_name=self.run_a, action_type="Create", doctype_name="Project",
				action_summary="t", payload=dict(base, doctype="User"),
			)

	def test_submit_request_denied_on_other_company_document(self):
		with self.assertRaises(PermissionDenied):
			self._engine(USER_A).create_approval_request(
				run_name=self.run_a, action_type="Workflow", doctype_name="Project",
				document_name=self.project_b, action_summary="t",
				payload={"doctype": "Project", "name": self.project_b, "action": "Approve"},
			)

	# -- A7 endpoints --------------------------------------------------------


	def test_unrelated_user_cannot_reject_approval(self):
		approval = self._request_update(USER_A, self.project_a, {"notes": "to reject"})
		with self.assertRaises(PermissionDenied):
			self._engine(USER_B).reject(approval.name, user=USER_B)
		self.assertEqual(frappe.db.get_value("AI Approval Request", approval.name, "status"), "Pending")

	def test_report_rejects_another_users_prepared_report(self):
		from ai_agent.tools import reports

		frappe.set_user("Administrator")
		prepared = frappe.get_doc({"doctype": "Prepared Report", "report_name": "Project Summary"})
		prepared.name = frappe.generate_hash(length=10)
		prepared.owner = USER_B
		prepared.db_insert()
		try:
			ctx = self._as(USER_A)
			with patch("frappe.desk.query_report.run") as mocked:
				result = reports.handle_run(
					{"report_name": "Project Summary", "prepared_report_name": prepared.name}, ctx
				)
			mocked.assert_not_called()
			self.assertEqual(result, {"error": "Prepared report not found or not accessible."})
		finally:
			frappe.set_user("Administrator")
			frappe.db.delete("Prepared Report", {"name": prepared.name})

	def test_gemini_test_connection_requires_system_manager(self):
		from ai_agent.ai_agent.doctype.gemini_ocr_settings.gemini_ocr_settings import test_connection

		frappe.set_user(USER_A)
		with patch("ai_agent.runtime.gemini_ocr.test_gemini_ocr_connection") as mocked:
			with self.assertRaises(frappe.PermissionError):
				test_connection()
		mocked.assert_not_called()


class TestConversationOwnership(IntegrationTestCase):
	"""A6: conversations are private unless admin auditing is enabled (and logged)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_user(USER_A, ["Projects User", "Desk User"])
		_ensure_user(USER_B, ["Projects User", "Desk User"])
		_ensure_user(USER_SM, ["System Manager", "Projects User"])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_disable_test_users()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("AI Settings", "allow_admin_conversation_audit", 0)
		self.run = make_test_run(user=USER_A)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("AI Settings", "allow_admin_conversation_audit", 0)
		delete_docs("AI Run", [self.run.name])
		frappe.db.delete("Access Log", {"reference_document": self.run.name})
		frappe.db.commit()

	def test_run_executes_only_as_owner(self):
		from ai_agent.runtime import orchestrator

		frappe.set_user(USER_B)
		with patch.object(orchestrator, "Orchestrator") as mocked:
			orchestrator.execute_run(self.run.name)
		mocked.assert_not_called()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "status"), "Failed")


class TestBackgroundUserFallbacks(IntegrationTestCase):
	"""A3 / A8: no Administrator fallback, no privileged WhatsApp user."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.run = make_test_run()

	def tearDown(self):
		frappe.set_user("Administrator")
		delete_docs("AI Run", [self.run.name])
		_disable_test_users()
		frappe.db.commit()

	def test_whatsapp_support_user_must_be_low_privilege(self):
		from ai_agent.whatsapp_support import settings

		_ensure_user(USER_SM, ["System Manager", "Projects User"])
		_ensure_user(USER_A, ["Projects User", "Desk User"])
		self.assertIsNotNone(settings.support_user_problem("Administrator"))
		self.assertIsNotNone(settings.support_user_problem(USER_SM))
		self.assertIsNotNone(settings.support_user_problem(""))
		self.assertIsNone(settings.support_user_problem(USER_A))

	def test_whatsapp_settings_reject_privileged_user(self):
		_ensure_user(USER_SM, ["System Manager", "Projects User"])
		doc = frappe.get_single("AI WhatsApp Support Settings")
		doc.support_user = USER_SM
		with self.assertRaises(frappe.ValidationError):
			doc.validate()
		doc.support_user = "Administrator"
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_whatsapp_has_no_administrator_default(self):
		from ai_agent.whatsapp_support import settings

		with patch.object(settings, "_setting", return_value=""), patch.object(settings, "_conf", return_value=""):
			self.assertIsNone(settings.support_user())
