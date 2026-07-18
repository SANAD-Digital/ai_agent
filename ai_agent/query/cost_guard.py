import frappe
from ai_agent.runtime.constants import MAX_ROWS_DEFAULT


class QueryCostGuard:
    """Evaluates query cost and decides whether to allow, lower limit, require more filters, or deny."""

    def evaluate(self, spec, permission_context):
        """Return one of: allow, allow_with_lower_limit, require_more_filters, deny."""
        doc_type = spec.get("doctype", "")
        filters = spec.get("filters", [])
        limit = spec.get("limit", MAX_ROWS_DEFAULT)
        group_by = spec.get("group_by", [])
        parent_doctype = spec.get("parent_doctype") or self._parent_doctype_from_filters(filters)
        date_fields = self._date_fields(doc_type)
        parent_date_fields = self._date_fields(parent_doctype) if parent_doctype else set()

        has_date_filter = any(
            self._is_date_filter(f.get("field"), date_fields, parent_date_fields)
            for f in filters
            if isinstance(f, dict)
        )
        has_parent_scope = any(
            self._base_field(f.get("field")) == "parent" and f.get("operator") in ("=", "in")
            for f in filters
            if isinstance(f, dict)
        )
        has_limited_text_search = self._has_limited_text_search(doc_type, filters, limit)
        has_selective_filters = self._has_selective_filters(doc_type, filters)

        is_high_volume = self._is_high_volume(doc_type)

        if is_high_volume and not has_date_filter and not has_parent_scope and not has_selective_filters and not has_limited_text_search:
            if not self._has_aggregate_fields(spec) and not group_by:
                return "require_more_filters"

        if is_high_volume and limit > 200:
            return "allow_with_lower_limit"

        if len(group_by) > 3:
            return "allow_with_lower_limit"

        if not has_date_filter and is_high_volume and not has_parent_scope and not has_selective_filters and not has_limited_text_search:
            return "require_more_filters"

        return "allow"

    def _date_fields(self, doc_type):
        fields = {"creation", "modified"}
        try:
            meta = frappe.get_meta(doc_type)
        except Exception:
            return fields

        for field in meta.fields:
            if field.fieldtype in {"Date", "Datetime"} and field.fieldname:
                fields.add(field.fieldname)
        return fields

    def _base_field(self, fieldname):
        if not fieldname:
            return ""
        return str(fieldname).split(".", 1)[0]

    def _is_date_filter(self, fieldname, date_fields, parent_date_fields):
        if not fieldname:
            return False
        fieldname = str(fieldname)
        if fieldname.startswith("parent."):
            return fieldname.split(".", 1)[1] in parent_date_fields
        return fieldname in date_fields

    def _has_aggregate_fields(self, spec):
        return any(
            isinstance(field, dict) and field.get("aggregate")
            for field in spec.get("fields", [])
        )

    def _has_selective_filters(self, doc_type, filters):
        try:
            meta = frappe.get_meta(doc_type)
        except Exception:
            return False

        selective_fieldtypes = {"Link", "Dynamic Link", "Select"}
        fields_by_name = {field.fieldname: field for field in meta.fields if field.fieldname}
        equality_filters = 0
        for flt in filters or []:
            if not isinstance(flt, dict):
                continue
            fieldname = self._base_field(flt.get("field"))
            operator = flt.get("operator")
            value = flt.get("value")
            if operator not in ("=", "in") or value in (None, "", []):
                continue
            if fieldname in {"name", "parent", "parenttype", "parentfield"}:
                equality_filters += 1
                continue
            field = fields_by_name.get(fieldname)
            if not field:
                continue
            if field.fieldtype in selective_fieldtypes or field.reqd or field.search_index:
                equality_filters += 1
        return equality_filters >= 2

    def _has_limited_text_search(self, doc_type, filters, limit):
        if not limit or limit > 50:
            return False

        try:
            meta = frappe.get_meta(doc_type)
        except Exception:
            return False

        searchable_fields = {
            "name",
            meta.title_field,
            meta.search_fields,
        }
        for field in meta.fields:
            if field.fieldname and (field.search_index or field.fieldtype in {"Data", "Small Text", "Text"}):
                searchable_fields.add(field.fieldname)

        searchable = set()
        for value in searchable_fields:
            if not value:
                continue
            if isinstance(value, str):
                searchable.update(part.strip() for part in value.split(",") if part.strip())

        for flt in filters or []:
            if not isinstance(flt, dict):
                continue
            fieldname = self._base_field(flt.get("field"))
            operator = flt.get("operator")
            value = flt.get("value")
            if operator in ("like", "not like") and fieldname in searchable and value not in (None, "", "%%"):
                return True

        return False

    def _is_high_volume(self, doc_type):
        try:
            meta = frappe.get_meta(doc_type)
            if getattr(meta, "istable", False):
                return True
            return frappe.db.count(doc_type) > 10000
        except Exception:
            return False

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
