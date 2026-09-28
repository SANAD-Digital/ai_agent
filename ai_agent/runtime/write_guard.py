"""Field-level validation for agent write payloads.

Applied twice: when the agent requests an approval, and again right before the
approved action executes (the approver's permissions may differ from the
requester's, and settings or permissions may change in between).
"""

import frappe
from frappe.model import no_value_fields, table_fields

# Never settable through an update: identity, audit trail and lifecycle.
BLOCKED_UPDATE_FIELDS = frozenset({
    "docstatus", "name", "owner", "creation", "modified", "modified_by",
    "amended_from", "idx", "doctype", "parent", "parenttype", "parentfield",
})
# A new draft may carry a name (Prompt naming) but never lifecycle/audit data.
BLOCKED_CREATE_FIELDS = frozenset({
    "owner", "creation", "modified", "modified_by", "amended_from",
    "parent", "parenttype", "parentfield",
})
# Child rows may reference an existing row by name; everything else is blocked.
BLOCKED_CHILD_FIELDS = frozenset({
    "docstatus", "owner", "creation", "modified", "modified_by",
    "parent", "parenttype", "parentfield",
})


class WritePayloadError(frappe.ValidationError):
    pass


def _writable_fieldnames(doctype, user, parent_doctype=None, *, allow_computed=False):
    """Fields this user may write at a permitted permlevel.

    Updates also exclude read-only/hidden fields. Creates keep them
    (``allow_computed``): Frappe recomputes fetched/derived values on insert,
    and mapped payloads legitimately carry them.
    """
    meta = frappe.get_meta(doctype)
    levels = set(
        meta.get_permlevel_access(
            permission_type="write",
            parenttype=parent_doctype if meta.istable else None,
            user=user,
        )
    )
    writable = {}
    for df in meta.fields:
        if not df.fieldname or df.fieldtype in no_value_fields and df.fieldtype not in table_fields:
            continue
        if df.fieldtype in ("Password", "Password Hash"):
            continue
        if (df.read_only or df.hidden) and not allow_computed:
            continue
        if (df.permlevel or 0) not in levels:
            continue
        writable[df.fieldname] = df
    return writable


def blocked_fields(doctype, payload, user, *, mode="update", parent_doctype=None):
    """Return a sorted list of payload keys the user may not set ("field" or "table[i].field")."""
    if not isinstance(payload, dict):
        return ["<payload>"]
    blocked_system = BLOCKED_UPDATE_FIELDS if mode == "update" else BLOCKED_CREATE_FIELDS
    if parent_doctype:
        blocked_system = BLOCKED_CHILD_FIELDS
    meta = frappe.get_meta(doctype)
    writable = _writable_fieldnames(doctype, user, parent_doctype, allow_computed=mode == "create")
    problems = []
    for key, value in payload.items():
        if key == "doctype" and (mode != "update" or parent_doctype):
            continue
        if key == "docstatus" and mode == "create":
            # A create request may only produce a draft.
            if value not in (None, 0, "0"):
                problems.append(key)
            continue
        if key == "name" and parent_doctype:
            continue
        if key in blocked_system:
            problems.append(key)
            continue
        df = writable.get(key)
        if not df:
            # Creates ignore keys that are not fields (Frappe drops them on insert).
            if mode == "create" and not meta.get_field(key):
                continue
            problems.append(key)
            continue
        if df.fieldtype in table_fields:
            if value in (None, ""):
                continue
            if not isinstance(value, list):
                problems.append(key)
                continue
            for index, row in enumerate(value):
                for child_key in blocked_fields(
                    df.options, row, user, mode=mode, parent_doctype=doctype
                ):
                    problems.append(f"{key}[{index}].{child_key}")
    return sorted(set(problems))


def assert_payload_allowed(doctype, payload, user, *, mode="update"):
    problems = blocked_fields(doctype, payload, user, mode=mode)
    if problems:
        raise WritePayloadError(
            f"These fields cannot be set on {doctype} through the agent: {', '.join(problems)}"
        )
