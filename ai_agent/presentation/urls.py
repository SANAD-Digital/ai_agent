"""Links into the ERPNext desk for documents shown in a result."""

import frappe


def slug(doctype):
    return str(doctype).lower().replace(" ", "-")


def doc_href(doctype, name):
    if not doctype or not name:
        return None
    try:
        return frappe.utils.get_url(f"/app/{slug(doctype)}/{frappe.utils.quote(str(name), safe='')}")
    except Exception:
        return f"/app/{slug(doctype)}/{name}"


