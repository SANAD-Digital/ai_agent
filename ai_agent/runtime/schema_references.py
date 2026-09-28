"""Generate compact schema summaries for the most used DocTypes.

The agent reads these (skills.read_reference on frappe-schema-discovery)
instead of live schema.describe_doctype dumps. Standard fields only: site
custom fields are left to live discovery.

    bench --site <site> execute ai_agent.runtime.schema_references.generate
"""

import os

import frappe

from ai_agent.tools.schema import _fields_by_usefulness

DOCTYPES = (
	"Sales Invoice", "Sales Invoice Item", "Purchase Invoice", "Purchase Invoice Item",
	"Payment Entry", "Journal Entry", "Journal Entry Account", "GL Entry", "Stock Ledger Entry",
	"Bin", "Item", "Customer", "Supplier", "Sales Order", "Delivery Note",
)
SKIP_TYPES = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold", "Password", "Image")
MAX_FIELDS = 45


def generate(target=None):
	target = target or os.path.join(frappe.get_app_path("ai_agent"), "skills", "frappe-schema-discovery", "references")
	os.makedirs(target, exist_ok=True)
	written = []
	for doctype in DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		meta = frappe.get_meta(doctype)
		fields = [
			df for df in _fields_by_usefulness(meta)
			if df.fieldname and not df.hidden and df.fieldtype not in SKIP_TYPES and not df.get("is_custom_field")
		][:MAX_FIELDS]
		lines = [
			f"# {doctype}",
			"",
			f"Module: {meta.module}. Submittable: {'yes' if meta.is_submittable else 'no'}."
			+ (" Child table." if meta.istable else ""),
			"Standard ERPNext v16 fields, most used first (site custom fields are not listed).",
			"",
			"| fieldname | type | label | options |",
			"|---|---|---|---|",
		]
		for df in fields:
			options = (df.options or "").replace("\n", " / ")[:60]
			lines.append(f"| {df.fieldname} | {df.fieldtype} | {df.label or ''} | {options} |")
		name = doctype.lower().replace(" ", "-")
		with open(os.path.join(target, f"{name}.md"), "w", encoding="utf-8") as handle:
			handle.write("\n".join(lines) + "\n")
		written.append(name)
	return written
