import re
import frappe
from ai_agent.query.validator import QueryValidator
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


def handle_get_document(arguments, permission_context):
    """Handle data.get_document tool call."""
    doc_type = arguments.get("doctype")
    name = arguments.get("name")

    if not doc_type or not name:
        return {"error": "doctype and name are required"}

    if not permission_context.has_doctype_permission(doc_type, "read"):
        return {"error": f"No read permission on '{doc_type}'"}

    try:
        doc = frappe.get_doc(doc_type, name)
    except frappe.DoesNotExistError:
        return {"error": f"Document '{name}' not found in '{doc_type}'"}

    meta = frappe.get_meta(doc_type)
    result = {"name": doc.name, "doctype": doc_type}

    for field in meta.fields:
        if field.hidden or field.fieldtype in ("Password", "Password Hash"):
            continue
        if not permission_context.check_field_access(doc_type, field.fieldname, "read"):
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

    try:
        doc = frappe.get_doc(parent_doctype, parent_name)
    except frappe.DoesNotExistError:
        return {"error": f"Parent document '{parent_name}' not found"}

    children = doc.get(child_field, [])
    limit = min(arguments.get("limit", 100), 500)

    rows = []
    for child in children[:limit]:
        row = {"name": child.name} if hasattr(child, "name") else {}
        for key, val in child.as_dict().items():
            if key not in ("name", "parent", "parenttype", "parentfield"):
                row[key] = val
        rows.append(row)

    return {"rows": rows, "row_count": len(rows)}


def handle_count(arguments, permission_context):
    """Handle data.count tool call."""
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
                filters.append([flt.get("field"), flt.get("operator") or "=", flt.get("value")])
            else:
                filters.append(flt)
    else:
        filters = raw_filters

    try:
        count = frappe.db.count(doc_type, filters=filters)
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
