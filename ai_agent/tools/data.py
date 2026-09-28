import re
import frappe
from ai_agent.query.validator import QueryValidator, validate_filter_value
from ai_agent.runtime.exceptions import QueryValidationError
from ai_agent.query.cost_guard import QueryCostGuard
from ai_agent.query.v16_adapter import V16Adapter
from ai_agent.runtime.constants import SENSITIVE_FIELD_PATTERNS
from ai_agent.runtime.artifact_service import ArtifactService


AGGREGATE_FIELD_RE = re.compile(
    r"^(COUNT|SUM|AVG|MIN|MAX)\(([a-z][a-z0-9_]*)\)(?:\s+as\s+([a-z][a-z0-9_]*))?$",
    re.IGNORECASE,
)


def handle_query(arguments, permission_context):
    """Handle data.query tool call."""
    spec = arguments.get("specification") or arguments
    if "doctype" not in spec:
        return {"error": "specification must include 'doctype'"}
    spec = _normalize_query_spec(spec)

    validator = QueryValidator(permission_context)
    try:
        spec = validator.validate(spec)
    except Exception as e:
        return {"error": str(e)}

    cost_guard = QueryCostGuard()
    cost_result = cost_guard.evaluate(spec, permission_context)
    if cost_result == "deny":
        return {"error": "Query denied due to high cost. Please add more filters."}
    elif cost_result == "require_more_filters":
        return {"error": "Query requires additional filters (date range, company, etc.)."}
    elif cost_result == "allow_with_lower_limit":
        spec["limit"] = min(spec.get("limit", 100), 100)

    adapter = V16Adapter()
    try:
        result = adapter.execute(spec, permission_context)
    except Exception as e:
        return {"error": f"Query execution failed: {str(e)}"}

    rows = result.get("rows", [])
    rows = _redact_sensitive_fields(spec["doctype"], rows, permission_context)
    artifact_info = _create_query_artifact(
        getattr(permission_context, "run_name", None),
        spec,
        rows,
        result.get("row_count", len(rows)),
    )

    return {
        "rows": rows[:500],
        "row_count": result.get("row_count", len(rows)),
        "truncated": len(rows) > 500,
        "artifact": artifact_info,
    }


def _create_query_artifact(run_name, spec, rows, row_count):
    if not run_name or len(rows) <= 50:
        return None
    columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    if not columns:
        return None
    artifact = ArtifactService().create_table(
        run_name,
        f"{spec.get('doctype')} query results",
        rows,
        columns,
        total_row_count=row_count,
    )
    return {
        "artifact_name": artifact.name,
        "title": artifact.title,
        "row_count": artifact.row_count,
        "is_truncated": bool(artifact.is_truncated),
    }


def _normalize_query_spec(spec):
    normalized = dict(spec)
    normalized["fields"] = [
        _normalize_field_spec(field)
        for field in normalized.get("fields", [])
    ]
    return normalized


def _normalize_field_spec(field):
    if not isinstance(field, str):
        return field

    match = AGGREGATE_FIELD_RE.match(field.strip())
    if not match:
        return field

    aggregate, fieldname, alias = match.groups()
    return {
        "field": fieldname,
        "aggregate": aggregate.upper(),
        "alias": alias or f"{aggregate.lower()}_{fieldname}",
    }


NOT_ACCESSIBLE_ERROR = "Document not found or not accessible."


def _load_readable_document(doc_type, name, permission_context):
    """Load a document only if this user may read that exact row.

    Missing and forbidden documents return the same generic error so the
    agent cannot probe for the existence of records outside the user's scope.
    """
    try:
        doc = frappe.get_doc(doc_type, name)
    except frappe.DoesNotExistError:
        return None
    if not permission_context.can_access_document(doc, "read"):
        return None
    return doc


def _is_blocked_field(field):
    fieldname = (field.fieldname or "").lower()
    return (
        field.fieldtype in ("Password", "Password Hash")
        or any(p in fieldname for p in SENSITIVE_FIELD_PATTERNS)
    )


def _child_rows(child_doctype, parent_doctype, children, permission_context, limit=None):
    """Serialize child rows keeping only fields readable under the parent's DocPerms."""
    meta = frappe.get_meta(child_doctype)
    readable = [
        field for field in meta.fields
        if field.fieldname
        and not field.hidden
        and not _is_blocked_field(field)
        and field.fieldtype not in frappe.model.no_value_fields
        and permission_context.check_field_access(
            child_doctype, field.fieldname, "read", parent_doctype
        )
    ]
    rows = []
    for child in children[:limit] if limit else children:
        row = {"name": child.name, "idx": child.idx}
        for field in readable:
            val = child.get(field.fieldname)
            if val is not None:
                row[field.fieldname] = val
        rows.append(row)
    return rows


def handle_get_document(arguments, permission_context):
    """Handle data.get_document tool call."""
    doc_type = arguments.get("doctype")
    name = arguments.get("name")

    if not doc_type or not name:
        return {"error": "doctype and name are required"}

    if not permission_context.has_doctype_permission(doc_type, "read"):
        return {"error": f"No read permission on '{doc_type}'"}

    doc = _load_readable_document(doc_type, name, permission_context)
    if not doc:
        return {"error": NOT_ACCESSIBLE_ERROR}

    meta = frappe.get_meta(doc_type)
    result = {"name": doc.name, "doctype": doc_type}

    for field in meta.fields:
        if field.hidden or _is_blocked_field(field):
            continue
        if not permission_context.check_field_access(doc_type, field.fieldname, "read"):
            continue
        if field.fieldtype in frappe.model.table_fields:
            rows = _child_rows(field.options, doc_type, doc.get(field.fieldname) or [], permission_context)
            if rows:
                result[field.fieldname] = rows
            continue
        val = doc.get(field.fieldname)
        if val is not None:
            result[field.fieldname] = val

    return result


def handle_get_child_records(arguments, permission_context):
    """Handle data.get_child_records tool call."""
    parent_doctype = arguments.get("parent_doctype")
    parent_name = arguments.get("parent_name")
    child_field = arguments.get("child_field")

    if not all([parent_doctype, parent_name, child_field]):
        return {"error": "parent_doctype, parent_name, and child_field are required"}

    if not permission_context.has_doctype_permission(parent_doctype, "read"):
        return {"error": f"No read permission on '{parent_doctype}'"}

    table_field = frappe.get_meta(parent_doctype).get_field(child_field)
    if not table_field or table_field.fieldtype not in frappe.model.table_fields:
        return {"error": f"'{child_field}' is not a child table of '{parent_doctype}'"}
    if not permission_context.check_field_access(parent_doctype, child_field, "read"):
        return {"error": f"No read access to field '{child_field}'"}

    doc = _load_readable_document(parent_doctype, parent_name, permission_context)
    if not doc:
        return {"error": NOT_ACCESSIBLE_ERROR}

    try:
        limit = max(1, min(int(arguments.get("limit") or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    children = doc.get(child_field) or []
    rows = _child_rows(table_field.options, parent_doctype, children, permission_context, limit)
    return {"rows": rows, "row_count": len(rows), "total_rows": len(children)}


def handle_count(arguments, permission_context):
    """Handle data.count tool call.

    Counts through frappe.get_list so User Permissions, permission-query
    hooks and if_owner rules apply (frappe.db.count bypasses all of them).
    """
    doc_type = arguments.get("doctype")
    if not doc_type:
        return {"error": "doctype is required"}

    if not permission_context.has_doctype_permission(doc_type, "read"):
        return {"error": f"No read permission on '{doc_type}'"}

    raw_filters = arguments.get("filters", [])
    filters = []
    if isinstance(raw_filters, list):
        for flt in raw_filters:
            if isinstance(flt, dict) and "field" in flt:
                operator = flt.get("operator") or "="
                try:
                    validate_filter_value(operator, flt.get("value"))
                except QueryValidationError as exc:
                    return {"error": str(exc)}
                filters.append([flt.get("field"), operator, flt.get("value")])
            else:
                filters.append(flt)
    else:
        filters = raw_filters

    try:
        rows = frappe.get_list(
            doc_type,
            filters=filters,
            fields=[{"COUNT": "name", "as": "count"}],
            user=permission_context.user,
            limit_page_length=0,
        )
        count = int((rows[0] or {}).get("count") or 0) if rows else 0
        return {"count": count}
    except Exception as e:
        return {"error": f"Count execution failed: {str(e)}"}


def _redact_sensitive_fields(doc_type, rows, permission_context):
    """Remove fields user cannot access."""
    if not rows:
        return rows

    meta = frappe.get_meta(doc_type)
    blocked = set()
    for field in meta.fields:
        if field.fieldtype in ("Password", "Password Hash"):
            blocked.add(field.fieldname)
        if any(p in (field.fieldname or "").lower() for p in SENSITIVE_FIELD_PATTERNS):
            blocked.add(field.fieldname)
        if not permission_context.check_field_access(doc_type, field.fieldname, "read"):
            blocked.add(field.fieldname)

    if not blocked:
        return rows

    cleaned = []
    for row in rows:
        if isinstance(row, dict):
            cleaned.append({k: v for k, v in row.items() if k not in blocked})
        else:
            cleaned.append(row)
    return cleaned
