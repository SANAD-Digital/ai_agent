import frappe
from ai_agent.runtime.constants import DENYLIST_DOCTYPES


def handle_search_doctypes(arguments, permission_context):
    """Handle schema.search_doctypes tool call."""
    query = (
        arguments.get("query")
        or arguments.get("keyword")
        or arguments.get("search_term")
        or arguments.get("text")
        or arguments.get("search")
        or ""
    ).lower()
    module = arguments.get("module")
    limit = min(arguments.get("limit", 20), 50)

    filters = {}
    if module:
        filters["module"] = module

    all_dt = frappe.get_all("DocType", filters=filters, fields=["name", "module", "istable", "description"])

    results = []
    for dt in all_dt:
        if dt.name in DENYLIST_DOCTYPES:
            continue
        if dt.istable:
            continue
        if not permission_context.has_doctype_permission(dt.name, "read"):
            continue

        name_lower = dt.name.lower()
        desc_lower = (dt.description or "").lower()
        module_lower = (dt.module or "").lower()

        if query and query not in name_lower and query not in desc_lower and query not in module_lower:
            continue

        results.append({
            "name": dt.name,
            "module": dt.module,
            "description": dt.description or "",
        })

        if len(results) >= limit:
            break

    return {"doctypes": results, "count": len(results)}


def handle_describe_doctype(arguments, permission_context):
    """Handle schema.describe_doctype tool call."""
    doc_type = arguments.get("doctype")
    if not doc_type:
        return {"error": "doctype is required"}

    if not frappe.db.exists("DocType", doc_type):
        return {"error": f"DocType '{doc_type}' not found"}

    if not permission_context.has_doctype_permission(doc_type, "read"):
        return {"error": f"No read permission on '{doc_type}'"}

    meta = frappe.get_meta(doc_type)

    fields = []
    for field in meta.fields:
        if field.hidden:
            continue
        if field.fieldtype in (
            "Password", "Password Hash", "Section Break", "Column Break", 
            "Tab Break", "HTML", "Button", "Heading"
        ):
            continue
        f_dict = {
            "fieldname": field.fieldname,
            "fieldtype": field.fieldtype,
        }
        if field.label:
            f_dict["label"] = field.label
        if field.options:
            f_dict["options"] = field.options
        if field.reqd:
            f_dict["reqd"] = 1
        if field.read_only:
            f_dict["read_only"] = 1
        fields.append(f_dict)

    links = []
    child_tables = []
    for field in meta.fields:
        if field.fieldtype == "Link" and field.options:
            links.append({"fieldname": field.fieldname, "links_to": field.options})
        elif field.fieldtype == "Table":
            child_tables.append({"fieldname": field.fieldname, "child_doctype": field.options})

    return {
        "name": doc_type,
        "module": meta.module,
        "title_field": meta.title_field,
        "fields": fields,
        "links": links,
        "child_tables": child_tables,
        "is_child_table": bool(getattr(meta, "istable", False)),
        "readable_parent_doctypes": (
            permission_context.get_readable_parent_doctypes(doc_type)
            if getattr(meta, "istable", False) and hasattr(permission_context, "get_readable_parent_doctypes")
            else []
        ),
        "is_submittable": meta.is_submittable,
        "track_changes": meta.track_changes,
    }


def handle_describe_links(arguments, permission_context):
    """Handle schema.describe_links tool call."""
    doc_type = arguments.get("doctype")
    depth = min(arguments.get("depth", 1), 2)

    if not doc_type or not frappe.db.exists("DocType", doc_type):
        return {"error": "Invalid doctype"}

    meta = frappe.get_meta(doc_type)
    links = []
    for field in meta.fields:
        if field.fieldtype == "Link" and field.options:
            if permission_context.has_doctype_permission(field.options, "read"):
                links.append({
                    "fieldname": field.fieldname,
                    "links_to": field.options,
                })

    return {"doctype": doc_type, "links": links, "depth": depth}


def handle_describe_workflow(arguments, permission_context):
    """Handle schema.describe_workflow tool call."""
    doc_type = arguments.get("doctype")
    if not doc_type:
        return {"error": "doctype is required"}

    workflows = frappe.get_all("Workflow", filters={"document_type": doc_type}, fields=["name"])
    if not workflows:
        return {"doctype": doc_type, "workflow": None, "message": "No workflow found"}

    workflow = frappe.get_doc("Workflow", workflows[0].name)
    states = [{"state": s.state, "allow_edit": s.allow_edit} for s in workflow.states]
    transitions = []
    for t in workflow.transitions:
        transitions.append({
            "state": t.state,
            "action": t.action,
            "next_state": t.next_state,
            "allowed": t.allowed,
        })

    return {
        "doctype": doc_type,
        "workflow": workflow.name,
        "states": states,
        "transitions": transitions,
    }
