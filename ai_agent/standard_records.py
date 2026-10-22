"""Records the app ships as files: ``ai_agent/ai_agent/<doctype>/<name>/<name>.json`` (``importable_doctypes`` in
hooks.py). On migrate Frappe re-imports a file whose ``modified`` is newer than the record, by deleting the record
and inserting the file. Each controller's ``prepare_for_import`` calls ``keep_site_values`` so what the site owns
survives that: fields the file does not carry, the fields named site-owned, and stored passwords."""

import frappe
from frappe.model import no_value_fields, table_fields

ROW_META = ("name", "parent", "parenttype", "parentfield", "owner", "creation", "modified", "modified_by")


def keep_site_values(docdict, site_owned=(), keep_all=False):
	"""Copy the existing record's values into ``docdict`` before Frappe re-inserts it. ``keep_all``: the file only
	creates the record; an existing one stays exactly as the site has it."""
	name = frappe.db.exists(docdict["doctype"], docdict["name"])
	if not name:
		return
	existing = frappe.get_doc(docdict["doctype"], name)
	docdict["name"] = existing.name  # names compare case-insensitively: keep the site's spelling
	for df in existing.meta.fields:
		field = df.fieldname
		if not (keep_all or field in site_owned or field not in docdict):
			continue
		if df.fieldtype in table_fields:
			docdict[field] = [{k: v for k, v in row.as_dict().items() if k not in ROW_META} for row in existing.get(field)]
		elif df.fieldtype == "Password":
			docdict[field] = existing.get_password(field, raise_exception=False) or ""
		elif df.fieldtype not in no_value_fields:
			docdict[field] = existing.get(field)
