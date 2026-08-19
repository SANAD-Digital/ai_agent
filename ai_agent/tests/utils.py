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
