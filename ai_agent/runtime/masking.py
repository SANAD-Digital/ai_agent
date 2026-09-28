"""Mask personal/financial identifiers before any tool result reaches a model.

Masked: IBANs and bank account numbers, national / residence / passport IDs,
and individual pay (salary doctypes and salary-like fields). The lists extend
from AI Settings (masking_extra_fields, masking_extra_doctypes).
"""

import re

import frappe

MASK = "[محجوب]"

# Substrings of fieldnames whose values are always masked.
SENSITIVE_FIELD_PARTS = (
	# Bank identifiers. Not "account_number": that is the chart-of-accounts code.
	"iban", "bank_ac_no", "bank_account_no",
	"passport", "national_id", "nationality_id", "id_number", "iqama", "residence_id",
	"civil_id", "personal_id", "ssn", "social_security",
	"salary", "ctc", "gross_pay", "net_pay", "base_gross", "base_net_pay", "earnings", "deductions",
	"cost_to_company",
)
# Whole fieldnames that are only sensitive inside pay documents (see SALARY_DOCTYPES).
PAY_FIELDS = ("base", "variable", "rounded_total", "total_deduction", "amount", "default_amount", "additional_amount")
# Documents that describe one person's pay: every amount in them is masked.
SALARY_DOCTYPES = frozenset({
	"Salary Slip", "Salary Structure Assignment", "Salary Detail", "Additional Salary",
	"Employee Incentive", "Retention Bonus", "Employee Benefit Claim",
})
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
KEEP_KEYS = frozenset({"name", "doctype", "docstatus", "idx", "count", "row_count", "total_rows", "employee", "employee_name"})


def _setting_lines(fieldname):
	try:
		raw = frappe.get_cached_doc("AI Settings").get(fieldname) or ""
	except Exception:
		return []
	return [line.strip() for line in raw.splitlines() if line.strip()]


def salary_doctypes():
	return SALARY_DOCTYPES | set(_setting_lines("masking_extra_doctypes"))


def is_sensitive_field(fieldname, doctype=None):
	name = str(fieldname or "").lower()
	if not name or name in KEEP_KEYS:
		return False
	extra = tuple(line.lower() for line in _setting_lines("masking_extra_fields"))
	if any(part in name for part in SENSITIVE_FIELD_PARTS + extra):
		return True
	return bool(doctype and doctype in salary_doctypes() and name in PAY_FIELDS)


def mask(value, doctype=None):
	"""Recursively mask sensitive fields and IBAN-looking strings."""
	pay_document = bool(doctype and doctype in salary_doctypes())
	return _mask(value, doctype, pay_document)


def _mask(value, doctype, pay_document):
	if isinstance(value, dict):
		out = {}
		for key, item in value.items():
			if is_sensitive_field(key, doctype):
				out[key] = MASK if item not in (None, "", 0) else item
			elif pay_document and isinstance(item, (int, float)) and not isinstance(item, bool) and key not in KEEP_KEYS:
				out[key] = MASK
			else:
				out[key] = _mask(item, doctype, pay_document)
		return out
	if isinstance(value, list):
		return [_mask(item, doctype, pay_document) for item in value]
	if isinstance(value, str) and IBAN_RE.search(value):
		return IBAN_RE.sub(MASK, value)
	return value


def result_doctype(tool_name, arguments):
	arguments = arguments or {}
	spec = arguments.get("specification") if isinstance(arguments.get("specification"), dict) else arguments
	return (
		spec.get("doctype")
		or arguments.get("parent_doctype")
		or arguments.get("doctype")
		or None
	)


def mask_tool_result(tool_name, arguments, result):
	return mask(result, result_doctype(tool_name, arguments))
