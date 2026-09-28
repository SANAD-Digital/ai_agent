"""Permission-aware document attachment tools."""

import frappe
from frappe.utils import cint


def handle_list(arguments, permission_context):
    doctype = arguments.get("doctype")
    document_name = arguments.get("document_name")
    limit = max(1, min(cint(arguments.get("limit")) or 50, 100))

    if not doctype or not document_name:
        return {"error": "doctype and document_name are required"}
    if not frappe.db.exists("DocType", doctype):
        return {"error": f"DocType '{doctype}' not found"}
    if not permission_context.has_doctype_permission(doctype, "read"):
        return {"error": f"No read permission on '{doctype}'"}

    try:
        document = frappe.get_doc(doctype, document_name)
        if not document.has_permission("read"):
            return {"error": f"No read permission on '{doctype}' document '{document_name}'"}
    except frappe.DoesNotExistError:
        return {"error": f"Document '{document_name}' not found"}

    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": doctype,
            "attached_to_name": document_name,
        },
        fields=["name", "file_name", "file_size", "is_private", "attached_to_field", "creation"],
        order_by="creation desc",
        page_length=limit,
        ignore_permissions=True,
    )
    return {
        "doctype": doctype,
        "document_name": document_name,
        "files": rows,
        "count": len(rows),
    }
