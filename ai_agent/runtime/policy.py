"""Product policies enforced across every agent path (tools, interceptor, MCP).

- No submit / cancel unless AI Settings.allow_submit_cancel_actions is on.
- No create/update of system configuration: Single DocTypes, *Settings,
  Company, and default-account fields.
- Model providers: Managed mode = Anthropic only; other providers only in
  Own Key mode with explicit consent.
"""

import frappe
from frappe.utils import cint

ANTHROPIC = "Anthropic"
THIRD_PARTY_TYPES = ("OpenAI", "OpenAI Compatible", "Ollama", "Gemini OCR")
SUBMIT_CANCEL_TOOLS = ("actions.request_submit", "actions.request_cancel")
PROTECTED_DOCTYPES = frozenset({"Company", "Party Account", "Mode of Payment Account", "Fiscal Year", "Accounting Period"})
PROTECTED_CHILD_TABLES = frozenset({"Party Account", "Mode of Payment Account"})


class PolicyViolation(frappe.ValidationError):
	pass


def _settings():
	return frappe.get_cached_doc("AI Settings")


# -- submit / cancel ---------------------------------------------------------


def submit_cancel_allowed():
	return bool(cint(_settings().get("allow_submit_cancel_actions")))


def workflow_changes_docstatus(doctype, name, action):
	"""True when the workflow transition submits or cancels the document."""
	from frappe.model.workflow import get_workflow_name

	workflow_name = get_workflow_name(doctype)
	if not workflow_name:
		return False
	workflow = frappe.get_cached_doc("Workflow", workflow_name)
	current_docstatus = cint(frappe.get_cached_value(doctype, name, "docstatus")) if name else 0
	state_field = workflow.workflow_state_field or "workflow_state"
	current_state = frappe.get_cached_value(doctype, name, state_field) if name else None
	states = {row.state: cint(row.doc_status) for row in workflow.states}
	for transition in workflow.transitions:
		if transition.action != action:
			continue
		if current_state and transition.state != current_state:
			continue
		if states.get(transition.next_state, current_docstatus) != current_docstatus:
			return True
	return False


# -- system configuration ----------------------------------------------------


def is_protected_doctype(doctype):
	if not doctype:
		return False
	if doctype in PROTECTED_DOCTYPES or doctype.endswith("Settings"):
		return True
	try:
		return bool(frappe.get_meta(doctype).issingle)
	except Exception:
		return False


def is_default_account_field(fieldname):
	name = str(fieldname or "").lower()
	return name.startswith("default_") and "account" in name


def protected_payload_fields(doctype, payload):
	"""Default-account fields and protected child tables in a write payload."""
	if not isinstance(payload, dict):
		return []
	meta = frappe.get_meta(doctype)
	found = []
	for key, value in payload.items():
		df = meta.get_field(key)
		if is_default_account_field(key):
			found.append(key)
		elif df and df.fieldtype in frappe.model.table_fields and isinstance(value, list):
			if df.options in PROTECTED_CHILD_TABLES and value:
				found.append(key)
				continue
			for index, row in enumerate(value):
				for child_key in protected_payload_fields(df.options, row):
					found.append(f"{key}[{index}].{child_key}")
	return found


def assert_write_target_allowed(doctype, payload):
	if is_protected_doctype(doctype):
		raise PolicyViolation(f"{doctype} is system configuration; it cannot be created or changed through the agent.")
	fields = protected_payload_fields(doctype, payload)
	if fields:
		raise PolicyViolation(
			f"Default accounts and account mappings cannot be set through the agent: {', '.join(fields)}."
		)


# -- providers ----------------------------------------------------------------


def provider_mode():
	return _settings().get("provider_mode") or "Managed"


def provider_allowed(provider_type):
	provider_type = provider_type or "OpenAI Compatible"
	if provider_type == ANTHROPIC and provider_mode() == "Managed":
		return True
	if provider_mode() == "Managed":
		return False
	if provider_type != ANTHROPIC and not cint(_settings().get("allow_third_party_providers")):
		return False
	allowed = {line.strip() for line in (_settings().get("allowed_providers") or "").splitlines() if line.strip()}
	return not allowed or provider_type in allowed


def assert_provider_allowed(provider_type):
	if not provider_allowed(provider_type):
		if provider_mode() == "Managed":
			raise PolicyViolation(
				f"Provider type '{provider_type}' is not allowed in Managed mode (Anthropic only). "
				"Switch AI Settings to Own Key mode and accept third-party data sharing to use it."
			)
		raise PolicyViolation(
			f"Provider type '{provider_type}' is not allowed: enable third-party providers and list it in AI Settings."
		)


def third_party_warning():
	"""UI warning text when data leaves to a non-Anthropic provider, else ""."""
	if provider_mode() == "Own Key" and cint(_settings().get("allow_third_party_providers")):
		return frappe._(
			"Note: Own Key mode is on with third-party providers. Questions, tool results and attachments are sent to the chosen provider (such as DeepSeek or Gemini)."
		)
	return ""
