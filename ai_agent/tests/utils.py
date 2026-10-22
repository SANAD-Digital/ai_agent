import contextlib
from unittest.mock import patch

import frappe

TEST_AGENT = "Unit Test Agent"


def ensure_test_agent():
	"""Create (or fetch) a disabled-by-default agent used only by automated tests."""
	if frappe.db.exists("AI Agent", TEST_AGENT):
		return frappe.get_doc("AI Agent", TEST_AGENT)
	agent = frappe.get_doc({
		"doctype": "AI Agent",
		"agent_name": TEST_AGENT,
		"enabled": 1,
		"is_default": 0,
		"allow_actions": 1,
		"system_instructions": "Agent used by ai_agent automated tests.",
	})
	agent.insert(ignore_permissions=True)
	return agent


def make_test_run(prompt="ai_agent automated test run", user="Administrator"):
	agent = ensure_test_agent()
	run = frappe.get_doc({
		"doctype": "AI Run",
		"run_id": frappe.generate_hash(length=12),
		"agent": agent.name,
		"user": user,
		"prompt": prompt,
		"status": "Queued",
	})
	run.insert(ignore_permissions=True)
	return run


def delete_docs(doctype, names):
	"""Best-effort cleanup for records the code under test committed to the DB."""
	sessions = []
	if doctype == "AI Run":
		for name in names:
			session = frappe.db.get_value("AI Run", name, "chat_session")
			if session:
				sessions.append(session)
	if doctype == "AI Run" and names:
		# A run leaves learned memory behind; left in place it crowds real memory out of later runs' context.
		for memory in frappe.get_all("AI Memory", filters={"source_run": ["in", list(names)]}, pluck="name"):
			try:
				frappe.delete_doc("AI Memory", memory, ignore_permissions=True, force=True)
			except Exception:
				pass
	for name in names:
		try:
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		except Exception:
			pass
	# Runs auto-create their chat session; drop sessions left empty by cleanup.
	for session in set(sessions):
		try:
			if not frappe.db.count("AI Run", {"chat_session": session}):
				frappe.delete_doc("AI Chat Session", session, ignore_permissions=True, force=True)
		except Exception:
			pass


def quota_bypass():
	"""Test users must not be blocked by a site's subscription user quota (site_quota app)."""
	if "site_quota" in frappe.get_installed_apps():
		return patch("site_quota.quota.validate_users_limit")
	return contextlib.nullcontext()


def disable_users(users):
	"""Disable test users afterwards so they do not count toward the site's user quota."""
	for user in users:
		if frappe.db.exists("User", user):
			frappe.db.set_value("User", user, "enabled", 0, update_modified=False)


def user_states(users):
	"""Snapshot of each user's enabled flag (None when the user does not exist yet), for restore_users."""
	return {user: frappe.db.get_value("User", user, "enabled") for user in users}


def restore_users(states):
	"""Put users back the way user_states found them: a user that was enabled before the test
	(for example one that also serves a real MCP client through an API key) stays enabled,
	a user the test created or found disabled is disabled so it does not count toward the quota."""
	for user, enabled in states.items():
		if not frappe.db.exists("User", user):
			continue
		frappe.db.set_value("User", user, "enabled", 1 if enabled else 0, update_modified=False)


TEST_PROVIDER = "Unit Test Provider"


def ensure_test_provider():
	"""A site with no enabled AI Provider (a fresh CI site) gets one for the tests; the tests replace the model
	with a scripted one, so it is never called. A site that has a provider keeps its own."""
	if frappe.get_all("AI Provider", filters={"enabled": 1}, limit=1):
		return
	frappe.get_doc({"doctype": "AI Provider", "provider_name": TEST_PROVIDER, "provider_type": "Anthropic", "enabled": 1,
		"is_default": 1, "api_key": "test-key", "default_model": "claude-test",
		"models": [{"model_name": "claude-test", "model_type": "Chat", "supports_tools": 1, "is_default": 1}],
	}).insert(ignore_permissions=True)
	frappe.db.commit()


def cleanup_leftovers():
	"""hooks.before_tests: remove what earlier test runs left behind (runs, conversations and finished approvals
	of the test agent and the @example.com test users), so a development site does not fill up with them.
	A site without an AI Provider gets the test one (ensure_test_provider)."""
	ensure_test_provider()
	db = frappe.db
	sessions = db.sql_list("select name from `tabAI Chat Session` where agent='Unit Test Agent' or user like '%%@example.com'")
	if sessions:
		messages = db.sql_list("select name from `tabAI Chat Message` where session in %s", (tuple(sessions),))
		if messages:
			db.sql("delete from `tabAI Message Block` where message in %s", (tuple(messages),))
			db.sql("delete from `tabAI Chat Message` where name in %s", (tuple(messages),))
		db.sql("delete from `tabAI Chat Session` where name in %s", (tuple(sessions),))
	runs = db.sql_list("select name from `tabAI Run` where agent='Unit Test Agent' or user like '%%@example.com'")
	if runs:
		db.sql("delete from `tabAI Tool Call` where run in %s", (tuple(runs),))
		db.sql("delete from `tabAI Run` where name in %s", (tuple(runs),))
	db.sql("delete from `tabAI Approval Request` where requested_by like '%%@example.com' and status != 'Pending'")
	db.commit()
