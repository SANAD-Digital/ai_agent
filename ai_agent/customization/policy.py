"""Who may customize, from where, how often (docs/customization/01 §3.6).

- Master switch ``allow_customization_actions`` plus one switch per kind, all off by default.
- Frappe's own roles (D3): System Manager, plus Script Manager for server-side code. Administrator passes.
  Checked for the requester at request time and for the approver at execution (who executes it).
- Surfaces: Desk chat when enabled; MCP only with ``customization_allow_mcp``; never the support bubble
  (Chatbot), WhatsApp, the API or an unknown surface.
- Never executed without a person: ``Customize`` is in ``runtime.policy.ALWAYS_HUMAN_TYPES``.
- Limits: ``customization_max_requests_per_hour`` (20), 5 pending per user, one pending per target,
  20k characters of code, 10 fields, 40 properties; a Customize approval lives at most 60 minutes.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from ai_agent.customization import registry
from ai_agent.customization.contract import required_roles_for
from ai_agent.runtime.exceptions import PermissionDenied

ACTION_TYPE = "Customize"
MASTER_SETTING = "allow_customization_actions"
MCP_SETTING = "customization_allow_mcp"
FOUR_EYES_SETTING = "customization_require_four_eyes"
RATE_SETTING = "customization_max_requests_per_hour"
DEFAULT_MAX_PER_HOUR = 20
MAX_PENDING_PER_USER = 5
MAX_TTL_MINUTES = 60
MAX_CODE_CHARS = 20_000
MAX_FIELDS = 10
MAX_PROPERTIES = 40
DESK = "Desk"
MCP = "MCP"
CODE_ARGS = ("script", "query", "report_script", "javascript", "html", "css", "message", "client_script")
# a new DocType or a web form brings more fields than a custom-field request: the kind sets its own cap
MAX_FIELDS_BY_KIND = {"doctype": 60}


def _settings():
	return frappe.get_cached_doc("AI Settings")


def _flag(field):
	return bool(cint(_settings().get(field)))


def enabled():
	return _flag(MASTER_SETTING)


def kind_enabled(kind_name):
	setting = registry.KIND_SETTINGS.get(kind_name)
	return enabled() and bool(setting) and _flag(setting)


def any_kind_enabled():
	return enabled() and any(_flag(s) for s in set(registry.KIND_SETTINGS.values()))


def four_eyes(kind_name=None, spec=None):
	"""A second approver is needed: the site's setting says so, or the kind always needs one
	(``four_eyes_required`` on the kind, or ``registry.ALWAYS_FOUR_EYES``: role permissions), or this
	request does (``four_eyes_for(spec)`` on the kind: a Permission Query server script)."""
	if _flag(FOUR_EYES_SETTING):
		return True
	if not kind_name:
		return False
	if kind_name in registry.ALWAYS_FOUR_EYES:
		return True
	try:
		kind = registry.get(kind_name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"ai_agent customize: four-eyes check could not load {kind_name}")
		return True  # fails closed: a kind that cannot be loaded needs a second person
	if getattr(kind, "four_eyes_required", False):
		return True
	getter = getattr(kind, "four_eyes_for", None)
	return bool(callable(getter) and spec is not None and getter(spec))


def surface_allowed(surface):
	if surface == DESK:
		return True
	if surface == MCP:
		return _flag(MCP_SETTING)
	return False  # Chatbot (support bubble), WhatsApp, API, unknown: never


def _roles(user):
	return set(frappe.get_roles(user))


def has_roles(user, roles):
	if user == "Administrator":
		return True
	if not user or user == "Guest":
		return False
	return set(roles or ()).issubset(_roles(user))


def kind_roles(kind_name, kind=None, spec=None):
	floor = tuple(registry.KIND_ROLES.get(kind_name, ("System Manager",)))
	own = required_roles_for(kind, spec) if kind is not None else ()
	return tuple(dict.fromkeys(floor + tuple(own)))


def available(user, surface):
	"""The customization tool group is offered to ``user`` on ``surface`` (the agent's own allow_actions /
	read_only are checked by the caller)."""
	return any_kind_enabled() and surface_allowed(surface) and has_roles(user, ("System Manager",))


def kind_available(kind_name, user, surface):
	return available(user, surface) and kind_enabled(kind_name) and has_roles(user, kind_roles(kind_name))


def tool_available(tool_name, user, surface):
	"""One customize.* tool: read tools need the group; a request tool also needs its kind enabled."""
	if not available(user, surface):
		return False
	kind_name = registry.REQUEST_TOOLS.get(tool_name)
	return kind_available(kind_name, user, surface) if kind_name else True


# -- the gate --------------------------------------------------------------------


def assert_allowed(kind_name, spec, user, phase, *, kind=None, surface=None):
	"""Raise PermissionDenied unless ``user`` may do this now.

	phase "request": the requester, on ``surface`` (settings, roles, surface, rate, pending limits).
	phase "requester": the requester again inside the approval engine (settings and roles only).
	phase "execute": the approver (settings and roles only; the surface was the request's).
	"""
	if not enabled():
		raise PermissionDenied("Customization through the assistant is turned off (AI Settings).")
	if not kind_enabled(kind_name):
		raise PermissionDenied(f"Customization of type '{kind_name}' is turned off (AI Settings).")
	roles = kind_roles(kind_name, kind, spec)
	if not has_roles(user, roles):
		who = "The approver" if phase == "execute" else "You"
		raise PermissionDenied(f"{who} ({user}) needs the role(s) {', '.join(roles)} for this change.")
	if phase == "request":
		if not surface_allowed(surface):
			raise PermissionDenied(f"Customization requests are not allowed from {surface or 'this channel'}.")
		_assert_rate(user)
		_assert_pending(user, kind_name, spec)


def _assert_rate(user):
	limit = cint(_settings().get(RATE_SETTING)) or DEFAULT_MAX_PER_HOUR
	since = add_to_date(now_datetime(), hours=-1)
	count = frappe.db.count("AI Approval Request", {"action_type": ACTION_TYPE, "requested_by": user, "creation": (">", since)})
	if count >= limit:
		raise PermissionDenied(f"Too many customization requests in the last hour ({count}/{limit}). Try again later.")


def pending(user=None):
	filters = {"action_type": ACTION_TYPE, "status": "Pending", "expires_at": (">", now_datetime())}
	if user:
		filters["requested_by"] = user
	return frappe.get_all("AI Approval Request", filters=filters, fields=["name", "payload_json", "requested_by"])


def _assert_pending(user, kind_name, spec):
	mine = pending(user)
	if len(mine) >= MAX_PENDING_PER_USER:
		raise PermissionDenied(
			f"You have {len(mine)} customization requests waiting for a decision; approve or reject them first."
		)
	key = target_key(kind_name, spec)
	for row in pending():
		if _payload(row.payload_json).get("target") == key:
			raise PermissionDenied(f"A customization request for the same target is already waiting: {row.name}.")


def target_key(kind_name, spec):
	"""One identity per target: doctype+fields, script name, report name."""
	spec = spec or {}
	doctype = spec.get("doctype") or spec.get("dt") or ""
	if kind_name == "properties":  # Customize Form saves the whole DocType: one request per DocType
		return f"properties:{doctype}"
	if kind_name == "custom_field":
		names = sorted(str(f.get("fieldname") or "") for f in spec.get("fields") or [] if isinstance(f, dict))
		return f"custom_field:{doctype}:{','.join(names)}"
	if kind_name == "report":
		return f"report:{spec.get('report_name') or spec.get('name') or ''}"
	if kind_name == "workflow":  # one active workflow per DocType
		return f"workflow:{spec.get('document_type') or doctype}"
	if kind_name == "naming" and spec.get("operation") == "set_doctype_naming":
		return f"properties:{doctype}"  # saved through Customize Form, like the properties kind
	if kind_name == "dashboard":  # a card and a chart may share a name
		return f"dashboard:{spec.get('object') or ''}:{spec.get('name') or ''}"
	if kind_name == "server_script" and spec.get("script_type") == "Permission Query":
		return f"server_script:permission_query:{spec.get('reference_doctype') or ''}"
	return f"{kind_name}:{spec.get('name') or doctype or ''}"


def _payload(text):
	try:
		value = json.loads(text or "{}")
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


# -- request size -------------------------------------------------------------------


def assert_size(args, kind_name=None):
	"""Limits on the raw arguments, before the kind sees them."""
	if not isinstance(args, dict):
		raise PermissionDenied("Arguments must be an object.")
	for key in CODE_ARGS:
		value = args.get(key)
		if isinstance(value, str) and len(value) > MAX_CODE_CHARS:
			raise PermissionDenied(f"`{key}` is {len(value)} characters; the limit is {MAX_CODE_CHARS}.")
	fields = args.get("fields")
	cap = MAX_FIELDS_BY_KIND.get(kind_name, MAX_FIELDS)
	if isinstance(fields, list) and len(fields) > cap:
		raise PermissionDenied(f"At most {cap} fields per request ({len(fields)} sent).")
	count = len(args.get("doctype_properties") or {}) if isinstance(args.get("doctype_properties"), dict) else 0
	field_properties = args.get("field_properties")
	if isinstance(field_properties, dict):
		count += sum(len(v) for v in field_properties.values() if isinstance(v, dict))
	if count > MAX_PROPERTIES:
		raise PermissionDenied(f"At most {MAX_PROPERTIES} properties per request ({count} sent).")


def approval_ttl_minutes():
	return min(cint(_settings().get("default_approval_ttl_minutes")) or 30, MAX_TTL_MINUTES)


# -- decisions ------------------------------------------------------------------------


def assert_can_approve(approval, user):
	"""Extra rules for approving a Customize request: four-eyes of its own, and the approver holds the
	kind's roles (they execute it under their own name)."""
	payload = _payload(approval.payload_json)
	kind_name = payload.get("kind")
	if four_eyes(kind_name, payload.get("spec")) and approval.requested_by == user:
		raise PermissionDenied("Customization needs a second person: the requester cannot approve their own change.")
	try:
		kind = registry.get(kind_name)
	except Exception:
		# fails closed: without the kind its own required roles are unknown (and it could not execute anyway)
		frappe.log_error(frappe.get_traceback(), f"ai_agent customize: approve check could not load {kind_name}")
		raise PermissionDenied(f"The customization kind '{kind_name}' cannot be loaded on this site; it cannot be approved.")
	roles = kind_roles(kind_name, kind, payload.get("spec"))
	if not has_roles(user, roles):
		raise PermissionDenied(f"Approving this change needs the role(s) {', '.join(roles)}.")
