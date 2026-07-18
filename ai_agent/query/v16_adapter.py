import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count, Sum, Avg, Min, Max


AGGREGATE_MAP = {
    "COUNT": Count,
    "SUM": Sum,
    "AVG": Avg,
    "MIN": Min,
    "MAX": Max,
}


class V16Adapter:
    """Query adapter for Frappe v16 using frappe.qb."""

    def execute(self, spec, permission_context):
        doc_type = spec["doctype"]
        fields = spec.get("fields", [])
        filters = spec.get("filters", [])
        group_by = spec.get("group_by", [])
        order_by = spec.get("order_by", [])
        distinct = spec.get("distinct", False)
        limit = spec.get("limit", 500)
        offset = spec.get("offset", 0)
        parent_doctype = spec.get("parent_doctype") or self._parent_doctype_from_filters(filters)
        is_child_table = bool(getattr(frappe.get_meta(doc_type), "istable", False))

        table = DocType(doc_type)
        parent_table = DocType(parent_doctype) if (parent_doctype and (is_child_table or self._has_parent_filters(filters))) else None
        qb_fields = self._build_fields(table, fields)
        qb_filters = self._build_filters(table, filters, parent_table)

        if group_by or parent_table:
            query = frappe.qb.from_(table).select(*qb_fields)
            if parent_table:
                query = query.join(parent_table).on(table.parent == parent_table.name)
                if parent_doctype:
                    query = query.where(table.parenttype == parent_doctype)
            for criterion in qb_filters:
                query = query.where(criterion)
            if group_by:
                query = query.groupby(*[getattr(table, field) for field in group_by])

            rows = query.run(as_dict=True)
            rows = self._post_sort(rows, order_by)
            return {"rows": rows[offset:offset + limit], "row_count": len(rows)}

        qb_group = ", ".join(group_by) if group_by else None
        alias_names = {
            field.get("alias")
            for field in fields
            if isinstance(field, dict) and field.get("alias")
        }
        db_order_by, post_order_by = self._split_order_by(order_by, alias_names)
        qb_order = self._build_order_by(db_order_by)

        query = frappe.qb.get_query(
            doc_type,
            fields=qb_fields,
            filters=qb_filters,
            group_by=qb_group,
            order_by=qb_order,
            limit=limit,
            offset=offset,
            distinct=distinct,
            ignore_permissions=False,
            user=permission_context.user,
        )

        rows = query.run(as_dict=True)
        rows = self._post_sort(rows, post_order_by)
        return {"rows": rows, "row_count": len(rows)}

    def _build_fields(self, table, fields):
        result = []
        for f in fields:
            if isinstance(f, str):
                result.append(getattr(table, f))
            elif isinstance(f, dict):
                fieldname = f.get("field")
                aggregate = f.get("aggregate")
                alias = f.get("alias")
                if aggregate and aggregate in AGGREGATE_MAP:
                    fn = AGGREGATE_MAP[aggregate]
                    col = fn(getattr(table, fieldname))
                    if alias:
                        col = col.as_(alias)
                    result.append(col)
                elif alias:
                    result.append(getattr(table, fieldname).as_(alias))
                else:
                    result.append(getattr(table, fieldname))
        return result

    def _build_filters(self, table, filters, parent_table=None):
        result = []
        for flt in filters:
            op = flt.get("operator")
            val = flt.get("value")
            col = self._column_for_filter(table, parent_table, flt.get("field"))
            if op == "=":
                result.append(col == val)
            elif op == "!=":
                result.append(col != val)
            elif op == ">":
                result.append(col > val)
            elif op == "<":
                result.append(col < val)
            elif op == ">=":
                result.append(col >= val)
            elif op == "<=":
                result.append(col <= val)
            elif op == "in":
                result.append(col.isin(val))
            elif op == "not in":
                result.append(col.notin(val))
            elif op == "like":
                result.append(col.like(val))
            elif op == "not like":
                result.append(col.not_like(val))
            elif op == "between" and isinstance(val, list) and len(val) == 2:
                result.append(col.between(val[0], val[1]))
            elif op == "is set":
                result.append(col.notnull())
            elif op == "is not set":
                result.append(col.isnull())
        return result

    def _column_for_filter(self, table, parent_table, fieldname):
        if fieldname and fieldname.startswith("parent."):
            if not parent_table:
                raise ValueError("parent_doctype is required for parent.<field> filters.")
            return getattr(parent_table, fieldname.split(".", 1)[1])
        return getattr(table, fieldname)

    def _has_parent_filters(self, filters):
        return any(
            isinstance(flt, dict)
            and isinstance(flt.get("field"), str)
            and flt.get("field").startswith("parent.")
            for flt in filters or []
        )

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

    def _build_order_by(self, order_by):
        result = []
        for ob in order_by:
            field = ob.get("field", "name")
            direction = ob.get("direction", "asc").lower()
            result.append(f"{field} {direction}")
        return ", ".join(result) if result else None

    def _split_order_by(self, order_by, alias_names):
        db_order_by = []
        post_order_by = []
        for ob in order_by:
            if ob.get("field") in alias_names:
                post_order_by.append(ob)
            else:
                db_order_by.append(ob)
        return db_order_by, post_order_by

    def _post_sort(self, rows, order_by):
        for ob in reversed(order_by or []):
            field = ob.get("field")
            reverse = ob.get("direction", "asc").lower() == "desc"
            rows = sorted(
                rows,
                key=lambda row: (row.get(field) is None, row.get(field) or 0),
                reverse=reverse,
            )
        return rows
