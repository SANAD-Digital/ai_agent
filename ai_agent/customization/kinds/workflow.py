"""Workflows: states, transitions and their activation (docs/customization/01 §3.1 "Workflow", §4.2).

The check never saves a Workflow: ``Workflow.on_update`` creates the ``workflow_state`` Custom Field (DDL commits,
so a savepoint cannot undo it) and writes the first state into existing documents. Instead it replays Frappe's own
rules in Python:

- ``frappe/workflow/doctype/workflow/workflow.py``: ``validate_docstatus`` (a state must exist, never 2 -> anything,
  never 1 -> 0, never 0 -> 2), ``set_active`` (one active workflow per DocType: activating one deactivates the
  others), ``create_custom_field_for_workflow_state`` and ``update_default_workflow_status`` (on save).
- ``frappe/model/workflow.py``: new documents start in ``states[0]`` (``validate_workflow``), conditions run through
  ``frappe.safe_eval`` with ``get_workflow_safe_globals`` and ``doc.as_dict()`` (``is_transition_condition_satisfied``),
  ``update_value`` with ``evaluate_as_expression`` the same way (``evaluate_workflow_value``), a document's Cancel
  sets the first state with doc_status 2 (``set_workflow_state_on_action``).
- ``workflow.js update_field_options``: ``update_field`` is a field of the DocType that holds a value.

Transition tasks (Webhook / Server Script / app methods) run code and are not offered; a workflow that has them is
left to a person.
"""

from __future__ import annotations

import re
from collections import deque

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOOL = "customize.request_workflow"
OPERATIONS = ("create", "update", "activate", "deactivate")
INTERNAL_OPERATIONS = ("delete",)  # only as the undo of a create
TOP_KEYS = ("operation", "workflow_name", "doctype", "activate", "states", "transitions", "send_email_alert",
	"override_status", "sample_document", "purpose", "reason")
STATE_KEYS = ("state", "doc_status", "allow_edit", "update_field", "update_value", "evaluate_as_expression",
	"is_optional_state", "message", "style")
TRANSITION_KEYS = ("state", "action", "next_state", "allowed", "allow_self_approval", "condition",
	"send_email_to_creator")
SNAP_STATE_KEYS = STATE_KEYS[:-1]  # style belongs to the Workflow State record, not the row
SNAP_TRANSITION_KEYS = TRANSITION_KEYS + ("transition_tasks",)
STATE_BITS = ("evaluate_as_expression", "is_optional_state")
TRANSITION_BITS = ("allow_self_approval", "send_email_to_creator")
STYLES = ("", "Primary", "Info", "Success", "Warning", "Danger", "Inverse")
DOC_STATUS = ("0", "1", "2")
DOC_STATUS_LABEL = {"0": "Draft", "1": "Submitted", "2": "Cancelled"}
MAX_STATES = 30
MAX_TRANSITIONS = 60
MAX_PATHS = 20
NAME_RE = re.compile(r"^[^<>\"`\\\n\r\t]{1,140}$")
STATE_FIELD = "workflow_state"


def _bit(value, default=0):
	return default if value in (None, "") else (1 if cint(value) else 0)


def _clean_name(value, where):
	text = re.sub(r"\s+", " ", cstr(value)).strip()
	if not text or not NAME_RE.match(text):
		raise CustomizationError(_("{0} must be 1-140 characters without quotes or angle brackets").format(where))
	return text


def _state_row(row, i):
	if not isinstance(row, dict):
		raise CustomizationError(_("states[{0}] must be an object").format(i))
	_base.reject_unknown(row, STATE_KEYS, f"states[{i}]")
	_base.require(row, ("state", "allow_edit"), f"states[{i}]")
	doc_status = cstr(row.get("doc_status", "0") or "0")
	if doc_status not in DOC_STATUS:
		raise CustomizationError(_("states[{0}].doc_status must be 0 (draft), 1 (submitted) or 2 (cancelled)").format(i))
	style = cstr(row.get("style") or "")
	if style not in STYLES:
		raise CustomizationError(_("states[{0}].style must be one of {1}").format(i, ", ".join(s for s in STYLES if s)))
	out = {
		"state": _clean_name(row["state"], f"states[{i}].state"),
		"doc_status": doc_status,
		"allow_edit": cstr(row["allow_edit"]).strip(),
		"update_field": cstr(row.get("update_field") or "").strip(),
		"update_value": cstr(row.get("update_value") or ""),
		"evaluate_as_expression": _bit(row.get("evaluate_as_expression")),
		"is_optional_state": _bit(row.get("is_optional_state")),
		"message": cstr(row.get("message") or ""),
	}
	if out["update_value"] and not out["update_field"]:
		raise CustomizationError(_("states[{0}].update_value needs update_field").format(i))
	if style:
		out["style"] = style
	return out


def _transition_row(row, i):
	if not isinstance(row, dict):
		raise CustomizationError(_("transitions[{0}] must be an object").format(i))
	_base.reject_unknown(row, TRANSITION_KEYS, f"transitions[{i}]")
	_base.require(row, ("state", "action", "next_state", "allowed"), f"transitions[{i}]")
	return {
		"state": _clean_name(row["state"], f"transitions[{i}].state"),
		"action": _clean_name(row["action"], f"transitions[{i}].action"),
		"next_state": _clean_name(row["next_state"], f"transitions[{i}].next_state"),
		"allowed": cstr(row["allowed"]).strip(),
		"allow_self_approval": _bit(row.get("allow_self_approval"), 1),  # Frappe's default is 1
		"condition": cstr(row.get("condition") or "").strip(),
		"send_email_to_creator": _bit(row.get("send_email_to_creator")),
	}


def _row_snapshot(row, keys, bits):
	return {k: (cint(row.get(k)) if k in bits else cstr(row.get(k) or "")) for k in keys}


def _role_holders(role):
	"""Enabled users (not Administrator/Guest) who hold ``role``."""
	if role in ("All", "Desk User"):
		return cint(frappe.db.count("User", {"enabled": 1, "name": ["not in", ("Administrator", "Guest")]}))
	rows = frappe.db.sql(
		"""select count(distinct hr.parent) from `tabHas Role` hr join `tabUser` u on u.name = hr.parent
		where hr.parenttype = 'User' and hr.role = %s and u.enabled = 1 and u.name not in ('Administrator', 'Guest')""",
		(role,),
	)
	return cint(rows[0][0]) if rows else 0


def _role_perms(doctype, role):
	"""The role's permlevel-0 rights on ``doctype``: Custom DocPerm when the DocType has any, else DocPerm
	(the same precedence as ``frappe.permissions.get_all_perms``)."""
	source = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
	rows = frappe.get_all(source, filters={"parent": doctype, "role": role, "permlevel": 0},
		fields=["read", "write", "submit", "cancel"])
	return {k: any(cint(r.get(k)) for r in rows) for k in ("read", "write", "submit", "cancel")}


def _active_workflow(doctype):
	return frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name") if doctype else None


class WorkflowKind:
	name = "workflow"
	setting = "allow_customize_workflows"
	required_roles = ("System Manager",)

	# -- normalize --------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, TOOL)
		_base.require(args, ("operation", "workflow_name"), TOOL)
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		name = _clean_name(args["workflow_name"], "workflow_name")
		spec = {"operation": operation, "workflow_name": name, "name": name, "requested_by": user}
		purpose = args.get("purpose") or args.get("reason")
		if purpose:
			spec["purpose"] = re.sub(r"\s+", " ", cstr(purpose)).strip()[:300]
		exists = frappe.db.exists("Workflow", name)

		if operation == "create":
			_base.require(args, ("doctype", "states", "transitions"), f"{TOOL} (create)")
			if exists:
				raise CustomizationError(_("A workflow named {0} already exists").format(name),
					hints=[_("Use operation update to change it, or pick another workflow_name")])
			spec["doctype"] = cstr(args["doctype"]).strip()
			spec["activate"] = _bit(args.get("activate"), 1)
		else:
			if not exists:
				raise CustomizationError(_("There is no workflow {0}").format(name),
					hints=[_("List the workflows of a DocType with customize__inspect (include: workflows)")])
			current_doctype = frappe.db.get_value("Workflow", name, "document_type")
			if args.get("doctype") and cstr(args["doctype"]).strip() != current_doctype:
				raise CustomizationError(_("{0} belongs to {1}; a workflow cannot move to another DocType").format(name, current_doctype),
					hints=[_("Create a new workflow for the other DocType")])
			spec["doctype"] = current_doctype
			if operation in ("activate", "deactivate"):
				extra = sorted(k for k in ("states", "transitions", "activate", "send_email_alert", "override_status") if args.get(k) not in (None, ""))
				if extra:
					raise CustomizationError(_("{0} takes only workflow_name; got {1}").format(operation, ", ".join(extra)),
						hints=[_("Change states or transitions with operation update")])
				return spec
			if args.get("activate") not in (None, ""):
				raise CustomizationError(_("update does not change activation"), hints=[_("Use operation activate or deactivate")])

		if args.get("states") not in (None, ""):
			states = args["states"]
			if not isinstance(states, list) or not states:
				raise CustomizationError(_("states must be a non-empty list"))
			if len(states) > MAX_STATES:
				raise CustomizationError(_("At most {0} states").format(MAX_STATES))
			spec["states"] = [_state_row(row, i) for i, row in enumerate(states, 1)]
		if args.get("transitions") not in (None, ""):
			transitions = args["transitions"]
			if not isinstance(transitions, list) or not transitions:
				raise CustomizationError(_("transitions must be a non-empty list"))
			if len(transitions) > MAX_TRANSITIONS:
				raise CustomizationError(_("At most {0} transitions").format(MAX_TRANSITIONS))
			spec["transitions"] = [_transition_row(row, i) for i, row in enumerate(transitions, 1)]
		for key in ("send_email_alert", "override_status"):
			if args.get(key) not in (None, ""):
				spec[key] = _bit(args[key])
		if args.get("sample_document"):
			spec["sample_document"] = cstr(args["sample_document"]).strip()
		if operation == "update" and not any(k in spec for k in ("states", "transitions", "send_email_alert", "override_status")):
			raise CustomizationError(_("Nothing to change: send states, transitions, send_email_alert or override_status"))
		return spec

	# -- snapshot -----------------------------------------------------------------
	def snapshot(self, spec):
		name = spec.get("workflow_name") or spec.get("name")
		doctype = spec.get("doctype")
		out = {"workflow_name": name, "doctype": doctype, "active_workflow": _active_workflow(doctype) or ""}
		if not name or not frappe.db.exists("Workflow", name):
			out["exists"] = False
			return out
		doc = frappe.get_doc("Workflow", name)
		out.update({
			"exists": True,
			"document_type": doc.document_type,
			"is_active": cint(doc.is_active),
			"workflow_state_field": cstr(doc.workflow_state_field),
			"send_email_alert": cint(doc.send_email_alert),
			"override_status": cint(doc.override_status),
			"states": [_row_snapshot(s, SNAP_STATE_KEYS, STATE_BITS) for s in doc.states],
			"transitions": [_row_snapshot(t, SNAP_TRANSITION_KEYS, TRANSITION_BITS) for t in doc.transitions],
		})
		return out

	# -- effective workflow ---------------------------------------------------------
	def _effective(self, spec):
		current = self.snapshot(spec)
		states = spec.get("states")
		transitions = spec.get("transitions")
		if states is None:
			states = current.get("states") or []
		if transitions is None:
			transitions = current.get("transitions") or []
		return states, transitions, current

	# -- check --------------------------------------------------------------------
	def check(self, spec, user):
		operation = spec["operation"]
		doctype = spec.get("doctype")
		checks = _base.role_checks(user, self.required_roles, _("Frappe lets only System Managers change workflows"))
		reason = _base.customizable_reason(doctype, allow_custom=True)
		if not reason and frappe.get_meta(doctype).istable:
			reason = _("{0} is a child table; workflows run on the parent document").format(doctype)
		if reason:
			checks.append(Check("workflow.doctype", "critical", reason))
			return _base.build_report(checks, risk="high")
		if operation == "create" and frappe.db.exists("Workflow", spec["workflow_name"]):
			checks.append(Check("workflow.name_taken", "critical", _("A workflow named {0} already exists; use operation update").format(spec["workflow_name"])))
		if operation != "create" and not frappe.db.exists("Workflow", spec["workflow_name"]):
			checks.append(Check("workflow.missing", "critical", _("There is no workflow {0}").format(spec["workflow_name"])))
			return _base.build_report(checks, risk="medium")

		states, transitions, current = self._effective(spec)
		meta = frappe.get_meta(doctype)
		state_field = current.get("workflow_state_field") or STATE_FIELD
		active = _active_workflow(doctype)
		will_be_active = {"create": bool(spec.get("activate")), "activate": True, "deactivate": False,
			"delete": False}.get(operation, bool(current.get("is_active")))
		impact, preview = [], {"workflow_name": spec["workflow_name"], "doctype": doctype, "operation": operation}

		if operation == "delete":
			return self._check_delete(spec, checks, current, meta, state_field)

		if operation in ("create", "update"):
			if any(t.get("transition_tasks") for t in current.get("transitions") or []) and "transitions" in spec:
				checks.append(Check("workflow.transition_tasks", "critical",
					_("{0} has transition tasks (code that runs on a transition); change its transitions by hand").format(spec["workflow_name"])))
			sample, sample_note = self._sample(spec, doctype, user)
			if sample_note:
				checks.append(sample_note)
			checks += self._check_states(states, meta, doctype, state_field, sample)
			checks += self._check_transitions(states, transitions, meta, doctype, sample)
			checks += self._check_graph(states, transitions, meta)
			checks += self._check_policy(states, transitions)
			missing_states = sorted({s["state"] for s in states} - set(frappe.get_all("Workflow State", pluck="name")))
			missing_actions = sorted({t["action"] for t in transitions} - set(frappe.get_all("Workflow Action Master", pluck="name")))
			if missing_states:
				impact.append(_("Creates the Workflow State record(s): {0}").format(", ".join(missing_states)))
			if missing_actions:
				impact.append(_("Creates the Workflow Action record(s): {0}").format(", ".join(missing_actions)))
			preview.update(self._preview(states, transitions))
			preview["sample_document"] = sample.name if sample and not sample.is_new() else ""
		else:
			preview.update(self._preview(states, transitions))
			if operation == "activate" and not states:
				checks.append(Check("workflow.states", "critical", _("{0} has no states").format(spec["workflow_name"])))
			if operation == "activate":
				checks += self._check_graph(states, transitions, meta)
				checks += self._check_policy(states, transitions)

		# activation
		if will_be_active:
			if active and active != spec["workflow_name"]:
				impact.append(_("Deactivates the active workflow {0} on {1} (one active workflow per DocType)").format(active, doctype))
			if not meta.get_field(state_field):
				impact.append(_("Frappe adds a hidden field {0} (Link to Workflow State) to {1}").format(state_field, doctype))
			impact += self._document_impact(doctype, states, state_field, meta)
			if meta.is_submittable:
				impact.append(_("Users submit and cancel {0} only through the workflow actions").format(doctype))
		elif operation == "deactivate":
			impact.append(_("{0} documents no longer follow a workflow; their {1} value stays").format(doctype, state_field))
		elif operation == "create":
			impact.append(_("The workflow is saved inactive: nothing changes until it is activated"))
		if will_be_active and current.get("is_active") and operation == "update":
			impact.append(_("Applies at once to every {0} document").format(doctype))
		if operation == "activate" and current.get("is_active"):
			checks.append(Check("workflow.already_active", "info", _("{0} is already active").format(spec["workflow_name"])))
		if operation == "deactivate" and not current.get("is_active"):
			checks.append(Check("workflow.already_inactive", "info", _("{0} is already inactive").format(spec["workflow_name"])))

		if not any(c.level == "critical" for c in checks):
			checks.insert(0, Check("workflow.valid", "ok", _("Frappe's workflow rules accept every state and transition")))
		risk = "medium" if will_be_active or operation == "deactivate" else "low"
		return _base.build_report(checks, impact=impact, preview=preview, risk=risk)

	def _sample(self, spec, doctype, user):
		name = spec.get("sample_document")
		if name:
			if not frappe.db.exists(doctype, name) or not frappe.has_permission(doctype, "read", doc=name, user=user):
				return None, Check("workflow.sample", "critical", _("{0} {1} does not exist or {2} cannot read it").format(doctype, name, user))
			return frappe.get_doc(doctype, name), None
		rows = frappe.get_all(doctype, order_by="modified desc", limit=1, pluck="name")
		if rows:
			return frappe.get_doc(doctype, rows[0]), Check("workflow.sample", "info",
				_("Conditions were evaluated on {0} {1}").format(doctype, rows[0]))
		return frappe.new_doc(doctype), Check("workflow.sample", "info",
			_("{0} has no documents: conditions were evaluated on a new empty document").format(doctype))

	def _check_role(self, role, where, doctype, needs):
		out = []
		if not role or not frappe.db.exists("Role", role):
			return [Check("workflow.role", "critical", _("{0}: there is no role {1}").format(where, role))]
		if role == "Guest":
			return [Check("workflow.role", "critical", _("{0}: Guest cannot be given workflow rights").format(where))]
		if not _role_holders(role):
			out.append(Check("workflow.role_unheld", "warning", _("{0}: no enabled user holds the role {1}").format(where, role)))
		perms = _role_perms(doctype, role)
		missing = [p for p in needs if not perms.get(p)]
		if missing and role not in ("System Manager", "Administrator"):
			out.append(Check("workflow.role_permission", "warning",
				_("{0}: the role {1} has no {2} permission on {3}, so its users cannot apply it").format(where, role, "/".join(missing), doctype)))
		return out

	def _check_states(self, states, meta, doctype, state_field, sample):
		from frappe.model import no_value_fields
		from frappe.model.workflow import get_workflow_safe_globals

		out = []
		names = [s["state"] for s in states]
		dupes = sorted({n for n in names if names.count(n) > 1})
		if dupes:
			out.append(Check("workflow.state_duplicate", "critical", _("States listed more than once: {0}").format(", ".join(dupes))))
		if states and states[0]["doc_status"] != "0":
			out.append(Check("workflow.first_state", "critical",
				_("New documents start in the first state ({0}); it must have doc_status 0").format(states[0]["state"])))
		submittable = cint(meta.is_submittable)
		for s in states:
			where = _("State {0}").format(s["state"])
			if s["doc_status"] != "0" and not submittable:
				out.append(Check("workflow.doc_status", "critical",
					_("{0}: doc_status {1} needs a submittable DocType; {2} is not").format(where, s["doc_status"], doctype)))
			out += self._check_role(s["allow_edit"], where, doctype, ("write",) if s["doc_status"] == "0" else ())
			field = s.get("update_field")
			if field:
				df = meta.get_field(field)
				if field in ("name", "docstatus", state_field):
					out.append(Check("workflow.update_field", "critical", _("{0}: update_field cannot be {1}").format(where, field)))
				elif not df:
					out.append(Check("workflow.update_field", "critical", _("{0}: {1} has no field {2}").format(where, doctype, field)))
				elif df.fieldtype in no_value_fields:
					out.append(Check("workflow.update_field", "critical", _("{0}: {1} is a {2} and holds no value").format(where, field, df.fieldtype)))
				elif s["doc_status"] == "1" and not cint(df.allow_on_submit):
					out.append(Check("workflow.update_field", "warning",
						_("{0}: {1} is not 'Allow on Submit'; Frappe refuses to change it on a submitted document").format(where, field)))
				if s.get("evaluate_as_expression") and s.get("update_value") and sample is not None:
					try:
						frappe.safe_eval(s["update_value"], get_workflow_safe_globals(), dict(doc=sample.as_dict()))
					except Exception as e:
						out.append(Check("workflow.update_value", "critical", _("{0}: update_value fails: {1}").format(where, cstr(e)[:200])))
		return out

	def _check_transitions(self, states, transitions, meta, doctype, sample):
		from frappe.model.workflow import get_workflow_safe_globals

		out = []
		by_name = {s["state"]: s for s in states}
		seen = set()
		for i, t in enumerate(transitions, 1):
			where = _("Transition {0} ({1} -> {2})").format(i, t["state"], t["next_state"])
			# frappe/workflow/doctype/workflow/workflow.py validate_docstatus
			state, next_state = by_name.get(t["state"]), by_name.get(t["next_state"])
			for label, row in ((t["state"], state), (t["next_state"], next_state)):
				if row is None:
					out.append(Check("workflow.transition_state", "critical", _("{0}: {1} is not one of the states").format(where, label)))
			if state and next_state:
				a, b = state["doc_status"], next_state["doc_status"]
				if a == "2":
					out.append(Check("workflow.docstatus", "critical", _("{0}: cannot change the state of a cancelled document").format(where)))
				elif a == "1" and b == "0":
					out.append(Check("workflow.docstatus", "critical", _("{0}: a submitted document cannot go back to draft").format(where)))
				elif a == "0" and b == "2":
					out.append(Check("workflow.docstatus", "critical", _("{0}: cannot cancel before submitting").format(where)))
			needs = ()
			if state and next_state:
				needs = {("0", "0"): ("write",), ("0", "1"): ("submit",), ("1", "1"): ("write",), ("1", "2"): ("cancel",)}.get(
					(state["doc_status"], next_state["doc_status"]), ())
			out += self._check_role(t["allowed"], where, doctype, needs)
			key = (t["state"], t["action"], t["allowed"])
			if key in seen:
				out.append(Check("workflow.transition_duplicate", "warning",
					_("{0}: the action {1} from {2} for {3} is listed twice; Frappe uses the last one").format(where, t["action"], t["state"], t["allowed"])))
			seen.add(key)
			if t.get("condition"):
				try:
					compile(t["condition"], "<condition>", "eval")
				except SyntaxError as e:
					out.append(Check("workflow.condition", "critical", _("{0}: the condition is not a Python expression: {1}").format(where, e.msg)))
					continue
				if sample is not None:
					try:
						frappe.safe_eval(t["condition"], get_workflow_safe_globals(), dict(doc=sample.as_dict()))
					except Exception as e:
						out.append(Check("workflow.condition", "critical", _("{0}: the condition fails: {1}").format(where, frappe.utils.strip_html(cstr(e))[:200])))
		return out

	def _check_graph(self, states, transitions, meta):
		out = []
		if not states:
			return out
		names = [s["state"] for s in states]
		by_name = {s["state"]: s for s in states}
		edges = {}
		for t in transitions:
			if t["state"] in by_name and t["next_state"] in by_name:
				edges.setdefault(t["state"], []).append(t["next_state"])
		reached = {names[0]}
		queue = deque([names[0]])
		while queue:
			for nxt in edges.get(queue.popleft(), []):
				if nxt not in reached:
					reached.add(nxt)
					queue.append(nxt)
		# a document's own Cancel button moves it to the first cancelled state (model/workflow.py set_workflow_state_on_action)
		if any(by_name[n]["doc_status"] == "1" for n in reached):
			reached |= {n for n in names if by_name[n]["doc_status"] == "2"}
		unreachable = [n for n in names if n not in reached]
		if unreachable:
			out.append(Check("workflow.unreachable", "critical",
				_("No transition leads from {0} to: {1}").format(names[0], ", ".join(unreachable))))
		for n in names:
			if edges.get(n):
				continue
			status = by_name[n]["doc_status"]
			if status == "0":
				out.append(Check("workflow.dead_end", "warning",
					_("{0} is a draft state with no way out: documents there can never move on (fine for a final Rejected state)").format(n)))
			elif status == "1":
				out.append(Check("workflow.final", "info", _("{0} is a final state").format(n)))
		if cint(meta.is_submittable) and not any(by_name.get(t["next_state"], {}).get("doc_status") == "1" for t in transitions):
			out.append(Check("workflow.never_submits", "warning",
				_("No transition submits the document: with the workflow active, {0} can never be submitted").format(meta.name)))
		return out

	def _check_policy(self, states, transitions):
		"""docs/customization/01 §4.2: a transition that submits or cancels is refused while the agent may not
		submit/cancel (AI Settings allow_submit_cancel_actions = 0)."""
		from ai_agent.runtime import policy

		if policy.submit_cancel_allowed():
			return []
		by_name = {s["state"]: s for s in states}
		changing = [t for t in transitions if t["state"] in by_name and t["next_state"] in by_name
			and by_name[t["state"]]["doc_status"] != by_name[t["next_state"]]["doc_status"]]
		if not changing:
			return []
		names = ", ".join(f"{t['state']} -> {t['next_state']}" for t in changing[:5])
		return [Check("workflow.submit_cancel_policy", "critical",
			_("Transitions that submit or cancel ({0}) are turned off for the agent in AI Settings (Allow Submit/Cancel Actions)").format(names))]

	def _document_impact(self, doctype, states, state_field, meta):
		"""Frappe's update_default_workflow_status gives every document with an empty state the first state of its
		docstatus; documents whose docstatus has no state keep an empty state."""
		out = []
		counts = {cstr(r[0]): cint(r[1]) for r in frappe.db.sql(f"select docstatus, count(*) from `tab{doctype}` group by docstatus")}
		with_state = {s["doc_status"] for s in states}
		first = {}
		for s in states:
			first.setdefault(s["doc_status"], s["state"])
		has_column = state_field in frappe.db.get_table_columns(doctype)
		for status in sorted(counts):
			total = counts[status]
			if not total:
				continue
			label = _(DOC_STATUS_LABEL.get(status, status))
			if status not in with_state:
				out.append(_("{0} {1} document(s) ({2}) match no state: they show no workflow state").format(total, doctype, label))
				continue
			empty = total
			if has_column:
				empty = cint(frappe.db.sql(
					f"select count(*) from `tab{doctype}` where docstatus = %s and ifnull(`{state_field}`, '') = ''", (cint(status),))[0][0])
			if empty:
				out.append(_("{0} {1} document(s) ({2}) without a state are set to {3}").format(empty, doctype, label, first[status]))
		return out

	def _check_delete(self, spec, checks, current, meta, state_field):
		impact = [_("Deletes the workflow {0}").format(spec["workflow_name"])]
		if spec.get("reactivate"):
			if frappe.db.exists("Workflow", spec["reactivate"]):
				impact.append(_("Reactivates the workflow {0}").format(spec["reactivate"]))
			else:
				checks.append(Check("workflow.reactivate", "warning", _("The previous workflow {0} no longer exists").format(spec["reactivate"])))
		removable = [s for s in spec.get("remove_states") or [] if frappe.db.exists("Workflow State", s)]
		if removable:
			impact.append(_("Removes the Workflow State record(s) it created when nothing else uses them: {0}").format(", ".join(removable)))
		if spec.get("remove_actions"):
			impact.append(_("Removes the Workflow Action record(s) it created when nothing else uses them: {0}").format(", ".join(spec["remove_actions"])))
		if meta.get_field(state_field):
			impact.append(_("The {0} field and the values in existing documents stay").format(state_field))
		return _base.build_report(checks, impact=impact, preview={"workflow_name": spec["workflow_name"], "operation": "delete",
			**self._preview(current.get("states") or [], current.get("transitions") or [])}, risk="medium")

	def _preview(self, states, transitions):
		by_name = {s["state"]: s for s in states}
		state_rows = [{"state": s["state"], "doc_status": s["doc_status"], "doc_status_label": DOC_STATUS_LABEL.get(s["doc_status"], ""),
			"allow_edit": s["allow_edit"],
			"update": f"{s['update_field']} = {s['update_value']}" if s.get("update_field") else "",
			"optional": cint(s.get("is_optional_state"))} for s in states]
		transition_rows = [{"from": t["state"], "action": t["action"], "to": t["next_state"], "role": t["allowed"],
			"self_approval": cint(t.get("allow_self_approval")), "condition": t.get("condition") or ""} for t in transitions]
		return {
			"states": {"columns": ["state", "doc_status", "allow_edit", "update", "optional"], "rows": state_rows},
			"transitions": {"columns": ["from", "action", "to", "role", "self_approval", "condition"], "rows": transition_rows},
			"paths": self._paths(states, transitions, by_name),
		}

	def _paths(self, states, transitions, by_name):
		"""Plain-text paths from the first state to every state with no way out (or back to a visited one)."""
		if not states:
			return []
		edges = {}
		for t in transitions:
			edges.setdefault(t["state"], []).append(t)
		paths = []

		def walk(state, text, seen):
			if len(paths) >= MAX_PATHS:
				return
			out = [t for t in edges.get(state, []) if t["next_state"] in by_name]
			if not out:
				paths.append(text)
				return
			for t in out:
				step = f"{text} -> [{t['action']}: {t['allowed']}] -> {t['next_state']}"
				if t["next_state"] in seen:
					paths.append(step + " (loop)")
				else:
					walk(t["next_state"], step, seen | {t["next_state"]})

		first = states[0]["state"]
		walk(first, first, {first})
		return paths

	# -- execute ----------------------------------------------------------------------
	def _ensure_masters(self, states, transitions):
		created_states, created_actions = [], []
		for s in states:
			if not frappe.db.exists("Workflow State", s["state"]):
				frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": s["state"], "style": s.get("style") or ""}).insert()
				created_states.append(s["state"])
		for t in transitions:
			if not frappe.db.exists("Workflow Action Master", t["action"]):
				frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": t["action"]}).insert()
				created_actions.append(t["action"])
		return created_states, created_actions

	def _set_rows(self, doc, states, transitions):
		if states is not None:
			doc.set("states", [])
			for s in states:
				doc.append("states", {k: s.get(k) for k in SNAP_STATE_KEYS})
		if transitions is not None:
			doc.set("transitions", [])
			for t in transitions:
				doc.append("transitions", {k: t.get(k) for k in TRANSITION_KEYS})

	def execute(self, spec, user, approval=None):
		operation = spec["operation"]
		name = spec["workflow_name"]
		doctype = spec["doctype"]
		before = self.snapshot(spec)
		previous_active = before.get("active_workflow") or None
		with _base.as_user(user):
			if not frappe.has_permission("Workflow", "write" if operation != "create" else "create"):
				raise frappe.PermissionError(_("{0} cannot change workflows").format(user))
			if operation == "create":
				created_states, created_actions = self._ensure_masters(spec["states"], spec["transitions"])
				doc = frappe.new_doc("Workflow")
				doc.update({"workflow_name": name, "document_type": doctype, "is_active": cint(spec.get("activate")),
					"send_email_alert": cint(spec.get("send_email_alert")), "override_status": cint(spec.get("override_status")),
					"workflow_state_field": STATE_FIELD})
				self._set_rows(doc, spec["states"], spec["transitions"])
				doc.insert()
				undo = {"operation": "delete", "workflow_name": doc.name, "name": doc.name, "doctype": doctype,
					"remove_states": created_states, "remove_actions": created_actions,
					"reactivate": previous_active if spec.get("activate") and previous_active != doc.name else None,
					"purpose": _("Undo: remove the workflow {0}").format(doc.name)}
				targets = [{"doctype": "Workflow", "name": doc.name}]
			elif operation == "update":
				created_states, created_actions = self._ensure_masters(spec.get("states") or [], spec.get("transitions") or [])
				doc = frappe.get_doc("Workflow", name)
				self._set_rows(doc, spec.get("states"), spec.get("transitions"))
				for key in ("send_email_alert", "override_status"):
					if key in spec:
						doc.set(key, spec[key])
				doc.save()
				undo = {"operation": "update", "workflow_name": name, "name": name, "doctype": doctype,
					"purpose": _("Undo: restore the workflow {0}").format(name)}
				if "states" in spec:
					undo["states"] = before["states"]
				if "transitions" in spec:
					undo["transitions"] = before["transitions"]
				for key in ("send_email_alert", "override_status"):
					if key in spec:
						undo[key] = before[key]
				if created_states or created_actions:
					undo["remove_states"], undo["remove_actions"] = created_states, created_actions
				targets = [{"doctype": "Workflow", "name": name}]
			elif operation in ("activate", "deactivate"):
				doc = frappe.get_doc("Workflow", name)
				doc.is_active = 1 if operation == "activate" else 0
				doc.save()  # Workflow.set_active deactivates every other workflow of the DocType
				if operation == "activate" and previous_active and previous_active != name:
					undo = {"operation": "activate", "workflow_name": previous_active, "name": previous_active, "doctype": doctype}
				elif operation == "activate" and not before.get("is_active"):
					undo = {"operation": "deactivate", "workflow_name": name, "name": name, "doctype": doctype}
				elif operation == "deactivate" and before.get("is_active"):
					undo = {"operation": "activate", "workflow_name": name, "name": name, "doctype": doctype}
				else:
					undo = {}
				targets = [{"doctype": "Workflow", "name": name}]
			else:  # delete: the undo of a create
				frappe.delete_doc("Workflow", name)
				kept = self._remove_masters(spec.get("remove_states") or [], spec.get("remove_actions") or [])
				if spec.get("reactivate") and frappe.db.exists("Workflow", spec["reactivate"]):
					other = frappe.get_doc("Workflow", spec["reactivate"])
					other.is_active = 1
					other.save()
				frappe.clear_cache(doctype=doctype)
				frappe.cache.hdel("workflow", doctype)
				return {"targets": [{"doctype": "Workflow", "name": name}], "undo": {}, "kept": kept}
			if operation == "update" and (spec.get("remove_states") or spec.get("remove_actions")):
				self._remove_masters(spec.get("remove_states") or [], spec.get("remove_actions") or [])
		frappe.clear_cache(doctype=doctype)
		frappe.cache.hdel("workflow", doctype)
		return {"targets": targets, "undo": undo}

	def _remove_masters(self, states, actions):
		"""Delete the Workflow State / Workflow Action records this kind created, unless something links to them
		(another workflow, or documents that carry the state)."""
		kept = []
		for doctype, names in (("Workflow State", states), ("Workflow Action Master", actions)):
			for record in names:
				if not frappe.db.exists(doctype, record):
					continue
				savepoint = "ai_workflow_master"
				frappe.db.savepoint(savepoint)
				try:
					frappe.delete_doc(doctype, record)
				except (frappe.LinkExistsError, frappe.ValidationError):
					frappe.db.rollback(save_point=savepoint)
					frappe.clear_last_message()
					kept.append(record)
		return kept

	# -- verify -----------------------------------------------------------------------
	def verify(self, spec, result):
		operation = spec["operation"]
		name = spec["workflow_name"]
		exists = frappe.db.exists("Workflow", name)
		if operation == "delete":
			if exists:
				return [Check("verify.deleted", "critical", _("{0} still exists").format(name))]
			checks = [Check("verify.workflow", "ok", _("{0} is removed").format(name))]
			if result.get("kept"):
				checks.append(Check("verify.kept", "info", _("Kept because something still uses them: {0}").format(", ".join(result["kept"]))))
			return checks
		if not exists:
			return [Check("verify.exists", "critical", _("Workflow {0} does not exist").format(name))]
		after = self.snapshot(spec)
		checks = []
		for key, snap_keys, bits in (("states", SNAP_STATE_KEYS, STATE_BITS), ("transitions", TRANSITION_KEYS, TRANSITION_BITS)):
			if spec.get(key) is not None:
				expected = [_row_snapshot(r, snap_keys, bits) for r in spec[key]]
				actual = [{k: r.get(k) for k in snap_keys} for r in after.get(key) or []]
				if expected != actual:
					checks.append(Check("verify.rows", "critical", _("The saved {0} differ from the approved ones").format(key)))
		expected_active = {"create": spec.get("activate"), "activate": 1, "deactivate": 0}.get(operation)
		if expected_active is not None and cint(after.get("is_active")) != cint(expected_active):
			checks.append(Check("verify.active", "critical", _("{0} is not {1}").format(name, _("active") if expected_active else _("inactive"))))
		if cint(after.get("is_active")):
			field = after.get("workflow_state_field") or STATE_FIELD
			if not frappe.get_meta(spec["doctype"], cached=False).get_field(field):
				checks.append(Check("verify.state_field", "critical", _("{0} has no {1} field").format(spec["doctype"], field)))
			others = frappe.get_all("Workflow", {"document_type": spec["doctype"], "is_active": 1, "name": ["!=", name]}, pluck="name")
			if others:
				checks.append(Check("verify.single_active", "critical", _("Other active workflows: {0}").format(", ".join(others))))
		if not checks:
			checks.append(Check("verify.workflow", "ok", _("{0} is saved as approved").format(name)))
		return checks

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "activate": _("Activate"), "deactivate": _("Deactivate"),
			"delete": _("Delete")}[spec["operation"]]
		return _("{0} workflow {1} on {2}").format(verb, spec["workflow_name"], spec.get("doctype") or "-")

	def get_source(self, name, user=None):
		doc = frappe.get_doc("Workflow", name)
		snap = self.snapshot({"workflow_name": doc.name, "doctype": doc.document_type})
		return {"kind": self.name, **snap}


KIND = WorkflowKind()
