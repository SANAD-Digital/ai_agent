import re
import frappe
from ai_agent.runtime.constants import ALLOWED_AGGREGATES, ALLOWED_OPERATORS
from ai_agent.runtime.exceptions import QueryValidationError


FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ALLOWED_DATE_BUCKETS = {"year", "quarter", "month", "week", "day"}
PARENT_FIELD_RE = re.compile(r"^parent\.([a-z][a-z0-9_]*)$")
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]*$")
STANDARD_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by",
    "docstatus", "idx", "parent", "parenttype", "parentfield",
}


def validate_filter_value(operator, value):
    """Reject values whose shape does not fit the operator.

    Without this, a malformed value (e.g. {"item": [a, b]} for between) is
    silently dropped or mis-bound and the tool returns a wrong answer.
    """
    if operator not in ALLOWED_OPERATORS:
        raise QueryValidationError(
            f"Operator '{operator}' not allowed (use one of: {', '.join(sorted(ALLOWED_OPERATORS))})."
        )
    if operator == "between":
        if not (isinstance(value, list) and len(value) == 2):
            raise QueryValidationError("'between' requires value as a two-item array: [from, to].")
    elif operator in ("in", "not in"):
        if not isinstance(value, list):
            raise QueryValidationError(f"'{operator}' requires value as an array.")
    elif operator not in ("is set", "is not set") and isinstance(value, (dict, list)):
        raise QueryValidationError(f"'{operator}' requires a single scalar value.")


class QueryValidator:
    """Validates Query Specification JSON before execution."""

    def __init__(self, permission_context):
        self.ctx = permission_context
        self._parent_doctype = None

    def _field_parent(self):
        return getattr(self, "_parent_doctype", None)

    def validate(self, spec):
        """Validate a query specification dict. Raises QueryValidationError on failure."""
        if not isinstance(spec, dict):
            raise QueryValidationError("Query specification must be a JSON object.")

        doc_type = spec.get("doctype")
        if not doc_type:
            raise QueryValidationError("doctype is required.")

        if not frappe.db.exists("DocType", doc_type):
            raise QueryValidationError(f"DocType '{doc_type}' does not exist.")

        if not self.ctx.has_doctype_permission(doc_type, "read"):
            raise QueryValidationError(f"No read permission on DocType '{doc_type}'.")

        fields = spec.get("fields", [])
        if not fields:
            raise QueryValidationError("At least one field is required.")
        if len(fields) > 50:
            raise QueryValidationError("Too many fields (max 50).")

        parent_doctype = (
            spec.get("parent_doctype")
            or self._parent_doctype_from_filters(spec.get("filters", []))
            or self._default_readable_parent_doctype(doc_type)
        )
        if parent_doctype:
            spec["parent_doctype"] = parent_doctype
        # Child-table permlevels are evaluated against the parent's DocPerms.
        self._parent_doctype = parent_doctype

        self._validate_fields(doc_type, fields)

        filters = spec.get("filters", [])
        filters = self._normalize_child_parent_filters(doc_type, filters, parent_doctype)
        spec["filters"] = filters
        if len(filters) > 20:
            raise QueryValidationError("Too many filters (max 20).")
        self._validate_filters(doc_type, filters, parent_doctype)

        group_by = spec.get("group_by", [])
        if group_by:
            self._validate_group_by(doc_type, group_by, fields)

        order_by = spec.get("order_by", [])
        if order_by:
            self._validate_order_by(doc_type, order_by, fields, group_by)

        limit = spec.get("limit", 500)
        if not isinstance(limit, int) or limit < 1:
            raise QueryValidationError("limit must be a positive integer.")
        if limit > 500:
            limit = 500
            spec["limit"] = limit

        offset = spec.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise QueryValidationError("offset must be a non-negative integer.")
        if offset > 10000:
            raise QueryValidationError("offset too large (max 10000).")

        return spec

    def _validate_fields(self, doc_type, fields):
        meta = frappe.get_meta(doc_type)
        allowed_fieldnames = {f.fieldname for f in meta.fields if not f.hidden}
        allowed_fieldnames.update(STANDARD_FIELDS)

        seen_aliases = set()
        for f in fields:
            if isinstance(f, str):
                if f == "*":
                    raise QueryValidationError("Wildcard '*' is not allowed.")
                if f not in allowed_fieldnames:
                    raise QueryValidationError(f"Field '{f}' not found or not accessible.")
                if not self.ctx.check_field_access(doc_type, f, "read", self._field_parent()):
                    raise QueryValidationError(f"No read access to field '{f}'.")
            elif isinstance(f, dict):
                fieldname = f.get("field")
                aggregate = f.get("aggregate")
                alias = f.get("alias")

                if not isinstance(fieldname, str) or not fieldname:
                    raise QueryValidationError(
                        f"Invalid field specification: {f} (expected a name or {{field, aggregate, alias}})."
                    )
                if fieldname not in allowed_fieldnames:
                    raise QueryValidationError(f"Field '{fieldname}' not found.")
                if not self.ctx.check_field_access(doc_type, fieldname, "read", self._field_parent()):
                    raise QueryValidationError(f"No read access to field '{fieldname}'.")

                if aggregate and aggregate not in ALLOWED_AGGREGATES:
                    raise QueryValidationError(f"Aggregate '{aggregate}' not allowed.")

                if alias:
                    if not ALIAS_RE.match(alias):
                        raise QueryValidationError(f"Invalid alias '{alias}'.")
                    if alias in seen_aliases:
                        raise QueryValidationError(f"Duplicate alias '{alias}'.")
                    seen_aliases.add(alias)
            else:
                raise QueryValidationError(f"Invalid field specification: {f}")

    def _validate_filters(self, doc_type, filters, parent_doctype=None):
        meta = frappe.get_meta(doc_type)
        allowed_fieldnames = {f.fieldname for f in meta.fields if not f.hidden}
        allowed_fieldnames.update(STANDARD_FIELDS)

        for flt in filters:
            if not isinstance(flt, dict):
                raise QueryValidationError("Each filter must be an object.")
            field = flt.get("field")
            operator = flt.get("operator")
            if not field or not operator:
                raise QueryValidationError("Filter must have 'field' and 'operator'.")
            if self._is_parent_field_filter(doc_type, field, parent_doctype):
                pass
            elif field not in allowed_fieldnames:
                raise QueryValidationError(f"Filter field '{field}' not found.")
            validate_filter_value(operator, flt.get("value"))

    def _normalize_child_parent_filters(self, doc_type, filters, parent_doctype=None):
        if not parent_doctype or not getattr(frappe.get_meta(doc_type), "istable", False):
            return filters

        child_fields = {f.fieldname for f in frappe.get_meta(doc_type).fields if not f.hidden}
        child_fields.update(STANDARD_FIELDS)
        parent_fields = {f.fieldname for f in frappe.get_meta(parent_doctype).fields if not f.hidden}
        parent_fields.update(STANDARD_FIELDS)

        normalized = []
        for flt in filters or []:
            if not isinstance(flt, dict):
                normalized.append(flt)
                continue
            field = flt.get("field")
            if (
                isinstance(field, str)
                and "." not in field
                and field not in child_fields
                and field in parent_fields
            ):
                flt = dict(flt)
                flt["field"] = f"parent.{field}"
            normalized.append(flt)
        return normalized

    def _is_parent_field_filter(self, doc_type, field, parent_doctype):
        match = PARENT_FIELD_RE.match(field or "")
        if not match:
            return False

        child_meta = frappe.get_meta(doc_type)
        if not getattr(child_meta, "istable", False):
            raise QueryValidationError("parent.<field> filters are only allowed for child table DocTypes.")

        if not parent_doctype:
            raise QueryValidationError("parent_doctype is required when filtering with parent.<field>.")

        if not frappe.db.exists("DocType", parent_doctype):
            raise QueryValidationError(f"Parent DocType '{parent_doctype}' does not exist.")

        if not self.ctx.has_doctype_permission(parent_doctype, "read"):
            raise QueryValidationError(f"No read permission on parent DocType '{parent_doctype}'.")

        parent_field = match.group(1)
        parent_meta = frappe.get_meta(parent_doctype)
        allowed_parent_fields = {f.fieldname for f in parent_meta.fields if not f.hidden}
        allowed_parent_fields.update(STANDARD_FIELDS)

        if parent_field not in allowed_parent_fields:
            raise QueryValidationError(f"Parent filter field '{parent_field}' not found.")
        if not self.ctx.check_field_access(parent_doctype, parent_field, "read"):
            raise QueryValidationError(f"No read access to parent field '{parent_field}'.")
        return True

    def _parent_doctype_from_filters(self, filters):
        for flt in filters or []:
            if (
                isinstance(flt, dict)
                and flt.get("field") == "parenttype"
                and flt.get("operator") == "="
                and flt.get("value")
            ):
                return flt.get("value")
        return None

    def _default_readable_parent_doctype(self, doc_type):
        try:
            if not getattr(frappe.get_meta(doc_type), "istable", False):
                return None
            parents = self.ctx.get_readable_parent_doctypes(doc_type)
            return parents[0] if len(parents) == 1 else None
        except Exception:
            return None

    def _validate_group_by(self, doc_type, group_by, fields):
        meta = frappe.get_meta(doc_type)
        allowed_fieldnames = {f.fieldname for f in meta.fields if not f.hidden}
        allowed_fieldnames.update(STANDARD_FIELDS)

        for index, gb in enumerate(group_by):
            # {field} without a bucket is a plain field group; clients often
            # send every group_by entry as an object.
            if isinstance(gb, dict) and not gb.get("bucket"):
                gb = group_by[index] = gb.get("field")
            if isinstance(gb, dict):
                self._validate_group_by_bucket(doc_type, meta, allowed_fieldnames, gb)
            elif gb not in allowed_fieldnames:
                raise QueryValidationError(f"group_by field '{gb}' not found.")

    def _validate_group_by_bucket(self, doc_type, meta, allowed_fieldnames, group):
        fieldname = group.get("field")
        bucket = str(group.get("bucket") or "").lower()
        alias = group.get("alias")

        if bucket not in ALLOWED_DATE_BUCKETS:
            raise QueryValidationError(
                f"group_by bucket '{bucket}' not allowed "
                f"(use one of: {', '.join(sorted(ALLOWED_DATE_BUCKETS))})."
            )
        if fieldname not in allowed_fieldnames:
            raise QueryValidationError(f"group_by field '{fieldname}' not found.")

        field_def = meta.get_field(fieldname)
        # creation/modified are standard Datetime columns without a DocField.
        fieldtype = field_def.fieldtype if field_def else (
            "Datetime" if fieldname in ("creation", "modified") else ""
        )
        if fieldtype not in ("Date", "Datetime"):
            raise QueryValidationError(
                f"group_by bucket requires a Date/Datetime field; "
                f"'{fieldname}' is {fieldtype or 'not a date field'}."
            )
        if field_def and not self.ctx.check_field_access(doc_type, fieldname, "read", self._field_parent()):
            raise QueryValidationError(f"No read access to field '{fieldname}'.")
        if alias and not ALIAS_RE.match(alias):
            raise QueryValidationError(f"Invalid alias '{alias}'.")

    def _validate_order_by(self, doc_type, order_by, fields=None, group_by=None):
        meta = frappe.get_meta(doc_type)
        allowed_fieldnames = {f.fieldname for f in meta.fields if not f.hidden}
        allowed_fieldnames.update(STANDARD_FIELDS)
        for field_spec in fields or []:
            if isinstance(field_spec, dict) and field_spec.get("alias"):
                allowed_fieldnames.add(field_spec.get("alias"))
        for group in group_by or []:
            if isinstance(group, dict) and group.get("field") and group.get("bucket"):
                allowed_fieldnames.add(
                    group.get("alias") or f"{group.get('field')}_{group.get('bucket')}"
                )

        for ob in order_by:
            if not isinstance(ob, dict):
                raise QueryValidationError("order_by items must be objects.")
            field = ob.get("field")
            if field not in allowed_fieldnames:
                raise QueryValidationError(f"order_by field '{field}' not found.")
            direction = ob.get("direction", "asc").lower()
            if direction not in ("asc", "desc"):
                raise QueryValidationError(f"Invalid order direction '{direction}'.")
