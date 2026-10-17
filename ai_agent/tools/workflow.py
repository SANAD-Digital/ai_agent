"""Document workflow, by the user's own permissions.

workflow.get_transitions answers "where is this document and what may *I* do next":
the transitions are the ones Frappe itself would offer this user (role, condition,
self-approval), so the agent never proposes an action the user could not take.
"""

import frappe
from frappe.utils import cint


def workflow_of(doctype):
	from frappe.model.workflow import get_workflow_name

	name = get_workflow_name(doctype)
	return frappe.get_cached_doc("Workflow", name) if name else None


def available_transitions(doc, user, workflow=None):
	"""The transitions from the document's current state that ``user`` may apply:
	the same rules as Frappe's get_transitions and apply_workflow (role, condition,
	self-approval), evaluated for ``user`` rather than the session."""
	from frappe.model.workflow import has_approval_access, is_transition_condition_satisfied

	workflow = workflow or workflow_of(doc.doctype)
	if not workflow:
		return []
	current = doc.get(workflow.workflow_state_field or "workflow_state")
	roles = set(frappe.get_roles(user))
	out = []
	for t in workflow.transitions:
		if t.state != current or t.allowed not in roles:
			continue
		if not is_transition_condition_satisfied(t, doc) or not has_approval_access(user, doc, t):
			continue
		out.append(t)
	return out


def changes_docstatus(workflow, doc, next_state):
	states = {s.state: cint(s.doc_status) for s in workflow.states}
	return states.get(next_state, cint(doc.docstatus)) != cint(doc.docstatus)


def handle_get_transitions(arguments, permission_context):
	"""Handle workflow.get_transitions: current state and the actions open to this user."""
	from ai_agent.runtime import policy

	doctype = arguments.get("doctype")
	name = arguments.get("document_name")
	if not doctype or not name:
		return {"error": "doctype and document_name are required"}
	user = permission_context.user
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name) or not frappe.has_permission(doctype, "read", doc=name, user=user):
		return {"error": "Document not found or not accessible."}
	workflow = workflow_of(doctype)
	if not workflow:
		return {"doctype": doctype, "document_name": name, "workflow": None, "note": "This DocType has no active workflow; there are no workflow actions."}

	doc = frappe.get_doc(doctype, name)
	state_field = workflow.workflow_state_field or "workflow_state"
	submit_cancel = policy.submit_cancel_allowed()
	mine = available_transitions(doc, user, workflow)
	actions = []
	for t in mine:
		docstatus_change = changes_docstatus(workflow, doc, t.next_state)
		actions.append({
			"action": t.action,
			"next_state": t.next_state,
			"submits_or_cancels": docstatus_change,
			"agent_may_request": submit_cancel or not docstatus_change,
		})
	# what exists from this state but needs another role (or someone other than the creator)
	taken = {(t.action, t.next_state) for t in mine}
	others = [
		{"action": t.action, "next_state": t.next_state, "allowed_role": t.allowed}
		for t in workflow.transitions
		if t.state == doc.get(state_field) and (t.action, t.next_state) not in taken
	]
	note = (
		"Request one of `actions` with actions__request_workflow_action; the user approves it in the chat. "
		"An action with agent_may_request=false submits or cancels the document, which is turned off in AI Settings: the user does it from the document."
	)
	if not actions:
		note = "This user has no workflow action on this document in its current state."
	return {
		"doctype": doctype,
		"document_name": name,
		"workflow": workflow.name,
		"state": doc.get(state_field),
		"docstatus": cint(doc.docstatus),
		"actions": actions,
		"needs_another_user": others,
		"note": note,
	}
