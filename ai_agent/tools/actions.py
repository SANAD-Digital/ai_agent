import frappe
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied


NON_INPUT_FIELDTYPES = {
    "Section Break",
    "Column Break",
    "Tab Break",
    "HTML",
    "Button",
    "Image",
    "Fold",
}


def _is_input_required_field(field):
    return (
        field.fieldname
        and field.reqd
        and not field.hidden
        and not field.read_only
        and field.fieldtype not in NON_INPUT_FIELDTYPES
    )


def _required_input_fields(meta):
    return [field.fieldname for field in meta.fields if _is_input_required_field(field)]


def _ignored_required_fields(meta):
    return [
        field.fieldname
        for field in meta.fields
        if field.fieldname and field.reqd and not _is_input_required_field(field)
    ]


def _build_document(doc_type, payload):
    normalized_payload = dict(payload or {})
    normalized_payload["doctype"] = doc_type
    return frappe.get_doc(normalized_payload)


def _apply_document_defaults(doc):
    # Both come from the selling/buying/stock controllers and walk doc.items; Journal Entry, Payment Entry
    # and others inherit them from AccountsController without having an items table.
    if not doc.meta.get_field("items"):
        return
    for method_name in ("set_missing_values", "calculate_taxes_and_totals"):
        if hasattr(doc, method_name):
            getattr(doc, method_name)()


def handle_prepare(arguments, permission_context):
    """Handle actions.prepare_document tool call."""
    doc_type = arguments.get("doctype")
    if not doc_type:
        return {"error": "doctype is required"}

    if not frappe.db.exists("DocType", doc_type):
        return {"error": f"DocType '{doc_type}' not found"}

    if not permission_context.has_doctype_permission(doc_type, "read"):
        return {"error": f"No read permission on '{doc_type}'"}

    meta = frappe.get_meta(doc_type)
    payload = arguments.get("payload", {})
    required_fields = _required_input_fields(meta)
    ignored_required_fields = _ignored_required_fields(meta)

    missing = [field for field in required_fields if field not in payload]
    warnings = []

    if not missing:
        try:
            doc = _build_document(doc_type, payload)
            _apply_document_defaults(doc)
        except Exception as exc:
            warnings.append(str(exc))

    return {
        "doctype": doc_type,
        "required_fields": required_fields,
        "ignored_required_fields": ignored_required_fields,
        "provided_fields": list(payload.keys()),
        "missing_fields": missing,
        "warnings": warnings,
        "payload": payload,
    }


def handle_validate(arguments, permission_context):
    """Handle actions.validate_document tool call."""
    doc_type = arguments.get("doctype")
    payload = arguments.get("payload", {})

    if not doc_type:
        return {"error": "doctype is required"}

    # The same checks a save runs (defaults, mandatory, links, controller validate), never a bare
    # doc.validate(): controllers expect _action and _doc_before_save. Inside a savepoint, rolled back.
    frappe.db.savepoint("ai_validate_document")
    try:
        doc = _build_document(doc_type, payload)
        _apply_document_defaults(doc)
        # Document.insert() up to the write: defaults, name (series counter rolled back), parent links on
        # child rows, then before_validate / validate / before_save and the mandatory and link checks.
        doc._set_defaults()
        doc.set_user_and_timestamp()
        doc.set_docstatus()
        doc.run_method("before_insert")
        doc.set_new_name()
        doc.set_parent_in_children()
        doc.flags.in_insert = True
        doc._action = "save"
        doc.load_doc_before_save()
        doc.run_before_save_methods()
        doc._validate()
        return {"valid": True, "doctype": doc_type}
    except Exception as e:
        return {"valid": False, "errors": frappe.utils.strip_html(str(e))}
    finally:
        frappe.db.rollback(save_point="ai_validate_document")
        frappe.local.message_log = []


def handle_request_create(arguments, permission_context):
    """Handle actions.request_create_draft tool call."""
    doc_type = arguments.get("doctype")
    payload = arguments.get("payload", {})

    if not doc_type:
        return {"error": "doctype is required"}

    if not permission_context.has_doctype_permission(doc_type, "create"):
        return {"error": f"No create permission on '{doc_type}'"}

    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Create",
            doctype_name=doc_type,
            action_summary=f"Create {doc_type} draft",
            impact_summary=f"A new {doc_type} will be created as draft.",
            payload=payload,
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": f"Create {doc_type} draft",
    }


def handle_request_bulk_create(arguments, permission_context):
    """Handle actions.request_bulk_create tool call."""
    documents = _normalize_bulk_documents(arguments.get("documents") or [])
    if not documents:
        return {"error": "documents is required"}

    for document in documents:
        doc_type = document["doctype"]
        if not permission_context.has_doctype_permission(doc_type, "create"):
            return {"error": f"No create permission on '{doc_type}'"}

    doctypes = sorted({document["doctype"] for document in documents})
    summary = arguments.get("summary") or f"Create {len(documents)} draft documents"
    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Bulk Create",
            doctype_name=doctypes[0] if len(doctypes) == 1 else None,
            action_summary=summary,
            impact_summary=f"{len(documents)} draft documents will be created after one approval.",
            payload={"documents": documents},
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": summary,
        "document_count": len(documents),
        "doctypes": doctypes,
    }


def handle_request_update(arguments, permission_context):
    """Handle actions.request_update tool call."""
    doc_type = arguments.get("doctype")
    doc_name = arguments.get("document_name")
    payload = arguments.get("payload", {})

    if not all([doc_type, doc_name]):
        return {"error": "doctype and document_name are required"}

    if not permission_context.has_doctype_permission(doc_type, "write"):
        return {"error": f"No write permission on '{doc_type}'"}

    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    # The engine checks row-level write access and the payload fields, and
    # records the document's current `modified` for the concurrency check.
    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Update",
            doctype_name=doc_type,
            document_name=doc_name,
            action_summary=f"Update {doc_type} {doc_name}",
            payload=payload,
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": f"Update {doc_type} {doc_name}",
    }


def handle_request_submit(arguments, permission_context):
    """Handle actions.request_submit tool call."""
    doc_type = arguments.get("doctype")
    doc_name = arguments.get("document_name")

    if not all([doc_type, doc_name]):
        return {"error": "doctype and document_name are required"}

    if not permission_context.has_doctype_permission(doc_type, "submit"):
        return {"error": f"No submit permission on '{doc_type}'"}

    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Submit",
            doctype_name=doc_type,
            document_name=doc_name,
            action_summary=f"Submit {doc_type} {doc_name}",
            impact_summary=f"Document {doc_name} will be submitted (docstatus=1).",
            payload={"doctype": doc_type, "name": doc_name},
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": f"Submit {doc_type} {doc_name}",
    }


def handle_request_cancel(arguments, permission_context):
    """Handle actions.request_cancel tool call."""
    doc_type = arguments.get("doctype")
    doc_name = arguments.get("document_name")

    if not all([doc_type, doc_name]):
        return {"error": "doctype and document_name are required"}

    if not permission_context.has_doctype_permission(doc_type, "cancel"):
        return {"error": f"No cancel permission on '{doc_type}'"}

    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Cancel",
            doctype_name=doc_type,
            document_name=doc_name,
            action_summary=f"Cancel {doc_type} {doc_name}",
            impact_summary=f"Document {doc_name} will be cancelled (docstatus=2).",
            payload={"doctype": doc_type, "name": doc_name},
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": f"Cancel {doc_type} {doc_name}",
    }


def handle_request_workflow(arguments, permission_context):
    """Handle actions.request_workflow_action tool call."""
    doc_type = arguments.get("doctype")
    doc_name = arguments.get("document_name")
    action = arguments.get("action")

    if not all([doc_type, doc_name, action]):
        return {"error": "doctype, document_name, and action are required"}

    engine = ApprovalEngine(permission_context)
    run_name = arguments.get("run")

    try:
        approval = engine.create_approval_request(
            run_name=run_name,
            action_type="Workflow",
            doctype_name=doc_type,
            document_name=doc_name,
            action_summary=f"Workflow action '{action}' on {doc_type} {doc_name}",
            payload={"doctype": doc_type, "name": doc_name, "action": action},
        )
    except PermissionDenied as exc:
        return {"error": str(exc)}

    return {
        "approval_required": True,
        "approval_id": approval.name,
        "action_summary": f"Workflow: {action}",
    }


def _normalize_bulk_documents(documents):
    normalized = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        payload = dict(document.get("payload") or {})
        doc_type = document.get("doctype") or payload.get("doctype")
        if not doc_type or not payload:
            continue
        payload["doctype"] = doc_type
        normalized.append({"doctype": doc_type, "payload": payload})
    return normalized
