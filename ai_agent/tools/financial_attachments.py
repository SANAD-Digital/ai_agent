import json

import frappe


def handle_get_session_ocr_text(arguments, permission_context):
    """Return the complete selected + ready OCR text for one Financial Attachment Session."""

    session_name = str(arguments.get("session_name") or "").strip()
    if not session_name:
        return {"error": "session_name is required"}

    if not permission_context.has_doctype_permission(
        "Financial Attachment Session", "read"
    ):
        return {"error": "No read permission on Financial Attachment Session"}

    session = frappe.db.get_value(
        "Financial Attachment Session",
        session_name,
        [
            "name",
            "user",
            "company",
            "status",
            "page_count",
            "selected_page_count",
            "read_page_count",
        ],
        as_dict=True,
    )

    if not session:
        return {"error": "Financial Attachment Session was not found"}

    # Another user's session is never readable by the agent, auditors included.
    if session.user != permission_context.user:
        return {"error": "No access to this Financial Attachment Session"}

    if (
        permission_context.company
        and session.company
        and session.company != permission_context.company
    ):
        return {"error": "Session company does not match the AI Run company"}

    page_filters = {
        "parent": session.name,
        "parenttype": "Financial Attachment Session",
    }
    actual_selected_page_count = frappe.db.count(
        "Financial Attachment Session Page",
        {**page_filters, "selected": 1},
    )
    selected_read_page_count = frappe.db.count(
        "Financial Attachment Session Page",
        {**page_filters, "selected": 1, "status": "Read"},
    )
    total_read_page_count = frappe.db.count(
        "Financial Attachment Session Page",
        {**page_filters, "status": "Read"},
    )

    rows = frappe.get_all(
        "Financial Attachment Session Page",
        filters={**page_filters, "selected": 1, "status": "Read"},
        fields=[
            "page_id",
            "file_name",
            "page_number",
            "text_preview",
            "warnings_json",
        ],
        order_by="idx asc",
        limit_page_length=0,
    )

    pages = []

    for row in rows:
        text = str(row.text_preview or "").strip()

        if not text:
            continue

        pages.append(
            {
                "page_id": row.page_id,
                "file_name": row.file_name or "Attachment",
                "page_number": int(row.page_number or 1),
                "text": text,
                "warnings": _parse_warnings(row.warnings_json),
            }
        )

    returned_page_count = len(pages)
    expected_page_count = int(actual_selected_page_count or 0)

    complete = (
        returned_page_count > 0
        and returned_page_count == expected_page_count
    )

    return {
        "session_name": session.name,
        "company": session.company,
        "status": session.status,

        "page_count": int(session.page_count or 0),
        "selected_page_count": expected_page_count,
        "read_page_count": int(total_read_page_count or 0),
        "selected_read_page_count": int(selected_read_page_count or 0),
        "stored_selected_page_count": int(session.selected_page_count or 0),
        "stored_read_page_count": int(session.read_page_count or 0),
        "returned_page_count": returned_page_count,

        "complete": complete,

        "pages": pages,
    }


def _parse_warnings(value):
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return [str(value)] if value else []
    return parsed if isinstance(parsed, list) else []
