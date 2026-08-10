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

    roles = frappe.get_roles(permission_context.user)

    if (
        session.user != permission_context.user
        and "System Manager" not in roles
    ):
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

    combined_text = "\n\n".join(
        (
            f"[{page['file_name']} - page {page['page_number']}]\n"
            f"{page['text']}"
        )
        for page in pages
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
        "text": combined_text,
    }


def _parse_warnings(value):
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return [str(value)] if value else []
    return parsed if isinstance(parsed, list) else []


def handle_list_leaf_accounts(arguments, permission_context):
    """Return relevant leaf accounts only."""

    company = str(
        arguments.get("company")
        or permission_context.company
        or ""
    ).strip()

    if not company:
        return {"error": "company is required"}

    if not permission_context.has_doctype_permission("Account", "read"):
        return {"error": "No read permission on Account"}

    account_types = [
        str(value).strip()
        for value in (arguments.get("account_types") or [])
        if str(value).strip()
    ]

    search = str(arguments.get("search") or "").strip()

    filters = {
        "company": company,
        "is_group": 0,
        "disabled": 0,
    }

    if account_types:
        filters["account_type"] = ("in", account_types[:10])

    if search:
        filters["name"] = ("like", f"%{search}%")

    limit = min(
        max(int(arguments.get("limit") or 30), 1),
        100,
    )

    rows = frappe.get_all(
        "Account",
        filters=filters,
        fields=[
            "name",
            "account_name",
            "account_type",
            "root_type",
        ],
        order_by="name asc",
        limit_page_length=limit,
    )

    return {
        "company": company,
        "returned_count": len(rows),
        "accounts": [
            {
                "name": row.name,
                "account_name": row.account_name or "",
                "account_type": row.account_type or "",
                "root_type": row.root_type or "",
            }
            for row in rows
        ],
    }
